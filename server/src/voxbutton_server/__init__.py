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
    def __init__(self, model: str, device: str, compute_type: str, language: str | None, prompt: str | None):
        from faster_whisper import WhisperModel

        t = time.time()
        self.model = WhisperModel(model, device=device, compute_type=compute_type)
        self.language = language
        self.prompt = prompt
        self.lock = threading.Lock()
        log(f"model {model} loaded on {device} in {time.time() - t:.1f}s")

    def __call__(self, audio: bytes, language: str | None) -> tuple[str, str]:
        with self.lock:
            segments, info = self.model.transcribe(
                io.BytesIO(audio),
                language=language or self.language,
                initial_prompt=self.prompt,
                vad_filter=True,
                beam_size=5,
            )
            text = " ".join(s.text.strip() for s in segments).strip()
        return text, info.language


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def make_handler(transcribe: Transcriber, token: str, do_type: bool):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, code: int, body: dict) -> None:
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def authorized(self) -> bool:
            got = self.headers.get("Authorization", "")
            return secrets.compare_digest(got, f"Bearer {token}")

        def do_GET(self):
            if self.path == "/health":
                self.reply(200, {"ok": True})
            else:
                self.reply(404, {"error": "not found"})

        def do_POST(self):
            if not self.path.startswith("/transcribe"):
                return self.reply(404, {"error": "not found"})
            if not self.authorized():
                return self.reply(401, {"error": "bad token"})
            length = int(self.headers.get("Content-Length") or 0)
            if not 0 < length <= MAX_BODY:
                return self.reply(413, {"error": "empty or too large"})
            audio = self.rfile.read(length)
            language = self.headers.get("X-Language") or None
            t = time.time()
            try:
                text, lang = transcribe(audio, language)
            except Exception as e:  # bad audio, CUDA hiccup...
                log(f"transcribe failed: {e}")
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
                    return self.reply(500, {"error": f"wtype failed: {e}", "text": text})
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
    ap.add_argument("--prompt", help="initial prompt to bias vocabulary (names, jargon)")
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
    transcriber = Transcriber(args.model, args.device, args.compute_type, args.language, args.prompt)
    server = ThreadingHTTPServer((host, args.port), make_handler(transcriber, token, not args.no_type))
    log(f"listening on http://{host}:{args.port} (token in {TOKEN_FILE})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
