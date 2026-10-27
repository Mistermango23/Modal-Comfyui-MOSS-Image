# Modal-Comfyui-MOSS-Image
####
# ComfyUI on Modal (MOSS Image)

Run ComfyUI with the MOSS image on [Modal](https://modal.com) and access it through a temporary tunnel, with no local GPU required.

## Requirements

- A [Modal](https://modal.com) account
- Python 3.10 or newer

## Installation

Install the Modal client:

```bash
pip install modal
```

If a newer version is available, update it:

```bash
pip install modal --upgrade
```

## Connect Modal to Your Account

Link the Modal CLI with your account (this opens a browser window for authentication):

```bash
modal setup or python -m modal setup
```

## Usage

Start ComfyUI with the tunnel:

```bash
modal run modal_comfyui.py::run_tunnel
```

Modal will build the image and print a public tunnel URL in the terminal. Open it in your browser to use ComfyUI.

## Using Your Own LoRAs (Hugging Face)

If you want to load your own LoRAs from [huggingface.co](https://huggingface.co), you need to connect Modal to your Hugging Face account with an access token.

1. Create an access token on Hugging Face under **Settings → Access Tokens** (a read token is sufficient).
2. Add the token to Modal as a secret, either in the Modal dashboard under **Secrets** or via the CLI:

   ```bash
   modal secret create huggingface-secret HF_TOKEN=hf_your_token_here
   ```

3. Make sure the secret name matches the one referenced in `modal_comfyui.py`.

Your LoRAs will then be downloaded from Hugging Face when the container starts.

## Notes

- Never commit your Hugging Face token to the repository.
- GPU usage on Modal is billed per second, so stop the app when you are done (`Ctrl+C` in the terminal).
