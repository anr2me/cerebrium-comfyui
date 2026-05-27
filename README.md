# ComfyUI on Cerebrium
> Converted from **anr2me/modal-comfyui** (Modal) → **Cerebrium** serverless.

## File structure

```
.
├── cerebrium.toml          ← Deployment config (hardware, scaling, build steps)
├── main.py                 ← FastAPI app — starts ComfyUI, proxies all requests
├── download_models.py      ← Model downloader — called during BUILD (no GPU)
├── install_plugins.py      ← Plugin installer — called during BUILD (no GPU)
├── install_wheels.py       ← Wheel installer  — called during BUILD (no GPU)
├── models.example.py       ← Copy to models.py and edit
├── plugins.example.py      ← Copy to plugins.example.py and edit
├── extra_model_paths.yaml  ← Optional extra model search paths for ComfyUI
└── requirements_comfy.txt  ← Extra pip packages for ComfyUI/custom nodes
```

---

## GPU cost principle

```
                BUILD PHASE          RUNTIME PHASE
                (shell_commands)     (entrypoint)
                ─────────────────    ──────────────────────────
GPU allocated?  NO  ✓               YES
                apt install         ComfyUI subprocess starts
                pip / torch         Inference runs
                comfy-cli install   FastAPI proxies requests
                git clone nodes
                model downloads
                bandit scan
```

Everything expensive but GPU-irrelevant runs in `shell_commands` so you are
**never billed for GPU time during setup**. The GPU meter only starts when
`main.py` boots up and a request arrives.

---

## Modal → Cerebrium concept mapping

| Modal | Cerebrium |
|---|---|
| `modal.Image.debian_slim().run_commands()` | `shell_commands` in `cerebrium.toml` |
| `Image.uv_pip_install(["torch", ...])` | `shell_commands` uv pip install |
| `modal.Volume.from_name("hf-hub-cache")` | `/cache` directory (persistent build layer) |
| `image.run_function(download_all)` | `shell_commands` → `python download_models.py` |
| `comfy node install ...` in `run_commands` | `shell_commands` → `python install_plugins.py --registry` |
| `git clone` ext plugins in `run_commands` | `shell_commands` → `python install_plugins.py --ext` |
| `image.run_function(install_wheels)` | `shell_commands` → `python install_wheels.py` |
| `@app.cls(gpu="L4")` | `[cerebrium.hardware] compute = "ADA_L4"` |
| `scaledown_window=60` | `[cerebrium.scaling] cooldown = 60` |
| `@modal.concurrent(max_inputs=10)` | `replica_concurrency = 10` |
| `timeout=3600` | `response_grace_period = 3600` |
| `ComfyMix.start_checkpoint()` | `lifespan()` in `main.py` |
| `ComfyMix.api()` → `web_app` | `main.py` FastAPI app |
| `ComfyGPU` separate container | Not needed — single container, ComfyUI is local |
| `shared_dict` (cross-container state) | In-process Python variables (single container) |
| `modal.Secret.from_name("huggingface-secret")` | `[cerebrium.secrets] HF_TOKEN` |
| `modal serve` / `modal deploy` | `cerebrium deploy` |

---

## Setup

### 1. Install Cerebrium CLI

```bash
pip install cerebrium
cerebrium login
```

### 2. Configure models

```bash
cp models.example.py models.py
# Edit models.py — add your checkpoints, LoRAs, VAEs, etc.
```

### 3. Configure custom nodes

```bash
cp plugins.example.py plugins.py
# Edit plugins.py — add your comfy_plugins and comfy_plugins_ext entries.
```

### 4. (Optional) Add your workflow

Export your workflow from ComfyUI:
1. **Settings → Enable Dev Mode Options**
2. **Save (API Format)** → save as `workflow_api.json` in this directory

The build step will auto-install all node dependencies for the workflow.

### 5. Set HuggingFace token (for gated models)

In the [Cerebrium dashboard](https://dashboard.cerebrium.ai) → **Secrets**,
add a secret named `HF_TOKEN` with your HF access token.

### 6. Deploy

```bash
cerebrium deploy
```

The first deploy is slower (it runs all `shell_commands`). Subsequent deploys
with unchanged `shell_commands` use the cached image layer — much faster.

---

## Usage

Access the ComfyUI UI directly in your browser:

```
https://api.aws.us-east-1.cerebrium.ai/v4/p-<YOUR_PROJECT_ID>/comfyui/
```

Or use the API (same as standard ComfyUI HTTP API):

```bash
# Queue a prompt
curl -X POST \
  "https://api.aws.us-east-1.cerebrium.ai/v4/p-<ID>/comfyui/prompt" \
  -H "Authorization: Bearer <TOKEN>" \
  -H "Content-Type: application/json" \
  -d @workflow_api.json

# Check queue
curl "https://api.aws.us-east-1.cerebrium.ai/v4/p-<ID>/comfyui/queue" \
  -H "Authorization: Bearer <TOKEN>"

# Download output image
curl "https://api.aws.us-east-1.cerebrium.ai/v4/p-<ID>/comfyui/view?filename=output.png&type=output" \
  -H "Authorization: Bearer <TOKEN>" \
  -o output.png
```

---

## Hardware selection

| Use case | `compute` value |
|---|---|
| SD1.5 / SDXL / ACE Step | `AMPERE_A10` |
| FLUX.1 / SDXL + ControlNet | `ADA_L40` |
| Wan 2.2 / CogVideoX / LTX Video | `BLACKWELL_RTX6000` |

Set `min_replicas = 1` to eliminate cold starts (always-on billing).

---
