"""voxbutton server: receives audio over HTTP, transcribes it with Whisper on
the local GPU and types the text into the focused Wayland window."""

import argparse
import glob
import io
import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "voxbutton"
TOKEN_FILE = CONFIG_DIR / "token"
MAX_BODY = 50 * 1024 * 1024  # ~25 min of 16 kHz mono WAV


def ensure_cuda_libs() -> None:
    """cuBLAS/cuDNN come from pip wheels; ctranslate2 only finds them through
    LD_LIBRARY_PATH, which has to be set before the process starts."""
    if os.environ.get("VOXBUTTON_REEXEC"):
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


def tailscale_ip() -> str | None:
    try:
        out = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=5)
        ip = out.stdout.strip().splitlines()
        return ip[0] if out.returncode == 0 and ip else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def type_text(text: str) -> None:
    subprocess.run(["wtype", "--", text], check=True, timeout=30)


class Transcriber:
    def __init__(self, model: str, device: str, compute_type: str, language: str | None, prompt: str | None,
                 languages: list[str], min_level: float):
        from faster_whisper import WhisperModel

        t = time.time()
        self.model = WhisperModel(model, device=device, compute_type=compute_type)
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

    def __call__(self, audio: bytes, language: str | None) -> tuple[str, str]:
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
                initial_prompt=self.prompt,
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


class Remote:
    """Lets a button on this machine drive a microphone on another one.

    Mic agents (the Mac and iOS apps) long-poll /agent/wait for "start"/"stop"
    and post the recording to /transcribe like any other client; the button
    calls /record/toggle and polls /state to show what's happening.

    Each agent says how good a pick it is right now (its priority): the Mac
    reports 2 while Moonlight is in front and 0 otherwise, the iPhone always 1.
    A recording goes to the best one, and its stop goes to the same agent."""

    AGENT_TIMEOUT = 35  # s without a poll before the agent counts as gone
    MAX_RECORDING = 300  # s, safety net if the stop never arrives

    def __init__(self):
        self.cond = threading.Condition()
        self.state = "idle"  # idle | recording | busy
        self.since = time.time()
        self.agents: dict[str, dict] = {}  # name -> {"seen", "prio", "gen"}
        self.cmds: dict[str, str] = {}
        self.active: str | None = None
        self.flash = ("", 0.0)  # ("done" | "error", when)

    def _set(self, state: str) -> None:
        self.state, self.since = state, time.time()

    def _alive(self) -> dict[str, dict]:
        now = time.time()
        return {n: a for n, a in self.agents.items() if now - a["seen"] < self.AGENT_TIMEOUT}

    def _best(self) -> str | None:
        ready = [(a["prio"], a["seen"], n) for n, a in self._alive().items() if a["prio"] > 0]
        return max(ready)[2] if ready else None

    def wait(self, name: str, prio: int, timeout: float = 25) -> str | None:
        with self.cond:
            a = self.agents.setdefault(name, {"gen": 0})
            a["gen"] += 1  # a newer poll from the same agent supersedes this one
            gen = a["gen"]
            a["seen"], a["prio"] = time.time(), prio
            self.cond.notify_all()
            self.cond.wait_for(lambda: a["gen"] != gen or name in self.cmds, timeout)
            a["seen"] = time.time()
            if a["gen"] != gen:
                return None
            return self.cmds.pop(name, None)

    def toggle(self) -> str:
        with self.cond:
            if self.state == "idle":
                best = self._best()
                if not best:
                    self.flash = ("error", time.time())
                    return "no microphone available" if self._alive() else "no microphone agent connected"
                self.active = best
                self.cmds[best] = "start"
                self._set("recording")
                log(f"recording on {best}")
            elif self.state == "recording" and self.active:
                self.cmds[self.active] = "stop"
                self._set("busy")
            self.cond.notify_all()
            return ""

    def finished(self, ok: bool) -> None:
        with self.cond:
            if self.state != "idle":
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
                "flash": kind if now - at < 1.2 else "",
            }


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def make_handler(transcribe: Transcriber, remote: Remote, token: str, do_type: bool, trusted: set[str]):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def log_error(self, fmt, *args):
            # Surfaces e.g. a client speaking TLS to this plain-HTTP port.
            log(f"{self.client_address[0]}: " + fmt % args)

        def reply(self, code: int, body: dict) -> None:
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

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
            elif path == "/agent/wait":
                q = parse_qs(urlsplit(self.path).query)
                name = q.get("name", ["mac"])[0][:32]
                try:
                    prio = int(q.get("prio", ["1"])[0])
                except ValueError:
                    prio = 1
                self.reply(200, {"cmd": remote.wait(name, prio)})
            else:
                self.reply(404, {"error": "not found"})

        def do_POST(self):
            path = urlsplit(self.path).path
            if path not in ("/transcribe", "/record/toggle", "/agent/error"):
                return self.reply(404, {"error": "not found"})
            if not self.authorized():
                return self.reply(401, {"error": "bad token"})
            if path == "/record/toggle":
                err = remote.toggle()
                return self.reply(503 if err else 200, {"error": err} if err else remote.snapshot())
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
                remote.finished(ok)

        def transcribe(self):
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 < length <= MAX_BODY:
                log(f"rejected body: length={length} type={self.headers.get('Content-Type')}")
                self.finished(False)
                return self.reply(413, {"error": "empty or too large"})
            audio = self.rfile.read(length)
            language = self.headers.get("X-Language") or None
            t = time.time()
            try:
                text, lang = transcribe(audio, language)
            except Exception as e:  # bad audio, CUDA hiccup...
                log(f"transcribe failed: {e}")
                self.finished(False)
                return self.reply(500, {"error": str(e)})
            log(f"[{lang}] {time.time() - t:.2f}s: {text!r}")
            typed = False
            if text and do_type and self.headers.get("X-Type", "1") != "0":
                try:
                    # Trailing space so consecutive dictations don't glue together.
                    type_text(text + " ")
                    typed = True
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
    ap.add_argument("--host", help="bind address (default: this machine's Tailscale IPv4)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--model", default="large-v3-turbo")
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--compute-type", default="float16")
    ap.add_argument("--language", help="force a language (e.g. en, pt); default: auto-detect")
    ap.add_argument("--languages", default="",
                    help="comma-separated languages auto-detection may pick from (e.g. en,pt)")
    ap.add_argument("--min-level", type=float, default=-34,
                    help="skip clips whose loudest 100 ms is quieter than this, in dBFS (default -34)")
    ap.add_argument("--prompt", help="initial prompt to bias vocabulary (names, jargon)")
    ap.add_argument("--trust", action="append", default=[], metavar="IP",
                    help="accept requests from this IP without a token (e.g. a Tailscale device); repeatable")
    ap.add_argument("--no-type", action="store_true", help="only return the text, don't type it")
    ap.add_argument("--print-token", action="store_true", help="print the auth token and exit")
    args = ap.parse_args()

    token = load_token()
    if args.print_token:
        print(token)
        return
    if not args.no_type and not shutil.which("wtype"):
        sys.exit("wtype not found: install it (e.g. `sudo pacman -S wtype`) or pass --no-type")

    host = args.host or tailscale_ip() or "127.0.0.1"
    transcriber = Transcriber(args.model, args.device, args.compute_type, args.language, args.prompt,
                              [l for l in args.languages.split(",") if l], args.min_level)
    server = ThreadingHTTPServer((host, args.port), make_handler(transcriber, Remote(), token, not args.no_type, set(args.trust)))
    log(f"listening on http://{host}:{args.port} (token in {TOKEN_FILE})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
