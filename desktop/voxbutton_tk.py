#!/usr/bin/env python3
"""VoxButton for Windows and X11 desktops (Tk, no extra packages).

A small always-on-top button that never takes keyboard focus, so the text
lands in the window you were typing in:

    left button   dictate      right button   voice command ("next tab", ...)
    record mode (settings): toggle = click to start and stop, hold = talk
                  while holding, always = click once, it types at every pause
    drag          move it (the position is remembered); in hold mode drag
                  by the gear
    gear          settings: devices, how to connect, commands, modes

On Hyprland use linux/voxbutton-button.py instead (Wayland windows can't
place themselves). Talks to the voxbutton server on this machine; the address
comes from the server's config (server.json) or --server.
"""

import argparse
import json
import math
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
from pathlib import Path
from tkinter import ttk

sys.path.insert(0, str(Path(__file__).resolve().parent))
import autostart  # noqa: E402

RECORD_MODES = [
    ("toggle", "Toggle: click to start, click to stop"),
    ("hold", "Hold: talk while holding the button"),
    ("always", "Always on: click once, it types at every pause"),
]

WIN = sys.platform == "win32"
if WIN:
    CONFIG_DIR = Path(os.environ.get("APPDATA", Path.home())) / "voxbutton"
else:
    CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "voxbutton"
BUTTON_FILE = CONFIG_DIR / "button.json"
REPO = "https://github.com/brunosr3003/voxbutton"

SIZE = 64
GEAR_R = 9
KEY = "#010203"  # transparent color key on Windows
COLORS = {
    "offline": "#5a5a61", "idle": "#222226", "recording": "#e53333", "command": "#3373f2",
    "busy": "#f29a1a", "listening": "#1aa699", "done": "#33bf59", "error": "#994dd9",
}


# --- server ----------------------------------------------------------------


def tailscale_ip() -> str | None:
    exe = shutil.which("tailscale")
    if not exe and WIN:
        p = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Tailscale" / "tailscale.exe"
        exe = str(p) if p.exists() else None
    if not exe:
        return None
    try:
        flags = 0x08000000 if WIN else 0  # CREATE_NO_WINDOW
        out = subprocess.run([exe, "ip", "-4"], capture_output=True, text=True, timeout=5, creationflags=flags)
        return out.stdout.split()[0] if out.returncode == 0 and out.stdout.split() else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def default_server() -> str:
    cfg = {}
    try:
        cfg = json.loads((CONFIG_DIR / "server.json").read_text())
    except (OSError, ValueError):
        pass
    host = cfg.get("host") or tailscale_ip() or "127.0.0.1"
    return f"http://{host}:{cfg.get('port', 8765)}"


class Client:
    def __init__(self, server: str, token: str):
        self.server, self.token = server.rstrip("/"), token

    def call(self, method: str, path: str, body: dict | None = None, timeout: float = 5) -> dict:
        data = json.dumps(body).encode() if body is not None else (b"" if method == "POST" else None)
        req = urllib.request.Request(self.server + path, method=method, data=data)
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            try:
                return json.load(e)
            except ValueError:
                return {"error": f"HTTP {e.code}"}
        except (OSError, ValueError) as e:
            return {"error": str(e)}


# Tk may only be touched from its own thread: workers queue callbacks here and
# the button's tick runs them.
UI: "queue.Queue[tuple]" = queue.Queue()


def on_ui(fn, *args) -> None:
    UI.put((fn, args))


# --- the button ---------------------------------------------------------------


def no_activate(win: tk.Tk) -> None:
    """Windows: clicks never activate the button, focus stays where you type."""
    import ctypes

    user32 = ctypes.windll.user32
    hwnd = user32.GetParent(win.winfo_id()) or win.winfo_id()
    GWL_EXSTYLE, WS_EX_NOACTIVATE, WS_EX_TOOLWINDOW, WS_EX_TOPMOST = -20, 0x08000000, 0x80, 0x8
    get = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
    put = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)
    put(hwnd, GWL_EXSTYLE, get(hwnd, GWL_EXSTYLE) | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_TOPMOST)


