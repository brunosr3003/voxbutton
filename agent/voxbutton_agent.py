#!/usr/bin/env python3
"""VoxButton mic agent for Linux and Windows: lends this machine's microphone
to the voxbutton server, like the Mac and iPhone apps do. Use it on the
computer you sit at (e.g. a laptop running Moonlight); the button on the PC
then records here when this machine is the one receiving the stream.

    voxbutton_agent.py                 run in the foreground
    voxbutton_agent.py --install       run at login (systemd user service on
                                       Linux, the Startup folder on Windows)
    voxbutton_agent.py --uninstall
    voxbutton_agent.py --devices       list microphones (Windows: their numbers)

Config: config.json in ~/.config/voxbutton (Windows: %APPDATA%\voxbutton),
same as the Mac app:
    {"server": "http://<pc-tailscale-ip>:8765", "token": "..."}

No Python packages needed. Linux records with parec, pw-record or arecord;
Windows with the built-in WinMM API.
"""

import argparse
import json
import math
import os
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from array import array
from pathlib import Path

WIN = sys.platform == "win32"
if WIN:
    CONFIG = Path(os.environ.get("APPDATA", Path.home())) / "voxbutton" / "config.json"
else:
    CONFIG = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "voxbutton" / "config.json"
RATE = 16000
CHUNK = 1600  # samples per read: 100 ms
BYTES_PER_SECOND = RATE * 2

SPEECH = -40.0  # dBFS: the least that counts as talking
PREROLL = 0.3  # s kept from before the speech started
ONCE_TIMEOUT = 8.0  # s a voice command waits for speech
ONCE_CHUNK = 4.0  # s: a voice command goes at most this long after speech starts


LOG_FILE = CONFIG.with_name("agent.log")


def log(msg: str) -> None:
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    if sys.stdout is None:  # pythonw (Windows autostart): no console, keep a log file
        try:
            CONFIG.parent.mkdir(parents=True, exist_ok=True)
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass
        return
    print(line, flush=True)


# --- recording ------------------------------------------------------------------


def recorder_cmd(device: str | None) -> list[str]:
    if shutil.which("parec"):
        return ["parec", "--format=s16le", f"--rate={RATE}", "--channels=1", "--latency-msec=50",
                *([f"--device={device}"] if device else [])]
    if shutil.which("pw-record"):
        return ["pw-record", "--format=s16", f"--rate={RATE}", "--channels=1",
                *([f"--target={device}"] if device else []), "-"]
    if shutil.which("arecord"):
        return ["arecord", "-q", "-f", "S16_LE", "-r", str(RATE), "-c", "1", "-t", "raw",
                *(["-D", device] if device else [])]
    sys.exit("no recorder found: install pulseaudio-utils (parec), pipewire (pw-record) or alsa-utils (arecord)")


