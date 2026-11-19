from __future__ import annotations

import os
import queue
import shutil
import struct
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np
import sounddevice as sd

# ---------------------------------------------------------------------------
# macOS: CoreAudio HAL helpers for switching the system default output device.
# Imported lazily so the rest of the app works on Windows/Linux without PyObjC.
# ---------------------------------------------------------------------------

def _load_coreaudio():
    """Return the CoreAudio module and objc module, or (None, None) if unavailable."""
    try:
        import CoreAudio as _ca
        import objc as _objc
        return _ca, _objc
    except ImportError:
        return None, None


def _ca_make_addr(ca, selector: int):
    return ca.AudioObjectPropertyAddress(
        selector,
        ca.kAudioObjectPropertyScopeGlobal,
        ca.kAudioObjectPropertyElementMain,
    )


def _ca_get_cfstring(ca, objc_mod, device_id: int, selector: int) -> Optional[str]:
    try:
        addr = _ca_make_addr(ca, selector)
        _, size = ca.AudioObjectGetPropertyDataSize(device_id, addr, 0, None, None)
        buf = bytearray(size)
        ca.AudioObjectGetPropertyData(device_id, addr, 0, b'', size, buf)
        ptr = struct.unpack_from('Q', buf, 0)[0]
        return str(objc_mod.objc_object(c_void_p=ptr)) if ptr else None
    except Exception:
        return None


def _ca_get_device_id(ca, selector: int) -> int:
    addr = _ca_make_addr(ca, selector)
    _, size = ca.AudioObjectGetPropertyDataSize(ca.kAudioObjectSystemObject, addr, 0, None, None)
    buf = bytearray(size)
    ca.AudioObjectGetPropertyData(ca.kAudioObjectSystemObject, addr, 0, b'', size, buf)
    return struct.unpack_from('I', buf, 0)[0]


def _ca_set_device_id(ca, selector: int, device_id: int) -> int:
    addr = _ca_make_addr(ca, selector)
    data = struct.pack('I', device_id)
    return ca.AudioObjectSetPropertyData(ca.kAudioObjectSystemObject, addr, 0, b'', 4, data)


def _ca_all_device_ids(ca) -> Tuple[int, ...]:
    addr = _ca_make_addr(ca, ca.kAudioHardwarePropertyDevices)
    _, size = ca.AudioObjectGetPropertyDataSize(ca.kAudioObjectSystemObject, addr, 0, None, None)
    buf = bytearray(size)
    ca.AudioObjectGetPropertyData(ca.kAudioObjectSystemObject, addr, 0, b'', size, buf)
    return struct.unpack_from(f'{size // 4}I', buf)


def _ca_find_device_id_by_name(ca, objc_mod, name: str) -> Optional[int]:
    for did in _ca_all_device_ids(ca):
        device_name = _ca_get_cfstring(ca, objc_mod, did, ca.kAudioObjectPropertyName)
        if device_name and device_name.lower() == name.lower():
            return did
    return None


@dataclass
class _SavedSystemOutput:
    """Snapshot of macOS system output device IDs taken before routing starts."""
    default_output_id: int
    default_system_id: int


# AVFoundation authorization status codes for AVMediaTypeAudio.
_AVF_NOT_DETERMINED = 0
_AVF_RESTRICTED = 1
_AVF_DENIED = 2
_AVF_AUTHORIZED = 3


def ensure_microphone_access(timeout: float = 60.0) -> Tuple[bool, str]:
    """macOS: make sure microphone (audio-input) permission is resolved BEFORE
    any input stream is opened.

    Capturing from a loopback device counts as microphone access under macOS
    TCC. If the permission is still "not determined" when PortAudio opens the
    input stream, the TCC prompt interrupts the in-flight CoreAudio call and the
    process crashes. Requesting access up front and waiting for the user's answer
    avoids that crash.

    Returns (granted, status). On non-macOS, or if AVFoundation is unavailable,
    returns (True, ...) so the caller proceeds as before.
    """
    if sys.platform != "darwin":
        return True, "not-macos"
    try:
        import AVFoundation as AVF
    except ImportError:
        return True, "avfoundation-unavailable"

    media = AVF.AVMediaTypeAudio
    status = AVF.AVCaptureDevice.authorizationStatusForMediaType_(media)
    if status == _AVF_AUTHORIZED:
        return True, "authorized"
    if status in (_AVF_DENIED, _AVF_RESTRICTED):
        return False, "denied"

    # Not determined: trigger the prompt and wait for the user's response. The
    # completion handler runs on a background GCD queue, so waiting here is safe.
    event = threading.Event()
    result = {"granted": False}

    def handler(granted) -> None:
        result["granted"] = bool(granted)
        event.set()

    AVF.AVCaptureDevice.requestAccessForMediaType_completionHandler_(media, handler)
    event.wait(timeout)
    return result["granted"], ("authorized" if result["granted"] else "denied")



