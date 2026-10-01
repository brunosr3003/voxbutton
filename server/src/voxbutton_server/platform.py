"""Typing, key presses and window info for the desktop the server runs on.

    Hyprland, Sway   wtype (virtual keyboard protocol); hyprctl / swaymsg extras
    GNOME, KDE       a uinput virtual keyboard (they don't take wtype's protocol)
    other Wayland    wtype when installed, else uinput
    X11              xdotool
    Windows          SendInput through ctypes, no extra installs

"typing" in server.json forces a method: wtype, uinput, xdotool or auto.

Key combos use X keysym-style names everywhere ("ctrl+shift+Tab", "Return",
"Page_Up", "BackSpace", "F5", "a"); the Windows backend maps them to virtual
keys."""

import json
import os
import shutil
import subprocess
import sys


def detect() -> str:
    if sys.platform == "win32":
        return "windows"
    if os.environ.get("WAYLAND_DISPLAY"):
        return "wayland"
    return "x11"


KIND = detect()
HYPRLAND = bool(os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"))
SWAY = bool(os.environ.get("SWAYSOCK"))
_DESKTOP = os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
KDE = "kde" in _DESKTOP
GNOME = "gnome" in _DESKTOP

TYPING = "auto"  # resolved by configure()


def configure(typing: str = "auto") -> None:
    """Picks how to type: wtype on compositors that take its protocol, a uinput
    keyboard on GNOME/KDE (or when wtype is missing), xdotool on X11."""
    global TYPING
    if typing not in ("auto", "wtype", "uinput", "xdotool"):
        typing = "auto"
    if typing == "auto":
        if KIND == "windows":
            typing = "sendinput"
        elif KIND == "x11":
            typing = "xdotool"
        elif (HYPRLAND or SWAY) or (shutil.which("wtype") and not (GNOME or KDE)):
            typing = "wtype"
        else:
            typing = "uinput"
    TYPING = typing


def missing_tool() -> str | None:
    """What this desktop needs for typing but lacks, if anything."""
    if TYPING == "auto":
        configure()
    if TYPING == "uinput":
        if not os.access("/dev/uinput", os.W_OK):
            return ("write access to /dev/uinput (add the udev rule KERNEL==\"uinput\", TAG+=\"uaccess\" "
                    "or join the input group, then log in again)")
        return None
    tool = {"wtype": "wtype", "xdotool": "xdotool"}.get(TYPING)
    return tool if tool and not shutil.which(tool) else None


class Unsupported(LookupError):
    pass


# --- Linux -------------------------------------------------------------------


def _run(*args: str, timeout: float = 30) -> str:
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=timeout).stdout


def _hypr(lua: str) -> None:
    if not HYPRLAND:
        raise Unsupported("only available on Hyprland")
    _run("hyprctl", "dispatch", lua, timeout=5)


# --- Windows -----------------------------------------------------------------

if KIND == "windows":
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    INPUT_KEYBOARD, KEYEVENTF_KEYUP, KEYEVENTF_UNICODE, KEYEVENTF_EXTENDEDKEY = 1, 0x2, 0x4, 0x1

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

    class MOUSEINPUT(ctypes.Structure):  # only here so the union has the right size
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

    class _U(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", _U)]

    VK = {
        "ctrl": 0x11, "shift": 0x10, "alt": 0x12, "super": 0x5B, "logo": 0x5B,
        "Return": 0x0D, "Tab": 0x09, "Escape": 0x1B, "BackSpace": 0x08, "Delete": 0x2E, "space": 0x20,
        "Page_Up": 0x21, "Page_Down": 0x22, "End": 0x23, "Home": 0x24,
        "Left": 0x25, "Up": 0x26, "Right": 0x27, "Down": 0x28,
        **{f"F{i}": 0x6F + i for i in range(1, 13)},
    }
    EXTENDED = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2E}

    def _vk(name: str) -> int:
        if name in VK:
            return VK[name]
        if len(name) == 1 and name.isalnum():
            return ord(name.upper())
        raise Unsupported(f"no Windows key for {name!r}")

    def _send(events: list[INPUT]) -> None:
        arr = (INPUT * len(events))(*events)
        if user32.SendInput(len(events), arr, ctypes.sizeof(INPUT)) != len(events):
            raise OSError(ctypes.get_last_error(), "SendInput was blocked")

    def _key_event(vk: int, up: bool) -> INPUT:
        flags = (KEYEVENTF_KEYUP if up else 0) | (KEYEVENTF_EXTENDEDKEY if vk in EXTENDED else 0)
        return INPUT(type=INPUT_KEYBOARD, u=_U(ki=KEYBDINPUT(wVk=vk, dwFlags=flags)))

    def _unicode_events(text: str) -> list[INPUT]:
        out = []
        data = text.replace("\n", "\r").encode("utf-16-le")
        for i in range(0, len(data), 2):
            unit = int.from_bytes(data[i:i + 2], "little")  # surrogate pairs go as two units
            for up in (False, True):
                out.append(INPUT(type=INPUT_KEYBOARD, u=_U(ki=KEYBDINPUT(
                    wScan=unit, dwFlags=KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0)))))
        return out

    def _foreground_exe() -> str:
        hwnd = user32.GetForegroundWindow()
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        h = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(len(buf))
            if not kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return ""
            return os.path.splitext(os.path.basename(buf.value))[0]
        finally:
            kernel32.CloseHandle(h)


