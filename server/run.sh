#!/bin/bash
# Starts the voxbutton server in the background, logging to ~/.local/state/voxbutton.log.
# Meant to be launched from the compositor's autostart so wtype sees WAYLAND_DISPLAY.
cd "$(dirname "$0")"
export PATH="$HOME/.local/bin:$PATH"  # uv, and wtype if built from source
mkdir -p ~/.local/state
exec uv run voxbutton-server "$@" >> ~/.local/state/voxbutton.log 2>&1
