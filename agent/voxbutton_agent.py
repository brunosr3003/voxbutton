#!/usr/bin/env python3
"""VoxButton mic agent for Linux: lends this machine's microphone to the
voxbutton server, like the Mac and iPhone apps do. Use it on the Linux box you
sit at (e.g. a laptop running Moonlight); the button on the PC then records
here when this machine is the one receiving the stream.

    voxbutton_agent.py                 run in the foreground
    voxbutton_agent.py --install       run at login (systemd user service)
    voxbutton_agent.py --uninstall

Config: ~/.config/voxbutton/config.json, same as the Mac app:
    {"server": "http://<pc-tailscale-ip>:8765", "token": "..."}

Records with whatever is there: parec (PulseAudio / PipeWire-pulse),
pw-record (PipeWire) or arecord (ALSA). No Python packages needed.
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

CONFIG = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "voxbutton" / "config.json"
RATE = 16000
CHUNK = 1600  # samples per read: 100 ms
BYTES_PER_SECOND = RATE * 2

SPEECH = -40.0  # dBFS: the least that counts as talking
PREROLL = 0.3  # s kept from before the speech started
ONCE_TIMEOUT = 8.0  # s a voice command waits for speech
ONCE_CHUNK = 4.0  # s: a voice command goes at most this long after speech starts


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, flush=True)


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
        self.recorder = Recorder(self.device, self.on_chunk)
        self.recorder.start()

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


def install(on: bool) -> None:
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
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", help="voxbutton server (default: from config.json)")
    ap.add_argument("--token", help="auth token (default: from config.json)")
    ap.add_argument("--name", default=socket.gethostname().split(".")[0].lower(), help="name shown in settings")
    ap.add_argument("--device", help="input device / source (default: the system default)")
    ap.add_argument("--install", action="store_true", help="run at login as a systemd user service")
    ap.add_argument("--uninstall", action="store_true")
    args = ap.parse_args()

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
