"""voxbutton server: receives audio over HTTP, transcribes it with Whisper on
the local GPU and types the text into the focused window (Linux or Windows).

Settings come from server.json in the config directory, then command-line
flags; the settings window writes back to server.json."""

import argparse
import glob
import io
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from voxbutton_server import commands, platform

if sys.platform == "win32":
    CONFIG_DIR = Path(os.environ.get("APPDATA", Path.home())) / "voxbutton"
else:
    CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "voxbutton"
TOKEN_FILE = CONFIG_DIR / "token"
CONFIG_FILE = CONFIG_DIR / "server.json"
LEGACY_SETTINGS = CONFIG_DIR / "settings.json"

DEFAULTS = {
    "host": "",  # empty: this machine's Tailscale IPv4, else 127.0.0.1
    "port": 8765,
    "model": "large-v3-turbo",
    "device": "auto",  # cuda when there's an NVIDIA GPU, else cpu
    "compute_type": "default",
    "language": "",  # force one language for dictation; empty: auto-detect
    "languages": [],  # languages auto-detection may pick from, e.g. ["en", "pt"]
    "min_level": -34.0,  # dBFS; quieter clips count as silence
    "prompt": "",  # biases Whisper's vocabulary (names, jargon)
    "trust": [],  # IPs allowed without the token, e.g. a phone's Tailscale address
    "public_url": "",  # HTTPS address for devices outside the tailnet, shown in settings
    "type": True,  # type the text; false only returns it
    "record_mode": "toggle",  # toggle: click/click · hold: push-to-talk · always: keeps listening
    "listen_pause": 0.6,  # always on: seconds of quiet that send what you said
    "listen_chunk": 6.0,  # always on: while you keep talking, send about this often (seconds)
}
RECORD_MODES = ("toggle", "hold", "always")


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    for f in (CONFIG_FILE, LEGACY_SETTINGS):
        if f.exists():
            try:
                cfg.update({k: v for k, v in json.loads(f.read_text()).items() if k in DEFAULTS})
            except (ValueError, AttributeError) as e:
                print(f"ignoring {f}: {e}", file=sys.stderr)
    return cfg


def save_config(changes: dict) -> None:
    saved = {}
    if CONFIG_FILE.exists():
        try:
            saved = json.loads(CONFIG_FILE.read_text())
        except ValueError:
            pass
    saved.update(changes)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(saved, indent=2) + "\n")
    LEGACY_SETTINGS.unlink(missing_ok=True)  # folded into server.json
MAX_BODY = 50 * 1024 * 1024  # ~25 min of 16 kHz mono WAV


def ensure_cuda_libs() -> None:
    """cuBLAS/cuDNN come from pip wheels. On Windows their DLL folders are added
    to the search path; on Linux ctranslate2 only finds them through
    LD_LIBRARY_PATH, which has to be set before the process starts."""
    if sys.platform == "win32":
        for d in glob.glob(str(Path(sys.prefix) / "Lib/site-packages/nvidia/*/bin")):
            os.add_dll_directory(d)
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
        return
    if sys.platform != "linux" or os.environ.get("VOXBUTTON_REEXEC"):
        return
    base = Path(sys.prefix) / "lib"
    dirs = sorted(glob.glob(str(base / "python3*/site-packages/nvidia/*/lib")))
    if not dirs:
        return
    env = dict(os.environ)
    env["LD_LIBRARY_PATH"] = ":".join(dirs + [p for p in [env.get("LD_LIBRARY_PATH")] if p])
    env["VOXBUTTON_REEXEC"] = "1"
    os.execve(sys.executable, [sys.executable, "-m", "voxbutton_server", *sys.argv[1:]], env)


def load_token() -> str:
    if TOKEN_FILE.exists():
        return TOKEN_FILE.read_text().strip()
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(24)
    TOKEN_FILE.write_text(token + "\n")
    TOKEN_FILE.chmod(0o600)
    return token


def tailscale_cli() -> str:
    win = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Tailscale" / "tailscale.exe"
    return shutil.which("tailscale") or (str(win) if win.exists() else "tailscale")


