#!/usr/bin/env python3
"""Floating microphone button for Hyprland.

Left-click to dictate, right-click for a voice command ("next tab",
"workspace 2", "send"...); click again (either button) to stop. The recording itself
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
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "voxbutton"

COLORS = {
    "offline": (0.35, 0.35, 0.38, 0.55),
    "idle": (0.13, 0.13, 0.15, 0.88),
    "recording": (0.90, 0.20, 0.20, 1.0),
    "command": (0.20, 0.45, 0.95, 1.0),
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

        click = Gtk.GestureClick()
        click.set_button(0)  # any button: left = dictation, right = command
        click.connect("pressed", self.on_click)
        self.area.add_controller(click)

        threading.Thread(target=self.poll_loop, daemon=True).start()
        GLib.timeout_add(50, self.tick)

    # --- network (background threads) ---

    def poll_loop(self) -> None:
        while True:
            s = self.client.call("GET", "/state")
            GLib.idle_add(self.apply_state, s)
            time.sleep(0.25)

    def on_click(self, gesture, *_):
        if self.state == "busy":
            return
        mode = "command" if gesture.get_current_button() == 3 else "chat"

        def go():
            self.focus.restore()
            r = self.client.call("POST", f"/record/toggle?mode={mode}")
            GLib.idle_add(self.apply_state, r)

        threading.Thread(target=go, daemon=True).start()

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
            if s.get("flash"):
                self.flash(s["flash"])
        self.area.queue_draw()
        return False

    def flash(self, kind: str) -> None:
        if kind != self.flash_kind or time.time() > self.flash_until:
            self.flash_kind = kind
            self.flash_until = time.time() + (2.0 if kind == "error" else 0.8)

    def tick(self) -> bool:
        if self.state == "recording" or time.time() < self.flash_until:
            self.area.queue_draw()
        return True

    def draw(self, _area, cr, w, h):
        look = self.flash_kind if time.time() < self.flash_until else self.state
        color = "command" if look == "recording" and self.mode == "command" else look
        r, g, b, a = COLORS.get(color, COLORS["idle"])
        cx, cy, rad = w / 2, h / 2, min(w, h) / 2 - 4
        if look == "recording":
            pulse = 0.5 + 0.5 * math.sin(time.time() * 5)
            cr.set_source_rgba(r, g, b, 0.30)
            cr.arc(cx, cy, rad + 1 + 2 * pulse, 0, 2 * math.pi)
            cr.fill()
        cr.set_source_rgba(r, g, b, a)
        cr.arc(cx, cy, rad - 2, 0, 2 * math.pi)
        cr.fill()

        cr.set_source_rgba(1, 1, 1, 0.95 if look != "offline" else 0.6)
        s = rad / 22
        if look == "busy":
            for i in (-1, 0, 1):
                cr.arc(cx + i * 7 * s, cy, 2.4 * s, 0, 2 * math.pi)
                cr.fill()
            return
        # Microphone: capsule, cradle, stem and base.
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
    ap.add_argument("--x", default="monitor_w-84", help="Hyprland expression for the x position")
    ap.add_argument("--y", default="monitor_h*0.45", help="Hyprland expression for the y position")
    args = ap.parse_args()

    token = (CONFIG_DIR / "token").read_text().strip()
    add_window_rule(args.x, args.y)
    GLib.set_prgname(APP_ID)  # becomes the Wayland app_id, i.e. Hyprland's class
    app = Gtk.Application()
    app.connect("activate", lambda a: Button(a, Client(args.server, token)).present())
    app.run([])


if __name__ == "__main__":
    main()
