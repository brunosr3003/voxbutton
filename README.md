# voxbutton

A floating microphone button for voice coding. Click it, talk, click again. Your
speech is transcribed by Whisper running locally on your own GPU and typed into
whatever window has focus, such as a terminal running Claude Code or your editor.

It's built for a setup where you drive a Linux box remotely (for example from a
Mac through Moonlight/Sunshine, which doesn't forward the microphone). The button
lives on the Linux desktop, and the Mac lends it its microphone:

```
 Linux PC (GPU)                                       Mac
┌───────────────────────────────┐    Tailscale   ┌──────────────────────┐
│ voxbutton-button (GTK)        │                │ VoxButton.app        │
│   click → /record/toggle      │                │ (no window; agent)   │
│ voxbutton-server              │ ── start ────▶ │ records the Mac mic  │
│   faster-whisper on the GPU   │ ◀── audio ──── │                      │
│   → wtype into focused window │                │                      │
└───────────────────────────────┘                └──────────────────────┘
```

- The button floats on every workspace. Clicking it hands keyboard focus
  straight back to the window you were typing in, so the text lands there.
- Transcription runs on your machine. No cloud APIs.
- Text is typed with `wtype` (Wayland virtual keyboard), so accents and Unicode
  come through correctly.
- Silence is skipped instead of being handed to Whisper, which likes to
  hallucinate on it.

## Linux server

Requirements: an NVIDIA GPU, [uv](https://docs.astral.sh/uv/), `wtype`, and a
Wayland compositor that supports the virtual keyboard protocol (Hyprland, Sway…).

```sh
sudo pacman -S wtype          # or your distro's equivalent
cd server
./run.sh                      # logs to ~/.local/state/voxbutton.log
uv run voxbutton-server --print-token
```

On first start it downloads the Whisper model (~1.6 GB). CUDA libraries come
from pip wheels, so you don't need a system CUDA toolkit. By default the server
binds to the machine's Tailscale IPv4 on port 8765 and requires the token
(generated in `~/.config/voxbutton/token`).

Start it from your compositor's autostart so `wtype` can reach the Wayland
session. For Hyprland:

```lua
hl.exec_cmd("/path/to/voxbutton/server/run.sh")   -- hyprland.lua
```
```ini
exec-once = /path/to/voxbutton/server/run.sh     # hyprland.conf
```

Useful options (`uv run voxbutton-server --help`):

| flag | default | |
|---|---|---|
| `--model` | `large-v3-turbo` | any faster-whisper model (`small`, `medium`, `large-v3`…) |
| `--language` | auto | force a language, e.g. `en` or `pt` |
| `--languages` | any | languages auto-detection may pick, e.g. `en,pt` |
| `--min-level` | `-34` | clips quieter than this (dBFS, loudest 100 ms) count as silence |
| `--prompt` | none | bias the vocabulary: project names, jargon |
| `--host` / `--port` | Tailscale IP / 8765 | |
| `--no-type` | | only return the text |
| `--trust IP` | | accept this IP without a token (repeatable) |

## Linux button

Needs Python with GTK 4 bindings (`python-gobject`) and Hyprland. The script
registers its own window rules at runtime (float, pin, no focus on open or on
hover), so the Hyprland config isn't touched. It can't use Hyprland's `no_focus`,
because windows with that rule receive no clicks.

```sh
linux/voxbutton-button.py            # right edge, vertically centered
linux/voxbutton-button.py --x 20 --y "monitor_h-84"   # bottom-left
```

States: dark = ready, red (pulsing) = recording, orange = transcribing,
green = typed, purple = error, faded gray = no microphone agent connected.

For Hyprland autostart, next to the server:

```lua
hl.exec_cmd("/path/to/voxbutton/linux/voxbutton-button.py")
```

## Mac app

Requirements: macOS 14+ and the Xcode command line tools.

```sh
cd mac
./build.sh --install          # builds, copies to ~/Applications and launches
```

Create `~/.config/voxbutton/config.json`:

```json
{
  "server": "http://100.x.y.z:8765",
  "token": "<output of --print-token>",
  "language": "en"
}
```

`language` is optional. Set it if auto-detection picks the wrong language, which
can happen with an accent. By default the app has no window. It waits for the
Linux button in the background. Add `"button": true` to get a floating button on
the Mac as well.

Using it:

- **Click** to start recording. The button turns red, and a halo shows your mic level.
- **Click again** to send. Orange means it's transcribing. Green means the text was
  typed. Purple means something failed (see Console.app, filter `voxbutton`).
- **Drag** to move it. Its position is remembered.
- **Right-click** for Quit.

The first recording asks for microphone permission. To start it at login, add
VoxButton to System Settings → General → Login Items.

## iPhone (iOS Shortcut)

Moonlight doesn't forward the phone's microphone, but a Shortcut can record and
send audio on its own, even while Moonlight stays in the foreground.

iOS Shortcuts insists on HTTPS, so put the server behind a TLS reverse proxy.
For example, use nginx with a Let's Encrypt certificate on a VPS, reaching the
PC through `ssh -R 127.0.0.1:18765:<pc-ip>:8765 vps`. Pass the token in the URL
so the Shortcut needs no custom headers.

Create a Shortcut with two actions:

1. **Record Audio**. Set Start Recording to *Immediately*, and Finish Recording
   to *On Tap*.
2. **Get Contents of URL**. URL `https://your.domain/transcribe?token=<token>`,
   Method *POST*, Request Body *File*, file = *Recorded Audio*.

Then bind it to Settings → Accessibility → Touch → **Back Tap** (double tap), or
to the Action Button. Double-tap the back of the phone, talk, tap to stop, and
the text is typed on the PC. The server accepts `.m4a` as well as WAV.

## API

`POST /transcribe` with the audio file as the body and
`Authorization: Bearer <token>` (or `?token=<token>`). Optional headers: `X-Language: en` and
`X-Type: 0` (don't type, just return the text). The response is
`{"text": "...", "language": "en", "typed": true}`.

```sh
curl -H "Authorization: Bearer $TOKEN" --data-binary @clip.wav http://host:8765/transcribe
```