# --- the interface ------------------------------------------------------------


def type_text(text: str) -> None:
    if TYPING == "auto":
        configure()
    if KIND == "windows":
        _send(_unicode_events(text))
    elif TYPING == "uinput":
        from voxbutton_server.uinput import Keyboard

        Keyboard.get().type(text)
    elif TYPING == "wtype":
        _run("wtype", "--", text)
    else:
        _run("xdotool", "type", "--clearmodifiers", "--delay", "4", "--", text)


def key(combo: str, times: int = 1) -> None:
    """Press a combo like "ctrl+shift+Tab", `times` times."""
    *mods, k = combo.split("+")
    if KIND == "windows":
        vks = [_vk(m) for m in mods] + [_vk(k)]
        once = [_key_event(v, False) for v in vks] + [_key_event(v, True) for v in reversed(vks)]
        _send(once * times)
    elif TYPING == "uinput":
        from voxbutton_server.uinput import Keyboard

        Keyboard.get().key(combo, times)
    elif TYPING == "wtype":
        args = []
        for _ in range(times):
            args += [a for m in mods for a in ("-M", m)] + ["-k", k] + [a for m in reversed(mods) for a in ("-m", m)]
        _run("wtype", *args)
    else:
        _run("xdotool", "key", "--clearmodifiers", "--repeat", str(times), combo)


def active_app() -> str:
    """Lowercased class / program name of the focused window."""
    try:
        if KIND == "windows":
            return _foreground_exe().lower()
        if HYPRLAND:
            return (json.loads(_run("hyprctl", "activewindow", "-j", timeout=3) or "{}").get("class") or "").lower()
        if SWAY:
            return _sway_focused()
        if KDE and shutil.which("kdotool"):
            return _run("kdotool", "getactivewindow", "getwindowclassname", timeout=3).strip().lower()
        if KIND == "x11" and shutil.which("xdotool"):
            return _run("xdotool", "getactivewindow", "getwindowclassname", timeout=3).strip().lower()
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return ""


def _sway_focused() -> str:
    def walk(node):
        if node.get("focused"):
            return node.get("app_id") or (node.get("window_properties") or {}).get("class") or ""
        for child in node.get("nodes", []) + node.get("floating_nodes", []):
            if (found := walk(child)) is not None:
                return found
        return None

    return (walk(json.loads(_run("swaymsg", "-t", "get_tree", timeout=3))) or "").lower()


def focus_workspace(n: int) -> None:
    if HYPRLAND:
        _hypr(f'hl.dsp.focus({{ workspace = "{n}" }})')
    elif SWAY:
        _run("swaymsg", "workspace", "number", str(n), timeout=5)
    elif KDE and shutil.which("kdotool"):
        _run("kdotool", "set_desktop", str(n), timeout=5)
    elif KIND == "x11":
        _run("xdotool", "set_desktop", str(n - 1))
    else:
        raise Unsupported("workspaces by number aren't available on this desktop")


def focus_direction(d: str) -> None:
    """d is one of l, r, u, d."""
    if HYPRLAND:
        _hypr(f'hl.dsp.focus({{ direction = "{d}" }})')
    elif SWAY:
        _run("swaymsg", "focus", {"l": "left", "r": "right", "u": "up", "d": "down"}[d], timeout=5)
    else:
        raise Unsupported("moving focus by direction is only available on Hyprland and Sway")


def close_window() -> None:
    """Closes the focused window, with all its tabs. Apps with something still
    running (kitty with a session, a browser) ask to confirm first."""
    if HYPRLAND:
        app = active_app()
        if not app or app.startswith("voxbutton"):
            raise Unsupported("no app window in focus")
        _hypr("hl.dsp.window.close()")  # acts on the active window, which is the one checked above
    elif SWAY:
        _run("swaymsg", "kill", timeout=5)
    else:
        key("alt+F4")


def open_terminal(cmd: list[str]) -> None:
    """Opens a new terminal window running `cmd` in the home folder: kitty
    when it's there, else another known terminal; Windows Terminal on Windows."""
    home = os.path.expanduser("~")
    if KIND == "windows":
        if shutil.which("wt"):
            args = ["wt", "-d", home, *cmd]
        else:
            args = ["cmd", "/c", "start", "", "/d", home, *cmd]
        subprocess.Popen(args, creationflags=0x00000008)  # DETACHED_PROCESS
        return
    for term, prefix in (("kitty", ["--directory", home]), ("alacritty", ["--working-directory", home, "-e"]),
                         ("foot", ["--working-directory", home]), ("wezterm", ["start", "--cwd", home, "--"]),
                         ("gnome-terminal", ["--working-directory", home, "--"]), ("konsole", ["--workdir", home, "-e"]),
                         ("xterm", ["-e"])):
        if shutil.which(term):
            break
    else:
        raise Unsupported("no terminal emulator found")
    env = dict(os.environ)
    env["PATH"] = os.path.join(home, ".local", "bin") + os.pathsep + env.get("PATH", "")
    subprocess.Popen([term, *prefix, *cmd], env=env, cwd=home, start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
