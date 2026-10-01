#!/usr/bin/env python3
"""Shows a corrected version of what you just dictated, at the bottom of the
screen, for a few seconds. It never takes focus (it's only there to read), so
typing carries on where it was. The button starts it; one at a time.

Usage: voxbutton-correction.py ORIGINAL CORRECTED [--seconds 10]
"""

import argparse
import subprocess

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk  # noqa: E402

APP_ID = "voxbutton-correction"
WIDTH = 720

CSS = """
window, window.background { background: transparent; }
.card { background: #18181b; border-radius: 14px; padding: 14px 18px;
        border: 1px solid rgba(255, 255, 255, 0.08); }
.tag { font-size: 0.8em; font-weight: bold; opacity: 0.55; }
.said { color: #b8b8be; }
.better { color: #ffffff; font-size: 1.15em; font-weight: 600; }
.better-tag { color: #57e389; }
"""


def add_window_rule() -> None:
    # no_focus: never takes focus, so it can't swallow what you're typing.
    lua = ('hl.window_rule({ match = { class = "^(%s)$" }, float = true, pin = true, no_focus = true, '
           'no_initial_focus = true, decorate = false, border_size = 0, no_shadow = true, no_blur = true, '
           'move = { "(monitor_w-%d)*0.5", "monitor_h-60-window_h" } })' % (APP_ID, WIDTH))
    try:
        subprocess.run(["hyprctl", "eval", lua], capture_output=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pass


def label(text: str, *classes: str) -> Gtk.Label:
    lb = Gtk.Label(label=text, xalign=0, wrap=True, selectable=False)
    for c in classes:
        lb.add_css_class(c)
    return lb


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("original")
    ap.add_argument("corrected")
    ap.add_argument("--seconds", type=float, default=10)
    args = ap.parse_args()
    add_window_rule()
    GLib.set_prgname(APP_ID)
    app = Gtk.Application()

    def activate(a):
        win = Gtk.ApplicationWindow(application=a, title="voxbutton correction")
        win.set_decorated(False)
        win.set_default_size(WIDTH, -1)
        css = Gtk.CssProvider()
        css.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(win.get_display(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        card.add_css_class("card")
        card.append(label("YOU SAID", "tag"))
        card.append(label(args.original, "said"))
        sep = Gtk.Box(margin_top=4)
        card.append(sep)
        card.append(label("BETTER", "tag", "better-tag"))
        card.append(label(args.corrected, "better"))
        win.set_child(card)
        win.present()
        GLib.timeout_add(int(args.seconds * 1000), a.quit)

    app.connect("activate", activate)
    app.run([])


if __name__ == "__main__":
    main()
