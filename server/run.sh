#!/bin/bash
# Starts the voxbutton server in the background, logging to ~/.local/state/voxbutton.log.
# Meant to be launched from the compositor's autostart so wtype sees WAYLAND_DISPLAY.
cd "$(dirname "$0")"
mkdir -p ~/.local/state
exec uv run voxbutton-server "$@" >> ~/.local/state/voxbutton.log 2>&1
