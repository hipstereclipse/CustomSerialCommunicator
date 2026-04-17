"""
TurboWindow — independent QMainWindow for the Pfeiffer TC600 controller.

Architecturally separate from the gauge workspace: turbos are Pfeiffer products,
not INFICON gauges.  This window has its own connection management and does not
share state with the gauge main window.

Layout:
  ┌─────────────────────────────────────────┐
  │  Port: [COM??▼]  Addr: [1]  [Connect]  │
  ├─────────────────────────────────────────┤
  │  Speed gauge (0 … max)                  │
  │  Current: 0 A    Power: 0 W             │
  ├─────────────────────────────────────────┤
  │  [Start Pump]  [Stop Pump]  [Vent]      │
  │  [Standby ON]  [Ack Error]              │
  ├─────────────────────────────────────────┤
  │  Error code: no Err                     │
  │  Op hours: 0 h   Firmware: ---          │
  └─────────────────────────────────────────┘
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import serial.tools.list_ports
from PyQt6.QtCore import Qt, QSettings, pyqtSlot
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QSpinBox, QComboBox,
    QGroupBox, QFormLayout, QStatusBar, QProgressBar,
    QSizePolicy,
)

from serial_comm.turbos.tc600_protocol import TC600Protocol, ERROR_DESCRIPTIONS
from serial_comm.turbos.turbo_worker import TurboWorker, TurboStatus
from serial_comm.models import DeviceError
from serial_comm.transport import TransportConfig

logger = logging.getLogger(__name__)

# TC600 max rated speed varies by pump; 1500 Hz is the TC600 drive unit max
_MAX_SPEED_HZ = 1500


class TurboWindow(QMainWindow):
    """
    Standalone window for Pfeiffer TC600 turbo pump control.

    Opened from the main window via Devices → Open Turbo Controller.
    Can be closed independently; does not destroy the main window.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Pfeiffer TC600 Turbo Controller")
        self.resize(520, 460)
        self._settings = QSettings()

        self._worker: TurboWorker | None = None
        self._protocol: TC600Protocol | None = None
        self._connected = False

        self._build_ui()
        self._populate_ports()

    # ------------------------------------------------------------------
    # Build UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setSpacing(6)

        # --- Connection bar ---
        conn_grp = QGroupBox("Connection")
        conn_form = QFormLayout(conn_grp)

        port_row = QHBoxLayout()
        self._port_combo = QComboBox()
        port_row.addWidget(self._port_combo)

        refresh_btn = QPushButton("⟳")
        refresh_btn.setFixedWidth(30)
        refresh_btn.clicked.connect(self._populate_ports)
        port_row.addWidget(refresh_btn)
        conn_form.addRow("Port:", port_row)

        self._addr_spin = QSpinBox()
        self._addr_spin.setRange(1, 255)
        self._addr_spin.setValue(1)
        conn_form.addRow("Address:", self._addr_spin)

        self._connect_btn = QPushButton("Connect")
        self._connect_btn.clicked.connect(self._on_connect_toggle)
        conn_form.addRow("", self._connect_btn)
        root.addWidget(conn_grp)

        # --- Speed display ---
        speed_grp = QGroupBox("Pump Status")
        speed_layout = QVBoxLayout(speed_grp)

        self._speed_bar = QProgressBar()
        self._speed_bar.setRange(0, _MAX_SPEED_HZ)
        self._speed_bar.setValue(0)
        self._speed_bar.setFormat("%v Hz")
        self._speed_bar.setTextVisible(True)
        speed_layout.addWidget(self._speed_bar)

        metrics_row = QHBoxLayout()
        self._current_label = QLabel("Current: — A")
        self._power_label = QLabel("Power: — W")
        self._pump_on_label = QLabel("Pump: OFF")
        metrics_row.addWidget(self._pump_on_label)
        metrics_row.addWidget(self._current_label)
        metrics_row.addWidget(self._power_label)
        speed_layout.addLayout(metrics_row)
        root.addWidget(speed_grp)

        # --- Controls ---
        ctrl_grp = QGroupBox("Controls")
        ctrl_layout = QHBoxLayout(ctrl_grp)

        self._start_btn = QPushButton("Start Pump")
        self._start_btn.clicked.connect(self._on_start_pump)
        self._start_btn.setEnabled(False)
        ctrl_layout.addWidget(self._start_btn)

        self._stop_btn = QPushButton("Stop Pump")
        self._stop_btn.clicked.connect(self._on_stop_pump)
        self._stop_btn.setEnabled(False)
        ctrl_layout.addWidget(self._stop_btn)

        self._vent_btn = QPushButton("Vent")
        self._vent_btn.clicked.connect(self._on_vent)
        self._vent_btn.setEnabled(False)
        ctrl_layout.addWidget(self._vent_btn)

        self._standby_btn = QPushButton("Standby ON")
        self._standby_btn.setCheckable(True)
        self._standby_btn.clicked.connect(self._on_standby)
        self._standby_btn.setEnabled(False)
        ctrl_layout.addWidget(self._standby_btn)

        self._ack_btn = QPushButton("Ack Error")
        self._ack_btn.clicked.connect(self._on_ack_error)
        self._ack_btn.setEnabled(False)
        ctrl_layout.addWidget(self._ack_btn)

        root.addWidget(ctrl_grp)

        # --- Info ---
        info_grp = QGroupBox("Info")
        info_form = QFormLayout(info_grp)

        self._error_label = QLabel("no Err")
        info_form.addRow("Error code:", self._error_label)

        self._hours_label = QLabel("—")
        info_form.addRow("Op hours:", self._hours_label)

        self._fw_label = QLabel("—")
        info_form.addRow("Firmware:", self._fw_label)

        root.addWidget(info_grp)

        # Status bar
        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)
        self._status_bar.showMessage("Not connected")

    # ------------------------------------------------------------------
    # Ports
    # ------------------------------------------------------------------

    def _populate_ports(self) -> None:
        self._port_combo.clear()
        ports = [p.device for p in serial.tools.list_ports.comports()]
        for p in ports:
            self._port_combo.addItem(p)
        if not ports:
            self._port_combo.addItem("(none)")

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    @pyqtSlot()
    def _on_connect_toggle(self) -> None:
        if self._connected:
            self._do_disconnect()
        else:
            self._do_connect()

    def _do_connect(self) -> None:
        port = self._port_combo.currentText()
        addr = self._addr_spin.value()

        self._protocol = TC600Protocol(address=addr)
        cfg = TransportConfig(port=port, baud=9600)

        self._worker = TurboWorker(
            protocol=self._protocol,
            transport_config=cfg,
            poll_commands=[
                "pump_on", "actual_speed_hz", "motor_current_A",
                "motor_power_W", "error_code", "op_hours_TMP", "firmware",
            ],
            poll_interval=1.0,
            device_id=f"TC600 {port}:{addr}",
        )
        self._worker.status_ready.connect(self._on_status)
        self._worker.error_occurred.connect(self._on_worker_error)
        self._worker.connected.connect(self._on_connected)
        self._worker.disconnected.connect(self._on_disconnected)
        self._worker.start()
        self._status_bar.showMessage(f"Connecting to TC600 on {port}…")

    def _do_disconnect(self) -> None:
        if self._worker:
            self._worker.stop()
            self._worker.wait(3000)
            self._worker = None
        self._connected = False
        self._connect_btn.setText("Connect")
        self._set_controls_enabled(False)
        self._status_bar.showMessage("Disconnected")

    # ------------------------------------------------------------------
    # Worker signals
    # ------------------------------------------------------------------

    @pyqtSlot()
    def _on_connected(self) -> None:
        self._connected = True
        self._connect_btn.setText("Disconnect")
        self._set_controls_enabled(True)
        self._status_bar.showMessage("Connected")

    @pyqtSlot()
    def _on_disconnected(self) -> None:
        self._connected = False
        self._connect_btn.setText("Connect")
        self._set_controls_enabled(False)
        self._status_bar.showMessage("Disconnected")

    @pyqtSlot(object)
    def _on_status(self, status: TurboStatus) -> None:
        r = status.readings

        # Speed bar
        if "actual_speed_hz" in r and r["actual_speed_hz"].success:
            hz = int(r["actual_speed_hz"].value or 0)
            self._speed_bar.setValue(hz)

        # Current / power
        if "motor_current_A" in r and r["motor_current_A"].success:
            self._current_label.setText(f"Current: {r['motor_current_A'].value:.2f} A")
        if "motor_power_W" in r and r["motor_power_W"].success:
            self._power_label.setText(f"Power: {int(r['motor_power_W'].value or 0)} W")

        # Pump on/off
        if "pump_on" in r and r["pump_on"].success:
            on = r["pump_on"].value == 1.0
            self._pump_on_label.setText("Pump: ON" if on else "Pump: OFF")
            colour = "#4CE87A" if on else "#E84C4C"
            self._pump_on_label.setStyleSheet(f"color: {colour}; font-weight: bold;")

        # Error code
        if "error_code" in r and r["error_code"].success:
            code = r["error_code"].formatted.strip()
            desc = ERROR_DESCRIPTIONS.get(code, code)
            self._error_label.setText(f"{code} — {desc}")
            is_err = not code.startswith("no")
            self._error_label.setStyleSheet("color: red;" if is_err else "")

        # Op hours
        if "op_hours_TMP" in r and r["op_hours_TMP"].success:
            self._hours_label.setText(f"{int(r['op_hours_TMP'].value or 0)} h")

        # Firmware
        if "firmware" in r and r["firmware"].success:
            self._fw_label.setText(r["firmware"].formatted.strip())

        now = datetime.now().strftime("%H:%M:%S")
        self._status_bar.showMessage(f"Last update: {now}")

    @pyqtSlot(object)
    def _on_worker_error(self, error: DeviceError) -> None:
        msg = f"[{'WARN' if error.recoverable else 'ERR'}] {error.message}"
        self._status_bar.showMessage(msg)
        logger.warning("TurboWorker: %s", error.message)

    # ------------------------------------------------------------------
    # Control actions (send via TurboWorker)
    # ------------------------------------------------------------------

    @pyqtSlot()
    def _on_start_pump(self) -> None:
        self._send("pump_on", True)

    @pyqtSlot()
    def _on_stop_pump(self) -> None:
        self._send("pump_on", False)

    @pyqtSlot()
    def _on_vent(self) -> None:
        self._send("vent_enable", True)

    @pyqtSlot()
    def _on_standby(self) -> None:
        on = self._standby_btn.isChecked()
        self._standby_btn.setText("Standby ON" if not on else "Standby OFF")
        self._send("standby", on)

    @pyqtSlot()
    def _on_ack_error(self) -> None:
        self._send("error_ack", True)

    def _send(self, command: str, value) -> None:
        if self._worker:
            self._worker.send_command(command, value)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _set_controls_enabled(self, enabled: bool) -> None:
        for btn in (self._start_btn, self._stop_btn, self._vent_btn,
                    self._standby_btn, self._ack_btn):
            btn.setEnabled(enabled)

    def closeEvent(self, event) -> None:
        self._do_disconnect()
        super().closeEvent(event)
