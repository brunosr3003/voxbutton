#!/usr/bin/env python3
"""Shows a corrected version of what you just dictated, at the top of the
screen, until you close it (✕ or a click on it) or the next one replaces it.
It doesn't take focus when it appears, so typing carries on where it was.
The button starts it; one at a time.

Usage: voxbutton-correction.py ORIGINAL CORRECTED [--seconds N] [--ok]
       (--seconds 0 or omitted: no timer · --ok: nothing to correct, a green card)
"""

import argparse
import subprocess

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk  # noqa: E402

APP_ID = "voxbutton-note"
WIDTH = 720

CSS = """
window, window.background { background: transparent; }
.card { border-radius: 14px; padding: 14px 18px; }
/* red: it had to correct you · green: you got it right */
.card.wrong { background: #2b1214; border: 2px solid #e01b24; }
.card.ok { background: #10251a; border: 2px solid #2ec27e; }
.tag { font-size: 0.8em; font-weight: bold; }
.said-tag { color: #f66151; }
.said { color: #d8c4c4; }
.card.ok .said { color: #ffffff; font-size: 1.1em; }
.better { color: #ffffff; font-size: 1.15em; font-weight: 600; }
.better-tag { color: #57e389; }
.close { min-width: 22px; min-height: 22px; padding: 0; border-radius: 11px; background: rgba(255,255,255,0.08);
         color: #ffffff; border: none; box-shadow: none; }
.close:hover { background: rgba(255,255,255,0.22); }
"""


def add_window_rule() -> None:
    # No focus when it appears or on hover, so it can't swallow what you're typing;
    # it still takes clicks, for the close button.
    lua = ('hl.window_rule({ match = { class = "^(%s)$" }, float = true, pin = true, no_follow_mouse = true, '
           'no_initial_focus = true, decorate = false, border_size = 0, no_shadow = true, no_blur = true, '
           'move = { "(monitor_w-%d)*0.5", "70" } })' % (APP_ID, WIDTH))
    try:
        subprocess.run(["hyprctl", "eval", lua], capture_output=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pass


def close_and_refocus(app: Gtk.Application) -> None:
    """Closing it by a click gave it focus; hand focus back to the window you
    were in (the previous one in Hyprland's focus history) and quit."""
    try:
        import json

        clients = json.loads(subprocess.run(["hyprctl", "clients", "-j"], capture_output=True, text=True,
                                            timeout=3).stdout or "[]")
        prev = next((c for c in clients if c.get("focusHistoryID") == 1 and c.get("class") != APP_ID), None)
        if prev:
            subprocess.run(["hyprctl", "dispatch", 'hl.dsp.focus({ window = "address:%s" })' % prev["address"]],
                           capture_output=True, timeout=3)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    app.quit()


def label(text: str, *classes: str) -> Gtk.Label:
    lb = Gtk.Label(label=text, xalign=0, wrap=True, selectable=False)
    for c in classes:
        lb.add_css_class(c)
    return lb


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("original")
    ap.add_argument("corrected")
    ap.add_argument("--seconds", type=float, default=0, help="close after this long; 0 = only by hand")
    ap.add_argument("--ok", action="store_true", help="the sentence was already fine")
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
        card.add_css_class("ok" if args.ok else "wrong")

        def header(text: str, *classes: str) -> Gtk.Box:
            row = Gtk.Box(spacing=8)
            tag = label(text, *classes)
            tag.set_hexpand(True)
            row.append(tag)
            close = Gtk.Button(label="✕", valign=Gtk.Align.START, tooltip_text="Close")
            close.add_css_class("close")
            close.connect("clicked", lambda *_: close_and_refocus(a))
            row.append(close)
            return row

        if args.ok:
            card.append(header("✓ LOOKS GOOD", "tag", "better-tag"))
            card.append(label(args.original, "said"))
        else:
            card.append(header("✗ YOU SAID", "tag", "said-tag"))
            card.append(label(args.original, "said"))
            card.append(Gtk.Box(margin_top=4))
            card.append(label("✓ BETTER", "tag", "better-tag"))
            card.append(label(args.corrected, "better"))
        # A click anywhere on the card closes it too.
        click = Gtk.GestureClick()
        click.connect("released", lambda *_: close_and_refocus(a))
        card.add_controller(click)
        win.set_child(card)
        win.present()
        if args.seconds > 0:
            GLib.timeout_add(int(args.seconds * 1000), a.quit)

    app.connect("activate", activate)
    app.run([])


if __name__ == "__main__":
    main()
