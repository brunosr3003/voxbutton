#!/usr/bin/env python3
"""Floating microphone button for Hyprland.

Left button dictates, right button records a voice command ("next tab",
"workspace 2", "send"...). How depends on the record mode (settings):
toggle = click to start, click to stop; hold = talk while holding;
always = click once and it keeps listening, typing at every pause.
The gear opens the settings; hold the gear (or the button, outside hold
mode) and move to drag it somewhere else. The recording itself
happens wherever the mic agent runs (the Mac app), the text is typed here by
the voxbutton server. Clicking it hands keyboard focus straight back to the
window you were typing in, so that's where the text lands.

Usage: voxbutton-button.py [--server http://host:8765] [--x EXPR] [--y EXPR]
"""

import argparse
import json
import math
import os
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk  # noqa: E402

APP_ID = "voxbutton-button"
SIZE = 64
GEAR_R = 9  # the settings badge in the bottom-right corner
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "voxbutton"
POS_FILE = CONFIG_DIR / "button-hyprland.json"
HOLD_MS = 350  # press this long to drag instead of click

COLORS = {
    "offline": (0.35, 0.35, 0.38, 0.55),
    "idle": (0.13, 0.13, 0.15, 0.88),
    "recording": (0.90, 0.20, 0.20, 1.0),
    "command": (0.20, 0.45, 0.95, 1.0),
    "listening": (0.10, 0.65, 0.60, 1.0),
    "busy": (0.95, 0.60, 0.10, 1.0),
    "done": (0.20, 0.75, 0.35, 1.0),
    "error": (0.60, 0.30, 0.85, 1.0),
}


def default_server() -> str:
    try:
        ip = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=5).stdout.split()
    except (OSError, subprocess.TimeoutExpired):
        ip = []
    return f"http://{ip[0] if ip else '127.0.0.1'}:8765"


def hypr(*args: str) -> str:
    try:
        return subprocess.run(["hyprctl", *args], capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def add_window_rule(x: str, y: str) -> None:
    """Float and pin to every workspace, without grabbing focus on open or on
    hover. Added at runtime so the Hyprland config doesn't have to be touched
    (saving it reloads it). no_focus is not an option: such windows get no
    clicks at all."""
    lua = (
        'hl.window_rule({ match = { class = "^(%s)$" }, float = true, pin = true, '
        "no_initial_focus = true, no_follow_mouse = true, decorate = false, border_size = 0, no_shadow = true, no_blur = true, no_anim = true, "
        'size = { %d, %d }, move = { "%s", "%s" } })' % (APP_ID, SIZE, SIZE, x, y)
    )
    hypr("eval", lua)


class FocusKeeper:
    """Remembers the window you were typing in: the last one that held focus
    for a moment (so windows the pointer merely crosses don't count)."""

    DWELL = 0.6  # s

    def __init__(self):
        self.target: str | None = None
        self._seen: tuple[str, float] | None = None
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self) -> None:
        while True:
            try:
                w = json.loads(hypr("activewindow", "-j") or "{}")
            except ValueError:
                w = {}
            addr = w.get("address")
            if addr and w.get("class") != APP_ID:
                if not self._seen or self._seen[0] != addr:
                    self._seen = (addr, time.time())
                elif time.time() - self._seen[1] >= self.DWELL:
                    self.target = addr
            time.sleep(0.2)

    def restore(self) -> None:
        if self.target:
            hypr("dispatch", 'hl.dsp.focus({ window = "address:%s" })' % self.target)


class Client:
    def __init__(self, server: str, token: str):
        self.server = server.rstrip("/")
        self.token = token

    def call(self, method: str, path: str, timeout: float = 5) -> dict:
        req = urllib.request.Request(self.server + path, method=method, data=b"" if method == "POST" else None)
        req.add_header("Authorization", f"Bearer {self.token}")
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


