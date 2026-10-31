import os
import shlex
import socket
import subprocess
import time

import modal

# ----------------------------------------------------------------------------
# Options
# ----------------------------------------------------------------------------
# Debug toggles, set in your Windows terminal before `modal serve` / `modal run`
# (see "Windows commands" below for the exact syntax):
#   COMFY_SAFE=1                      start with ALL custom nodes disabled
#   COMFY_ONLY_NODES=a,b,c            start with ONLY these node folders enabled
#                                     (e.g. ComfyUI-KJNodes,rgthree-comfy)
#   COMFY_NO_MMAP=1                   load model files into RAM instead of mmap
#   COMFY_GIT_PULL=0                  do NOT auto-update on start (default: update)
# Environment variables of your local shell do NOT reach the container by
# themselves, so they are forwarded explicitly (see `debug_env` below).
DEBUG_VARS = ("COMFY_SAFE", "COMFY_ONLY_NODES", "COMFY_NO_MMAP", "COMFY_GIT_PULL")
debug_env = {k: os.environ[k] for k in DEBUG_VARS if os.environ.get(k)}

# Keep saved workflows, inputs and generated images on the Volume so they
# survive container shutdown. OFF by default: the Volume is a network
# filesystem, and ComfyUI reads/writes these files from inside its web server.
PERSIST_USER_DATA = False

# ---------------------------------------------------------------------------
# HOW UPDATES WORK (ComfyUI + custom nodes)
# ---------------------------------------------------------------------------
# AUTOMATIC (default): every time a container starts, ComfyUI and ALL custom
# nodes are `git pull`ed to their latest commit. Requirements are reinstalled
# only for repos that actually changed (torch stays pinned), so a start with
# nothing new is quick. These updates live in the running container; the next
# start pulls again, so you always get the newest version.
#
# BAKE INTO THE IMAGE (optional, makes starts faster after many updates):
# set COMFY_UPDATE to a NEW value (a date works) to rebuild the image with
# fresh clones. Torch stays cached so the working CUDA build isn't touched.
#
# FREEZE (optional): set COMFY_GIT_PULL=0 to skip the automatic pull and run
# exactly what is in the image (useful if an update breaks something).
#
# Windows commands (the script itself runs on Modal's Linux machines; only the
# commands below depend on your local terminal):
#
#   Command Prompt (cmd.exe) - one command per line, NO spaces around "=":
#       set COMFY_UPDATE=2026-11-02
#       modal run modal_comfyui.py::run_tunnel
#     clear a setting:   set COMFY_GIT_PULL=
#
#   PowerShell:
#       $env:COMFY_UPDATE="2026-11-02"
#       modal run modal_comfyui.py::run_tunnel
#     clear a setting:   Remove-Item Env:COMFY_GIT_PULL
#
# Settings stay active for that terminal window until you clear them or close
# it, so clear COMFY_GIT_PULL / COMFY_SAFE once you are done testing.
REFRESH = os.getenv("COMFY_UPDATE", "0")

# Your own LoRA repository on Hugging Face, downloaded into
# /vol/models/loras/My-MOSS-Image-Loras. EDIT THIS to "<username>/<repo-name>",
# e.g. "janedoe/My-MOSS-Image-Loras". While it still holds the placeholder below
# (or is empty), the LoRA download is skipped. Private repos work too, because
# your Hugging Face token (secret "huggingface-secret") is used.
# The repo is re-synced every time ComfyUI starts, so LoRAs you add or update on
# Hugging Face appear after a restart (no image rebuild). Files you DELETE from
# the repo are not removed from the volume.
USER_LORAS_REPO = "yourusername/your-MOSS-Image-Loras"
USER_LORAS_PLACEHOLDER = "yourusername/your-MOSS-Image-Loras"

# You can swap the GPU for an L40S, A100, H100, or something else.
GPU_TYPE = os.getenv("MODAL_GPU", "l40s")
PORT = 8188

# 1. Persistent volume for models (and optionally user data)
volume = modal.Volume.from_name("comfyui-models-vol", create_if_missing=True)


