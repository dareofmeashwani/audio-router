# Audio Router

Audio Router is a desktop app that routes **all live system audio** — *everything*
your computer is currently playing (call, music, video, anything) — to
**multiple output devices at the same time**.

It is captured automatically: there is **no source or input selection** in the
app. The only thing you choose is which output devices to route to.

You can:
- multi-select output devices — **the only selection the app needs**
- start/stop routing to all selected outputs at once

> Microphone input is intentionally **not** offered — this tool routes system
> playback audio, not mic audio.

## Live system-audio capture (per OS)

Install the OS-level audio libraries once:

```sh
make system-deps
```

The app then captures whatever the system is playing and routes it to your
selected outputs — automatically, with no source selection. How it works differs
per OS:

- **macOS** — no native loopback exists, so a virtual device (**BlackHole**) is
  used. `make system-deps` runs `brew install blackhole-2ch`. When you press
  **Start**, the app switches the system output to BlackHole (saving your current
  device) and routes BlackHole's audio to your selected outputs; on **Stop** your
  original output device is restored. No manual *Audio MIDI Setup* needed.
- **Windows** — WASAPI **loopback** (built into PortAudio, no driver needed). The
  app captures the current default output device's audio non-intrusively — it
  keeps playing there, and is also sent to your selected outputs. For *silent*
  routing (original device muted), install VB-CABLE and make it your default
  playback device.
- **Linux** — PulseAudio/PipeWire **monitor** source of the current default sink,
  captured non-intrusively. `make system-deps` installs `pulseaudio-utils` (for
  `pactl`) and the PortAudio runtime.

> macOS is the only platform that changes (and restores) the system output
> device, because BlackHole is a separate silent device. On Windows/Linux the
> loopback/monitor taps the active device without redirecting it.

## Tech stack

- Python
- PySide6 (UI)
- sounddevice + PortAudio (audio I/O)

## Project structure

```
AudioRouter/
├── requirements.txt
├── README.md
└── src/
    ├── main.py
    ├── list_devices.py
    └── nlistener/
        ├── __init__.py
        ├── audio_router.py
        └── ui.py
```

## Setup

```zsh
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

## Run

```zsh
python src/main.py
```

## Optional: list audio devices from terminal

```zsh
python src/list_devices.py
```

## Build executable

Install PyInstaller:

```zsh
pip install pyinstaller
```

Generate logo/icon assets:

```zsh
make logo
```

Build command (run from project root):

```zsh
pyinstaller --noconfirm --windowed --name AudioRouter --icon assets/icons/audio-router.icns --paths src src/main.py
```

Output binary:
- `dist/AudioRouter` (macOS/Linux)
- `dist/AudioRouter.exe` (Windows)

Generated logo/icon files:
- `assets/logo/audio-router-logo.svg` (vector source)
- `assets/icons/audio-router.png` (app icon, Linux fallback)
- `assets/icons/audio-router.ico` (Windows executable icon)
- `assets/icons/audio-router.icns` (macOS app bundle icon)

### Important cross-platform note

To support every OS, keep one shared codebase and build on each target OS:
- build on macOS for macOS executable
- build on Windows for Windows executable
- build on Linux for Linux executable

Cross-compiling GUI/audio apps is usually unreliable, so native-per-OS builds are recommended.

## Permissions / system notes

- macOS: allow microphone access when prompted.
- Linux: ensure audio stack is available (ALSA/PulseAudio/PipeWire).
- Windows: ensure input/output devices are enabled in system sound settings.
