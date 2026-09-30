#!/bin/bash
# Builds VoxButton.app next to this script. Usage: ./build.sh [--install]
# --install copies it to ~/Applications and launches it.
set -euo pipefail
cd "$(dirname "$0")"

APP=VoxButton.app
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS"
cp Info.plist "$APP/Contents/"
swiftc -O -o "$APP/Contents/MacOS/VoxButton" VoxButton.swift

# A stable signing identity keeps the microphone permission across rebuilds;
# ad-hoc signing works too but macOS asks again after every build.
IDENTITY=$(security find-identity -v -p codesigning 2>/dev/null | awk -F'"' '/Apple Development/ {print $2; exit}')
codesign --force --sign "${IDENTITY:--}" "$APP"
echo "built $APP (signed with: ${IDENTITY:-ad-hoc})"

if [[ "${1:-}" == "--install" ]]; then
    pkill -x VoxButton || true
    mkdir -p ~/Applications
    rm -rf ~/Applications/"$APP"
    cp -R "$APP" ~/Applications/
    open ~/Applications/"$APP"
    echo "installed and launched ~/Applications/$APP"
fi