# Substrings that identify a device as a system-audio capture source rather than
# a physical microphone. Matching is case-insensitive.
_LOOPBACK_NAME_HINTS = (
    "blackhole",
    "soundflower",
    "loopback",
    "vb-audio",
    "vb-cable",
    "cable output",
    "stereo mix",
    "wave out mix",
    "what u hear",
    "what you hear",
)
_MONITOR_NAME_HINTS = (
    ".monitor",
    "monitor of",
    "monitor source",
)

KIND_MICROPHONE = "Microphone / Input"
KIND_SYSTEM_LOOPBACK = "System Audio (loopback)"
KIND_SYSTEM_MONITOR = "System Audio (monitor)"


@dataclass
class AudioDevice:
    index: int
    name: str
    max_input_channels: int
    max_output_channels: int
    default_samplerate: float


@dataclass
class AudioSource:
    """A capturable source: a physical input, or a system-audio capture device."""

    index: int
    name: str
    channels: int
    samplerate: float
    kind: str
    # True when the source must be opened as a WASAPI loopback of an output
    # device (Windows). In that case `index` points at an *output* device.
    is_wasapi_loopback: bool = False

    @property
    def is_system_audio(self) -> bool:
        return self.kind in (KIND_SYSTEM_LOOPBACK, KIND_SYSTEM_MONITOR)


class _OutputWorker:
    def __init__(self, device_index: int, channels: int, samplerate: float, blocksize: int) -> None:
        self.device_index = device_index
        self.channels = channels
        self.samplerate = samplerate
        self.blocksize = blocksize
        self.queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=64)
        self._stream: sd.OutputStream | None = None
        self._lock = threading.Lock()
        self._pending = np.zeros((0, channels), dtype=np.float32)

    def start(self) -> None:
        # Cap how much audio may queue up on the output side. Input (loopback)
        # and output run on independent clocks; without a cap, clock drift lets
        # latency grow until playback lags badly. ~0.5 s is plenty of cushion.
        max_pending = int(self.samplerate * 0.5)

        def callback(outdata: np.ndarray, frames: int, _time, _status) -> None:
            while self._pending.shape[0] < frames:
                try:
                    chunk = self.queue.get_nowait()
                except queue.Empty:
                    break
                self._pending = np.vstack((self._pending, chunk))

            # Drop the oldest audio if we have drifted too far ahead, so output
            # stays close to real time instead of accumulating latency.
            if self._pending.shape[0] > max_pending:
                self._pending = self._pending[-max_pending:]

            if self._pending.shape[0] >= frames:
                outdata[:] = self._pending[:frames]
                self._pending = self._pending[frames:]
            else:
                available = self._pending.shape[0]
                if available > 0:
                    outdata[:available] = self._pending
                if available < frames:
                    outdata[available:] = 0
                self._pending = np.zeros((0, self.channels), dtype=np.float32)

        self._stream = sd.OutputStream(
            device=self.device_index,
            channels=self.channels,
            samplerate=self.samplerate,
            blocksize=self.blocksize,
            dtype="float32",
            callback=callback,
            # "high" gives PortAudio a larger safety buffer — far fewer dropouts
            # on real hardware than "low", at the cost of a little latency.
            latency="high",
        )
        self._stream.start()

    def push_chunk(self, chunk: np.ndarray, block: bool = False) -> None:
        if not block:
            with self._lock:
                if self.queue.full():
                    try:
                        self.queue.get_nowait()
                    except queue.Empty:
                        pass
                try:
                    self.queue.put_nowait(chunk)
                except queue.Full:
                    pass
            return
        # Backpressure mode (file playback): wait for room instead of dropping,
        # so decoding is throttled to playback speed. Bail out once the stream
        # is torn down by stop().
        while self._stream is not None:
            try:
                self.queue.put(chunk, timeout=0.1)
                return
            except queue.Full:
                continue

    def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None