# 2. Runs once at image-build time; results are stored on the Volume
def download_models():
    import os
    import subprocess

    os.makedirs("/vol/models/loras/MOSS-Image-Base-8-steps-dmd", exist_ok=True)
    os.makedirs("/vol/models/loras/MOSS-Image-SFT-8-steps-dmd", exist_ok=True)
    os.makedirs("/vol/models/loras/My-MOSS-Image-Loras", exist_ok=True)

    # The `hf` CLI reads HF_TOKEN from the environment automatically
    # (provided by the Modal secret), so no explicit login is needed.
    print("Downloading models to persistent volume...")
    subprocess.run(["hf", "download", "Comfy-Org/MOSS-Image", "--local-dir", "/vol/models"], check=True)
    subprocess.run(["hf", "download", "lodestones/MOSS-Image-Base-8-steps-dmd", "lora.safetensors", "lora_dmd2.safetensors", "--local-dir", "/vol/models/loras/MOSS-Image-Base-8-steps-dmd"], check=True)
    subprocess.run(["hf", "download", "lodestones/MOSS-Image-SFT-8-steps-dmd", "lora.safetensors", "lora_latest.safetensors", "--local-dir", "/vol/models/loras/MOSS-Image-SFT-8-steps-dmd"], check=True)
    if USER_LORAS_REPO and USER_LORAS_REPO != USER_LORAS_PLACEHOLDER:
        subprocess.run(["hf", "download", USER_LORAS_REPO, "--local-dir", "/vol/models/loras/My-MOSS-Image-Loras"], check=True)
    else:
        print("USER_LORAS_REPO not set - skipping your own LoRA download.")
    print("Models downloaded successfully!")

    # Make sure the files are persisted to the Volume.
    try:
        volume.commit()
    except Exception as e:  # not fatal: Modal also commits when the function exits
        print(f"volume.commit() skipped: {e}")


# 3. Custom nodes: (repo url, optional folder name)
NODES = [
    ("https://github.com/Comfy-Org/ComfyUI-Manager.git", None),
    ("https://github.com/kijai/ComfyUI-KJNodes.git", None),
    ("https://github.com/rgthree/rgthree-comfy.git", None),
    ("https://github.com/shootthesound/comfyUI-Realtime-Lora.git", None),
    ("https://github.com/scraed/LanPaint.git", None),
    ("https://github.com/TenStrip/10S-Comfy-nodes.git", "10S_Nodes"),
    ("https://github.com/WhatDreamsCost/WhatDreamsCost-ComfyUI.git", None),
    ("https://github.com/liconstudio/ComfyUI-Licon-MSR", None),
    ("https://github.com/larryvrh/ComfyUI-MiniMax-H3-Turbo", None),
    ("https://github.com/Lightricks/ComfyUI-LTXVideo.git", None),
    ("https://github.com/liconstudio/ComfyUI-LTX2.5-MSR", None),
    ("https://github.com/scottmudge/ComfyUI_MinimaxH3HybridLoader.git", None),
    ("https://github.com/PlagueKind/ComfyUI-PlagueKind-Nodes.git", None),
    ("https://github.com/ostris/ComfyUI-AIToolkit-MiniMaxH3.git", None),
    ("https://github.com/T8mars/comfyui-minimax-h3-audio-T8.git", "minimax-h3-audio-T8"),
    ("https://github.com/Azornes/Comfyui-Resolution-Master.git", None),
    ("https://github.com/bluvoll/ComfyUI-MageFlow-Compressed.git", None),
    ("https://github.com/xmarre/ComfyUI-VDN-H3-Plus.git", "ComfyUI-VDN-H3"),
    ("https://github.com/PedroMarinhoDev/ComfyUI-HunyuanImage3.git", None),
]

clone_cmds = [
    "cd /root/ComfyUI/custom_nodes && "
    f"git clone --depth 1 --recurse-submodules --shallow-submodules {url}"
    + (f" {name}" if name else "")
    for url, name in NODES
]

# Install each node's requirements with torch and ComfyUI's own packages
# PINNED, so no node can replace the CUDA build of torch or the web frontend.
# A failure is printed as "!! FAILED: <path>" instead of being swallowed.
install_reqs = (
    "pip freeze | grep -iE '^(torch|torchvision|torchaudio|comfyui[-_a-z0-9]*)==' "
    "> /tmp/constraints.txt; cat /tmp/constraints.txt; "
    "for f in /root/ComfyUI/custom_nodes/*/requirements.txt; do "
    'echo "== $f"; pip install -c /tmp/constraints.txt -r "$f" '
    '|| echo "!! FAILED: $f"; done'
)

image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install(
        "git", "curl", "wget", "build-essential", "ffmpeg",
        "libsm6", "libxext6", "libgl1", "libglib2.0-0",
    )
    .env({"PYTHONUNBUFFERED": "1", "HF_XET_HIGH_PERFORMANCE": "1"})
    .pip_install("huggingface_hub[cli]")
    .pip_install(
        "torch", "torchvision", "torchaudio",
        extra_index_url="https://download.pytorch.org/whl/cu130",
    )
    # Changing COMFY_UPDATE invalidates everything from here on, which forces
    # fresh clones of ComfyUI and all custom nodes (see "HOW TO UPDATE" above).
    .env({"COMFY_REFRESH": REFRESH})
    .run_commands("git clone https://github.com/Comfy-Org/ComfyUI.git /root/ComfyUI")
    .run_commands("cd /root/ComfyUI && pip install -r requirements.txt")
    .run_commands("mkdir -p /root/ComfyUI/custom_nodes", *clone_cmds)
    .run_commands(install_reqs)
    # Informational: confirm which torch build ended up in the image.
    .run_commands(
        "python -c \"import torch; print('torch', torch.__version__, 'cuda', torch.version.cuda)\""
    )
    # Point ComfyUI's models folder at the volume once, at build time.
    .run_commands("rm -rf /root/ComfyUI/models && ln -s /vol/models /root/ComfyUI/models")
)