def tailscale_ip() -> str | None:
    try:
        out = subprocess.run([tailscale_cli(), "ip", "-4"], capture_output=True, text=True, timeout=5)
        ip = out.stdout.strip().splitlines()
        return ip[0] if out.returncode == 0 and ip else None
    except (OSError, subprocess.TimeoutExpired):
        return None


type_text = platform.type_text


class Transcriber:
    def __init__(self, model: str, device: str, compute_type: str, language: str | None, prompt: str | None,
                 languages: list[str], min_level: float):
        from faster_whisper import WhisperModel

        t = time.time()
        self.model = WhisperModel(model, device=device, compute_type=compute_type)
        self.model_name = model
        self.language = language
        self.prompt = prompt
        self.languages = languages
        self.min_level = min_level
        self.lock = threading.Lock()
        log(f"model {model} loaded on {device} in {time.time() - t:.1f}s")

    def loudest(self, samples) -> float:
        """dBFS of the loudest 100 ms window."""
        import numpy as np

        w = 1600  # 100 ms at 16 kHz
        n = len(samples) // w
        if n == 0:
            return -120.0
        rms = np.sqrt((samples[: n * w].reshape(n, w) ** 2).mean(axis=1))
        return float(20 * np.log10(max(rms.max(), 1e-6)))

    def __call__(self, audio: bytes, language: str | None, prompt: str | None = None) -> tuple[str, str]:
        from faster_whisper.audio import decode_audio

        samples = decode_audio(io.BytesIO(audio))
        # Whisper invents text on silence ("Thank you.", "Продолжение следует..."),
        # and neither VAD nor no_speech_prob catch it; loudness does.
        level = self.loudest(samples)
        if level < self.min_level:
            log(f"silence ({level:.1f} dBFS < {self.min_level}), skipped")
            return "", ""

        def run(lang):
            return self.model.transcribe(
                samples,
                language=lang,
                initial_prompt=prompt or self.prompt,
                vad_filter=True,
                beam_size=5,
                condition_on_previous_text=False,
            )

        with self.lock:
            segments, info = run(language or self.language)
            if self.languages and info.language not in self.languages:
                # Segments are lazy, so re-running with the likeliest allowed language costs nothing extra.
                probs = dict(info.all_language_probs or [])
                best = max(self.languages, key=lambda l: probs.get(l, 0))
                segments, info = run(best)
            text = " ".join(s.text.strip() for s in segments).strip()
        return text, info.language


class StreamWatcher:
    """Finds the device you're streaming to (Moonlight over Tailscale): it's the
    Tailscale peer this machine is sending video to, megabits per second, while
    every other peer gets next to nothing."""

    INTERVAL = 2.0
    MIN_RATE = 500_000  # bit/s
    STICKY = 20  # s to keep the last streaming peer through a quiet moment

    def __init__(self):
        self.peer: str | None = None
        self.rate = 0.0
        self.seen = 0.0
        threading.Thread(target=self._loop, daemon=True).start()

    @staticmethod
    def _snapshot() -> dict[str, int]:
        try:
            out = subprocess.run([tailscale_cli(), "status", "--json"], capture_output=True, text=True, timeout=5)
            peers = json.loads(out.stdout).get("Peer") or {}
        except (OSError, subprocess.TimeoutExpired, ValueError):
            return {}
        return {ip: p.get("TxBytes", 0) for p in peers.values() for ip in p.get("TailscaleIPs", [])[:1]}

    def _loop(self) -> None:
        prev = self._snapshot()
        while True:
            time.sleep(self.INTERVAL)
            cur = self._snapshot()
            rates = {ip: (tx - prev[ip]) * 8 / self.INTERVAL for ip, tx in cur.items() if ip in prev}
            prev = cur
            ip, rate = max(rates.items(), key=lambda kv: kv[1], default=(None, 0))
            if ip and rate >= self.MIN_RATE:
                if ip != self.peer:
                    log(f"streaming to {ip} ({rate / 1e6:.1f} Mbit/s)")
                self.peer, self.rate, self.seen = ip, rate, time.time()

    def current(self) -> str | None:
        return self.peer if time.time() - self.seen < self.STICKY else None


