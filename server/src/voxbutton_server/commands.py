"""Voice commands: turn a short English utterance ("next tab", "workspace 3",
"send") into keystrokes or Hyprland actions.

Matching is plain regex over normalized text (lowercase, no punctuation,
number words as digits).
Key combos depend on the focused app, e.g. copying is ctrl+shift+c in a
terminal and ctrl+c elsewhere."""

import json
import re
import subprocess
import unicodedata
from dataclasses import dataclass
from typing import Callable

TERMINALS = {"kitty", "alacritty", "foot", "org.wezfurlong.wezterm", "com.mitchellh.ghostty", "konsole"}
BROWSERS = {"firefox", "zen", "chromium", "google-chrome", "brave-browser", "vivaldi-stable"}

NUMBERS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
}

# Biases Whisper towards the command vocabulary; short clips are otherwise a guessing game.
PROMPT = (
    "next tab, previous tab, new tab, close tab, tab 2, workspace 3, window left, focus right, enter, send, "
    "escape, cancel, delete that, clear line, backspace, scroll up, scroll down, copy, paste, select all, "
    "go back, reload, option 1, type hello."
)


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    words = [str(NUMBERS[w]) if w in NUMBERS else w for w in text.split()]
    return " ".join(words)


def active_class() -> str:
    try:
        out = subprocess.run(["hyprctl", "activewindow", "-j"], capture_output=True, text=True, timeout=3)
        return (json.loads(out.stdout or "{}").get("class") or "").lower()
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return ""


def key(combo: str, times: int = 1) -> None:
    """Press a combo like "ctrl+shift+Tab" with wtype."""
    *mods, k = combo.split("+")
    args = []
    for _ in range(times):
        args += [a for m in mods for a in ("-M", m)] + ["-k", k] + [a for m in reversed(mods) for a in ("-m", m)]
    subprocess.run(["wtype", *args], check=True, timeout=10)


def type_text(text: str) -> None:
    subprocess.run(["wtype", "--", text], check=True, timeout=30)


def hypr(lua: str) -> None:
    subprocess.run(["hyprctl", "dispatch", lua], check=True, capture_output=True, timeout=5)


@dataclass
class Ctx:
    app: str  # focused window's class, lowercased
    times: int  # from "... 3 times"
    last_typed: int  # characters of the last dictation, for "delete that"

    @property
    def terminal(self) -> bool:
        return self.app in TERMINALS

    @property
    def browser(self) -> bool:
        return self.app in BROWSERS


Action = Callable[[re.Match, Ctx], None]
COMMANDS: list[tuple[str, re.Pattern, Action]] = []
CATALOG: list[dict] = []  # what the settings window shows


def command(name: str, *patterns: str, say: list[str], does: str):
    def reg(fn: Action) -> Action:
        COMMANDS.append((name, re.compile(r"^(?:" + "|".join(patterns) + r")$"), fn))
        CATALOG.append({"name": name, "say": say, "does": does})
        return fn

    return reg


TAB = r"(?:tab|tabs)"
DIR = {"left": "l", "right": "r", "up": "u", "down": "d"}
SIDE = r"(left|right|up|down)"


@command("next tab", rf"next {TAB}", rf"{TAB} right", say=['next tab'], does='Next tab (ctrl+Tab)')
def _(m, c):
    key("ctrl+Tab", c.times)


@command("previous tab", rf"(?:previous|prev|last) {TAB}", rf"{TAB} left", say=['previous tab', 'last tab'], does='Previous tab (ctrl+shift+Tab)')
def _(m, c):
    key("ctrl+shift+Tab", c.times)


@command("go to tab", rf"(?:go to |switch to )?{TAB} (?:number )?(\d)", say=['tab 3', 'go to tab 3'], does='Jump to tab N (alt+N, browsers)')
def _(m, c):
    if c.terminal:
        raise LookupError("tab by number isn't available in the terminal")
    key(f"alt+{m.group(1)}")


@command("new tab", rf"(?:new|open(?: a)?(?: new)?) {TAB}", say=['new tab'], does='Open a tab (ctrl+shift+t in terminals, ctrl+t elsewhere)')
def _(m, c):
    key("ctrl+shift+t" if c.terminal else "ctrl+t")


@command("close tab", rf"close(?: the| this)? {TAB}", say=['close tab'], does='Close the tab (ctrl+shift+q in terminals, ctrl+w elsewhere)')
def _(m, c):
    key("ctrl+shift+q" if c.terminal else "ctrl+w")


@command("workspace", r"(?:go to |switch to )?(?:workspace|desktop|screen) (\d+)", say=['workspace 2', 'go to workspace 2'], does='Switch Hyprland workspace')
def _(m, c):
    hypr(f'hl.dsp.focus({{ workspace = "{m.group(1)}" }})')