class WinRecorder:
    """Windows: WinMM waveIn, 16 kHz mono 16-bit (Windows converts from the
    device's own format), polled buffers, no callbacks into Python."""

    NBUF = 6

    def __init__(self, device: str | None, on_chunk):
        import ctypes
        from ctypes import wintypes

        self.ct, self.wt = ctypes, wintypes
        self.winmm = ctypes.WinDLL("winmm")
        self.on_chunk = on_chunk
        self.device = int(device) if device not in (None, "") else 0xFFFFFFFF  # WAVE_MAPPER
        self.handle = wintypes.HANDLE()
        self.running = False

        class WAVEFORMATEX(ctypes.Structure):
            _fields_ = [("wFormatTag", wintypes.WORD), ("nChannels", wintypes.WORD),
                        ("nSamplesPerSec", wintypes.DWORD), ("nAvgBytesPerSec", wintypes.DWORD),
                        ("nBlockAlign", wintypes.WORD), ("wBitsPerSample", wintypes.WORD),
                        ("cbSize", wintypes.WORD)]

        class WAVEHDR(ctypes.Structure):
            pass

        WAVEHDR._fields_ = [("lpData", ctypes.c_void_p), ("dwBufferLength", wintypes.DWORD),
                            ("dwBytesRecorded", wintypes.DWORD), ("dwUser", ctypes.c_size_t),
                            ("dwFlags", wintypes.DWORD), ("dwLoops", wintypes.DWORD),
                            ("lpNext", ctypes.POINTER(WAVEHDR)), ("reserved", ctypes.c_size_t)]
        self.WAVEFORMATEX, self.WAVEHDR = WAVEFORMATEX, WAVEHDR
        w = self.winmm
        w.waveInOpen.argtypes = [ctypes.POINTER(wintypes.HANDLE), wintypes.UINT, ctypes.POINTER(WAVEFORMATEX),
                                 ctypes.c_size_t, ctypes.c_size_t, wintypes.DWORD]
        for name in ("waveInPrepareHeader", "waveInUnprepareHeader", "waveInAddBuffer"):
            getattr(w, name).argtypes = [wintypes.HANDLE, ctypes.POINTER(WAVEHDR), wintypes.UINT]
        for name in ("waveInStart", "waveInStop", "waveInReset", "waveInClose"):
            getattr(w, name).argtypes = [wintypes.HANDLE]

    def start(self) -> None:
        ct, w = self.ct, self.winmm
        fmt = self.WAVEFORMATEX(1, 1, RATE, BYTES_PER_SECOND, 2, 16, 0)
        rc = w.waveInOpen(ct.byref(self.handle), self.device, ct.byref(fmt), 0, 0, 0)
        if rc != 0:
            raise OSError(f"waveInOpen failed ({rc}): no microphone, or access is blocked in "
                          "Settings > Privacy > Microphone")
        size = CHUNK * 2
        self.bufs = [ct.create_string_buffer(size) for _ in range(self.NBUF)]
        self.hdrs = []
        for b in self.bufs:
            h = self.WAVEHDR()
            h.lpData, h.dwBufferLength = ct.cast(b, ct.c_void_p), size
            w.waveInPrepareHeader(self.handle, ct.byref(h), ct.sizeof(h))
            w.waveInAddBuffer(self.handle, ct.byref(h), ct.sizeof(h))
            self.hdrs.append(h)
        self.running = True
        w.waveInStart(self.handle)
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        ct, w = self.ct, self.winmm
        i = 0
        while self.running:
            h = self.hdrs[i]
            if not h.dwFlags & 1:  # WHDR_DONE
                time.sleep(0.01)
                continue
            data = ct.string_at(h.lpData, h.dwBytesRecorded)
            if not self.running:
                break
            h.dwFlags &= ~1
            h.dwBytesRecorded = 0
            w.waveInAddBuffer(self.handle, ct.byref(h), ct.sizeof(h))
            if data:
                self.on_chunk(data)
            i = (i + 1) % self.NBUF

    def stop(self) -> None:
        if not self.running:
            return
        self.running = False
        ct, w = self.ct, self.winmm
        w.waveInReset(self.handle)
        for h in self.hdrs:
            w.waveInUnprepareHeader(self.handle, ct.byref(h), ct.sizeof(h))
        w.waveInClose(self.handle)


def win_devices() -> list[str]:
    import ctypes
    from ctypes import wintypes

    class WAVEINCAPSW(ctypes.Structure):
        _fields_ = [("wMid", wintypes.WORD), ("wPid", wintypes.WORD), ("vDriverVersion", wintypes.UINT),
                    ("szPname", wintypes.WCHAR * 32), ("dwFormats", wintypes.DWORD),
                    ("wChannels", wintypes.WORD), ("wReserved1", wintypes.WORD)]

    winmm = ctypes.WinDLL("winmm")
    out = []
    for i in range(winmm.waveInGetNumDevs()):
        caps = WAVEINCAPSW()
        if winmm.waveInGetDevCapsW(i, ctypes.byref(caps), ctypes.sizeof(caps)) == 0:
            out.append(caps.szPname)
    return out