class Button(Gtk.ApplicationWindow):
    def __init__(self, app: Gtk.Application, client: Client):
        super().__init__(application=app, title="voxbutton")
        self.client = client
        self.focus = FocusKeeper()
        self.state = "offline"
        self.mode = "chat"
        self.record_mode = "toggle"
        self.correction_seen: int | None = None  # None until the first poll
        self.correction_proc: subprocess.Popen | None = None
        self.flash_until = 0.0
        self.flash_kind = ""
        self.set_decorated(False)
        self.set_default_size(SIZE, SIZE)
        self.set_resizable(False)

        css = Gtk.CssProvider()
        css.load_from_string("window, window.background { background: transparent; }")
        Gtk.StyleContext.add_provider_for_display(self.get_display(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        self.area = Gtk.DrawingArea()
        self.area.set_content_width(SIZE)
        self.area.set_content_height(SIZE)
        self.area.set_draw_func(self.draw)
        self.set_child(self.area)

        # Left button as a drag gesture: unlike a click gesture it still reports
        # the release after the pointer has moved, which a hold-and-drag needs.
        self.dragging = False
        self.hold_id = 0
        left = Gtk.GestureDrag(button=1)
        left.connect("drag-begin", self.on_left_down)
        left.connect("drag-end", self.on_left_up)
        self.area.add_controller(left)
        right = Gtk.GestureClick(button=3)
        right.connect("pressed", lambda g, n, x, y: self.on_right(True, x, y))
        right.connect("released", lambda g, n, x, y: self.on_right(False, x, y))
        self.area.add_controller(right)

        threading.Thread(target=self.poll_loop, daemon=True).start()
        GLib.timeout_add(50, self.tick)

    # --- network (background threads) ---

    def poll_loop(self) -> None:
        while True:
            s = self.client.call("GET", "/state")
            GLib.idle_add(self.apply_state, s)
            time.sleep(0.25)

    def on_gear(self, x: float, y: float) -> bool:
        gx, gy = SIZE - GEAR_R - 1, SIZE - GEAR_R - 1
        return (x - gx) ** 2 + (y - gy) ** 2 <= (GEAR_R + 3) ** 2

    def open_settings(self) -> None:
        """Brings the settings window up, starting it if it isn't running."""
        try:
            clients = json.loads(hypr("clients", "-j") or "[]")
        except ValueError:
            clients = []
        for c in clients:
            if c.get("class") == "voxbutton-settings":
                hypr("dispatch", 'hl.dsp.focus({ window = "address:%s" })' % c["address"])
                return
        script = Path(__file__).with_name("voxbutton-settings.py")
        subprocess.Popen([sys.executable, str(script), "--server", self.client.server],
                         start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # --- moving (click and hold) ---

    def on_left_down(self, gesture, x, y):
        self.down_at = (x, y)
        self.talking = False
        if self.record_mode == "hold" and not self.on_gear(x, y):
            # Push-to-talk: the hold is the recording, so there's no dragging here
            # (the gear still drags).
            self.talking = True
            self.send("/record/start?mode=chat")
            return
        self.hold_id = GLib.timeout_add(HOLD_MS, self.start_drag, x, y)

    def on_left_up(self, gesture, dx, dy):
        if self.hold_id:
            GLib.source_remove(self.hold_id)
            self.hold_id = 0
        if self.dragging:
            self.dragging = False  # the drag thread saves and gives focus back
            return
        if self.talking:
            self.talking = False
            self.send("/record/stop")
            return
        self.on_click(1, *self.down_at)

    def on_right(self, pressed: bool, x: float, y: float):
        if self.record_mode == "hold":
            self.send("/record/start?mode=command" if pressed else "/record/stop")
        elif not pressed:
            self.on_click(3, x, y)

    def send(self, path: str) -> None:
        def go():
            self.focus.restore()
            r = self.client.call("POST", path)
            GLib.idle_add(self.apply_state, r)

        threading.Thread(target=go, daemon=True).start()

    def start_drag(self, ox: float, oy: float) -> bool:
        self.hold_id = 0
        self.dragging = True
        self.area.queue_draw()
        threading.Thread(target=self.drag_loop, args=(ox, oy), daemon=True).start()
        return False

    def drag_loop(self, ox: float, oy: float) -> None:
        """Follows the cursor until release. Hyprland moves the *active* window,
        which is the button while you hold it; checked first, so nothing else
        ever gets moved."""
        try:
            active = json.loads(hypr("activewindow", "-j") or "{}")
        except ValueError:
            active = {}
        if active.get("class") != APP_ID:
            self.dragging = False
            GLib.idle_add(self.area.queue_draw)
            return
        last, started = None, time.time()
        while self.dragging and time.time() - started < 60:
            try:
                c = json.loads(hypr("cursorpos", "-j") or "{}")
                pos = (int(c["x"] - ox), int(c["y"] - oy))
            except (ValueError, KeyError, TypeError):
                pos = last
            if pos and pos != last:
                hypr("dispatch", "hl.dsp.window.move({ x = %d, y = %d })" % pos)
                last = pos
            time.sleep(0.016)
        self.dragging = False
        if last:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            POS_FILE.write_text(json.dumps({"x": last[0], "y": last[1]}) + "\n")
        self.focus.restore()
        GLib.idle_add(self.area.queue_draw)

    def on_click(self, button: int, x: float, y: float):
        if self.on_gear(x, y) and self.state not in ("recording", "listening"):
            threading.Thread(target=self.open_settings, daemon=True).start()
            return
        if self.state == "busy":
            return
        mode = "command" if button == 3 else "chat"
        self.send(f"/record/toggle?mode={mode}")

    # --- UI (main thread) ---

    def apply_state(self, s: dict) -> bool:
        if "error" in s:
            print("voxbutton:", s["error"], file=sys.stderr)
            self.flash("error")
            if "state" not in s:
                self.state = "offline" if "agent" not in s else self.state
        else:
            self.state = s["state"] if s.get("agent") or s["state"] != "idle" else "offline"
            self.mode = s.get("mode", "chat")
            self.record_mode = s.get("record_mode", self.record_mode)
            self.show_correction(s.get("correction") or {})
            if s.get("flash"):
                self.flash(s["flash"])
        self.area.queue_draw()
        return False

    def show_correction(self, c: dict) -> None:
        cid = c.get("id", 0)
        if self.correction_seen is None:  # don't replay the last one on startup
            self.correction_seen = cid
            return
        if cid == self.correction_seen or not c.get("corrected"):
            return
        self.correction_seen = cid
        if self.correction_proc and self.correction_proc.poll() is None:
            self.correction_proc.terminate()
        script = Path(__file__).with_name("voxbutton-correction.py")
        self.correction_proc = subprocess.Popen([sys.executable, str(script), c["original"], c["corrected"]],
                                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def flash(self, kind: str) -> None:
        if kind != self.flash_kind or time.time() > self.flash_until:
            self.flash_kind = kind
            self.flash_until = time.time() + (2.0 if kind == "error" else 0.8)

    def tick(self) -> bool:
        if self.state in ("recording", "listening") or time.time() < self.flash_until:
            self.area.queue_draw()
        return True

    def draw(self, _area, cr, w, h):
        look = self.flash_kind if time.time() < self.flash_until else self.state
        color = "command" if look in ("recording", "listening") and self.mode == "command" else look
        r, g, b, a = COLORS.get(color, COLORS["idle"])
        cx, cy, rad = w / 2, h / 2, min(w, h) / 2 - 4
        if look in ("recording", "listening"):
            pulse = 0.5 + 0.5 * math.sin(time.time() * 5)
            cr.set_source_rgba(r, g, b, 0.30)
            cr.arc(cx, cy, rad + 1 + 2 * pulse, 0, 2 * math.pi)
            cr.fill()
        cr.set_source_rgba(r, g, b, a)
        cr.arc(cx, cy, rad - 2, 0, 2 * math.pi)
        cr.fill()
        if self.dragging:
            cr.set_source_rgba(1, 1, 1, 0.8)
            cr.set_line_width(2)
            cr.arc(cx, cy, rad, 0, 2 * math.pi)
            cr.stroke()

        cr.set_source_rgba(1, 1, 1, 0.95 if look != "offline" else 0.6)
        s = rad / 22
        if look == "busy":
            for i in (-1, 0, 1):
                cr.arc(cx + i * 7 * s, cy, 2.4 * s, 0, 2 * math.pi)
                cr.fill()
        else:
            self.draw_mic(cr, cx, cy, s)
        if look not in ("recording", "listening"):
            self.draw_gear(cr, w - GEAR_R - 1, h - GEAR_R - 1)

    @staticmethod
    def draw_gear(cr, gx: float, gy: float) -> None:
        cr.set_source_rgba(0.22, 0.22, 0.25, 0.97)
        cr.arc(gx, gy, GEAR_R, 0, 2 * math.pi)
        cr.fill()
        cr.set_source_rgba(1, 1, 1, 0.9)
        teeth, outer, inner = 8, GEAR_R * 0.72, GEAR_R * 0.52
        for i in range(teeth * 2):
            a = i * math.pi / teeth
            r = outer if i % 2 == 0 else inner
            (cr.move_to if i == 0 else cr.line_to)(gx + r * math.cos(a), gy + r * math.sin(a))
        cr.close_path()
        cr.fill()
        cr.set_source_rgba(0.22, 0.22, 0.25, 1)
        cr.arc(gx, gy, GEAR_R * 0.24, 0, 2 * math.pi)
        cr.fill()

    @staticmethod
    def draw_mic(cr, cx: float, cy: float, s: float) -> None:
        # Capsule, cradle, stem and base.
        cw, ch = 8 * s, 14 * s
        top = cy - 11 * s
        cr.arc(cx, top + cw / 2, cw / 2, math.pi, 0)
        cr.arc(cx, top + ch - cw / 2, cw / 2, 0, math.pi)
        cr.close_path()
        cr.fill()
        cr.set_line_width(2.2 * s)
        cr.arc(cx, top + ch - cw / 2, 7 * s, 0, math.pi)
        cr.stroke()
        cr.move_to(cx, top + ch - cw / 2 + 7 * s)
        cr.line_to(cx, top + ch + 6 * s)
        cr.move_to(cx - 5 * s, top + ch + 6 * s)
        cr.line_to(cx + 5 * s, top + ch + 6 * s)
        cr.stroke()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", default=os.environ.get("VOXBUTTON_SERVER") or default_server())
    ap.add_argument("--x", help="Hyprland expression for the x position (default: where you left it)")
    ap.add_argument("--y", help="Hyprland expression for the y position")
    args = ap.parse_args()

    token = (CONFIG_DIR / "token").read_text().strip()
    x, y = "monitor_w-84", "monitor_h*0.45"
    try:
        saved = json.loads(POS_FILE.read_text())
        x, y = str(int(saved["x"])), str(int(saved["y"]))
    except (OSError, ValueError, KeyError, TypeError):
        pass
    add_window_rule(args.x or x, args.y or y)
    GLib.set_prgname(APP_ID)  # becomes the Wayland app_id, i.e. Hyprland's class
    app = Gtk.Application()
    app.connect("activate", lambda a: Button(a, Client(args.server, token)).present())
    app.run([])


if __name__ == "__main__":
    main()