class Remote:
    """Lets a button on this machine drive a microphone on another one.

    Mic agents (the Mac and iOS apps) long-poll /agent/wait for "start"/"stop"
    and post the recording to /transcribe like any other client; the button
    calls /record/toggle and polls /state to show what's happening.

    A recording goes to the agent on the device the screen is being streamed
    to (matched by IP, see StreamWatcher). Without a stream, each agent's own
    priority decides: the Mac reports 2 while Moonlight is in front and 0
    otherwise, the iPhone always 1. The stop goes to the same agent."""

    AGENT_TIMEOUT = 35  # s without a poll before the agent counts as gone
    MAX_RECORDING = 300  # s, safety net if the stop never arrives

    def __init__(self, stream: "StreamWatcher | None" = None):
        self.stream = stream
        self.cond = threading.Condition()
        self.state = "idle"  # idle | recording | busy | listening
        self.record_mode = "toggle"
        self.listen = {"pause": 0.6, "chunk": 6.0}  # sent to agents along with "listen"
        self.since = time.time()
        self.agents: dict[str, dict] = {}  # name -> {"seen", "prio", "gen"}
        self.cmds: dict[str, str] = {}
        self.active: str | None = None
        self.mode = "chat"  # of the current recording: "chat" types, "command" acts
        self.flash = ("", 0.0)  # ("done" | "error", when)

    def _set(self, state: str) -> None:
        self.state, self.since = state, time.time()

    def _alive(self) -> dict[str, dict]:
        now = time.time()
        return {n: a for n, a in self.agents.items() if now - a["seen"] < self.AGENT_TIMEOUT}

    def _best(self) -> str | None:
        alive = self._alive()
        peer = self.stream.current() if self.stream else None
        if peer:
            for n, a in alive.items():
                if peer in a.get("ips", ()):
                    return n
        ready = [(a["prio"], a["seen"], n) for n, a in alive.items() if a["prio"] > 0]
        return max(ready)[2] if ready else None

    def wait(self, name: str, prio: int, ips: set[str] = frozenset(), timeout: float = 25) -> str | None:
        with self.cond:
            a = self.agents.setdefault(name, {"gen": 0})
            a["ips"] = set(ips)
            a["gen"] += 1  # a newer poll from the same agent supersedes this one
            gen = a["gen"]
            a["seen"], a["prio"] = time.time(), prio
            self.cond.notify_all()
            self.cond.wait_for(lambda: a["gen"] != gen or name in self.cmds, timeout)
            a["seen"] = time.time()
            if a["gen"] != gen:
                return None
            return self.cmds.pop(name, None)

    def start(self, mode: str = "chat") -> str:
        """Starts a recording ("chat" or "command"), or continuous listening
        when the record mode is "always". Returns an error or ""."""
        with self.cond:
            if self.state != "idle":
                return ""
            best = self._best()
            if not best:
                self.flash = ("error", time.time())
                return "no microphone available" if self._alive() else "no microphone agent connected"
            self.active = best
            self.mode = mode
            listen = self.record_mode == "always" and mode == "chat"
            self.cmds[best] = "listen" if listen else "start"
            self._set("listening" if listen else "recording")
            log(f"{'listening' if listen else 'recording ' + mode} on {best}")
            self.cond.notify_all()
            return ""

    def stop(self) -> None:
        with self.cond:
            if self.state in ("recording", "listening") and self.active:
                self.cmds[self.active] = "stop"
                # Listening already delivered its segments; a recording still has to.
                self._set("busy" if self.state == "recording" else "idle")
                self.cond.notify_all()

    def toggle(self, mode: str = "chat") -> str:
        if self.state in ("recording", "listening"):
            self.stop()
            return ""
        return self.start(mode)

    def devices(self) -> dict:
        with self.cond:
            now = time.time()
            peer = self.stream.current() if self.stream else None
            best = self._best()
            return {
                "stream": {"peer": peer, "mbit": round(self.stream.rate / 1e6, 1) if peer else 0},
                "devices": [
                    {
                        "name": n,
                        "online": now - a["seen"] < self.AGENT_TIMEOUT,
                        "seen_ago": round(now - a["seen"]),
                        "ips": sorted(a.get("ips", ())),
                        "priority": a.get("prio", 0),
                        "streaming": bool(peer and peer in a.get("ips", ())),
                        "selected": n == best,
                    }
                    for n, a in sorted(self.agents.items())
                ],
            }

    def finished(self, ok: bool, segment: bool = False) -> None:
        with self.cond:
            if self.state != "idle" and not (segment and self.state == "listening"):
                self._set("idle")
            self.flash = ("done" if ok else "error", time.time())

    def snapshot(self) -> dict:
        with self.cond:
            now = time.time()
            if self.state == "recording" and now - self.since > self.MAX_RECORDING and self.active:
                self.cmds[self.active] = "stop"
                self._set("busy")
                self.cond.notify_all()
            elif self.state == "busy" and now - self.since > 60:
                self._set("idle")  # the agent never delivered
                self.flash = ("error", now)
            kind, at = self.flash
            return {
                "state": self.state,
                "agent": self._best() is not None or self.state != "idle",
                "mic": self.active if self.state != "idle" else self._best(),
                "mode": self.mode,
                "record_mode": self.record_mode,
                "flash": kind if now - at < 1.2 else "",
            }


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def make_handler(transcribe: Transcriber, remote: Remote, token: str, do_type: bool, trusted: set[str],
                 urls: dict[str, str]):
    last = {"typed": 0}  # length of the last dictation, for the "delete that" command

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def log_error(self, fmt, *args):
            # Surfaces e.g. a client speaking TLS to this plain-HTTP port.
            log(f"{self.client_address[0]}: " + fmt % args)

        def reply(self, code: int, body: dict) -> None:
            data = json.dumps(body).encode()
            try:
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass  # e.g. an agent's long-poll cancelled on its side

        def authorized(self) -> bool:
            if self.client_address[0] in trusted:
                return True
            got = self.headers.get("Authorization", "").removeprefix("Bearer ")
            if not got:
                # Also accepted as ?token=..., so clients like iOS Shortcuts only need a URL.
                got = parse_qs(urlsplit(self.path).query).get("token", [""])[0]
            return secrets.compare_digest(got.encode(), token.encode())

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == "/health":
                return self.reply(200, {"ok": True})
            if not self.authorized():
                return self.reply(401, {"error": "bad token"})
            if path == "/state":
                self.reply(200, remote.snapshot())
            elif path == "/info":
                self.reply(200, {
                    **remote.devices(),
                    "settings": {
                        "model": transcribe.model_name,
                        "languages": transcribe.languages,
                        "min_level": transcribe.min_level,
                        "record_mode": remote.record_mode,
                        "listen_pause": remote.listen["pause"],
                        "listen_chunk": remote.listen["chunk"],
                    },
                    "connect": {**urls, "token": token},
                    "commands": commands.CATALOG,
                })
            elif path == "/agent/wait":
                q = parse_qs(urlsplit(self.path).query)
                name = q.get("name", ["mac"])[0][:32]
                try:
                    prio = int(q.get("prio", ["1"])[0])
                except ValueError:
                    prio = 1
                # Direct clients are known by their address; behind the TLS proxy
                # they report their own (e.g. the iPhone's Tailscale IP).
                ips = {ip for ip in q.get("ips", [""])[0].split(",") if ip}
                ips.add(self.client_address[0])
                cmd = remote.wait(name, prio, ips)
                self.reply(200, {"cmd": cmd, **(remote.listen if cmd == "listen" else {})})
            else:
                self.reply(404, {"error": "not found"})

        def do_POST(self):
            path = urlsplit(self.path).path
            if path not in ("/transcribe", "/record/toggle", "/record/start", "/record/stop", "/agent/error", "/config"):
                return self.reply(404, {"error": "not found"})
            if not self.authorized():
                return self.reply(401, {"error": "bad token"})
            query = parse_qs(urlsplit(self.path).query)
            if path in ("/record/toggle", "/record/start", "/record/stop"):
                mode = "command" if query.get("mode") == ["command"] else "chat"
                if path == "/record/stop":
                    remote.stop()
                    err = ""
                else:
                    err = (remote.start if path == "/record/start" else remote.toggle)(mode)
                return self.reply(503 if err else 200, {"error": err} if err else remote.snapshot())
            if path == "/config":
                # Only from this machine's own settings window, never through the proxy.
                if self.headers.get("X-Forwarded-For"):
                    return self.reply(403, {"error": "local only"})
                try:
                    body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                    if "languages" in body:
                        transcribe.languages = [l.strip() for l in body["languages"] if l.strip()]
                    if "min_level" in body:
                        transcribe.min_level = float(body["min_level"])
                    if body.get("record_mode") in RECORD_MODES:
                        remote.record_mode = body["record_mode"]
                    if "listen_pause" in body:
                        remote.listen["pause"] = min(max(float(body["listen_pause"]), 0.2), 3.0)
                    if "listen_chunk" in body:
                        remote.listen["chunk"] = min(max(float(body["listen_chunk"]), 2.0), 30.0)
                except (ValueError, TypeError, AttributeError) as e:
                    return self.reply(400, {"error": str(e)})
                save_config({"languages": transcribe.languages, "min_level": transcribe.min_level,
                             "record_mode": remote.record_mode, "listen_pause": remote.listen["pause"],
                             "listen_chunk": remote.listen["chunk"]})
                log(f"settings: languages={transcribe.languages} min_level={transcribe.min_level} "
                    f"record_mode={remote.record_mode}")
                return self.reply(200, {"ok": True})
            if path == "/agent/error":
                log(f"agent error: {self.rfile.read(int(self.headers.get('Content-Length') or 0))[:300]!r}")
                remote.finished(False)
                return self.reply(200, {"ok": True})
            try:
                self.transcribe()
            except Exception:
                self.finished(False)
                raise

        def finished(self, ok: bool) -> None:
            # Only the mic agent's uploads belong to the button's recording.
            if self.headers.get("X-Agent"):
                remote.finished(ok, segment=bool(self.headers.get("X-Segment")))

        def transcribe(self):
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 < length <= MAX_BODY:
                log(f"rejected body: length={length} type={self.headers.get('Content-Type')}")
                self.finished(False)
                return self.reply(413, {"error": "empty or too large"})
            audio = self.rfile.read(length)
            language = self.headers.get("X-Language") or None
            query = parse_qs(urlsplit(self.path).query)
            mode = (query.get("mode") or [self.headers.get("X-Mode") or ""])[0]
            segment = bool(self.headers.get("X-Segment"))  # a pause-delimited piece of "always" listening
            if not mode and self.headers.get("X-Agent") and not segment:
                mode = remote.mode
            command = mode == "command"
            t = time.time()
            try:
                if command:
                    text, lang = transcribe(audio, "en", commands.PROMPT)  # commands are English
                else:
                    text, lang = transcribe(audio, language)
            except Exception as e:  # bad audio, CUDA hiccup...
                log(f"transcribe failed: {e}")
                self.finished(False)
                return self.reply(500, {"error": str(e)})
            if segment and (m := re.match(r"^\W*(?:command|comando)\b[\s,.:!-]*(.+)$", text, re.I)):
                # "command next tab" while always listening runs as a command.
                command, text = True, m.group(1)
            log(f"[{lang}]{' command' if command else ''}{' segment' if segment else ''} {time.time() - t:.2f}s: {text!r}")
            if command:
                if not text:
                    self.finished(False)
                    return self.reply(200, {"text": "", "command": None})
                try:
                    dry = not do_type or self.headers.get("X-Type") == "0"
                    name = commands.run(text, last["typed"], dry=dry)
                except (LookupError, OSError, subprocess.SubprocessError) as e:
                    log(f"  command failed: {e}")
                    self.finished(False)
                    return self.reply(200, {"text": text, "command": None, "error": str(e)})
                log(f"  → {name}")
                if name == "delete that":
                    last["typed"] = 0
                self.finished(True)
                return self.reply(200, {"text": text, "command": name})
            typed = False
            if text and do_type and self.headers.get("X-Type", "1") != "0":
                try:
                    # Trailing space so consecutive dictations don't glue together.
                    type_text(text + " ")
                    typed = True
                    last["typed"] = len(text) + 1
                except (OSError, subprocess.SubprocessError) as e:
                    log(f"wtype failed: {e}")
                    self.finished(False)
                    return self.reply(500, {"error": f"wtype failed: {e}", "text": text})
            self.finished(bool(text))
            self.reply(200, {"text": text, "language": lang, "typed": typed})

    return Handler


