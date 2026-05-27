"""
install_wheels.py — Installs prebuilt nunchaku + flash-attn wheels.
Called from shell_commands in cerebrium.toml → runs on CPU, NO GPU allocated.

Modal equivalent: install_wheels() run_function() step.

Differences from original:
  - BUG FIX #12: cp-tag is derived dynamically (not hardcoded cp312)
  - flash-attn-3 is NOT installed to avoid namespace conflict with flash-attn-4
    (see Modal bug #7)
"""

from __future__ import annotations

import subprocess
import sys


def get_torch_minor() -> str:
    """Return 'major.minor' of installed torch, e.g. '2.10'."""
    import torch
    return ".".join(torch.__version__.split(".")[:2])


def get_cp_tag() -> str:
    """Return cpython ABI tag, e.g. 'cp312'. Derived dynamically (fixes bug #12)."""
    vi = sys.version_info
    return f"cp{vi.major}{vi.minor}"


def pip_install_no_deps(url: str) -> None:
    subprocess.check_call([
        sys.executable, "-m", "uv", "pip", "install",
        "--system", "--no-deps", url,
    ])


def main() -> None:
    print("=== install_wheels.py (CPU build step — no GPU) ===")

    try:
        ver    = get_torch_minor()
        cp_tag = get_cp_tag()
    except ImportError:
        print("torch not installed yet — skipping wheel installs.")
        return

    print(f"  torch={ver}, python={cp_tag}")

    # ── nunchaku ──────────────────────────────────────────────────────────────
    nunchaku_url = (
        f"https://github.com/nunchaku-tech/nunchaku/releases/download/v1.2.1/"
        f"nunchaku-1.2.1+cu13.0torch{ver}-{cp_tag}-{cp_tag}-linux_x86_64.whl"
    )
    print(f"  [nunchaku] {nunchaku_url}")
    try:
        pip_install_no_deps(nunchaku_url)
        print("  ✓ nunchaku installed")
    except subprocess.CalledProcessError as exc:
        print(f"  ✗ nunchaku failed (non-fatal): {exc}")

    # ── flash-attn 2.x prebuilt wheel ─────────────────────────────────────────
    # NOTE: We do NOT install flash-attn-3 here because flash-attn-4 is already
    # installed in shell_commands, and having both causes a namespace conflict
    # (Modal bug #7 fixed).
    flash_url = (
        f"https://github.com/mjun0812/flash-attention-prebuild-wheels/releases/download/v0.9.0/"
        f"flash_attn-2.8.3+cu130torch{ver}-{cp_tag}-{cp_tag}-linux_x86_64.whl"
    )
    print(f"  [flash-attn] {flash_url}")
    try:
        pip_install_no_deps(flash_url)
        print("  ✓ flash-attn installed")
    except subprocess.CalledProcessError as exc:
        print(f"  ✗ flash-attn failed (non-fatal): {exc}")

    print("=== install_wheels.py complete ===")


if __name__ == "__main__":
    main()
