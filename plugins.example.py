# plugins.example.py
# Copy this file to plugins.py and edit it to configure custom node installation.
#
# Plugins are installed during the BUILD phase (CPU, no GPU cost) via
# install_plugins.py, called from shell_commands in cerebrium.toml.
#
# comfy_plugins     → installed via comfy-cli registry  (comfy node install ...)
# comfy_plugins_ext → installed via git clone + pip
#
# Find node IDs at: https://registry.comfy.org/

comfy_plugins = [
    # IMPORTANT: use the node ID from the ComfyUI registry, not the display name.
    "comfyui-kjnodes",
    "ComfyUI-WanVideoWrapper",
    "rgthree-comfy",
    "comfyui-easy-use",
    "comfyui-videohelpersuite",
    "comfyui-impact-pack",
    "comfyui-impact-subpack",
    "ComfyUI-Crystools",
    "raylight",
]

comfy_plugins_ext = [
    # External git-based plugins.
    # {
    #     "url":          "https://github.com/owner/repo.git",
    #     "branch":       "main",
    #     "requirements": "requirements.txt",   # or "pyproject.toml" or leave empty
    #     "install":      "install.py",          # or "setup.py" or leave empty
    #     "dependencies": "ninja 'numpy<2'",     # space-separated, --no-deps install
    # },

    {
        "url":    "https://github.com/Echoflare/ComfyUI-Reverse-Proxy-Fix.git",
        "branch": "main",
    },
    # NOTE: ComfyUI-Manager is intentionally NOT listed here separately —
    # it is already included via comfy-cli's --restore flag in shell_commands.
    # Adding it here too could cause a duplicate install conflict.
    #{
    #    "url": "https://github.com/Comfy-Org/ComfyUI-Manager.git", 
    #    "branch": "main",
    #    "requirements": "pyproject.toml requirements.txt",
    #},
    {
        "url":          "https://github.com/Lightricks/ComfyUI-LTXVideo.git",
        "branch":       "master",
        "dependencies": "kornia~=0.6.12",
    },
]
