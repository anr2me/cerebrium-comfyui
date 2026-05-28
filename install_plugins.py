"""
install_plugins.py — Custom node installer for Cerebrium build phase.
Called from shell_commands in cerebrium.toml → runs on CPU, NO GPU allocated.

Modal equivalent:
  comfy_plugins     → "comfy node install ..." (comfy-cli registry)
  comfy_plugins_ext → git clone + pip install requirements + python install.py

Usage (called by cerebrium.toml shell_commands):
  python install_plugins.py --registry    # install comfy_plugins
  python install_plugins.py --ext         # install comfy_plugins_ext
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

NODES_DIR = Path("/root/comfy/ComfyUI/custom_nodes")
NODES_DIR.mkdir(parents=True, exist_ok=True)

#sys.path.insert(0, "/app")


def install_registry_plugins() -> None:
    """
    Modal equivalent:
      image.run_commands("comfy node install " + " ".join(comfy_plugins))
    """
    try:
        from plugins import comfy_plugins
    except ImportError:
        print("WARNING: plugins.py not found — skipping registry plugins.")
        return

    if not comfy_plugins:
        print("No registry plugins configured.")
        return

    print(f"Installing {len(comfy_plugins)} registry plugin(s) via comfy-cli …")
    cmd = ["comfy", "node", "install"] + list(comfy_plugins)
    print(f"  $ {' '.join(cmd)}")
    subprocess.run(cmd, check=True)
    print("Registry plugins installed.")


def install_ext_plugins() -> None:
    """
    Modal equivalent:
      for plugin in comfy_plugins_ext:
          image.run_commands(f"cd {nodes_dir} && git clone --recurse-submodules ...")
          image.run_commands(f"uv pip install --no-deps -r requirements.txt")
          image.run_commands(f"python install.py")
          image.uv_pip_install(plugin_deps, extra_options="--no-deps")
    """
    try:
        from plugins import comfy_plugins_ext
    except ImportError:
        print("WARNING: plugins.py not found — skipping ext plugins.")
        return

    if not comfy_plugins_ext:
        print("No ext plugins configured.")
        return

    print(f"Installing {len(comfy_plugins_ext)} external plugin(s) …")

    for plugin in comfy_plugins_ext:
        url    = plugin["url"]
        branch = plugin.get("branch", "main")
        folder = url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
        dest   = NODES_DIR / folder

        # ── git clone ────────────────────────────────────────────────────────
        if dest.exists():
            print(f"  [skip clone] {folder} already exists — pulling instead")
            subprocess.run(["git", "-C", str(dest), "pull", "--ff-only"], check=False)
        else:
            print(f"  [git clone] {url} (branch={branch})")
            subprocess.run(
                [
                    "git", "clone",
                    "--recurse-submodules",
                    "--single-branch", 
                    "--branch", branch, 
                    url,
                    str(dest),
                ],
                check=True,
            )

        # ── pip install requirements ─────────────────────────────────────────
        plugin_reqs = plugin.get("requirements", "").strip()
        if plugin_reqs:
            req_files = [f"-r {f}" for f in plugin_reqs.split()]
            formatted = " ".join(req_files)
            print(f"  [pip reqs] {formatted}")
            subprocess.run(
                f"cd {dest} && uv pip install --system --no-deps {formatted}",
                shell=True,
                check=False,  # non-fatal; some req files have version conflicts
            )

        # ── run install script ───────────────────────────────────────────────
        plugin_install = plugin.get("install", "").strip()
        if plugin_install:
            if plugin_install.endswith(".py"):
                print(f"  [install.py] python {plugin_install}")
                subprocess.run(
                    [sys.executable, plugin_install],
                    cwd=str(dest),
                    check=False,
                )
            else:
                print(f"  [skip] unsupported install script: {plugin_install}")

        # ── install extra deps (e.g. "ninja" for custom kernels) ─────────────
        plugin_deps = plugin.get("dependencies", "").strip()
        if plugin_deps:
            deps = plugin_deps.split()
            print(f"  [extra deps] {deps}")
            subprocess.run(
                ["uv", "pip", "install", "--system", "--no-deps"] + deps,
                check=False,
            )

        print(f"  ✓ {folder}")

    print("Ext plugins installed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--registry", action="store_true", help="Install comfy_plugins")
    parser.add_argument("--ext",      action="store_true", help="Install comfy_plugins_ext")
    args = parser.parse_args()

    if args.registry:
        install_registry_plugins()
    if args.ext:
        install_ext_plugins()
    if not args.registry and not args.ext:
        install_registry_plugins()
        install_ext_plugins()