@command("focus window", rf"(?:focus )?window(?: on the| to the)? {SIDE}", rf"focus(?: the)? {SIDE}(?: window)?", say=['window left', 'focus right'], does='Move focus to the window on that side')
def _(m, c):
    d = next(g for g in m.groups() if g)
    hypr(f'hl.dsp.focus({{ direction = "{DIR[d]}" }})')


@command("enter", r"enter", r"send(?: it)?", r"submit", r"confirm", r"ok", r"okay", r"return", say=['send', 'enter', 'submit'], does='Press Enter')
def _(m, c):
    key("Return", c.times)


@command("escape", r"escape", r"esc", r"cancel", r"stop", r"interrupt", say=['stop', 'cancel', 'escape'], does='Press Escape (interrupts Claude Code)')
def _(m, c):
    key("Escape", c.times)


@command("delete that", r"(?:delete|undo|erase|scratch) (?:that|this|it)", say=['delete that', 'undo that'], does='Erase the last dictation')
def _(m, c):
    if not c.last_typed:
        raise LookupError("nothing dictated to delete")
    key("BackSpace", c.last_typed)


@command("clear line", r"clear(?: the)? line", say=['clear line'], does='Clear the current line')
def _(m, c):
    if c.terminal:
        key("ctrl+u")
    else:
        key("shift+Home")
        key("BackSpace")


@command("backspace", r"backspace", r"back space", say=['backspace'], does='Press Backspace')
def _(m, c):
    key("BackSpace", c.times)


@command("scroll up", r"scroll up", r"page up", say=['scroll up', 'page up'], does='Page up (shift+PageUp in terminals)')
def _(m, c):
    key("shift+Page_Up" if c.terminal else "Page_Up", c.times)


@command("scroll down", r"scroll down", r"page down", say=['scroll down', 'page down'], does='Page down (shift+PageDown in terminals)')
def _(m, c):
    key("shift+Page_Down" if c.terminal else "Page_Down", c.times)


@command("copy", r"copy(?: that| this)?", say=['copy'], does='Copy the selection')
def _(m, c):
    key("ctrl+shift+c" if c.terminal else "ctrl+c")


@command("paste", r"paste(?: it)?", say=['paste'], does='Paste')
def _(m, c):
    key("ctrl+shift+v" if c.terminal else "ctrl+v")


@command("select all", r"select all", say=['select all'], does='Select all')
def _(m, c):
    key("ctrl+shift+a" if c.terminal else "ctrl+a")


@command("go back", r"go back", r"back", say=['go back'], does='Browser back (alt+Left)')
def _(m, c):
    key("alt+Left", c.times)


@command("go forward", r"go forward", r"forward", say=['go forward'], does='Browser forward (alt+Right)')
def _(m, c):
    key("alt+Right", c.times)


@command("reload", r"reload(?: the)?(?: page)?", r"refresh(?: the)?(?: page)?", say=['reload', 'refresh'], does='Reload the page (F5)')
def _(m, c):
    key("F5")


@command("arrow", rf"(?:arrow |press )?{SIDE}(?: arrow)?", say=['up', 'down arrow', 'left'], does='Arrow keys')
def _(m, c):
    key({"u": "Up", "d": "Down", "l": "Left", "r": "Right"}[DIR[m.group(1)]], c.times)


@command("option", r"(?:option|number|choice|choose|pick) (\d)", say=['option 2', 'pick 1'], does='Type the number (Claude Code menus)')
def _(m, c):
    # Claude Code and similar menus pick an entry by its number.
    type_text(m.group(1))


@command("tab key", r"tab key", r"press tab", say=['tab key', 'press tab'], does='Press Tab')
def _(m, c):
    key("Tab", c.times)


CATALOG.append({"name": "type", "say": ["type git status"], "does": "Type the rest verbatim"})
CATALOG.append({"name": "repeat", "say": ["next tab 3 times"], "does": "Add “N times” to repeat a command"})

REPEAT = re.compile(r"^(.*?) (\d+) (?:times|x)$")
TYPE = re.compile(r"^(?:type|write)\s+(.+)$", re.I | re.S)


def run(text: str, last_typed: int = 0, dry: bool = False) -> str:
    """Runs the command in `text`, returns its name. Raises LookupError when
    nothing matches or the command doesn't apply here. `dry` only matches."""
    raw = text.strip()
    # "type ..." keeps the original casing and accents.
    stripped = re.sub(r"^[^\w]+", "", raw)
    if m := TYPE.match(stripped):
        if dry:
            return "type"
        type_text(m.group(1).strip().rstrip(".") + " ")
        return "type"

    t = normalize(raw)
    times = 1
    if m := REPEAT.match(t):
        t, times = m.group(1), min(int(m.group(2)), 20)
    ctx = Ctx(app=active_class(), times=times, last_typed=last_typed)
    for name, pat, fn in COMMANDS:
        if m := pat.match(t):
            if not dry:
                fn(m, ctx)
            return name
    raise LookupError(f"unknown command: {t!r}")
