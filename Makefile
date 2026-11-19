PYTHON := .venv/bin/python
PIP := .venv/bin/pip
PYTHONPATH := src
UNAME_S := $(shell uname -s)

ifeq ($(OS),Windows_NT)
ICON_FILE := assets/icons/audio-router.ico
else ifeq ($(UNAME_S),Darwin)
ICON_FILE := assets/icons/audio-router.icns
else
ICON_FILE := assets/icons/audio-router.png
endif

.PHONY: help setup install system-deps logo run list-devices check build clean

help:
	@echo "Available targets:"
	@echo "  make setup         - Create virtual environment and install Python dependencies"
	@echo "  make install       - Install/upgrade Python dependencies in existing virtual environment"
	@echo "  make system-deps   - Install OS-level audio libraries needed for system-audio capture"
	@echo "  make logo          - Generate logo/icon assets (png, ico, icns)"
	@echo "  make run           - Run Audio Router"
	@echo "  make list-devices  - Print detected input/output audio devices"
	@echo "  make check         - Compile source files for a quick syntax check"
	@echo "  make build         - Build executable with PyInstaller"
	@echo "  make clean         - Remove build artifacts and caches"

setup: .venv install

.venv:
	python3 -m venv .venv

install:
	$(PIP) install --upgrade pip
	$(PIP) install -r requirements.txt

# Install the OS-level libraries required to capture system audio.
# macOS   : BlackHole virtual device (the app switches output to it on Start).
# Linux   : PortAudio runtime + pulseaudio-utils (pactl) for monitor capture.
# Windows : WASAPI loopback is built in; VB-CABLE is only needed for silent routing.
system-deps:
ifeq ($(OS),Windows_NT)
	@echo "Windows: WASAPI loopback is built into PortAudio - no system library required."
	@echo "Optional (route without hearing the original device): install VB-CABLE,"
	@echo "  e.g.  choco install vb-cable   (or download from vb-audio.com)"
else ifeq ($(UNAME_S),Darwin)
	@echo "Installing macOS system-audio dependency (BlackHole) via Homebrew..."
	brew install blackhole-2ch
else
	@echo "Installing Linux system-audio dependencies..."
	@if command -v apt-get >/dev/null 2>&1; then \
		sudo apt-get update && sudo apt-get install -y libportaudio2 pulseaudio-utils; \
	elif command -v dnf >/dev/null 2>&1; then \
		sudo dnf install -y portaudio pulseaudio-utils; \
	elif command -v pacman >/dev/null 2>&1; then \
		sudo pacman -S --needed --noconfirm portaudio libpulse; \
	elif command -v zypper >/dev/null 2>&1; then \
		sudo zypper install -y libportaudio2 pulseaudio-utils; \
	else \
		echo "Unknown package manager. Please install PortAudio and pulseaudio-utils (pactl) manually."; \
	fi
endif

logo:
	$(PIP) install pillow
	$(PYTHON) tools/generate_logo_assets.py

run:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) src/main.py

list-devices:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) src/list_devices.py

check:
	$(PYTHON) -m compileall src

build: logo
	$(PIP) install pyinstaller
ifeq ($(OS),Windows_NT)
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m PyInstaller --noconfirm --clean --specpath build --windowed --name AudioRouter --icon $(ICON_FILE) --paths src src/main.py
else ifeq ($(UNAME_S),Darwin)
	# Build from the maintained spec so the microphone usage description and
	# bundle identifier (required to capture system audio) are baked in.
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m PyInstaller --noconfirm --clean AudioRouter.spec
else
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) -m PyInstaller --noconfirm --clean --specpath build --windowed --name AudioRouter --icon $(ICON_FILE) --paths src src/main.py
endif

clean:
	rm -rf build dist
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
