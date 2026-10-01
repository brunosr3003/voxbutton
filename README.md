# voxbutton

Voice coding with a floating two-button widget. Tap the **mic** and talk, and
your words are typed into whatever window has focus, such as a terminal running
Claude Code or your editor. Tap **`>_`** and say a command ("next tab", "send",
"open claude code"), and it runs as soon as you pause. Speech is transcribed by
Whisper on your own GPU, with no cloud APIs. An optional local model shows you
a corrected version of your English.

It's built for driving a Linux or Windows PC remotely, for example from a Mac or
an iPhone through Moonlight/Sunshine, which don't forward the microphone. The
widget lives on the PC, and the device you're streaming to lends it its mic:

```
 PC with a GPU (Linux or Windows)                     Mac / iPhone / laptop
┌──────────────────────────────────┐    Tailscale   ┌───────────────────────┐
│ widget: mic · >_ · gear (GTK/Tk) │                │ mic agent             │
│ voxbutton-server                 │ ── start ────▶ │ (Mac app, iOS app,    │
│   picks the mic you stream to    │ ◀── audio ──── │  or Linux/Windows     │
│   faster-whisper on the GPU      │                │  Python agent)        │
│   → types / runs the command     │                └───────────────────────┘
│ Ollama (optional English fixes)  │
└──────────────────────────────────┘
```

- The widget floats on every workspace and never keeps keyboard focus: what
  you say lands in the window you were typing in.
- The server sends each recording to the device that's receiving the Moonlight
  stream, so the mic in front of you is the one that listens.
- Text is typed through a virtual keyboard (`wtype`, `xdotool` or Windows
  SendInput), so accents and Unicode come through correctly.
- Silence is skipped instead of being handed to Whisper, which likes to make
  up text on it.
- Everything is configured from one file and a settings window, and it all
  starts with the computer.

## Contents

