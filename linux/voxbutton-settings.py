#!/usr/bin/env python3
"""VoxButton settings: connected devices, how to connect a new one, the voice
commands, and a few server settings. Opened from the gear on the button."""

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "desktop"))
import autostart  # noqa: E402

RECORD_MODES = [
    ("toggle", "Toggle: click to start, click to stop"),
    ("hold", "Hold: talk while holding the button"),
    ("always", "Always on: click once, it types at every pause"),
]

APP_ID = "voxbutton-settings"
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "voxbutton"
REPO = "https://github.com/brunosr3003/voxbutton"

CSS = """
.page { padding: 18px; }
.title { font-weight: bold; font-size: 1.15em; }
.dim { opacity: 0.65; }
.mono { font-family: monospace; }
.badge { border-radius: 9px; padding: 1px 8px; font-size: 0.85em; font-weight: bold; }
.badge.streaming { background: alpha(#3584e4, 0.25); color: #62a0ea; }
.badge.selected { background: alpha(#e01b24, 0.25); color: #f66151; }
.dot-on { color: #33d17a; }
.dot-off { color: #77767b; }
.cmd-say { font-family: monospace; font-weight: bold; }
.step { font-weight: bold; }
"""


def api(server: str, token: str, method: str, path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(server.rstrip("/") + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}"}
    except (OSError, ValueError) as e:
        return {"error": str(e)}


def label(text: str = "", *classes: str, xalign: float = 0, wrap: bool = False, selectable: bool = False) -> Gtk.Label:
    lb = Gtk.Label(label=text, xalign=xalign, wrap=wrap, selectable=selectable)
    for c in classes:
        lb.add_css_class(c)
    return lb


def box(vertical: bool = True, spacing: int = 8, *children: Gtk.Widget) -> Gtk.Box:
    b = Gtk.Box(orientation=Gtk.Orientation.VERTICAL if vertical else Gtk.Orientation.HORIZONTAL, spacing=spacing)
    for c in children:
        b.append(c)
    return b


class Settings(Gtk.ApplicationWindow):
    def __init__(self, app: Gtk.Application, server: str, token: str):
        super().__init__(application=app, title="VoxButton settings")
        self.server, self.token = server, token
        self.info: dict = {}
        self.set_default_size(620, 680)

        css = Gtk.CssProvider()
        css.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(self.get_display(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, vexpand=True)
        switcher = Gtk.StackSwitcher(stack=self.stack, halign=Gtk.Align.CENTER, margin_top=10, margin_bottom=4)
        self.stack.add_titled(self.scroll(self.devices_page()), "devices", "Devices")
        self.stack.add_titled(self.scroll(self.commands_page()), "commands", "Commands")
        self.stack.add_titled(self.scroll(self.general_page()), "general", "General")
        self.set_child(box(True, 0, switcher, self.stack))

        self.refresh()
        GLib.timeout_add(1500, self.refresh)

    @staticmethod
    def scroll(child: Gtk.Widget) -> Gtk.ScrolledWindow:
        sw = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        sw.set_child(child)
        return sw

    # --- pages ---

    def devices_page(self) -> Gtk.Widget:
        page = box(True, 12)
        page.add_css_class("page")
        self.stream_label = label("", "dim", wrap=True)
        self.device_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.device_list.add_css_class("boxed-list")
        self.device_list.set_placeholder(label("No devices yet. Connect one below.", "dim", xalign=0.5))
        page.append(label("Microphones", "title"))
        page.append(label("A click on the button records on the device that's receiving the Moonlight "
                          "stream. Without a stream, the Mac wins while Moonlight is in front on it, "
                          "then the iPhone.", "dim", wrap=True))
        page.append(self.stream_label)
        page.append(self.device_list)

        page.append(Gtk.Separator(margin_top=8))
        page.append(label("Connect a device", "title"))
        self.url_local = self.copy_row("Server (Tailscale)", "")
        self.url_public = self.copy_row("Server (public HTTPS)", "")
        self.token_row = self.copy_row("Token", "")
        for r in (self.url_local, self.url_public, self.token_row):
            page.append(r)

        page.append(label("Mac", "step"))
        page.append(label(
            f"1. Clone {REPO} and run  mac/build.sh --install\n"
            "2. Save the config below as ~/.config/voxbutton/config.json\n"
            "3. Allow the microphone when macOS asks. The app has no window; it waits in the background.",
            wrap=True, selectable=True))
        self.mac_config = self.copy_row("config.json", "", mono=True)
        page.append(self.mac_config)

        page.append(label("iPhone", "step"))
        page.append(label(
            "1. On a Mac with Xcode, run  ios/build.sh  with the phone plugged in (needs a development "
            "profile for the app; see the README).\n"
            "2. Open VoxButton on the phone, set Server to the public HTTPS address and paste the Token.\n"
            "3. Turn on “Lend microphone”. Keep the app open in the background; the orange dot means "
            "it's listening for the button.",
            wrap=True, selectable=True))
        return page

    def commands_page(self) -> Gtk.Widget:
        page = box(True, 12)
        page.add_css_class("page")
        page.append(label("Voice commands", "title"))
        page.append(label("Right-click the button (it turns blue), say the command in English, click again. "
                          "Left-click is plain dictation.", "dim", wrap=True))
        self.cmd_grid = Gtk.Grid(column_spacing=18, row_spacing=10)
        page.append(self.cmd_grid)
        return page

    def general_page(self) -> Gtk.Widget:
        page = box(True, 12)
        page.add_css_class("page")
        page.append(label("Recording", "title"))
        rec = Gtk.Grid(column_spacing=14, row_spacing=10)
        self.record_mode = Gtk.DropDown.new_from_strings([d for _, d in RECORD_MODES])
        self.record_mode.set_hexpand(True)
        rec.attach(label("Mode"), 0, 0, 1, 1)
        rec.attach(self.record_mode, 1, 0, 1, 1)
        rec.attach(label("Always on: start a sentence with “command” to run it as one, e.g. "
                         "“command next tab”. In hold mode, drag the button by its gear.", "dim", wrap=True), 1, 1, 1, 1)
        self.autostart = Gtk.Switch(halign=Gtk.Align.START, active=autostart.enabled())
        self.autostart.connect("state-set", self.on_autostart)
        rec.attach(label("Start with the computer"), 0, 2, 1, 1)
        rec.attach(self.autostart, 1, 2, 1, 1)
        self.autostart_hint = label(autostart.describe(), "dim", wrap=True)
        rec.attach(self.autostart_hint, 1, 3, 1, 1)
        page.append(rec)

        page.append(Gtk.Separator(margin_top=8))
        page.append(label("Transcription", "title"))
        grid = Gtk.Grid(column_spacing=14, row_spacing=10)
        self.model_label = label("", "mono")
        self.langs = Gtk.Entry(placeholder_text="any language", hexpand=True)
        self.min_level = Gtk.SpinButton.new_with_range(-70, -10, 1)
        rows = [
            ("Whisper model", self.model_label, "Set with --model when starting the server."),
            ("Dictation languages", self.langs, "Comma-separated, e.g. en,pt. Commands are always English."),
            ("Silence threshold (dBFS)", self.min_level,
             "Clips whose loudest moment is quieter are skipped, so Whisper doesn't invent text. "
             "Lower it if quiet speech gets ignored."),
        ]
        for i, (name, widget, hint) in enumerate(rows):
            grid.attach(label(name), 0, i * 2, 1, 1)
            grid.attach(widget, 1, i * 2, 1, 1)
            grid.attach(label(hint, "dim", wrap=True), 1, i * 2 + 1, 1, 1)
        page.append(grid)
        self.save_status = label("", "dim")
        save = Gtk.Button(label="Save", halign=Gtk.Align.START)
        save.add_css_class("suggested-action")
        save.connect("clicked", self.save)
        page.append(box(False, 12, save, self.save_status))

        page.append(Gtk.Separator(margin_top=8))
        page.append(label("Button", "title"))
        page.append(label("Left button: dictate · Right button: voice command · Gear: this window · "
                          "hold and move: drag it.\n"
                          "Colors: dark ready, red dictating, blue command, teal listening, orange "
                          "transcribing, green done, purple error, faded no microphone.", "dim", wrap=True))
        self.general_loaded = False
        return page

    def copy_row(self, name: str, value: str, mono: bool = False) -> Gtk.Box:
        val = label(value, "mono" if mono else "dim", wrap=True, selectable=True)
        val.set_hexpand(True)
        btn = Gtk.Button(icon_name="edit-copy-symbolic", tooltip_text="Copy", valign=Gtk.Align.CENTER)
        btn.connect("clicked", lambda *_: self.get_clipboard().set(val.get_text()))
        row = box(False, 10, label(name), val, btn)
        row.value = val
        return row

    # --- data ---

    def refresh(self) -> bool:
        def go():
            info = api(self.server, self.token, "GET", "/info")
            GLib.idle_add(self.apply, info)

        threading.Thread(target=go, daemon=True).start()
        return True

    def apply(self, info: dict) -> bool:
        if "error" in info:
            self.stream_label.set_text(f"Can't reach the server at {self.server}: {info['error']}")
            return False
        self.info = info
        st = info.get("stream") or {}
        devs = info.get("devices", [])
        who = next((d["name"] for d in devs if d["streaming"]), None)
        if st.get("peer"):
            self.stream_label.set_text(f"Streaming to {who.capitalize() if who else 'an unknown device'} ({st['peer']}, {st['mbit']} Mbit/s)")
        else:
            self.stream_label.set_text("No Moonlight stream right now")

        while (row := self.device_list.get_row_at_index(0)) is not None:
            self.device_list.remove(row)
        for d in devs:
            dot = label("●", "dot-on" if d["online"] else "dot-off")
            name = label(d["name"].capitalize(), "title")
            head = box(False, 8, dot, name)
            if d["streaming"]:
                head.append(label("streaming", "badge", "streaming"))
            if d["selected"]:
                head.append(label("uses this mic", "badge", "selected"))
            seen = "online" if d["online"] else f"offline, last seen {d['seen_ago']}s ago"
            sub = label(f"{seen} · {', '.join(d['ips']) or 'no address'} · priority {d['priority']}", "dim")
            row = box(True, 2, head, sub)
            row.set_margin_top(8)
            row.set_margin_bottom(8)
            row.set_margin_start(10)
            self.device_list.append(row)

        c = info.get("connect", {})
        self.url_local.value.set_text(c.get("local", ""))
        self.url_public.value.set_text(c.get("public") or "not set (start the server with --public-url)")
        self.token_row.value.set_text(c.get("token", ""))
        self.mac_config.value.set_text(json.dumps({"server": c.get("local", ""), "token": c.get("token", "")}))

        if not self.cmd_grid.get_first_child():
            for i, cmd in enumerate(info.get("commands", [])):
                self.cmd_grid.attach(label(" · ".join(cmd["say"]), "cmd-say"), 0, i, 1, 1)
                self.cmd_grid.attach(label(cmd["does"], "dim", wrap=True), 1, i, 1, 1)

        if not self.general_loaded and "settings" in info:
            s = info["settings"]
            self.model_label.set_text(s.get("model", ""))
            self.langs.set_text(",".join(s.get("languages") or []))
            self.min_level.set_value(s.get("min_level", -34))
            modes = [m for m, _ in RECORD_MODES]
            self.record_mode.set_selected(modes.index(s.get("record_mode", "toggle"))
                                          if s.get("record_mode") in modes else 0)
            self.general_loaded = True
        return False

    def on_autostart(self, switch, on: bool) -> bool:
        try:
            autostart.set_enabled(on)
            self.autostart_hint.set_text(autostart.describe() if on else "Off.")
        except (OSError, subprocess.SubprocessError) as e:
            self.autostart_hint.set_text(f"Couldn't change it: {e}")
            switch.set_state(autostart.enabled())
            return True
        return False

    def save(self, *_):
        body = {"languages": [l for l in self.langs.get_text().split(",") if l.strip()],
                "min_level": self.min_level.get_value(),
                "record_mode": RECORD_MODES[self.record_mode.get_selected()][0]}
        r = api(self.server, self.token, "POST", "/config", body)
        self.save_status.set_text("Saved" if r.get("ok") else f"Not saved: {r.get('error')}")


def add_window_rule() -> None:
    lua = ('hl.window_rule({ match = { class = "^(%s)$" }, float = true, center = true, '
           'size = { 620, 680 } })' % APP_ID)
    subprocess.run(["hyprctl", "eval", lua], capture_output=True, timeout=5)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--server", required=True)
    ap.add_argument("--page", choices=["devices", "commands", "general"], default="devices")
    args = ap.parse_args()
    token = (CONFIG_DIR / "token").read_text().strip()
    try:
        add_window_rule()
    except (OSError, subprocess.TimeoutExpired):
        pass
    GLib.set_prgname(APP_ID)
    app = Gtk.Application()
    def activate(a):
        w = Settings(a, args.server, token)
        w.stack.set_visible_child_name(args.page)
        w.present()

    app.connect("activate", activate)
    app.run([])


if __name__ == "__main__":
    main()