# Populate the volume during the build using the Hugging Face secret.
image = image.run_function(
    download_models,
    volumes={"/vol": volume},
    secrets=[modal.Secret.from_name("huggingface-secret")],
)

# 4. App + web server
app = modal.App("comfyui-modal-app")


def wait_for_server(proc: subprocess.Popen, port: int, timeout: int):
    """Block until ComfyUI accepts connections; fail fast if it crashes."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        code = proc.poll()
        if code is not None:
            raise RuntimeError(f"ComfyUI exited during startup with code {code}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return
        except OSError:
            time.sleep(1)
    raise TimeoutError(f"ComfyUI did not start on port {port} within {timeout}s")


SECRETS = [modal.Secret.from_name("huggingface-secret")] + (
    [modal.Secret.from_dict(debug_env)] if debug_env else []
)

# Modal's default CPU share is tiny (0.125 core). ComfyUI's web server, model
# loading and the browser's burst of requests all compete for it, which makes
# the page and websocket stall. Give it real CPU and RAM.
COMMON = dict(
    image=image,
    gpu=GPU_TYPE,
    volumes={"/vol": volume},
    cpu=4.0,
    memory=32768,  # MiB
    max_containers=1,
    secrets=SECRETS,
)


def refresh_user_loras():
    """Sync your own LoRA repo from Hugging Face on every start, so new or
    changed LoRAs show up without rebuilding the image. `hf download` only
    fetches files that changed, so it's quick when nothing is new."""
    if not USER_LORAS_REPO or USER_LORAS_REPO == USER_LORAS_PLACEHOLDER:
        return
    target = "/vol/models/loras/My-MOSS-Image-Loras"
    os.makedirs(target, exist_ok=True)
    print(f"Syncing LoRAs from {USER_LORAS_REPO} ...")
    try:
        r = subprocess.run(
            ["hf", "download", USER_LORAS_REPO, "--local-dir", target],
            timeout=600,
        )
        if r.returncode != 0:
            print(f"!! LoRA sync failed (exit {r.returncode}); using the LoRAs already on the volume.")
        else:
            try:
                volume.commit()  # keep the new files so the next start only checks for changes
            except Exception as e:
                print(f"volume.commit() skipped: {e}")
    except Exception as e:  # never block ComfyUI from starting because of this
        print(f"!! LoRA sync error: {e}; using the LoRAs already on the volume.")


def update_all():
    """Update ComfyUI and every custom node to the latest commit.

    Shallow clones: fetch the remote's current HEAD and reset to it. Requirements
    are reinstalled only for repos whose commit actually changed, with torch
    pinned so the CUDA build can't be replaced. Never blocks startup: any
    failure is printed and the existing code is used."""
    import glob
    from concurrent.futures import ThreadPoolExecutor

    comfy = "/root/ComfyUI"
    repos = [comfy] + [os.path.normpath(p) for p in sorted(glob.glob(f"{comfy}/custom_nodes/*/"))]
    repos = [r for r in repos if os.path.isdir(os.path.join(r, ".git"))]

    def sync(repo):
        q = shlex.quote(repo)
        try:
            before = subprocess.run(f"git -C {q} rev-parse HEAD", shell=True,
                                    capture_output=True, text=True, timeout=30).stdout.strip()
            r = subprocess.run(
                f"git -C {q} fetch --depth 1 origin HEAD && git -C {q} reset --hard FETCH_HEAD "
                f"&& git -C {q} submodule update --init --recursive --depth 1",
                shell=True, capture_output=True, text=True, timeout=240,
            )
            if r.returncode != 0:
                return repo, False, f"FAILED: {r.stderr.strip()[-200:]}"
            after = subprocess.run(f"git -C {q} rev-parse HEAD", shell=True,
                                   capture_output=True, text=True, timeout=30).stdout.strip()
            return repo, before != after, ("updated" if before != after else "already latest")
        except Exception as e:
            return repo, False, f"FAILED: {e}"

    print(f"Updating {len(repos)} repos (ComfyUI + custom nodes) ...")
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(sync, repos))
    changed = set()
    for repo, did_change, status in results:
        print(f"[update] {os.path.basename(repo)}: {status}")
        if did_change:
            changed.add(repo)

    def write_constraints(pattern, path):
        subprocess.run(f"pip freeze | grep -iE '{pattern}' > {path}; true", shell=True)

    def pip_reqs(repo, constraints):
        req = os.path.join(repo, "requirements.txt")
        if not os.path.isfile(req):
            return
        print(f"== pip install -r {req}")
        if subprocess.run(["pip", "install", "-c", constraints, "-r", req]).returncode != 0:
            print(f"!! FAILED: {req}")

    # ComfyUI first. Only torch is pinned here, so ComfyUI's own packages
    # (frontend, templates, ...) are allowed to move to the versions it needs.
    if comfy in changed:
        write_constraints("^(torch|torchvision|torchaudio)==", "/tmp/c_torch.txt")
        pip_reqs(comfy, "/tmp/c_torch.txt")

    # Then the nodes, with torch AND ComfyUI's (possibly just-updated) packages pinned.
    write_constraints("^(torch|torchvision|torchaudio|comfyui[-_a-z0-9]*)==", "/tmp/c_all.txt")
    for repo in sorted(changed - {comfy}):
        pip_reqs(repo, "/tmp/c_all.txt")


