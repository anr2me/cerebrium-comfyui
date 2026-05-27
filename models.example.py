# models.example.py
# Copy this file to models.py and edit it to configure your model downloads.
#
# Models are downloaded during the BUILD phase (CPU, no GPU cost) via
# download_models.py, then symlinked into /root/comfy/ComfyUI/models/.
#
# `model_dir` accepts two styles:
#
#  1. Relative path (recommended for standard ComfyUI model folders):
#     "checkpoints"       → /root/comfy/ComfyUI/models/checkpoints
#     "loras/wan22"       → /root/comfy/ComfyUI/models/loras/wan22
#
#  2. Absolute path (use when target is outside ComfyUI/models/,
#     e.g. a custom node's own model directory):
#     "/root/comfy/ComfyUI/custom_nodes/ComfyUI-ReActor/models/insightface"
#
# Common subdirs under ComfyUI/models/:
#   checkpoints, diffusion_models, vae, loras, text_encoders,
#   clip_vision, controlnet, upscale_models, embeddings.

models = [
    # ── HuggingFace downloads (via huggingface_hub) ──────────────────────────
    # Set HF_TOKEN in Cerebrium → Secrets for gated models and faster speed.
    #
    # {
    #     "repo_id":   "HF_REPO_ID",
    #     "filename":  "FILENAME",
    #     "model_dir": "checkpoints",
    # },

    {
        "repo_id":   "Comfy-Org/ace_step_1.5_ComfyUI_files",
        "filename":  "split_files/diffusion_models/acestep_v1.5_xl_turbo_bf16.safetensors",
        "model_dir": "diffusion_models",
    },
    {
        "repo_id":   "Comfy-Org/ace_step_1.5_ComfyUI_files",
        "filename":  "split_files/text_encoders/qwen_4b_ace15.safetensors",
        "model_dir": "text_encoders",
    },
    {
        "repo_id":   "Comfy-Org/ace_step_1.5_ComfyUI_files",
        "filename":  "split_files/text_encoders/qwen_0.6b_ace15.safetensors",
        "model_dir": "text_encoders",
    },
    {
        "repo_id":   "Comfy-Org/ace_step_1.5_ComfyUI_files",
        "filename":  "split_files/vae/ace_1.5_vae.safetensors",
        "model_dir": "vae",
    },
    {
        "repo_id":   "Comfy-Org/ERNIE-Image",
        "filename":  "diffusion_models/ernie-image-turbo.safetensors",
        "model_dir": "diffusion_models",
    },
    {
        "repo_id":   "Comfy-Org/ERNIE-Image",
        "filename":  "text_encoders/ministral-3-3b.safetensors",
        "model_dir": "text_encoders",
    },
    {
        "repo_id":   "Comfy-Org/ERNIE-Image",
        "filename":  "text_encoders/ernie-image-prompt-enhancer.safetensors",
        "model_dir": "text_encoders",
    },
    {
        "repo_id":   "Comfy-Org/ERNIE-Image",
        "filename":  "vae/flux2-vae.safetensors",
        "model_dir": "vae",
    },
]

models_ext = [
    # ── External downloads (via aria2c) ────────────────────────────────────
    # Use for CivitAI, direct URLs, etc.
    #
    # {
    #     "url":       "https://civitai.com/api/download/models/...",
    #     "filename":  "my_lora.safetensors",
    #     "model_dir": "loras",
    # },
]