def main() -> None:
    ensure_cuda_libs()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", action="store_true", help=f"print the settings in use (from {CONFIG_FILE}) and exit")
    ap.add_argument("--host", help="bind address (default: this machine's Tailscale IPv4)")
    ap.add_argument("--port", type=int)
    ap.add_argument("--model", help=f"faster-whisper model (default {DEFAULTS['model']})")
    ap.add_argument("--device", help="cuda, cpu or auto")
    ap.add_argument("--compute-type")
    ap.add_argument("--language", help="force a language for dictation (e.g. en, pt); default: auto-detect")
    ap.add_argument("--languages", help="comma-separated languages auto-detection may pick from (e.g. en,pt)")
    ap.add_argument("--min-level", type=float,
                    help="skip clips whose loudest 100 ms is quieter than this, in dBFS (default -34)")
    ap.add_argument("--prompt", help="initial prompt to bias vocabulary (names, jargon)")
    ap.add_argument("--trust", action="append", metavar="IP",
                    help="accept requests from this IP without a token (e.g. a Tailscale device); repeatable")
    ap.add_argument("--public-url", help="HTTPS address for devices outside the tailnet (shown in settings)")
    ap.add_argument("--no-type", action="store_true", help="only return the text, don't type it")
    ap.add_argument("--print-token", action="store_true", help="print the auth token and exit")
    args = ap.parse_args()

    token = load_token()
    if args.print_token:
        print(token)
        return

    cfg = load_config()
    for k in ("host", "port", "model", "device", "compute_type", "language", "min_level", "prompt", "public_url"):
        if getattr(args, k) is not None:
            cfg[k] = getattr(args, k)
    if args.languages is not None:
        cfg["languages"] = [l for l in args.languages.split(",") if l]
    if args.trust:
        cfg["trust"] = args.trust
    if args.no_type:
        cfg["type"] = False
    if args.config:
        print(json.dumps(cfg, indent=2))
        return

    if cfg["type"] and (tool := platform.missing_tool()):
        sys.exit(f"{tool} not found: install it (e.g. `sudo pacman -S {tool}`) or set \"type\": false")

    host = cfg["host"] or tailscale_ip() or "127.0.0.1"
    transcriber = Transcriber(cfg["model"], cfg["device"], cfg["compute_type"], cfg["language"] or None,
                              cfg["prompt"] or None, cfg["languages"], float(cfg["min_level"]))
    remote = Remote(StreamWatcher())
    remote.record_mode = cfg["record_mode"] if cfg["record_mode"] in RECORD_MODES else "toggle"
    remote.listen = {"pause": float(cfg["listen_pause"]), "chunk": float(cfg["listen_chunk"])}
    handler = make_handler(transcriber, remote, token, cfg["type"], set(cfg["trust"]),
                           {"local": f"http://{host}:{cfg['port']}", "public": cfg["public_url"]})
    server = ThreadingHTTPServer((host, int(cfg["port"])), handler)
    log(f"listening on http://{host}:{cfg['port']} ({platform.KIND}; config in {CONFIG_DIR})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