- [Using it](#using-it): the buttons, recording modes, voice commands, the English corrector
- [Server setup (Linux or Windows)](#server-setup-linux-or-windows)
- [Configuration](#configuration)
- [Microphones](#microphones): Mac app, iPhone app, Linux/Windows agent, iOS Shortcut
- [Settings window](#settings-window)
- [API](#api)

## Using it

### The widget

| part | tap | |
|---|---|---|
| **mic** (top) | dictate | the text is typed where you were |
| **`>_`** (bottom) | voice command | runs when you pause; right-click anywhere does the same |
| **gear** | settings | hold it and move to drag the widget; it remembers the spot |

Colors: dark = ready, red (pulsing) = dictating, blue = listening for a
command, teal = always-on listening, orange = transcribing, green = done,
purple = error, faded = no microphone connected.

### Recording modes

Pick one in settings → General → Recording:

| mode | how it works |
|---|---|
| **Toggle** (default) | tap to start, tap again to send |
| **Hold** | push-to-talk: it records while you hold the button |
| **Always on** | tap once and it keeps listening, typing what you say as you go: at every pause (0.6 s), and every few seconds (6 s) while you keep talking, cut between words. Start a sentence with "command" ("command next tab") or tap `>_` to make the next phrase a command. Tap the mic again to stop. |

Voice commands never need a second tap: the recording ends by itself when you
pause, or 4 s after you start talking. Pause detection follows the room's
background noise, so voices nearby don't keep it open.

### Voice commands

Commands are English only. For dictation, set `"language": "en"` in the
config to keep it English; auto-detection can switch languages and translate
what you said.

| say | does |
|---|---|
| `next tab` / `previous tab` | ctrl+Tab / ctrl+shift+Tab |
| `new tab` / `close tab` | per app (ctrl+shift+t / ctrl+shift+q in terminals, ctrl+t / ctrl+w elsewhere) |
| `tab 3` | alt+3 (browsers), ctrl+alt+3 (Windows Terminal) |
| `kill all tabs` | closes the focused window with all its tabs; kitty and browsers ask first, say `yes` |
| `open claude code` | new terminal window running Claude Code (kitty when installed; Windows Terminal on Windows) |
| `workspace 2` | switch workspace (Hyprland, X11) |
| `window left` / `focus right` | move focus between windows (Hyprland) |
| `enter`, `send`, `yes`, `confirm` | Enter |
| `escape`, `cancel`, `stop` | Escape (interrupts Claude Code) |
| `delete that`, `delete`, `undo` | erase the last dictation |
| `clear line`, `backspace` | |
| `scroll up` / `scroll down` | Page Up/Down (shift+ in terminals) |
| `copy`, `paste`, `select all` | per app |
| `go back`, `go forward`, `reload` | browser navigation |
| `up`, `down`, `left`, `right` | arrow keys |
| `option 2` | types "2" (Claude Code menus) |
| `type <anything>` | types the rest verbatim |

- **Chain them:** "up, down, send" or "next tab then send" run in order. Every
  part has to be a known command before any of them runs, so a half-understood
  sentence does nothing.
- **Repeat:** add "N times" ("next tab 3 times").
- Whisper often hears "Claude" as "cloud", so "open cloud code" works too.
- Each command is one small function in `server/src/voxbutton_server/commands.py`.

### English corrector

Optional, for non-native speakers. After each dictation, a card at the top of
the screen shows what you said and a corrected, more natural version:

- **red** card: "✗ You said / ✓ Better" when it fixed something;
- **green** card: "✓ Looks good" when you said it right (only if
  `corrector_show` is `always`).

What gets typed never changes; the card is there for you to learn from and
fix your sentence. It doesn't take focus when it appears. It stays until you
close it (✕, or a click on it) or your next phrase replaces it; a phrase that
needs no fixing closes the old card. Set `card_seconds` to close it on a timer
instead (that many seconds plus half a second per word).

It runs a small local model through [Ollama](https://ollama.com) on the same
GPU: about 2.5 GB of VRAM, 0.1–0.2 s per sentence once loaded. It unloads after
15 idle minutes and is warmed up again as soon as you start dictating.

```sh
ollama pull qwen2.5:3b        # the default; any Ollama model works ("corrector_model")
```

Turn it on with the switch in settings → General → English corrector, or
`"corrector": true` in `server.json`.

## Server setup (Linux or Windows)

Runs on the machine you type on. It needs [uv](https://docs.astral.sh/uv/) and,
for speed, an NVIDIA GPU. CUDA libraries come from pip wheels, so you don't
need a CUDA toolkit. Without a GPU it falls back to the CPU, where a smaller
model such as `small` is the better pick. On first start it downloads the
Whisper model (~1.6 GB) and creates the auth token.

| desktop | types with | widget | start it |
|---|---|---|---|
| Hyprland | `wtype` (`sudo pacman -S wtype`) | `linux/voxbutton-button.py` (GTK 4) | `linux/voxbutton-session.sh` |
| X11 (GNOME/KDE on Xorg, i3…) | `xdotool` | `desktop/voxbutton_tk.py` (Tk) | `linux/start-x11.sh` |
| Windows 10/11 | built-in (SendInput) | `desktop/voxbutton_tk.py` (Tk) | `windows\start.bat` |

**Start with the computer:** switch it on in settings → General, or run
`desktop/autostart.py on`. On Hyprland that's a systemd user service
(`voxbutton.service`) that waits for the session and then starts the server and
the widget. Your Hyprland config is left alone, because saving it makes
Hyprland reload. On Windows it's a shortcut in the Startup folder; elsewhere,
an XDG autostart entry.

The Hyprland widget registers its own window rules at runtime (float, pin, no
focus on open or on hover). It can't use Hyprland's `no_focus`, because windows
with that rule receive no clicks. Clicking it hands focus straight back to the
window you were typing in.

## Configuration

Everything lives in one folder: `~/.config/voxbutton/` on Linux,
`%APPDATA%\voxbutton\` on Windows.

| file | |
|---|---|
| `server.json` | server settings (below); the settings window writes here |
| `token` | the auth token, created on first start |
| `config.json` | on a mic agent: the server address and token |
| `button-hyprland.json` / `button.json` | where you left the widget |

`server.json` keys, all optional:

| key | default | |
|---|---|---|
| `model` | `large-v3-turbo` | any faster-whisper model (`small`, `medium`, `large-v3`…) |
| `device` | `auto` | `cuda`, `cpu` or `auto` |
| `language` | `""` | force the dictation language, e.g. `en`; empty auto-detects (and may translate when it guesses wrong) |
| `languages` | `[]` | languages auto-detection may pick, e.g. `["en", "pt"]` |
| `min_level` | `-34` | clips quieter than this (dBFS, loudest 100 ms) count as silence |
| `prompt` | `""` | biases the vocabulary: project names, jargon |
| `record_mode` | `toggle` | `toggle`, `hold` or `always` |
| `listen_pause` | `0.6` | always on: seconds of quiet that send what you said |
| `listen_chunk` | `6` | always on: while you keep talking, send about this often (seconds) |
| `corrector` | `false` | show a corrected version of your English |
| `corrector_model` | `qwen2.5:3b` | the Ollama model for it |
| `corrector_url` | `http://127.0.0.1:11434` | where Ollama listens |
| `corrector_show` | `changes` | `changes`: card only when something was corrected · `always`: after every dictation |
| `card_seconds` | `0` | 0: the card stays until closed or replaced; else a timer (plus ½ s per word) |
| `host` / `port` | Tailscale IP / `8765` | where to listen |
| `trust` | `[]` | IPs accepted without the token, e.g. a phone's Tailscale address |
| `public_url` | `""` | HTTPS address shown in settings for devices outside the tailnet |
| `type` | `true` | `false` only returns the text |

For example:

```json
{
  "language": "en",
  "record_mode": "toggle",
  "corrector": true,
  "trust": ["100.64.0.6"],
  "public_url": "https://voice.example.com"
}
```

Command-line flags (`uv run voxbutton-server --help`) override the file for one
run. `--config` prints the settings in use, and `--print-token` prints the token.

## Microphones

Any of these can lend its mic. With several connected, a recording goes to the
one receiving the Moonlight stream: the server measures how much it sends to
each Tailscale peer, and the device you're watching gets megabits per second.
Without a stream, the Mac wins while Moonlight is in front on it, then the
others. A new recording always replaces a leftover one, so a restart in the
middle of a recording can't leave a device stuck.

The settings window (Devices → Connect a device) shows the addresses and the
token with copy buttons.

### Mac app

Requirements: macOS 14+ and the Xcode command line tools.

```sh
cd mac
./build.sh --install          # builds, copies to ~/Applications and launches
```

Save `~/.config/voxbutton/config.json`:

```json
{"server": "http://100.x.y.z:8765", "token": "..."}
```

It has no window: it waits in the background for the widget on the PC. Allow
the microphone the first time, and add it to System Settings → General → Login
Items to start it at login. `"button": true` in the config adds a floating
button on the Mac itself (click to record, drag to move, right-click to quit).

### iPhone app

`ios/build.sh` builds the agent app on a Mac with Xcode and installs it on a
connected iPhone. It needs a development provisioning profile for the app;
optional defaults for the server and token come from `~/.config/voxbutton/ios.json`
on the Mac. Open VoxButton on the phone, check the server (the public HTTPS
address works from anywhere) and token, and turn on **Lend microphone**. The
app keeps the mic open in the background, which shows the orange dot, so a tap
on the PC can start a recording while Moonlight is in front.

### Linux and Windows agent

One Python file with no dependencies, for a laptop or desktop you stream to.
On Linux it records with `parec`, `pw-record` or `arecord`, whichever is there.
On Windows it uses the built-in WinMM API. Save the same `config.json` (Windows:
`%APPDATA%\voxbutton\config.json`), then:

```sh
agent/voxbutton_agent.py              # try it in the foreground
agent/voxbutton_agent.py --install    # run at login: systemd user service / Startup folder
agent/voxbutton_agent.py --devices    # list microphones; pick one with --device
```

On Windows, `windows\start-agent.bat` runs it with the Python from python.org.
In the background it logs to `%APPDATA%\voxbutton\agent.log`.

### iOS Shortcut (no app)

A Shortcut can record and send audio without the app, while Moonlight stays in
the foreground. It types on the PC but doesn't work with the widget's buttons.
Shortcuts insists on HTTPS, so put the server behind a TLS reverse proxy (e.g.
nginx with a Let's Encrypt certificate on a VPS, reaching the PC through
`ssh -R 127.0.0.1:18765:<pc-ip>:8765 vps`) and pass the token in the URL:

1. **Record Audio**. Set Start Recording to *Immediately*, and Finish Recording
   to *On Tap*.
2. **Get Contents of URL**. URL `https://your.domain/transcribe?token=<token>`,
   Method *POST*, Request Body *File*, file = *Recorded Audio*.

Bind it to Settings → Accessibility → Touch → **Back Tap**, or to the Action
Button.

## Settings window

The gear opens it. On Hyprland it's GTK, and elsewhere Tk.

- **Devices:** the connected microphones, live, with which one is receiving
  the stream and which one a tap will record on, plus how to connect a new Mac,
  iPhone, Linux or Windows machine.
- **Commands:** every voice command, with examples.
- **General:** recording mode and always-on timing, start with the computer,
  the English corrector (on/off, when to show the card, timer, model),
  dictation languages and the silence threshold.

## API

All endpoints take `Authorization: Bearer <token>` or `?token=<token>`.

| endpoint | |
|---|---|
| `POST /transcribe` | audio (WAV, m4a…) in the body; types it, or runs it as a command with `?mode=command`. Headers: `X-Language: en`, `X-Type: 0` (don't type, just return the text). Returns `{"text", "language", "typed"}` or `{"text", "command"}` |
| `POST /record/toggle?mode=chat\|command` | what a tap on the widget does; also `/record/start` and `/record/stop` |
| `GET /state` | recording state, mode, latest correction (what the widget polls) |
| `GET /info` | devices, stream, settings, connection details, the command list |
| `POST /config` | change settings (local only) |
| `GET /agent/wait?name=&prio=&ips=` | mic agents long-poll here for `start` / `listen` / `once` / `stop` |
| `GET /health` | no token needed |

```sh
curl -H "Authorization: Bearer $TOKEN" --data-binary @clip.wav http://host:8765/transcribe
```

## License

MIT, see [LICENSE](LICENSE).