class Recorder:
    """Streams 100 ms chunks of 16 kHz mono s16le to a callback until stopped."""

    def __init__(self, device: str | None, on_chunk):
        self.cmd = recorder_cmd(device)
        self.on_chunk = on_chunk
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        self.proc = subprocess.Popen(self.cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        threading.Thread(target=self._read, args=(self.proc,), daemon=True).start()

    def _read(self, proc: subprocess.Popen) -> None:
        need = CHUNK * 2
        while proc.poll() is None:
            data = proc.stdout.read(need)
            if not data:
                break
            self.on_chunk(data)

    def stop(self) -> None:
        if self.proc:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self.proc = None


def level_db(chunk: bytes) -> float:
    samples = array("h", chunk[: len(chunk) // 2 * 2])
    if not samples:
        return -120.0
    mean = sum(s * s for s in samples) / len(samples) / (32768.0 * 32768.0)
    return 10 * math.log10(max(mean, 1e-12))


def wav(pcm: bytes) -> bytes:
    return (b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt "
            + struct.pack("<IHHIIHH", 16, 1, 1, RATE, BYTES_PER_SECOND, 2, 16)
            + b"data" + struct.pack("<I", len(pcm)) + pcm)


class Segmenter:
    """Cuts a live stream into pieces: at every pause, and every `chunk`
    seconds while talking (at the quietest moment of the last 1.5 s). Speech
    is judged against the room's background noise, so a noisy room doesn't
    keep a piece open forever."""

    def __init__(self, pause: float, chunk: float, on_piece):
        self.pause, self.chunk, self.on_piece = pause, chunk, on_piece
        self.ring = bytearray()
        self.current = bytearray()
        self.frames: list[tuple[int, float]] = []  # (end offset, level)
        self.talking = False
        self.quiet = 0.0
        self.peak = -120.0
        self.floor_levels: list[float] = []

    def noise_floor(self) -> float:
        return sorted(self.floor_levels)[len(self.floor_levels) // 2] if self.floor_levels else -60.0

    def feed(self, chunk: bytes) -> None:
        level = level_db(chunk)
        seconds = len(chunk) / BYTES_PER_SECOND
        if self.talking:
            self.current += chunk
            self.frames.append((len(self.current), level))
            self.peak = max(self.peak, level)
            quiet_below = max(SPEECH, self.noise_floor() + 6, self.peak - 20)
            self.quiet = self.quiet + seconds if level < quiet_below else 0.0
            if self.quiet >= self.pause:
                self.send(len(self.current))
            elif len(self.current) / BYTES_PER_SECOND >= self.chunk:
                start = len(self.current) - int(1.5 * BYTES_PER_SECOND)
                cands = [f for f in self.frames if start <= f[0] < len(self.current)]
                self.send(min(cands, key=lambda f: f[1])[0] if cands else len(self.current))
        elif level >= max(SPEECH, self.noise_floor() + 10):
            self.talking, self.quiet, self.peak = True, 0.0, level
            self.current = bytearray(self.ring) + chunk
            self.frames = [(len(self.ring), -120.0), (len(self.current), level)]
        else:
            self.floor_levels = (self.floor_levels + [level])[-15:]  # ~1.5 s
            self.ring = (self.ring + chunk)[-int(PREROLL * BYTES_PER_SECOND) // 2 * 2:]

    def send(self, cut: int) -> None:
        piece = bytes(self.current[:cut])
        if cut >= len(self.current):
            self.talking = False
            self.current, self.frames, self.ring = bytearray(), [], bytearray()
        else:
            self.current = self.current[cut:]
            self.frames = [(e - cut, l) for e, l in self.frames if e > cut]
        if len(piece) / BYTES_PER_SECOND >= 0.4:
            self.on_piece(piece)

    def flush(self) -> bool:
        """Sends what's left of a piece in progress; returns whether it did."""
        if self.talking and len(self.current) / BYTES_PER_SECOND >= 0.4:
            self.send(len(self.current))
            return True
        self.talking = False
        return False


# --- the agent ---------------------------------------------------------------------


def local_ips() -> list[str]:
    ips = set()
    if WIN:
        try:
            ips.update(socket.gethostbyname_ex(socket.gethostname())[2])
        except OSError:
            pass
        exe = shutil.which("tailscale") or str(Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
                                                 / "Tailscale" / "tailscale.exe")
        try:
            out = subprocess.run([exe, "ip", "-4"], capture_output=True, text=True, timeout=5, creationflags=0x08000000)
            ips.update(out.stdout.split())
        except (OSError, subprocess.TimeoutExpired):
            pass
        return sorted(ip for ip in ips if not ip.startswith("127."))
    try:
        out = subprocess.run(["ip", "-4", "-o", "addr"], capture_output=True, text=True, timeout=3).stdout
        for line in out.splitlines():
            parts = line.split()
            if "inet" in parts:
                ip = parts[parts.index("inet") + 1].split("/")[0]
                if not ip.startswith("127."):
                    ips.add(ip)
    except (OSError, subprocess.TimeoutExpired):
        pass
    return sorted(ips)


def moonlight_running() -> bool:
    try:
        if WIN:
            out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Moonlight.exe", "/NH"], capture_output=True,
                                 text=True, timeout=5, creationflags=0x08000000).stdout
            return "moonlight" in out.lower()
        return subprocess.run(["pgrep", "-if", "moonlight"], capture_output=True, timeout=3).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


class Agent:
    def __init__(self, server: str, token: str, name: str, device: str | None):
        self.server, self.token, self.name = server.rstrip("/"), token, name
        self.device = device
        self.lock = threading.Lock()
        self.recorder: Recorder | None = None
        self.mode = ""  # "" | record | listen | once
        self.buffer = bytearray()
        self.segmenter: Segmenter | None = None
        self.once_timer: threading.Timer | None = None

    # http

    def request(self, method: str, path: str, body: bytes | None = None, headers: dict | None = None,
                timeout: float = 40) -> tuple[int, dict]:
        req = urllib.request.Request(self.server + path, data=body, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, {}
        except (OSError, ValueError) as e:
            return 0, {"error": str(e)}

    def upload(self, pcm: bytes, segment: bool) -> None:
        def go():
            headers = {"Content-Type": "audio/wav", "X-Agent": "1", **({"X-Segment": "1"} if segment else {})}
            code, r = self.request("POST", "/transcribe", wav(pcm), headers, timeout=120)
            log(f"sent {len(pcm) / BYTES_PER_SECOND:.1f}s → {r.get('command') or r.get('text') or r.get('error') or code!r}")

        threading.Thread(target=go, daemon=True).start()

    def report_error(self, msg: str) -> None:
        threading.Thread(target=lambda: self.request("POST", "/agent/error", msg.encode(), timeout=10), daemon=True).start()

    # audio

    def on_chunk(self, chunk: bytes) -> None:
        with self.lock:
            if self.mode == "record":
                self.buffer += chunk
            elif self.mode in ("listen", "once") and self.segmenter:
                self.segmenter.feed(chunk)

    def on_piece(self, pcm: bytes) -> None:  # called with self.lock held (from feed)
        if self.mode == "once":
            self.mode = ""
            self._end_once_timer()
            threading.Thread(target=self._stop_recorder, daemon=True).start()
            self.upload(pcm, segment=False)
        else:
            self.upload(pcm, segment=True)

    def _start_recorder(self) -> None:
        self.recorder = (WinRecorder if WIN else Recorder)(self.device, self.on_chunk)
        try:
            self.recorder.start()
        except OSError as e:
            log(str(e))
            self.recorder = None
            with self.lock:
                self.mode = ""
            self.report_error(str(e))

    def _stop_recorder(self) -> None:
        r, self.recorder = self.recorder, None
        if r:
            r.stop()

    def _end_once_timer(self) -> None:
        if self.once_timer:
            self.once_timer.cancel()
            self.once_timer = None

    # commands from the server

    def handle(self, cmd: str, params: dict) -> None:
        if cmd in ("start", "listen", "once") and self.mode:
            # A new recording replaces whatever is left over, e.g. after the server
            # restarted mid-recording and its "stop" never came.
            log(f"dropping a leftover {self.mode}")
            self._end_once_timer()
            self._stop_recorder()
            with self.lock:
                self.mode = ""
        busy = self.mode != ""
        if cmd == "start" and not busy:
            with self.lock:
                self.buffer, self.mode = bytearray(), "record"
            self._start_recorder()
            log("recording")
        elif cmd in ("listen", "once") and not busy:
            once = cmd == "once"
            pause = float(params.get("pause", 0.6))
            chunk = ONCE_CHUNK if once else float(params.get("chunk", 6))
            with self.lock:
                self.segmenter = Segmenter(pause, chunk, self.on_piece)
                self.mode = cmd
            self._start_recorder()
            if once:
                self.once_timer = threading.Timer(ONCE_TIMEOUT, self.end_once)
                self.once_timer.start()
            log("listening for a command" if once else "listening")
        elif cmd == "stop" and self.mode == "record":
            self._stop_recorder()
            with self.lock:
                pcm, self.mode = bytes(self.buffer), ""
            self.upload(pcm, segment=False)
        elif cmd == "stop" and self.mode == "once":
            self.end_once()
        elif cmd == "stop" and self.mode == "listen":
            self._stop_recorder()
            with self.lock:
                self.segmenter.flush()
                self.mode = ""
            log("stopped listening")
        elif cmd in ("start", "stop", "listen", "once"):
            self.report_error(f"got {cmd} while {self.mode or 'idle'}")

    def end_once(self) -> None:
        self._end_once_timer()
        self._stop_recorder()
        with self.lock:
            if self.mode != "once":
                return
            sent = self.segmenter.flush()  # on_piece uploads it and clears the mode
            if not sent:
                self.mode = ""
        if not sent:
            self.report_error("no speech heard")

    def run(self) -> None:
        log(f"agent {self.name!r} → {self.server}")
        while True:
            prio = 2 if moonlight_running() else 1
            q = f"/agent/wait?name={self.name}&prio={prio}&ips={','.join(local_ips())}"
            code, r = self.request("GET", q, timeout=40)
            if code != 200:
                log(f"server: {r.get('error') or code}; retrying")
                time.sleep(3)
                continue
            if r.get("cmd"):
                self.handle(r["cmd"], r)


# --- setup -----------------------------------------------------------------------


UNIT = "voxbutton-agent.service"


def install_windows(on: bool) -> None:
    link = Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/Startup/voxbutton-agent.lnk"
    if not on:
        link.unlink(missing_ok=True)
        print(f"removed {link}")
        return
    pyw = Path(sys.executable).with_name("pythonw.exe")
    ps = ("$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:VB_LINK);"
          "$s.TargetPath = $env:VB_EXE; $s.Arguments = '\"' + $env:VB_SCRIPT + '\"';"
          "$s.WorkingDirectory = $env:VB_DIR; $s.Save()")
    env = {**os.environ, "VB_LINK": str(link), "VB_EXE": str(pyw if pyw.exists() else sys.executable),
           "VB_SCRIPT": str(Path(__file__).resolve()), "VB_DIR": str(Path(__file__).resolve().parent)}
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True, env=env)
    subprocess.Popen([env["VB_EXE"], env["VB_SCRIPT"]], creationflags=0x00000008)  # DETACHED_PROCESS
    print(f"added {link} and started the agent")


def install(on: bool) -> None:
    if WIN:
        install_windows(on)
        return
    unit = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "systemd/user" / UNIT
    if on:
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text(f"""[Unit]
Description=voxbutton mic agent (lends this machine's microphone)
After=network-online.target pipewire.service

[Service]
ExecStart={sys.executable} {Path(__file__).resolve()}
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
""")
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "--user", "enable", "--now", UNIT], check=True)
        print(f"installed and started {UNIT} (logs: journalctl --user -u {UNIT})")
    else:
        subprocess.run(["systemctl", "--user", "disable", "--now", UNIT])
        unit.unlink(missing_ok=True)
        print(f"removed {UNIT}")


def main() -> None:
    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        # The Windows console is cp1252 and can't print "→" or accented text.
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", help="voxbutton server (default: from config.json)")
    ap.add_argument("--token", help="auth token (default: from config.json)")
    ap.add_argument("--name", default=socket.gethostname().split(".")[0].lower(), help="name shown in settings")
    ap.add_argument("--device", help="input device: a source name on Linux, a number from --devices on Windows "
                                     "(default: the system default)")
    ap.add_argument("--install", action="store_true", help="run at login as a systemd user service")
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--devices", action="store_true", help="list microphones and exit")
    args = ap.parse_args()

    if args.devices:
        if WIN:
            for i, name in enumerate(win_devices()):
                print(f"{i}: {name}")
        else:
            subprocess.run(["pactl", "list", "short", "sources"] if shutil.which("pactl") else ["arecord", "-l"])
        return
    if args.install or args.uninstall:
        install(args.install)
        return
    cfg = {}
    try:
        cfg = json.loads(CONFIG.read_text())
    except (OSError, ValueError):
        pass
    server, token = args.server or cfg.get("server"), args.token or cfg.get("token")
    if not server or not token:
        sys.exit(f"set server and token in {CONFIG} (or --server/--token); the settings window shows both")
    Agent(server, token, args.name, args.device or cfg.get("device")).run()


if __name__ == "__main__":
    main()