class AudioRouter:
    def __init__(self) -> None:
        self._input_stream: sd.InputStream | None = None
        self._file_proc: subprocess.Popen | None = None
        self._file_thread: threading.Thread | None = None
        self._output_workers: Dict[int, _OutputWorker] = {}
        self._dispatcher_thread: threading.Thread | None = None
        self._dispatch_running = threading.Event()
        self._fanout_queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=128)
        self._blocking_fanout = False
        self._channels = 2
        self._samplerate = 48000.0
        self._blocksize = 256
        self._saved_system_output: Optional[_SavedSystemOutput] = None
        # Peak amplitude (0..1) of the most recent captured block, for the UI to
        # show whether the source is actually receiving audio.
        self._input_level = 0.0

    # ----- device discovery -------------------------------------------------

    @staticmethod
    def _wasapi_hostapi_index() -> Optional[int]:
        for index, hostapi in enumerate(sd.query_hostapis()):
            if "wasapi" in hostapi["name"].lower():
                return index
        return None

    @staticmethod
    def _classify_input(name: str) -> str:
        lowered = name.lower()
        if any(hint in lowered for hint in _LOOPBACK_NAME_HINTS):
            return KIND_SYSTEM_LOOPBACK
        if any(hint in lowered for hint in _MONITOR_NAME_HINTS):
            return KIND_SYSTEM_MONITOR
        return KIND_MICROPHONE

    @staticmethod
    def list_devices() -> List[AudioDevice]:
        devices = []
        for index, item in enumerate(sd.query_devices()):
            devices.append(
                AudioDevice(
                    index=index,
                    name=item["name"],
                    max_input_channels=int(item["max_input_channels"]),
                    max_output_channels=int(item["max_output_channels"]),
                    default_samplerate=float(item["default_samplerate"]),
                )
            )
        return devices

    @staticmethod
    def _is_loopback_device(name: str) -> bool:
        """True for virtual loopback/capture devices that must not appear as outputs."""
        lowered = name.lower()
        return any(hint in lowered for hint in (*_LOOPBACK_NAME_HINTS, *_MONITOR_NAME_HINTS))

    def list_output_devices(self) -> List[AudioDevice]:
        return [
            device for device in self.list_devices()
            if device.max_output_channels > 0 and not self._is_loopback_device(device.name)
        ]

    def list_sources(self) -> List[AudioSource]:
        """Every capturable source: physical inputs, monitor/loopback inputs, and
        (on Windows) output devices exposed as WASAPI loopback sources."""
        sources: List[AudioSource] = []
        devices = sd.query_devices()

        for index, item in enumerate(devices):
            if int(item["max_input_channels"]) > 0:
                sources.append(
                    AudioSource(
                        index=index,
                        name=item["name"],
                        channels=int(item["max_input_channels"]),
                        samplerate=float(item["default_samplerate"]),
                        kind=self._classify_input(item["name"]),
                    )
                )

        if sys.platform.startswith("win"):
            wasapi_index = self._wasapi_hostapi_index()
            if wasapi_index is not None:
                for index, item in enumerate(devices):
                    if item["hostapi"] == wasapi_index and int(item["max_output_channels"]) > 0:
                        sources.append(
                            AudioSource(
                                index=index,
                                name=item["name"],
                                channels=int(item["max_output_channels"]),
                                samplerate=float(item["default_samplerate"]),
                                kind=KIND_SYSTEM_LOOPBACK,
                                is_wasapi_loopback=True,
                            )
                        )
        return sources

    def has_system_audio_source(self) -> bool:
        return any(source.is_system_audio for source in self.list_sources())

    def default_system_source(self) -> Optional[AudioSource]:
        """The system-audio source to capture automatically (no UI selection),
        chosen per-OS so it follows the device the system is actually playing on:

        - macOS  : the loopback device (BlackHole); start() switches the system
                   output to it and restores on stop.
        - Windows: the WASAPI loopback of the current default output device
                   (non-intrusive — audio keeps playing on it).
        - Linux  : the PulseAudio/PipeWire monitor of the current default sink
                   (non-intrusive).
        """
        system_sources = [source for source in self.list_sources() if source.is_system_audio]
        if not system_sources:
            return None

        if sys.platform.startswith("win"):
            default_output = self._default_output_index()
            if default_output is not None:
                for source in system_sources:
                    if source.is_wasapi_loopback and source.index == default_output:
                        return source
            for source in system_sources:
                if source.is_wasapi_loopback:
                    return source
            return system_sources[0]

        if sys.platform.startswith("linux") or sys.platform.startswith("freebsd"):
            monitor_name = self._linux_default_monitor_name()
            if monitor_name:
                for source in system_sources:
                    if source.kind == KIND_SYSTEM_MONITOR and monitor_name.lower() in source.name.lower():
                        return source
            for source in system_sources:
                if source.kind == KIND_SYSTEM_MONITOR:
                    return source
            return system_sources[0]

        # macOS and anything else: prefer an explicit loopback device.
        for source in system_sources:
            if source.kind == KIND_SYSTEM_LOOPBACK:
                return source
        return system_sources[0]

    @staticmethod
    def _default_output_index() -> Optional[int]:
        """PortAudio index of the current default output device, if known."""
        try:
            default = sd.default.device
            output_index = default[1] if isinstance(default, (list, tuple)) else None
            if output_index is not None and output_index >= 0:
                return int(output_index)
        except Exception:
            pass
        return None

    @staticmethod
    def _linux_default_monitor_name() -> Optional[str]:
        """Best-effort: the monitor source name of the current default sink,
        via `pactl`. Returns None if pactl is unavailable."""
        if not shutil.which("pactl"):
            return None
        try:
            result = subprocess.run(
                ["pactl", "get-default-sink"],
                capture_output=True, text=True, timeout=2.0,
            )
            sink = result.stdout.strip()
            return f"{sink}.monitor" if sink else None
        except Exception:
            return None

    @staticmethod
    def system_audio_setup_hint() -> str:
        if sys.platform == "darwin":
            return (
                "No system-audio source found.\n\n"
                "macOS cannot capture app/system playback without a virtual audio "
                "device. Install BlackHole (free):\n"
                "    brew install blackhole-2ch\n\n"
                "That's all — the app switches the system output to BlackHole when "
                "you press Start and restores it when you press Stop. No manual "
                "Audio MIDI Setup needed."
            )
        if sys.platform.startswith("win"):
            return (
                "No system-audio source found.\n\n"
                "Pick a '(loopback)' source to capture whatever your speakers are "
                "playing. If none appear, enable 'Stereo Mix' in Windows sound "
                "settings, or install VB-CABLE."
            )
        return (
            "No system-audio source found.\n\n"
            "On Linux, select a 'Monitor of <device>' source to capture system "
            "playback. Ensure PulseAudio or PipeWire is running."
        )

    # ----- media files -------------------------------------------------------

    @staticmethod
    def ffmpeg_path() -> Optional[str]:
        return shutil.which("ffmpeg")

    @staticmethod
    def ffmpeg_available() -> bool:
        return AudioRouter.ffmpeg_path() is not None

    @staticmethod
    def ffmpeg_missing_hint() -> str:
        if sys.platform == "darwin":
            return "ffmpeg is required to play media files. Install it with:\n    brew install ffmpeg"
        if sys.platform.startswith("win"):
            return (
                "ffmpeg is required to play media files. Install it with:\n"
                "    winget install Gyan.FFmpeg\n"
                "or download from https://ffmpeg.org/download.html and add it to PATH."
            )
        return (
            "ffmpeg is required to play media files. Install it with your package\n"
            "manager, e.g.  sudo apt install ffmpeg"
        )

    # ----- format negotiation ------------------------------------------------

    @staticmethod
    def _candidate_samplerates(preferred: float, output_devices: List[int]) -> List[float]:
        device_defaults = [float(sd.query_devices(device_id)["default_samplerate"]) for device_id in output_devices]
        common_rates = [preferred, *device_defaults, 48000.0, 44100.0, 32000.0, 96000.0]
        candidates: List[float] = []
        for rate in common_rates:
            if rate <= 0 or rate in candidates:
                continue
            candidates.append(rate)
        return candidates

    def _input_extra_settings(self, source: AudioSource):
        if source.is_wasapi_loopback and hasattr(sd, "WasapiSettings"):
            try:
                return sd.WasapiSettings(loopback=True)
            except TypeError:
                # Older sounddevice/PortAudio without loopback support.
                return None
        return None

    def _find_compatible_config(
        self, source: AudioSource, output_devices: List[int], requested_channels: int
    ) -> Tuple[float, int]:
        channels = min(requested_channels, source.channels)
        if channels < 1:
            raise ValueError("Selected source does not expose any channels.")

        for device_id in output_devices:
            output_info = sd.query_devices(device_id)
            channels = min(channels, int(output_info["max_output_channels"]))
        if channels < 1:
            raise ValueError("One or more selected output devices do not support output channels.")

        extra = self._input_extra_settings(source)
        for samplerate in self._candidate_samplerates(source.samplerate, output_devices):
            try:
                sd.check_input_settings(
                    device=source.index,
                    channels=channels,
                    samplerate=samplerate,
                    dtype="float32",
                    extra_settings=extra,
                )
                for output_device in output_devices:
                    sd.check_output_settings(
                        device=output_device,
                        channels=channels,
                        samplerate=samplerate,
                        dtype="float32",
                    )
                return samplerate, channels
            except Exception:
                continue

        raise RuntimeError(
            "No common audio format found for the selected source and output devices. "
            "Try a different combination."
        )

    def _find_output_config(self, output_devices: List[int], requested_channels: int) -> Tuple[float, int]:
        """Pick a samplerate/channel count every output device accepts. Used for
        file playback, where ffmpeg resamples the file to match (no input device)."""
        channels = requested_channels
        for device_id in output_devices:
            channels = min(channels, int(sd.query_devices(device_id)["max_output_channels"]))
        if channels < 1:
            raise ValueError("One or more selected output devices do not support output channels.")

        preferred = float(sd.query_devices(output_devices[0])["default_samplerate"])
        for samplerate in self._candidate_samplerates(preferred, output_devices):
            try:
                for output_device in output_devices:
                    sd.check_output_settings(
                        device=output_device,
                        channels=channels,
                        samplerate=samplerate,
                        dtype="float32",
                    )
                return samplerate, channels
            except Exception:
                continue

        raise RuntimeError("No common audio format found for the selected output devices.")

    # ----- lifecycle ---------------------------------------------------------

    def is_running(self) -> bool:
        return self._input_stream is not None or self._file_proc is not None

    # ----- macOS system-output switching ------------------------------------

    def _switch_system_output_to_loopback(self, source: AudioSource) -> None:
        """On macOS, point the system output at the loopback device (BlackHole)
        so all app/system audio flows into our capture source. The previous
        output is saved and restored on stop(). No-op on other platforms or if
        the source is not a loopback device.

        Raises RuntimeError with an actionable message if switching is required
        but impossible, rather than silently routing no audio."""
        if sys.platform != "darwin" or not source.is_system_audio:
            return
        ca, objc_mod = _load_coreaudio()
        if ca is None:
            raise RuntimeError(
                "Cannot switch the macOS system output to the loopback device: "
                "the CoreAudio bindings are unavailable. Install "
                "'pyobjc-framework-CoreAudio' (included in requirements.txt). "
                "In a packaged app, ensure it was bundled."
            )

        loopback_id = _ca_find_device_id_by_name(ca, objc_mod, source.name)
        if loopback_id is None:
            # The capture device exists in PortAudio but not via CoreAudio by
            # this name; leave the output as-is (capture may still be set up).
            return

        try:
            saved = _SavedSystemOutput(
                default_output_id=_ca_get_device_id(ca, ca.kAudioHardwarePropertyDefaultOutputDevice),
                default_system_id=_ca_get_device_id(ca, ca.kAudioHardwarePropertyDefaultSystemOutputDevice),
            )
        except Exception as error:
            raise RuntimeError(f"Could not read the current system output device: {error}")

        # Don't switch (or save) if system output is already the loopback device.
        if saved.default_output_id == loopback_id:
            return

        err_output = _ca_set_device_id(ca, ca.kAudioHardwarePropertyDefaultOutputDevice, loopback_id)
        err_system = _ca_set_device_id(ca, ca.kAudioHardwarePropertyDefaultSystemOutputDevice, loopback_id)
        if err_output != 0 and err_system != 0:
            raise RuntimeError(
                "Failed to switch the macOS system output to the loopback device "
                f"(CoreAudio error {err_output})."
            )
        self._saved_system_output = saved

    def _restore_system_output(self) -> None:
        """Restore the macOS system output device saved before routing started."""
        saved = self._saved_system_output
        if saved is None:
            return
        self._saved_system_output = None

        ca, _ = _load_coreaudio()
        if ca is None:
            return
        try:
            _ca_set_device_id(ca, ca.kAudioHardwarePropertyDefaultOutputDevice, saved.default_output_id)
            _ca_set_device_id(ca, ca.kAudioHardwarePropertyDefaultSystemOutputDevice, saved.default_system_id)
        except Exception:
            pass

    def _start_outputs(
        self, output_devices: List[int], samplerate: float, channels: int, blocksize: int
    ) -> None:
        for device_id in output_devices:
            worker = _OutputWorker(
                device_index=device_id,
                channels=channels,
                samplerate=samplerate,
                blocksize=blocksize,
            )
            worker.start()
            self._output_workers[device_id] = worker

        self._dispatch_running.set()
        self._dispatcher_thread = threading.Thread(target=self._dispatch_loop, daemon=True)
        self._dispatcher_thread.start()

    def _enqueue_fanout(self, chunk: np.ndarray, block: bool) -> None:
        if not block:
            if self._fanout_queue.full():
                try:
                    self._fanout_queue.get_nowait()
                except queue.Empty:
                    pass
            try:
                self._fanout_queue.put_nowait(chunk)
            except queue.Full:
                pass
            return
        while self._dispatch_running.is_set():
            try:
                self._fanout_queue.put(chunk, timeout=0.1)
                return
            except queue.Full:
                continue

    def start(
        self,
        source: AudioSource,
        output_devices: List[int],
        channels: int = 2,
        blocksize: int = 1024,
    ) -> None:
        if not output_devices:
            raise ValueError("Please select at least one output device.")
        if self.is_running():
            raise RuntimeError("Audio router is already running.")

        unique_outputs = list(dict.fromkeys(output_devices))
        samplerate, compatible_channels = self._find_compatible_config(
            source=source,
            output_devices=unique_outputs,
            requested_channels=channels,
        )

        self._channels = compatible_channels
        self._samplerate = samplerate
        self._blocksize = blocksize
        self._output_workers = {}
        self._blocking_fanout = False
        self._input_level = 0.0

        try:
            self._switch_system_output_to_loopback(source)
            self._start_outputs(unique_outputs, samplerate, compatible_channels, blocksize)

            def callback(indata: np.ndarray, _frames: int, _time, _status) -> None:
                chunk = np.array(indata, dtype=np.float32, copy=True)
                if chunk.size:
                    self._input_level = float(np.max(np.abs(chunk)))
                self._enqueue_fanout(chunk, block=False)

            self._input_stream = sd.InputStream(
                device=source.index,
                channels=compatible_channels,
                samplerate=samplerate,
                blocksize=blocksize,
                dtype="float32",
                callback=callback,
                latency="high",
                extra_settings=self._input_extra_settings(source),
            )
            self._input_stream.start()
        except Exception:
            self.stop()
            raise

    def input_level(self) -> float:
        """Peak amplitude (0..1) of the most recently captured block. 0 while
        idle; stays ~0 if the source is not actually receiving audio."""
        return self._input_level if self.is_running() else 0.0

    def start_file(
        self,
        file_path: str,
        output_devices: List[int],
        channels: int = 2,
        blocksize: int = 512,
        on_finished: Optional[Callable[[], None]] = None,
    ) -> None:
        """Decode a media file (video or audio) with ffmpeg and route its audio,
        paced to real time, to every selected output device. No input device
        is involved — the file itself is the source."""
        if not output_devices:
            raise ValueError("Please select at least one output device.")
        if self.is_running():
            raise RuntimeError("Audio router is already running.")
        if not self.ffmpeg_available():
            raise RuntimeError(self.ffmpeg_missing_hint())
        if not os.path.isfile(file_path):
            raise ValueError(f"File not found: {file_path}")

        unique_outputs = list(dict.fromkeys(output_devices))
        samplerate, compatible_channels = self._find_output_config(unique_outputs, channels)

        self._channels = compatible_channels
        self._samplerate = samplerate
        self._blocksize = blocksize
        self._output_workers = {}
        self._blocking_fanout = True

        try:
            self._start_outputs(unique_outputs, samplerate, compatible_channels, blocksize)

            command = [
                self.ffmpeg_path(),
                "-nostdin",
                "-loglevel", "error",
                "-i", file_path,
                "-vn",
                "-f", "f32le",
                "-acodec", "pcm_f32le",
                "-ac", str(compatible_channels),
                "-ar", str(int(samplerate)),
                "-",
            ]
            self._file_proc = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
            )
            self._file_thread = threading.Thread(
                target=self._file_reader_loop,
                args=(compatible_channels, samplerate, on_finished),
                daemon=True,
            )
            self._file_thread.start()
        except Exception:
            self.stop()
            raise

    def _file_reader_loop(
        self, channels: int, samplerate: float, on_finished: Optional[Callable[[], None]]
    ) -> None:
        proc = self._file_proc
        if proc is None or proc.stdout is None:
            return

        read_frames = 1024
        bytes_needed = read_frames * channels * 4
        frames_emitted = 0
        start_time = time.monotonic()
        completed = False

        try:
            while self._dispatch_running.is_set():
                data = proc.stdout.read(bytes_needed)
                if not data:
                    completed = True
                    break

                samples = np.frombuffer(data, dtype=np.float32)
                usable = (samples.shape[0] // channels) * channels
                if usable == 0:
                    continue
                chunk = samples[:usable].reshape(-1, channels).copy()

                self._enqueue_fanout(chunk, block=True)
                frames_emitted += chunk.shape[0]

                # Pace decoding to real time so buffers neither starve nor overflow.
                remaining = (start_time + frames_emitted / float(samplerate)) - time.monotonic()
                while remaining > 0 and self._dispatch_running.is_set():
                    nap = min(0.05, remaining)
                    time.sleep(nap)
                    remaining -= nap
        finally:
            if completed:
                # Let the already-queued tail drain before signalling completion.
                deadline = time.monotonic() + 5.0
                while self._dispatch_running.is_set() and time.monotonic() < deadline:
                    if self._fanout_queue.empty() and all(
                        worker.queue.empty() for worker in self._output_workers.values()
                    ):
                        break
                    time.sleep(0.05)
                time.sleep(0.2)
                if on_finished is not None and self._dispatch_running.is_set():
                    try:
                        on_finished()
                    except Exception:
                        pass

    def _dispatch_loop(self) -> None:
        while self._dispatch_running.is_set():
            try:
                chunk = self._fanout_queue.get(timeout=0.2)
            except queue.Empty:
                continue

            for worker in self._output_workers.values():
                worker.push_chunk(chunk, block=self._blocking_fanout)

    def stop(self) -> None:
        self._dispatch_running.clear()

        if self._input_stream is not None:
            self._input_stream.stop()
            self._input_stream.close()
            self._input_stream = None

        if self._file_proc is not None:
            try:
                self._file_proc.terminate()
                self._file_proc.wait(timeout=1.0)
            except Exception:
                try:
                    self._file_proc.kill()
                except Exception:
                    pass
            self._file_proc = None

        if self._file_thread is not None:
            if self._file_thread is not threading.current_thread():
                self._file_thread.join(timeout=1.0)
            self._file_thread = None

        if self._dispatcher_thread is not None:
            self._dispatcher_thread.join(timeout=1.0)
            self._dispatcher_thread = None

        for worker in self._output_workers.values():
            worker.stop()
        self._output_workers = {}
        self._blocking_fanout = False

        while not self._fanout_queue.empty():
            try:
                self._fanout_queue.get_nowait()
            except queue.Empty:
                break

        # Restore the macOS system output device to its pre-routing state.
        self._restore_system_output()
