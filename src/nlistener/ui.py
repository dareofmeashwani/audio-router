from __future__ import annotations

from pathlib import Path
from typing import List

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .audio_router import AudioRouter, ensure_microphone_access


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Audio Router - Route system audio to multiple outputs")
        self.resize(720, 520)
        icon_path = Path(__file__).resolve().parents[2] / "assets" / "icons" / "audio-router.png"
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))

        self.router = AudioRouter()
        self.output_devices = []
        self.has_system_audio: bool = False

        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setSpacing(12)

        self.source_hint = QLabel("")
        self.source_hint.setWordWrap(True)
        self.source_hint.setStyleSheet("color: #888;")
        layout.addWidget(self.source_hint)

        layout.addWidget(QLabel("Output Devices (select one or more)"))
        self.output_list = QListWidget()
        self.output_list.setSelectionMode(QListWidget.MultiSelection)
        layout.addWidget(self.output_list)

        controls = QHBoxLayout()
        self.refresh_btn = QPushButton("Refresh Devices")
        self.help_btn = QPushButton("How to capture system audio")
        self.start_btn = QPushButton("Start Routing")
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setEnabled(False)
        controls.addWidget(self.refresh_btn)
        controls.addWidget(self.help_btn)
        controls.addWidget(self.start_btn)
        controls.addWidget(self.stop_btn)
        layout.addLayout(controls)

        self.status_label = QLabel("Status: Idle")
        self.status_label.setAlignment(Qt.AlignLeft)
        layout.addWidget(self.status_label)

        self.setCentralWidget(root)

        self.refresh_btn.clicked.connect(self.refresh_devices)
        self.help_btn.clicked.connect(self.show_system_audio_help)
        self.start_btn.clicked.connect(self.start_routing)
        self.stop_btn.clicked.connect(self.stop_routing)

        # Live signal meter: polls the capture level while routing so the user
        # can see whether system audio is actually being captured.
        self._level_timer = QTimer(self)
        self._level_timer.setInterval(200)
        self._level_timer.timeout.connect(self._update_level)
        self._routing_outputs: List[int] = []

        self.refresh_devices()

    # ----- helpers -----------------------------------------------------------

    def refresh_devices(self) -> None:
        self.output_devices = self.router.list_output_devices()
        self.has_system_audio = self.router.has_system_audio_source()

        self.output_list.clear()
        for device in self.output_devices:
            item = QListWidgetItem(f"[{device.index}] {device.name}")
            item.setData(Qt.UserRole, device.index)
            self.output_list.addItem(item)

        if self.has_system_audio:
            self.source_hint.setText(
                "All audio currently playing on this system — calls, music, video, "
                "anything — is routed to every selected output device. Choose your "
                "outputs and press Start.\n"
                "Tip: if an app was already playing before you pressed Start, restart "
                "its playback so it follows the switch. Watch the signal meter below "
                "to confirm audio is being captured."
            )
        else:
            self.source_hint.setText(
                "No system-audio capture is set up yet. Click 'How to capture system "
                "audio' to enable it."
            )

        self.status_label.setText(f"Status: {len(self.output_devices)} output device(s) loaded")
        self._update_start_enabled()

    def _update_start_enabled(self) -> None:
        if self.router.is_running():
            return
        self.start_btn.setEnabled(self.has_system_audio)

    def selected_output_ids(self) -> List[int]:
        return [item.data(Qt.UserRole) for item in self.output_list.selectedItems()]

    def show_system_audio_help(self) -> None:
        QMessageBox.information(self, "Capture system audio", self.router.system_audio_setup_hint())

    # ----- actions -----------------------------------------------------------

    def start_routing(self) -> None:
        output_devices = self.selected_output_ids()
        if not output_devices:
            QMessageBox.warning(self, "No Output Selected", "Select one or more output devices.")
            return

        source = self.router.default_system_source()
        if source is None:
            QMessageBox.warning(self, "No System Audio", self.router.system_audio_setup_hint())
            return

        # Resolve microphone permission BEFORE opening the input stream. Opening
        # it while the macOS permission prompt is pending crashes the app.
        granted, status = ensure_microphone_access()
        if not granted:
            if status == "denied":
                QMessageBox.warning(
                    self,
                    "Microphone Access Needed",
                    "AudioRouter needs microphone access to capture system audio.\n\n"
                    "Enable it in System Settings → Privacy & Security → Microphone, "
                    "then try again.",
                )
            else:
                QMessageBox.warning(
                    self,
                    "Microphone Access Needed",
                    "Microphone access was not granted. Please allow access and try again.",
                )
            return

        try:
            self.router.start(source=source, output_devices=output_devices)
        except Exception as error:
            QMessageBox.critical(self, "Routing Failed", str(error))
            return

        self._routing_outputs = output_devices
        self._set_running_ui(True)
        self._level_timer.start()
        self._update_level()

    def stop_routing(self) -> None:
        self._level_timer.stop()
        try:
            self.router.stop()
        except Exception as error:
            QMessageBox.critical(self, "Stop Failed", str(error))
            return
        self._set_running_ui(False)
        self.status_label.setText("Status: Stopped")

    def _update_level(self) -> None:
        """Refresh the status line with a live capture-signal meter."""
        if not self.router.is_running():
            return
        level = self.router.input_level()
        blocks = min(10, int(level * 12))
        meter = "█" * blocks + "·" * (10 - blocks)
        signal = "no signal — is audio playing?" if level < 1e-3 else f"[{meter}]"
        self.status_label.setText(
            f"Status: Routing → outputs {self._routing_outputs}   signal {signal}"
        )

    def _set_running_ui(self, running: bool) -> None:
        self.stop_btn.setEnabled(running)
        self.refresh_btn.setEnabled(not running)
        if running:
            self.start_btn.setEnabled(False)
        else:
            self._update_start_enabled()

    def closeEvent(self, event) -> None:
        self._level_timer.stop()
        if self.router.is_running():
            self.router.stop()
        event.accept()


def run_audio_router() -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.show()
    app.exec()
