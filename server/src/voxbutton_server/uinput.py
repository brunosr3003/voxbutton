"""A virtual keyboard through /dev/uinput, for Wayland desktops whose
compositor doesn't take wtype's virtual-keyboard protocol (GNOME, KDE).
Works under any compositor, with no packages: it's a kernel input device.

It types the US layout: characters map to the keys that produce them there.
Letters, digits and space are the same on most layouts; punctuation can
differ on non-US ones. Characters with no key (é, “) are simplified first.

Needs write access to /dev/uinput: many distros give it to the logged-in user;
otherwise add a udev rule (KERNEL=="uinput", TAG+="uaccess") or join the
"input" group."""

import fcntl
import os
import struct
import threading
import time
import unicodedata

UI_SET_EVBIT = 0x40045564
UI_SET_KEYBIT = 0x40045565
UI_DEV_SETUP = 0x405C5503
UI_DEV_CREATE = 0x5501
EV_SYN, EV_KEY, SYN_REPORT = 0x00, 0x01, 0x00

KEYS = {
    # named keys (X keysym-style, as used by the commands)
    "Escape": 1, "BackSpace": 14, "Tab": 15, "Return": 28, "space": 57,
    "Home": 102, "Up": 103, "Page_Up": 104, "Left": 105, "Right": 106, "End": 107, "Down": 108,
    "Page_Down": 109, "Insert": 110, "Delete": 111,
    **{f"F{i}": 58 + i for i in range(1, 11)}, "F11": 87, "F12": 88,
    # modifiers
    "ctrl": 29, "shift": 42, "alt": 56, "super": 125, "logo": 125,
    # letters and digits
    **{c: k for c, k in zip("qwertyuiop", range(16, 26))},
    **{c: k for c, k in zip("asdfghjkl", range(30, 39))},
    **{c: k for c, k in zip("zxcvbnm", range(44, 51))},
    **{d: k for d, k in zip("1234567890", range(2, 12))},
}

# Character → (key, shift) on a US keyboard.
CHARS: dict[str, tuple[int, bool]] = {" ": (57, False), "\n": (28, False), "\t": (15, False)}
for c in "abcdefghijklmnopqrstuvwxyz":
    CHARS[c] = (KEYS[c], False)
    CHARS[c.upper()] = (KEYS[c], True)
for d in "1234567890":
    CHARS[d] = (KEYS[d], False)
for plain, shifted, key in (("-", "_", 12), ("=", "+", 13), ("[", "{", 26), ("]", "}", 27), (";", ":", 39),
                            ("'", '"', 40), ("`", "~", 41), ("\\", "|", 43), (",", "<", 51), (".", ">", 52),
                            ("/", "?", 53)):
    CHARS[plain] = (key, False)
    CHARS[shifted] = (key, True)
for d, s in zip("1234567890", "!@#$%^&*()"):
    CHARS[s] = (KEYS[d], True)

FOLD = {"“": '"', "”": '"', "„": '"', "‘": "'", "’": "'", "–": "-", "—": "-", "…": "...", " ": " "}


def simplify(text: str) -> str:
    """Brings text down to what a US keyboard can type: curly quotes and dashes
    become plain ones, accents come off (é → e); anything else is dropped."""
    text = "".join(FOLD.get(c, c) for c in text)
    text = unicodedata.normalize("NFKD", text)
    return "".join(c for c in text if c in CHARS)


class Keyboard:
    _instance = None
    _lock = threading.Lock()

    @classmethod
    def get(cls) -> "Keyboard":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def __init__(self):
        self.fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
        fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_KEY)
        for code in range(1, 249):
            fcntl.ioctl(self.fd, UI_SET_KEYBIT, code)
        # struct uinput_setup: input_id (bustype, vendor, product, version), name[80], ff_effects_max
        setup = struct.pack("HHHH80sI", 0x03, 0x1209, 0x5678, 1, b"voxbutton virtual keyboard", 0)
        fcntl.ioctl(self.fd, UI_DEV_SETUP, setup)
        fcntl.ioctl(self.fd, UI_DEV_CREATE)
        time.sleep(0.5)  # give the compositor a moment to pick the new device up
        self.lock = threading.Lock()

    def _emit(self, type_: int, code: int, value: int) -> None:
        now = time.time()
        os.write(self.fd, struct.pack("llHHi", int(now), int(now % 1 * 1e6), type_, code, value))

    def _press(self, codes: list[int]) -> None:
        for c in codes:
            self._emit(EV_KEY, c, 1)
            self._emit(EV_SYN, SYN_REPORT, 0)
        time.sleep(0.004)
        for c in reversed(codes):
            self._emit(EV_KEY, c, 0)
            self._emit(EV_SYN, SYN_REPORT, 0)
        time.sleep(0.004)

    def key(self, combo: str, times: int = 1) -> None:
        *mods, k = combo.split("+")
        try:
            codes = [KEYS[m] for m in mods] + [KEYS[k] if k in KEYS else KEYS[k.lower()]]
        except KeyError as e:
            raise LookupError(f"no key for {e.args[0]!r}") from None
        with self.lock:
            for _ in range(times):
                self._press(codes)

    def type(self, text: str) -> None:
        with self.lock:
            for c in simplify(text):
                code, shift = CHARS[c]
                self._press([KEYS["shift"], code] if shift else [code])
