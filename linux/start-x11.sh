#!/bin/bash
# X11 desktops (GNOME/KDE on Xorg, i3...): starts the server and the Tk button.
# On Hyprland use server/run.sh + linux/voxbutton-button.py instead.
cd "$(dirname "$0")/../server"
export PATH="$HOME/.local/bin:$PATH"
mkdir -p ~/.local/state
nohup uv run voxbutton-server >> ~/.local/state/voxbutton.log 2>&1 &
sleep 3
exec uv run python ../desktop/voxbutton_tk.py