class Button:
    def __init__(self, root: tk.Tk, client: Client):
        self.root, self.client = root, client
        self.state, self.mode, self.record_mode = "offline", "chat", "toggle"
        self.talking = False
        self.correction_seen: int | None = None
        self.correction_win: tk.Toplevel | None = None
        self.flash_kind, self.flash_until = "", 0.0
        self.settings: Settings | None = None
        self.press: tuple[int, int, int, int] | None = None
        self.dragged = False

        root.overrideredirect(True)
        root.attributes("-topmost", True)
        bg = KEY if WIN else "#18181b"
        if WIN:
            root.attributes("-transparentcolor", KEY)
        pos = self.load_pos(root)
        root.geometry(f"{SIZE}x{SIZE}+{pos[0]}+{pos[1]}")
        self.cv = tk.Canvas(root, width=SIZE, height=SIZE, bg=bg, highlightthickness=0, bd=0)
        self.cv.pack()
        self.cv.bind("<ButtonPress-1>", self.on_press)
        self.cv.bind("<B1-Motion>", self.on_drag)
        self.cv.bind("<ButtonRelease-1>", lambda e: self.on_release(e, "chat"))
        self.cv.bind("<ButtonPress-3>", self.on_right_press)
        self.cv.bind("<ButtonRelease-3>", self.on_right_release)
        if WIN:
            root.update_idletasks()
            no_activate(root)

        threading.Thread(target=self.poll, daemon=True).start()
        self.tick()

    # position

    @staticmethod
    def load_pos(root: tk.Tk) -> tuple[int, int]:
        try:
            p = json.loads(BUTTON_FILE.read_text())
            return int(p["x"]), int(p["y"])
        except (OSError, ValueError, KeyError, TypeError):
            return root.winfo_screenwidth() - SIZE - 24, root.winfo_screenheight() // 2 - SIZE // 2

    def save_pos(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        BUTTON_FILE.write_text(json.dumps({"x": self.root.winfo_x(), "y": self.root.winfo_y()}) + "\n")

    # input

    def on_gear(self, x: int, y: int) -> bool:
        g = SIZE - GEAR_R - 1
        return (x - g) ** 2 + (y - g) ** 2 <= (GEAR_R + 3) ** 2 and self.state not in ("recording", "listening")

    def send(self, path: str) -> None:
        threading.Thread(target=lambda: self.apply_async(self.client.call("POST", path)), daemon=True).start()

    def on_press(self, e):
        self.press = (e.x_root, e.y_root, self.root.winfo_x(), self.root.winfo_y())
        self.dragged = False
        if self.record_mode == "hold" and not self.on_gear(e.x, e.y) and self.state != "busy":
            self.talking = True
            self.send("/record/start?mode=chat")

    def on_right_press(self, e):
        if self.record_mode == "hold":
            self.send("/record/start?mode=command")

    def on_right_release(self, e):
        if self.record_mode == "hold":
            self.send("/record/stop")
        else:
            self.on_release(e, "command")

    def on_drag(self, e):
        if not self.press or self.talking:
            return
        dx, dy = e.x_root - self.press[0], e.y_root - self.press[1]
        if not self.dragged and dx * dx + dy * dy < 16:
            return
        self.dragged = True
        self.root.geometry(f"+{self.press[2] + dx}+{self.press[3] + dy}")

    def on_release(self, e, mode: str):
        if self.talking and mode == "chat":
            self.talking = False
            self.send("/record/stop")
            return
        if self.dragged:
            self.dragged, self.press = False, None
            self.save_pos()
            return
        if self.on_gear(e.x, e.y):
            self.open_settings()
            return
        if self.state == "busy":
            return
        self.send(f"/record/toggle?mode={mode}")

    def open_settings(self):
        if self.settings and self.settings.win.winfo_exists():
            self.settings.win.deiconify()
            self.settings.win.lift()
            return
        self.settings = Settings(self.root, self.client)

    # state

    def poll(self):
        while True:
            self.apply_async(self.client.call("GET", "/state"))
            time.sleep(0.25)

    def apply_async(self, s: dict):
        on_ui(self.apply, s)

    def apply(self, s: dict):
        if "error" in s:
            self.flash("error")
            if "state" not in s and "agent" not in s:
                self.state = "offline"
        else:
            self.state = s["state"] if s.get("agent") or s["state"] != "idle" else "offline"
            self.mode = s.get("mode", "chat")
            self.record_mode = s.get("record_mode", self.record_mode)
            self.show_correction(s.get("correction") or {})
            if s.get("flash"):
                self.flash(s["flash"])

    def show_correction(self, c: dict):
        cid = c.get("id", 0)
        if self.correction_seen is None:  # don't replay the last one on startup
            self.correction_seen = cid
            return
        if cid == self.correction_seen or not c.get("corrected"):
            return
        self.correction_seen = cid
        if self.correction_win and self.correction_win.winfo_exists():
            self.correction_win.destroy()
        w = tk.Toplevel(self.root, bg="#18181b")
        w.overrideredirect(True)
        w.attributes("-topmost", True)
        width = 720
        for text, color, font in (("YOU SAID", "#7a7a80", ("TkDefaultFont", 8, "bold")),
                                  (c["original"], "#b8b8be", ("TkDefaultFont", 10)),
                                  ("BETTER", "#57e389", ("TkDefaultFont", 8, "bold")),
                                  (c["corrected"], "#ffffff", ("TkDefaultFont", 12, "bold"))):
            tk.Label(w, text=text, fg=color, bg="#18181b", font=font, wraplength=width - 36, justify="left",
                     anchor="w").pack(fill="x", padx=18, pady=(8 if text in ("YOU SAID", "BETTER") else 0, 0))
        tk.Frame(w, height=12, bg="#18181b").pack()
        w.update_idletasks()
        x = (w.winfo_screenwidth() - width) // 2
        y = 70
        w.geometry(f"{width}x{w.winfo_reqheight()}+{x}+{y}")
        if WIN:
            no_activate(w)
        w.after(10000, lambda: w.winfo_exists() and w.destroy())
        self.correction_win = w

    def flash(self, kind: str):
        if kind != self.flash_kind or time.time() > self.flash_until:
            self.flash_kind = kind
            self.flash_until = time.time() + (2.0 if kind == "error" else 0.8)

    def tick(self):
        while True:
            try:
                fn, args = UI.get_nowait()
            except queue.Empty:
                break
            fn(*args)
        self.draw()
        self.root.after(60, self.tick)

    def draw(self):
        cv = self.cv
        cv.delete("all")
        look = self.flash_kind if time.time() < self.flash_until else self.state
        color = COLORS["command" if look in ("recording", "listening") and self.mode == "command" else look]
        c, r = SIZE / 2, SIZE / 2 - 6
        if look in ("recording", "listening"):
            p = 2 + 2 * (0.5 + 0.5 * math.sin(time.time() * 5))
            cv.create_oval(c - r - p, c - r - p, c + r + p, c + r + p, fill=color, outline="", stipple="gray50")
        cv.create_oval(c - r, c - r, c + r, c + r, fill=color, outline="")
        fg = "#ffffff" if look != "offline" else "#b0b0b5"
        s = r / 22
        if look == "busy":
            for i in (-1, 0, 1):
                x = c + i * 7 * s
                cv.create_oval(x - 2.4 * s, c - 2.4 * s, x + 2.4 * s, c + 2.4 * s, fill=fg, outline="")
        else:
            top, cw, ch = c - 11 * s, 8 * s, 14 * s
            cv.create_rectangle(c - cw / 2, top + cw / 2, c + cw / 2, top + ch - cw / 2, fill=fg, outline="")
            cv.create_oval(c - cw / 2, top, c + cw / 2, top + cw, fill=fg, outline="")
            cv.create_oval(c - cw / 2, top + ch - cw, c + cw / 2, top + ch, fill=fg, outline="")
            cy = top + ch - cw / 2
            cv.create_arc(c - 7 * s, cy - 7 * s, c + 7 * s, cy + 7 * s, start=180, extent=180,
                          style="arc", outline=fg, width=2.2 * s)
            cv.create_line(c, cy + 7 * s, c, top + ch + 6 * s, fill=fg, width=2.2 * s)
            cv.create_line(c - 5 * s, top + ch + 6 * s, c + 5 * s, top + ch + 6 * s, fill=fg, width=2.2 * s)
        if look not in ("recording", "listening"):
            g = SIZE - GEAR_R - 1
            cv.create_oval(g - GEAR_R, g - GEAR_R, g + GEAR_R, g + GEAR_R, fill="#38383f", outline="")
            pts = []
            for i in range(16):
                a = i * math.pi / 8
                rr = GEAR_R * (0.72 if i % 2 == 0 else 0.52)
                pts += [g + rr * math.cos(a), g + rr * math.sin(a)]
            cv.create_polygon(pts, fill="#e6e6e6", outline="")
            h = GEAR_R * 0.24
            cv.create_oval(g - h, g - h, g + h, g + h, fill="#38383f", outline="")


# --- settings -----------------------------------------------------------------


class Settings:
    def __init__(self, root: tk.Tk, client: Client):
        self.client = client
        self.win = tk.Toplevel(root)
        self.win.title("VoxButton settings")
        self.win.geometry("640x680")
        self.win.attributes("-topmost", True)
        self.loaded = False
        if not WIN and "clam" in ttk.Style().theme_names():
            ttk.Style().theme_use("clam")

        nb = ttk.Notebook(self.win)
        nb.pack(fill="both", expand=True, padx=8, pady=8)
        self.devices = ttk.Frame(nb, padding=14)
        self.commands = ttk.Frame(nb, padding=14)
        self.general = ttk.Frame(nb, padding=14)
        nb.add(self.devices, text="Devices")
        nb.add(self.commands, text="Commands")
        nb.add(self.general, text="General")
        self.build_devices()
        self.build_commands()
        self.build_general()
        self.refresh()

    @staticmethod
    def heading(parent, text):
        ttk.Label(parent, text=text, font=("TkDefaultFont", 11, "bold")).pack(anchor="w", pady=(8, 4))

    def copy_row(self, parent, name: str) -> tk.StringVar:
        var = tk.StringVar()
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=2)
        ttk.Label(row, text=name, width=22).pack(side="left")
        ttk.Entry(row, textvariable=var, state="readonly").pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(row, text="Copy", command=lambda: self.copy(var.get())).pack(side="right")
        return var

    def copy(self, text: str):
        self.win.clipboard_clear()
        self.win.clipboard_append(text)

    def build_devices(self):
        f = self.devices
        self.heading(f, "Microphones")
        ttk.Label(f, wraplength=580, foreground="#777", text=(
            "A click on the button records on the device that's receiving the Moonlight stream. "
            "Without a stream, the Mac wins while Moonlight is in front on it, then the iPhone.")).pack(anchor="w")
        self.stream = tk.StringVar(value="…")
        ttk.Label(f, textvariable=self.stream).pack(anchor="w", pady=4)
        self.tree = ttk.Treeview(f, columns=("status", "ips", "role"), show="headings", height=4)
        for col, w in (("status", 150), ("ips", 250), ("role", 170)):
            self.tree.heading(col, text={"status": "Device", "ips": "Addresses", "role": ""}[col])
            self.tree.column(col, width=w, anchor="w")
        self.tree.pack(fill="x", pady=4)

        self.heading(f, "Connect a device")
        self.local = self.copy_row(f, "Server (Tailscale)")
        self.public = self.copy_row(f, "Server (public HTTPS)")
        self.token = self.copy_row(f, "Token")
        ttk.Label(f, wraplength=580, justify="left", text=(
            f"Mac: clone {REPO}, run mac/build.sh --install, and save this as "
            "~/.config/voxbutton/config.json:")).pack(anchor="w", pady=(8, 2))
        self.mac_cfg = self.copy_row(f, "config.json")
        ttk.Label(f, wraplength=580, justify="left", text=(
            "Linux or Windows: save the same config.json (Windows: in %APPDATA%\\voxbutton), then run "
            "agent/voxbutton_agent.py --install (starts now and at every login).")).pack(anchor="w", pady=(8, 2))
        ttk.Label(f, wraplength=580, justify="left", text=(
            "iPhone: build and install with ios/build.sh from a Mac, open VoxButton, set Server to the "
            "public HTTPS address, paste the Token and turn on “Lend microphone”.")).pack(anchor="w", pady=(8, 2))

    def build_commands(self):
        f = self.commands
        self.heading(f, "Voice commands")
        ttk.Label(f, wraplength=580, foreground="#777", text=(
            "Right-click the button (it turns blue), say the command in English, click again.")).pack(anchor="w")
        self.cmds = ttk.Treeview(f, columns=("say", "does"), show="headings")
        self.cmds.heading("say", text="Say")
        self.cmds.heading("does", text="Does")
        self.cmds.column("say", width=230)
        self.cmds.column("does", width=350)
        self.cmds.pack(fill="both", expand=True, pady=6)

    def build_general(self):
        f = self.general
        self.heading(f, "Recording")
        self.rec_mode = tk.StringVar(value=RECORD_MODES[0][1])
        ttk.Combobox(f, textvariable=self.rec_mode, values=[d for _, d in RECORD_MODES], state="readonly",
                     width=48).pack(anchor="w")
        ttk.Label(f, wraplength=580, foreground="#777", text=(
            "Always on: start a sentence with “command” to run it as one, e.g. “command next tab”. "
            "In hold mode, drag the button by its gear.")).pack(anchor="w", pady=(2, 6))
        self.pause = tk.DoubleVar(value=0.6)
        self.chunk = tk.DoubleVar(value=6)
        row = ttk.Frame(f)
        row.pack(anchor="w", pady=2)
        ttk.Label(row, text="Always on: send after a pause of (s)", width=36).pack(side="left")
        ttk.Spinbox(row, from_=0.2, to=3.0, increment=0.1, textvariable=self.pause, width=6).pack(side="left")
        row = ttk.Frame(f)
        row.pack(anchor="w", pady=2)
        ttk.Label(row, text="Always on: while talking, send every (s)", width=36).pack(side="left")
        ttk.Spinbox(row, from_=2, to=30, increment=1, textvariable=self.chunk, width=6).pack(side="left")
        self.auto = tk.BooleanVar(value=autostart.enabled())
        ttk.Checkbutton(f, text="Start with the computer", variable=self.auto,
                        command=self.on_autostart).pack(anchor="w")
        self.auto_hint = tk.StringVar(value=autostart.describe())
        ttk.Label(f, textvariable=self.auto_hint, foreground="#777", wraplength=580).pack(anchor="w")

        self.heading(f, "English corrector")
        self.corr = tk.BooleanVar(value=False)
        self.corr_model = tk.StringVar()
        ttk.Checkbutton(f, text="Show a better version after each dictation", variable=self.corr).pack(anchor="w")
        row = ttk.Frame(f)
        row.pack(anchor="w", pady=2)
        ttk.Label(row, text="Model (Ollama)", width=16).pack(side="left")
        ttk.Entry(row, textvariable=self.corr_model, width=24).pack(side="left")
        ttk.Label(f, wraplength=580, foreground="#777", text=(
            "A card at the top of the screen shows what you said and a corrected version; what gets "
            "typed doesn't change.")).pack(anchor="w")

        self.heading(f, "Transcription")
        self.model = tk.StringVar()
        self.langs = tk.StringVar()
        self.level = tk.DoubleVar(value=-34)
        grid = ttk.Frame(f)
        grid.pack(fill="x")
        ttk.Label(grid, text="Whisper model").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Label(grid, textvariable=self.model).grid(row=0, column=1, sticky="w")
        ttk.Label(grid, text="Dictation languages").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(grid, textvariable=self.langs, width=24).grid(row=1, column=1, sticky="w")
        ttk.Label(grid, text="e.g. en,pt (commands are always English)", foreground="#777").grid(row=2, column=1, sticky="w")
        ttk.Label(grid, text="Silence threshold (dBFS)").grid(row=3, column=0, sticky="w", pady=4)
        ttk.Spinbox(grid, from_=-70, to=-10, increment=1, textvariable=self.level, width=8).grid(row=3, column=1, sticky="w")
        ttk.Label(grid, text="quieter clips are skipped so Whisper doesn't invent text",
                  foreground="#777").grid(row=4, column=1, sticky="w")
        self.saved = tk.StringVar()
        row = ttk.Frame(f)
        row.pack(anchor="w", pady=10)
        ttk.Button(row, text="Save", command=self.save).pack(side="left")
        ttk.Label(row, textvariable=self.saved).pack(side="left", padx=10)
        self.heading(f, "Button")
        ttk.Label(f, wraplength=580, foreground="#777", text=(
            "Left button: dictate · Right button: voice command · Drag: move · Gear: this window.\n"
            "Colors: dark ready, red dictating, blue command, teal listening, orange transcribing, "
            "green done, purple error, gray no microphone.")).pack(anchor="w")

    def refresh(self):
        if not self.win.winfo_exists():
            return
        threading.Thread(target=lambda: on_ui(self.apply, self.client.call("GET", "/info")), daemon=True).start()
        self.win.after(1500, self.refresh)

    def apply(self, info: dict):
        if not self.win.winfo_exists():
            return
        if "error" in info:
            self.stream.set(f"Can't reach the server at {self.client.server}: {info['error']}")
            return
        devs = info.get("devices", [])
        who = next((d["name"] for d in devs if d["streaming"]), None)
        st = info.get("stream") or {}
        self.stream.set(f"Streaming to {who.capitalize() if who else 'an unknown device'} "
                        f"({st['peer']}, {st['mbit']} Mbit/s)" if st.get("peer") else "No Moonlight stream right now")
        self.tree.delete(*self.tree.get_children())
        for d in devs:
            role = " · ".join(x for x, on in (("streaming", d["streaming"]), ("uses this mic", d["selected"])) if on)
            status = f"{'●' if d['online'] else '○'} {d['name'].capitalize()}"
            self.tree.insert("", "end", values=(status, ", ".join(d["ips"]), role))
        c = info.get("connect", {})
        self.local.set(c.get("local", ""))
        self.public.set(c.get("public") or "not set (public_url in server.json)")
        self.token.set(c.get("token", ""))
        self.mac_cfg.set(json.dumps({"server": c.get("local", ""), "token": c.get("token", "")}))
        if not self.loaded:
            for cmd in info.get("commands", []):
                self.cmds.insert("", "end", values=(" · ".join(cmd["say"]), cmd["does"]))
            s = info.get("settings", {})
            self.model.set(s.get("model", ""))
            self.langs.set(",".join(s.get("languages") or []))
            self.level.set(s.get("min_level", -34))
            self.corr.set(bool(s.get("corrector")))
            self.corr_model.set(s.get("corrector_model", ""))
            self.pause.set(s.get("listen_pause", 0.6))
            self.chunk.set(s.get("listen_chunk", 6))
            self.rec_mode.set(dict(RECORD_MODES).get(s.get("record_mode"), RECORD_MODES[0][1]))
            self.loaded = True

    def on_autostart(self):
        try:
            autostart.set_enabled(self.auto.get())
            self.auto_hint.set(autostart.describe() if self.auto.get() else "Off.")
        except (OSError, subprocess.SubprocessError) as e:
            self.auto_hint.set(f"Couldn't change it: {e}")
            self.auto.set(autostart.enabled())

    def save(self):
        try:
            level = float(self.level.get())
        except (tk.TclError, ValueError):
            self.saved.set("The threshold must be a number")
            return
        mode = next((m for m, d in RECORD_MODES if d == self.rec_mode.get()), "toggle")
        r = self.client.call("POST", "/config", {
            "languages": [l.strip() for l in self.langs.get().split(",") if l.strip()], "min_level": level,
            "record_mode": mode, "listen_pause": float(self.pause.get()), "listen_chunk": float(self.chunk.get()),
            "corrector": bool(self.corr.get()), "corrector_model": self.corr_model.get().strip()})
        self.saved.set("Saved" if r.get("ok") else f"Not saved: {r.get('error')}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", help="voxbutton server address (default: from server.json / Tailscale)")
    args = ap.parse_args()
    if WIN:
        try:
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass
    try:
        token = (CONFIG_DIR / "token").read_text().strip()
    except OSError:
        sys.exit(f"No token in {CONFIG_DIR}: start the voxbutton server once first.")
    root = tk.Tk(className="voxbutton")
    root.title("VoxButton")
    Button(root, Client(args.server or default_server(), token))
    root.mainloop()


if __name__ == "__main__":
    main()
