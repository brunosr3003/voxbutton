"""Start voxbutton with the computer, per platform:

    Windows   a shortcut to windows/start.bat in the Startup folder
    Hyprland  a systemd user service running linux/voxbutton-session.sh, which
              waits for the session (the Hyprland config is left alone: saving
              it makes Hyprland reload)
    other     an XDG autostart entry (GNOME, KDE, Sway, X11 desktops all run
              these at login) running linux/start-desktop.sh

Used by both settings windows; also runnable: autostart.py [on|off|status]."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WIN = sys.platform == "win32"
UNIT = "voxbutton.service"


def kind() -> str:
    if WIN:
        return "windows"
    if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE") and shutil.which("systemctl"):
        return "systemd"
    return "xdg"


def _startup_link() -> Path:
    return Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/Startup/voxbutton.lnk"


def _unit_file() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "systemd/user" / UNIT


def _desktop_file() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "autostart/voxbutton.desktop"


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=15)


def enabled() -> bool:
    k = kind()
    if k == "windows":
        return _startup_link().exists()
    if k == "systemd":
        return _systemctl("is-enabled", UNIT).stdout.strip() == "enabled"
    return _desktop_file().exists()


def describe() -> str:
    return {
        "windows": "Adds voxbutton to the Windows Startup folder.",
        "systemd": "A systemd user service starts it at boot and waits for Hyprland.",
        "xdg": "An autostart entry starts it when you log in.",
    }[kind()]


def set_enabled(on: bool) -> None:
    k = kind()
    if k == "windows":
        link = _startup_link()
        if not on:
            link.unlink(missing_ok=True)
            return
        target = ROOT / "windows" / "start.bat"
        ps = ("$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:VB_LINK);"
              "$s.TargetPath = $env:VB_TARGET; $s.WorkingDirectory = $env:VB_DIR; $s.WindowStyle = 7; $s.Save()")
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True, timeout=30,
                       env={**os.environ, "VB_LINK": str(link), "VB_TARGET": str(target), "VB_DIR": str(target.parent)},
                       creationflags=0x08000000)
    elif k == "systemd":
        if on:
            unit = _unit_file()
            unit.parent.mkdir(parents=True, exist_ok=True)
            unit.write_text(f"""[Unit]
Description=voxbutton (Whisper server + floating mic button)
After=network-online.target

[Service]
ExecStart={ROOT / "linux" / "voxbutton-session.sh"}
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
""")
            _systemctl("daemon-reload")
            r = _systemctl("enable", UNIT)
        else:
            r = _systemctl("disable", UNIT)
        if r.returncode != 0:
            raise OSError(r.stderr.strip() or "systemctl failed")
    else:
        f = _desktop_file()
        if not on:
            f.unlink(missing_ok=True)
            return
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(f"""[Desktop Entry]
Type=Application
Name=VoxButton
Comment=Floating mic button with local Whisper
Exec={ROOT / "linux" / "start-desktop.sh"}
X-GNOME-Autostart-enabled=true
""")


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else "status"
    if arg in ("on", "off"):
        set_enabled(arg == "on")
    print(f"{kind()}: {'on' if enabled() else 'off'}")
