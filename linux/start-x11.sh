#!/bin/bash
# Kept for old autostart entries: linux/start-desktop.sh covers X11 and Wayland.
exec "$(dirname "$0")/start-desktop.sh" "$@"
