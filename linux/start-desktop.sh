#!/bin/bash
# Any Linux desktop except Hyprland: GNOME, KDE Plasma, Sway, X11 (i3, Xfce…).
# Starts the server and the Tk widget, which on Wayland runs through XWayland:
# it stays on top and never takes focus there too. Typing goes through wtype
# where the compositor allows it and a uinput keyboard otherwise (GNOME, KDE).
# On Hyprland use linux/voxbutton-session.sh (the GTK widget) instead.
cd "$(dirname "$0")/../server"
export PATH="$HOME/.local/bin:$PATH"
mkdir -p ~/.local/state
uv run voxbutton-server >> ~/.local/state/voxbutton.log 2>&1 &
server=$!
trap 'kill $server 2>/dev/null' EXIT
sleep 3
uv run python ../desktop/voxbutton_tk.py
