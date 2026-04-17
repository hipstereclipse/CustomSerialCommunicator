"""
TurboWindow — independent QMainWindow for the Pfeiffer TC600 controller.

Architecturally separate from the gauge workspace: turbos are Pfeiffer products,
not INFICON gauges.  This window has its own connection management.

Tabs:
  Dashboard — speed/status overview, per-parameter retrieve buttons with
              cyclic-enable checkboxes, pump controls.
  Terminal  — quick-command sender + raw-frame entry with coloured log.
"""

from __future__ import annotations

import html
import logging
from datetime import datetime, timezone
from typing import Any

import serial.tools.list_ports
from PyQt6.QtCore import Qt, QSettings, pyqtSlot
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QPushButton,
    QRadioButton, QScrollArea, QSizePolicy, QSpinBox, QStatusBar,
    QTabWidget, QTextEdit, QVBoxLayout, QWidget, QProgressBar,
    QButtonGroup,
)

from serial_comm.turbos.tc600_protocol import TC600Protocol, ERROR_DESCRIPTIONS
from serial_comm.turbos.turbo_worker import TurboWorker, TurboStatus
from serial_comm.models import DeviceError, GaugeReading
from serial_comm.transport import TransportConfig

logger = logging.getLogger(__name__)

_MAX_SPEED_HZ = 1500

# (command_name, display_label, unit_suffix, default_cyclic_on)
_STATUS_ROWS: list[tuple[str, str, str, bool]] = [
    ("actual_speed_hz",    "Speed",             "Hz",  True),
    ("motor_current_A",    "Current",           "A",   True),
    ("motor_power_W",      "Power",             "W",   True),
    ("motor_temp_C",       "Motor Temp",        "°C",  False),
    ("electronics_temp_C", "Electronics Temp",  "°C",  False),
    ("bearing_temp_C",     "Bearing Temp",      "°C",  False),
    ("pump_on",            "Pump Status",       "",    True),
    ("error_code",         "Error Code",        "",    True),
    ("warning_code",       "Warning Code",      "",    False),
    ("op_hours_TMP",       "Operating Hours",   "h",   False),
    ("firmware",           "Firmware",          "",    False),
]

_COL_TX  = "#4C9BE8"
_COL_RX  = "#4CE87A"
_COL_ERR = "#E84C4C"
_COL_TS  = "#888888"


