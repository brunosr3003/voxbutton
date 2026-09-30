#!/bin/bash
# Runs the server and the Hyprland button for the current graphical session.
# Meant for a systemd user service started at boot (see desktop/autostart.py):
# it waits for Hyprland, adopts its environment, and exits when the session
# goes away so systemd can restart it for the next one.
set -u
cd "$(dirname "$0")"
export PATH="$HOME/.local/bin:$PATH"
RUN="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

# Wait for Hyprland and its Wayland socket.
until sock=$(ls -t "$RUN"/hypr/*/.socket.sock 2>/dev/null | head -1) && [[ -n $sock ]] &&
      way=$(cd "$RUN" && ls -t wayland-[0-9]* 2>/dev/null | grep -v '\.lock$' | head -1) && [[ -n $way ]]; do
    sleep 2
done
export HYPRLAND_INSTANCE_SIGNATURE=$(basename "$(dirname "$sock")")
export WAYLAND_DISPLAY=$way
export XDG_RUNTIME_DIR=$RUN
sleep 3  # let the compositor finish starting up

../server/run.sh &
server=$!
trap 'kill $server 2>/dev/null' EXIT
python3 ./voxbutton-button.py