def launch_comfy() -> subprocess.Popen:
    """Start ComfyUI in the background and wait until it accepts connections."""
    # Pick up anything committed to the volume since the container was built.
    try:
        volume.reload()
    except Exception as e:
        print(f"volume.reload() skipped: {e}")

    # Guard against a dangling models symlink if the volume is empty.
    os.makedirs("/vol/models", exist_ok=True)

    refresh_user_loras()

    # Update on every start unless explicitly frozen with COMFY_GIT_PULL=0.
    if os.environ.get("COMFY_GIT_PULL", "1") != "0":
        try:
            update_all()
        except Exception as e:  # never block ComfyUI from starting
            print(f"!! update failed, using the code in the image: {e}")

    cmd = ["python", "main.py", "--listen", "0.0.0.0", "--port", str(PORT)]

    if PERSIST_USER_DATA:
        for sub in ("user", "input", "output"):
            os.makedirs(f"/vol/{sub}", exist_ok=True)
        cmd += [
            "--user-directory", "/vol/user",
            "--input-directory", "/vol/input",
            "--output-directory", "/vol/output",
        ]

    only_nodes = [n.strip() for n in os.environ.get("COMFY_ONLY_NODES", "").split(",") if n.strip()]
    if only_nodes:
        cmd += ["--disable-all-custom-nodes", "--whitelist-custom-nodes", *only_nodes]
    elif os.environ.get("COMFY_SAFE") == "1":
        cmd.append("--disable-all-custom-nodes")

    if os.environ.get("COMFY_NO_MMAP") == "1":
        cmd.append("--disable-mmap")

    print("Launching:", " ".join(cmd))

    # Popen (NOT subprocess.run): the caller must get control back once the
    # server is up.
    proc = subprocess.Popen(cmd, cwd="/root/ComfyUI")
    wait_for_server(proc, PORT, timeout=840)
    return proc


# ---------------------------------------------------------------------------
# Option 1 (default): Modal web endpoint.  Run with:  modal serve modal_comfyui.py
# Traffic goes through Modal's Python proxy (which also relays the websocket).
# ---------------------------------------------------------------------------
@app.function(**COMMON, scaledown_window=1200, timeout=86400)
# IMPORTANT: without this, the container handles ONE request at a time. The
# ComfyUI page opens a long-lived websocket plus dozens of parallel asset
# requests, so they would queue behind it -> blank/slow page.
@modal.concurrent(max_inputs=100)
@modal.web_server(PORT, startup_timeout=900)
def run():
    launch_comfy()


# ---------------------------------------------------------------------------
# Option 2: direct tunnel. Run with:  modal run modal_comfyui.py::run_tunnel
# Uses modal.forward(), which exposes the port directly (no Python proxy in
# between), so websockets reach ComfyUI untouched. The URL is printed in the
# terminal; the app stops when you press Ctrl+C (or after TUNNEL_HOURS).
# NOTE: anyone who has the URL can use your ComfyUI, so don't share it.
# ---------------------------------------------------------------------------
TUNNEL_HOURS = 4


@app.function(**COMMON, timeout=TUNNEL_HOURS * 3600)
def run_tunnel():
    proc = launch_comfy()
    with modal.forward(PORT) as tunnel:
        print("=" * 60)
        print(f"ComfyUI is ready at: {tunnel.url}")
        print("=" * 60)
        while proc.poll() is None:
            time.sleep(5)