class TurboWindow(QMainWindow):
    """
    Standalone window for Pfeiffer TC600 turbo pump control.

    Opened from the main window via Devices → Open Turbo Controller.
    Can be closed independently; does not destroy the main window.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Pfeiffer TC600 Turbo Controller")
        self.resize(600, 700)
        self._settings = QSettings()

        self._worker: TurboWorker | None = None
        self._protocol: TC600Protocol | None = None
        self._connected = False

        # Per-row widgets: cmd → (cyclic_check, value_label, retrieve_btn)
        self._row_widgets: dict[str, tuple[QCheckBox, QLabel, QPushButton]] = {}

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
        root.setContentsMargins(8, 8, 8, 8)

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

        # --- Tabs ---
        self._tabs = QTabWidget()
        self._tabs.addTab(self._build_dashboard_tab(), "Dashboard")
        self._tabs.addTab(self._build_terminal_tab(), "Terminal")
        root.addWidget(self._tabs)

        # Status bar
        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)
        self._status_bar.showMessage("Not connected")

    # ------------------------------------------------------------------
    # Dashboard tab
    # ------------------------------------------------------------------

    def _build_dashboard_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setSpacing(6)

        # Speed bar + pump indicator
        speed_grp = QGroupBox("Pump Status")
        speed_layout = QVBoxLayout(speed_grp)

        self._speed_bar = QProgressBar()
        self._speed_bar.setRange(0, _MAX_SPEED_HZ)
        self._speed_bar.setValue(0)
        self._speed_bar.setFormat("%v Hz")
        self._speed_bar.setTextVisible(True)
        speed_layout.addWidget(self._speed_bar)

        pump_row = QHBoxLayout()
        self._pump_label = QLabel("Pump: OFF")
        self._pump_label.setStyleSheet("color: #E84C4C; font-weight: bold;")
        pump_row.addWidget(self._pump_label)
        pump_row.addStretch()
        speed_layout.addLayout(pump_row)
        layout.addWidget(speed_grp)

        # Controls
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
        layout.addWidget(ctrl_grp)

        # Per-parameter status table (scrollable)
        status_grp = QGroupBox("Status")
        status_inner = QWidget()
        status_form = QFormLayout(status_inner)
        status_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        status_form.setVerticalSpacing(4)

        for cmd, label, unit, default_cyclic in _STATUS_ROWS:
            row_widget = QWidget()
            row_layout = QHBoxLayout(row_widget)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(6)

            cyc_check = QCheckBox()
            cyc_check.setToolTip("Include in cyclic polling")
            cyc_check.setChecked(default_cyclic)
            cyc_check.toggled.connect(self._on_cyclic_toggle)
            row_layout.addWidget(cyc_check)

            val_label = QLabel("—")
            val_label.setMinimumWidth(140)
            row_layout.addWidget(val_label)

            retrieve_btn = QPushButton("Retrieve")
            retrieve_btn.setFixedWidth(70)
            retrieve_btn.setEnabled(False)
            retrieve_btn.clicked.connect(
                lambda _checked=False, c=cmd: self._on_retrieve(c)
            )
            row_layout.addWidget(retrieve_btn)
            row_layout.addStretch()

            self._row_widgets[cmd] = (cyc_check, val_label, retrieve_btn)
            status_form.addRow(f"{label}:", row_widget)

        scroll = QScrollArea()
        scroll.setWidget(status_inner)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        grp_layout = QVBoxLayout(status_grp)
        grp_layout.addWidget(scroll)
        layout.addWidget(status_grp)

        # Cyclic update controls
        cyc_grp = QGroupBox("Cyclic Updates")
        cyc_layout = QHBoxLayout(cyc_grp)

        self._cyclic_check = QCheckBox("Enable")
        self._cyclic_check.setChecked(True)
        self._cyclic_check.toggled.connect(self._on_cyclic_master_toggle)
        cyc_layout.addWidget(self._cyclic_check)

        cyc_layout.addWidget(QLabel("Interval:"))
        self._interval_spin = QDoubleSpinBox()
        self._interval_spin.setRange(0.2, 60.0)
        self._interval_spin.setSingleStep(0.1)
        self._interval_spin.setDecimals(1)
        self._interval_spin.setValue(1.0)
        self._interval_spin.setSuffix(" s")
        cyc_layout.addWidget(self._interval_spin)

        apply_btn = QPushButton("Apply")
        apply_btn.clicked.connect(self._on_apply_interval)
        cyc_layout.addWidget(apply_btn)
        cyc_layout.addStretch()
        layout.addWidget(cyc_grp)

        return tab

    # ------------------------------------------------------------------
    # Terminal tab
    # ------------------------------------------------------------------

    def _build_terminal_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setSpacing(6)

        # Quick command panel
        quick_grp = QGroupBox("Quick Command")
        quick_form = QFormLayout(quick_grp)
        quick_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self._term_cmd_combo = QComboBox()
        self._term_cmd_combo.setMinimumWidth(200)
        quick_form.addRow("Command:", self._term_cmd_combo)

        type_row = QHBoxLayout()
        self._query_radio = QRadioButton("Query (read)")
        self._set_radio = QRadioButton("Set (write)")
        self._query_radio.setChecked(True)
        btn_grp = QButtonGroup(self)
        btn_grp.addButton(self._query_radio, 0)
        btn_grp.addButton(self._set_radio, 1)
        self._set_radio.toggled.connect(self._on_term_type_changed)
        type_row.addWidget(self._query_radio)
        type_row.addWidget(self._set_radio)
        type_row.addStretch()
        quick_form.addRow("Type:", type_row)

        self._term_value = QLineEdit()
        self._term_value.setPlaceholderText("Value (for Set only)")
        self._term_value.setEnabled(False)
        quick_form.addRow("Value:", self._term_value)

        self._term_send_btn = QPushButton("Send")
        self._term_send_btn.setEnabled(False)
        self._term_send_btn.clicked.connect(self._on_term_send)
        quick_form.addRow("", self._term_send_btn)
        layout.addWidget(quick_grp)

        # Output display
        self._term_output = QTextEdit()
        self._term_output.setReadOnly(True)
        self._term_output.setFont(QFont("Courier New", 9))
        self._term_output.document().setMaximumBlockCount(2000)
        self._term_output.setPlaceholderText(
            "Command log appears here.\nUse Query to read a parameter, Set to write one."
        )
        layout.addWidget(self._term_output)

        # Raw frame entry
        raw_grp = QGroupBox("Raw Frame")
        raw_layout = QHBoxLayout(raw_grp)
        self._raw_input = QLineEdit()
        self._raw_input.setPlaceholderText("Not available while worker is connected")
        self._raw_input.setEnabled(False)
        raw_layout.addWidget(self._raw_input)
        layout.addWidget(raw_grp)

        return tab

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

    def _populate_term_commands(self) -> None:
        """Fill the terminal command combo from the TC600 parameter table."""
        self._term_cmd_combo.clear()
        if self._protocol is None:
            return
        for cmd in self._protocol.all_commands:
            self._term_cmd_combo.addItem(cmd)

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

        initial_poll = self._build_poll_list()
        self._worker = TurboWorker(
            protocol=self._protocol,
            transport_config=cfg,
            poll_commands=initial_poll,
            poll_interval=self._interval_spin.value(),
            device_id=f"TC600 {port}:{addr}",
        )
        self._worker.status_ready.connect(self._on_status)
        self._worker.terminal_response.connect(self._on_terminal_response)
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
        self._populate_term_commands()
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

        # Pump label
        if "pump_on" in r and r["pump_on"].success:
            on = r["pump_on"].value == 1.0
            self._pump_label.setText("Pump: ON" if on else "Pump: OFF")
            colour = "#4CE87A" if on else "#E84C4C"
            self._pump_label.setStyleSheet(f"color: {colour}; font-weight: bold;")

        # Per-row status labels
        for cmd, (_cyc, val_lbl, _btn) in self._row_widgets.items():
            if cmd not in r:
                continue
            reading: GaugeReading = r[cmd]
            if not reading.success:
                val_lbl.setText(f"[ERR] {reading.error or ''}")
                val_lbl.setStyleSheet("color: #E84C4C;")
                continue

            if cmd == "error_code":
                code = (reading.formatted or "").strip()
                desc = ERROR_DESCRIPTIONS.get(code, code)
                val_lbl.setText(f"{code} — {desc}")
                val_lbl.setStyleSheet("color: red;" if not code.startswith("no") else "")
            elif cmd == "warning_code":
                code = (reading.formatted or "").strip()
                val_lbl.setText(code)
                val_lbl.setStyleSheet("color: orange;" if not code.startswith("no") else "")
            elif cmd == "pump_on":
                pass  # already handled above
            else:
                val_lbl.setText(reading.formatted or str(reading.value or "—"))
                val_lbl.setStyleSheet("")

        now = datetime.now(tz=timezone.utc).strftime("%H:%M:%S UTC")
        self._status_bar.showMessage(f"Last update: {now}")

    @pyqtSlot(str, object)
    def _on_terminal_response(self, label: str, reading: Any) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        if label.startswith("SET "):
            self._term_append(f"[{ts}] TX: {label}", _COL_TX)
        elif label.startswith("READ "):
            cmd = label[5:]
            if reading is not None and reading.success:
                val = reading.formatted or str(reading.value)
                self._term_append(f"[{ts}] RX: {cmd} = {val}", _COL_RX)
            elif reading is not None:
                self._term_append(f"[{ts}] RX: {cmd} [ERR] {reading.error}", _COL_ERR)

    @pyqtSlot(object)
    def _on_worker_error(self, error: DeviceError) -> None:
        msg = f"[{'WARN' if error.recoverable else 'ERR'}] {error.message}"
        self._status_bar.showMessage(msg)
        self._term_append(msg, _COL_ERR)
        logger.warning("TurboWorker: %s", error.message)

    # ------------------------------------------------------------------
    # Control actions
    # ------------------------------------------------------------------

    @pyqtSlot()
    def _on_start_pump(self) -> None:
        self._send_write("pump_on", True)

    @pyqtSlot()
    def _on_stop_pump(self) -> None:
        self._send_write("pump_on", False)

    @pyqtSlot()
    def _on_vent(self) -> None:
        self._send_write("vent_enable", True)

    @pyqtSlot()
    def _on_standby(self) -> None:
        on = self._standby_btn.isChecked()
        self._standby_btn.setText("Standby ON" if not on else "Standby OFF")
        self._send_write("standby", on)

    @pyqtSlot()
    def _on_ack_error(self) -> None:
        self._send_write("error_ack", True)

    def _send_write(self, command: str, value: Any) -> None:
        if self._worker:
            self._worker.send_command(command, value)

    # ------------------------------------------------------------------
    # Retrieve buttons
    # ------------------------------------------------------------------

    def _on_retrieve(self, command: str) -> None:
        if self._worker:
            self._worker.request_read(command)
            ts = datetime.now().strftime("%H:%M:%S")
            self._term_append(f"[{ts}] TX: READ {command} (→ Retrieve)", _COL_TX)

    # ------------------------------------------------------------------
    # Cyclic controls
    # ------------------------------------------------------------------

    def _build_poll_list(self) -> list[str]:
        """Return the list of commands currently checked for cyclic polling."""
        if not self._cyclic_check.isChecked():
            return []
        return [
            cmd for cmd, (cyc_check, _, __) in self._row_widgets.items()
            if cyc_check.isChecked()
        ]

    @pyqtSlot(bool)
    def _on_cyclic_toggle(self, _: bool) -> None:
        if self._worker:
            self._worker.set_poll_commands(self._build_poll_list())

    @pyqtSlot(bool)
    def _on_cyclic_master_toggle(self, enabled: bool) -> None:
        if self._worker:
            self._worker.set_poll_commands(self._build_poll_list())

    @pyqtSlot()
    def _on_apply_interval(self) -> None:
        if self._worker:
            self._worker._poll_interval = self._interval_spin.value()

    # ------------------------------------------------------------------
    # Terminal
    # ------------------------------------------------------------------

    @pyqtSlot(bool)
    def _on_term_type_changed(self, set_mode: bool) -> None:
        self._term_value.setEnabled(set_mode)

    @pyqtSlot()
    def _on_term_send(self) -> None:
        if not self._worker:
            return
        cmd = self._term_cmd_combo.currentText()
        if not cmd:
            return
        if self._set_radio.isChecked():
            raw_val = self._term_value.text().strip()
            try:
                val: Any = float(raw_val) if "." in raw_val else int(raw_val)
            except ValueError:
                val = raw_val
            self._worker.send_command(cmd, val)
            ts = datetime.now().strftime("%H:%M:%S")
            self._term_append(f"[{ts}] TX: SET {cmd} = {val}", _COL_TX)
        else:
            self._worker.request_read(cmd)
            ts = datetime.now().strftime("%H:%M:%S")
            self._term_append(f"[{ts}] TX: READ {cmd}", _COL_TX)

    def _term_append(self, text: str, colour: str = "") -> None:
        escaped = html.escape(text)
        frag = f'<span style="color:{colour}">{escaped}</span>' if colour else escaped
        self._term_output.append(frag)
        sb = self._term_output.verticalScrollBar()
        sb.setValue(sb.maximum())

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _set_controls_enabled(self, enabled: bool) -> None:
        for btn in (self._start_btn, self._stop_btn, self._vent_btn,
                    self._standby_btn, self._ack_btn):
            btn.setEnabled(enabled)
        for _cyc, _val, rtv in self._row_widgets.values():
            rtv.setEnabled(enabled)
        self._term_send_btn.setEnabled(enabled)

    def closeEvent(self, event) -> None:
        self._do_disconnect()
        super().closeEvent(event)
