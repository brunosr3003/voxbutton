# voxbutton

A floating microphone button for voice coding. Click it, talk, click again. Your
speech is transcribed by Whisper running locally on your own GPU and typed into
whatever window has focus, such as a terminal running Claude Code or your editor.

It's built for a setup where you drive a Linux or Windows PC remotely (for example from a
Mac through Moonlight/Sunshine, which doesn't forward the microphone). The button
lives on the PC's desktop, and the Mac (or an iPhone) lends it its microphone:

```
 PC with a GPU (Linux or Windows)                    Mac
┌───────────────────────────────┐    Tailscale   ┌──────────────────────┐
│ voxbutton button (GTK or Tk)  │                │ VoxButton.app        │
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

## Server (Linux or Windows)

Runs on the machine you type on. It needs [uv](https://docs.astral.sh/uv/) and,
for speed, an NVIDIA GPU (CUDA libraries come from pip wheels, so you don't need
a CUDA toolkit; without a GPU it falls back to the CPU, where a smaller model
such as `small` is the better pick). On first start it downloads the Whisper
model (~1.6 GB) and creates the auth token.

| desktop | types with | start |
|---|---|---|
| Hyprland / Wayland | `wtype` (`sudo pacman -S wtype`) | `server/run.sh` from the compositor's autostart |
| X11 (GNOME/KDE on Xorg, i3…) | `xdotool` | `linux/start-x11.sh` (server + button) |
| Windows 10/11 | built-in (SendInput) | `windows\start.bat`; `windows\install-startup.bat` makes it start with Windows |

For Hyprland:

```lua
hl.exec_cmd("/path/to/voxbutton/server/run.sh")   -- hyprland.lua
hl.exec_cmd("/path/to/voxbutton/linux/voxbutton-button.py")
```

### Configuration

Everything lives in one folder: `~/.config/voxbutton/` on Linux,
`%APPDATA%\voxbutton\` on Windows.

| file | |
|---|---|
| `server.json` | server settings (below); the settings window writes here |
| `token` | the auth token, created on first start |
| `button.json` | where you left the Windows/X11 button |

`server.json` keys, all optional:

| key | default | |
|---|---|---|
| `model` | `large-v3-turbo` | any faster-whisper model (`small`, `medium`, `large-v3`…) |
| `device` | `auto` | `cuda`, `cpu` or `auto` |
| `language` | `""` | force one dictation language, e.g. `en`; empty auto-detects |
| `languages` | `[]` | languages auto-detection may pick, e.g. `["en", "pt"]` |
| `min_level` | `-34` | clips quieter than this (dBFS, loudest 100 ms) count as silence |
| `prompt` | `""` | biases the vocabulary: project names, jargon |
| `host` / `port` | Tailscale IP / `8765` | where to listen |
| `trust` | `[]` | IPs accepted without the token, e.g. a phone's Tailscale address |
| `public_url` | `""` | HTTPS address shown in settings for devices outside the tailnet |
| `type` | `true` | `false` only returns the text |

For example:

```json
{
  "languages": ["en", "pt"],
  "trust": ["100.64.0.6"],
  "public_url": "https://voice.example.com"
}
```

Command-line flags (`uv run voxbutton-server --help`) override the file for one
run; `--config` prints the settings in use, `--print-token` the token.

## The button

On **Windows** and **X11** it's `desktop/voxbutton_tk.py` (Tk, ships with
Python; the start scripts above launch it). It never takes keyboard focus, you
drag it wherever you want, and it remembers the spot.

On **Hyprland** it's `linux/voxbutton-button.py`. It needs Python with GTK 4
bindings (`python-gobject`). The script
registers its own window rules at runtime (float, pin, no focus on open or on
hover), so the Hyprland config isn't touched. It can't use Hyprland's `no_focus`,
because windows with that rule receive no clicks.

```sh
linux/voxbutton-button.py            # right edge, vertically centered
linux/voxbutton-button.py --x 20 --y "monitor_h-84"   # bottom-left
```

**Left-click** dictates: the text is typed where you were. **Right-click**
records a **voice command** instead (the button turns blue while it listens):

| say | does |
|---|---|
| `next tab` / `previous tab` | ctrl+Tab / ctrl+shift+Tab |
| `new tab` / `close tab` | per app (ctrl+shift+t / ctrl+shift+q in terminals, ctrl+t / ctrl+w elsewhere) |
| `tab 3` | alt+3 (browsers) |
| `workspace 2` | switch Hyprland workspace |
| `window left` / `focus right` | move focus between windows |
| `enter`, `send` | Return |
| `escape`, `cancel`, `stop` | Escape |
| `delete that` | erase the last dictation |
| `clear line`, `backspace` | |
| `scroll up` / `scroll down` | Page Up/Down (shift+ in terminals) |
| `copy`, `paste`, `select all` | per app |
| `go back`, `go forward`, `reload` | browser navigation |
| `up`, `down`, `left`, `right` | arrow keys |
| `option 2` | types "2" (Claude Code menus) |
| `open claude code` | new kitty window running Claude Code (Windows: Windows Terminal) |
| `type <anything>` | types the rest verbatim |

Add `N times` to repeat (`next tab 3 times`). Commands are English only. They
live in `server/src/voxbutton_server/commands.py`, one small function each.

The **gear** in the button's corner opens the settings window: which devices
are connected and which one a click will record on, how to connect a new Mac
or iPhone (addresses and token with copy buttons), the voice commands, and the
dictation languages and silence threshold (saved in
`~/.config/voxbutton/settings.json`).

States: dark = ready, red (pulsing) = dictating, blue = listening for a command, orange = transcribing,
green = typed, purple = error, faded gray = no microphone agent connected.

For Hyprland autostart, next to the server:

```lua
hl.exec_cmd("/path/to/voxbutton/linux/voxbutton-button.py")
```

## Mic agent (Linux and Windows)

Lends a computer's microphone to the button, the way the Mac and iPhone apps
do, e.g. on a laptop where you run Moonlight. It's one Python file with no
dependencies. On Linux it records with `parec`, `pw-record` or `arecord`,
whichever is there. On Windows it uses the built-in WinMM API.

Save the server address and token (the settings window has them, with copy
buttons) as `config.json` in `~/.config/voxbutton/` (Windows:
`%APPDATA%\voxbutton\`):

```json
{"server": "http://<pc-tailscale-ip>:8765", "token": "..."}
```

```sh
agent/voxbutton_agent.py              # try it in the foreground
agent/voxbutton_agent.py --install    # run at login: systemd user service / Startup folder
agent/voxbutton_agent.py --devices    # list microphones; pick one with --device
```

On Windows, `windows\start-agent.bat` runs it with the Python from python.org.
In the background it logs to `%APPDATA%\voxbutton\agent.log`. It reports its
addresses to the server, so it's picked when it's the machine receiving the
stream.

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
`X-Type: 0` (don't type, just return the text), and `?mode=command` to run the
speech as a voice command instead of typing it. The response is
`{"text": "...", "language": "en", "typed": true}`.

```sh
curl -H "Authorization: Bearer $TOKEN" --data-binary @clip.wav http://host:8765/transcribe
```
