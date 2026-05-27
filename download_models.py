"""
download_models.py — Model download script for Cerebrium build phase.
Called from shell_commands in cerebrium.toml → runs on CPU, NO GPU allocated.

Modal equivalent:
  hf_download()            → hf_hub_download()  with symlink into ComfyUI/models/
  download_external_model() → aria2c multi-connection download + symlink
  download_all()            → main() here

Models are downloaded to /cache (persistent across builds if cached),
then symlinked into /root/comfy/ComfyUI/models/<model_dir>/ so ComfyUI
can find them without duplicating disk usage.

Usage (called automatically by cerebrium.toml shell_commands):
  python /app/download_models.py

Usage with HF token:
  HF_TOKEN=hf_xxx python /app/download_models.py
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# ─────────────────────────────────────────────────────────────────────────────
# Paths  (mirrors the original Modal layout)
# ─────────────────────────────────────────────────────────────────────────────
CACHE_DIR        = Path("/cache")
COMFYUI_ROOT     = Path("/root/comfy/ComfyUI")
COMFY_MODELS_ROOT = COMFYUI_ROOT / "models"

# Directories to pre-create (same as download_all() in the original)
REQUIRED_DIRS = [
    CACHE_DIR,
    COMFYUI_ROOT / "input",
    COMFYUI_ROOT / "output",
    COMFYUI_ROOT / "user" / "default" / "workflows",
    COMFYUI_ROOT / "custom_nodes",
    COMFY_MODELS_ROOT,
]


def resolve_model_dir(model_dir: str) -> Path:
    """
    Relative paths → COMFY_MODELS_ROOT/<model_dir>
    Absolute paths → used as-is  (e.g. custom node model dirs)
    """
    p = Path(model_dir)
    return p if p.is_absolute() else COMFY_MODELS_ROOT / p


def ensure_dirs() -> None:
    for d in REQUIRED_DIRS:
        d.mkdir(parents=True, exist_ok=True)


# ─────────────────────────────────────────────────────────────────────────────
# HuggingFace download
# Modal equivalent: hf_download()
# ─────────────────────────────────────────────────────────────────────────────

def hf_download(repo_id: str, filename: str, model_dir: str = "checkpoints") -> None:
    from huggingface_hub import hf_hub_download

    token = os.environ.get("HF_TOKEN") or None
    print(f"  [HF] {repo_id}/{filename} → models/{model_dir}/")

    cached = hf_hub_download(
        repo_id   = repo_id,
        filename  = filename,
        cache_dir = str(CACHE_DIR),
        token     = token,
    )

    target_dir  = resolve_model_dir(model_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    local_name  = Path(filename).name
    target_path = target_dir / local_name

    # Re-create symlink (unlink first to allow re-runs)
    if target_path.exists() or target_path.is_symlink():
        target_path.unlink()
    target_path.symlink_to(cached)
    print(f"  ✓ symlinked → {target_path}")


# ─────────────────────────────────────────────────────────────────────────────
# External (aria2c) download
# Modal equivalent: download_external_model()
# ─────────────────────────────────────────────────────────────────────────────

def download_external_model(url: str, filename: str, model_dir: str) -> None:
    cached_path = CACHE_DIR / filename

    if not cached_path.exists():
        print(f"  [aria2c] downloading {filename} …")
        subprocess.run(
            [
                "aria2c",
                "--console-log-level=error",
                "--summary-interval=0",
                "-x", "16",
                "-s", "16",
                "-o", filename,
                "-d", str(CACHE_DIR),
                url,
            ],
            check=True,
            # Let stdout/stderr pass through so progress is visible in build logs
        )
    else:
        print(f"  [aria2c] already cached: {filename}")

    target_dir  = resolve_model_dir(model_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / filename

    if target_path.exists() or target_path.is_symlink():
        target_path.unlink()
    target_path.symlink_to(cached_path)
    print(f"  ✓ symlinked → {target_path}")


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    print("=== download_models.py (CPU build step — no GPU) ===")
    ensure_dirs()

    # Import user model config
    sys.path.insert(0, "/app")
    try:
        from models import models, models_ext
    except ImportError:
        print("WARNING: models.py not found — skipping model downloads.")
        print("Copy models.example.py to models.py and edit it.")
        return

    total = len(models) + len(models_ext)
    if total == 0:
        print("No models configured — nothing to download.")
        return

    print(f"\nDownloading {len(models)} HuggingFace model(s) …")
    for i, m in enumerate(models, 1):
        print(f"  [{i}/{len(models)}] {m['repo_id']}/{m['filename']}")
        try:
            hf_download(m["repo_id"], m["filename"], m.get("model_dir", "checkpoints"))
        except Exception as exc:
            print(f"  ✗ FAILED: {exc}")
            # Non-fatal: build continues; missing model will fail at runtime

    print(f"\nDownloading {len(models_ext)} external model(s) …")
    for i, m in enumerate(models_ext, 1):
        print(f"  [{i}/{len(models_ext)}] {m['filename']} from {m['url'][:60]}…")
        try:
            download_external_model(m["url"], m["filename"], m.get("model_dir", "checkpoints"))
        except Exception as exc:
            print(f"  ✗ FAILED: {exc}")

    print("\n=== download_models.py complete ===")


if __name__ == "__main__":
    main()
