"""
AddGaugeDialog — dialog for connecting a new gauge.

Features:
  - Model combo + port combo with one-click port refresh
  - "Scan Ports" button that probes each COM port in a background thread
  - Scan results list (click to auto-fill model/port)
  - Collapsible "Advanced Settings" panel:
      - Baud rate override
      - RS-232 / RS-485 mode selection
      - Address (moved here from the main form)
  - Commands multi-select (from device spec)
  - Poll interval spin
"""

from __future__ import annotations

import logging

import serial.tools.list_ports
from PyQt6.QtCore import Qt, pyqtSlot
from PyQt6.QtWidgets import (
    QButtonGroup, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QFormLayout, QComboBox, QGroupBox, QHBoxLayout, QLabel,
    QListWidget, QListWidgetItem, QPushButton, QRadioButton,
    QSpinBox, QVBoxLayout, QWidget,
)

from serial_comm.device_registry import DeviceRegistry
from serial_comm.models import DeviceSpec
from GUI.gauge_workspace.port_scanner import PortScanner

logger = logging.getLogger(__name__)

_BAUD_RATES = ["1200", "2400", "4800", "9600", "19200", "38400", "57600", "115200"]


class AddGaugeDialog(QDialog):
    """Modal dialog for selecting and configuring a gauge connection."""

    def __init__(self, registry: DeviceRegistry, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add Gauge")
        self.setMinimumWidth(460)
        self._registry = registry
        self._spec: DeviceSpec | None = None
        self._scanner: PortScanner | None = None

        self._build_ui()
        self._populate_models()
        self._populate_ports()

    # ------------------------------------------------------------------
    # Build UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(6)

        # ── Model row ──
        model_form = QFormLayout()
        model_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self._model_combo = QComboBox()
        self._model_combo.currentTextChanged.connect(self._on_model_changed)
        model_form.addRow("Model:", self._model_combo)
        layout.addLayout(model_form)

        # ── Port row ──
        port_row = QHBoxLayout()
        port_row.addWidget(QLabel("Port:"))
        self._port_combo = QComboBox()
        self._port_combo.setMinimumWidth(100)
        port_row.addWidget(self._port_combo)

        refresh_btn = QPushButton("↻")
        refresh_btn.setFixedWidth(30)
        refresh_btn.setToolTip("Refresh port list")
        refresh_btn.clicked.connect(self._populate_ports)
        port_row.addWidget(refresh_btn)

        self._scan_btn = QPushButton("Scan Ports")
        self._scan_btn.setToolTip("Auto-detect gauges on all available ports")
        self._scan_btn.clicked.connect(self._on_scan)
        port_row.addWidget(self._scan_btn)
        layout.addLayout(port_row)

        # ── Scan status + results ──
        self._scan_status = QLabel()
        self._scan_status.hide()
        layout.addWidget(self._scan_status)

        self._scan_list = QListWidget()
        self._scan_list.setMaximumHeight(90)
        self._scan_list.hide()
        self._scan_list.itemClicked.connect(self._on_scan_result_clicked)
        layout.addWidget(self._scan_list)

        # ── Advanced settings (collapsible) ──
        self._adv_toggle = QPushButton("▶  Advanced Settings")
        self._adv_toggle.setFlat(True)
        self._adv_toggle.setStyleSheet("text-align: left; font-weight: bold;")
        self._adv_toggle.clicked.connect(self._toggle_advanced)
        layout.addWidget(self._adv_toggle)

        self._adv_box = QGroupBox()
        self._adv_box.hide()
        adv_form = QFormLayout(self._adv_box)
        adv_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        # Baud rate
        self._baud_combo = QComboBox()
        self._baud_combo.addItems(_BAUD_RATES)
        self._baud_combo.setCurrentText("9600")
        adv_form.addRow("Baud rate:", self._baud_combo)

        # RS-232 / RS-485
        mode_row = QHBoxLayout()
        self._rs232_radio = QRadioButton("RS-232")
        self._rs485_radio = QRadioButton("RS-485")
        self._rs232_radio.setChecked(True)
        self._rs_group = QButtonGroup(self)
        self._rs_group.addButton(self._rs232_radio, 0)
        self._rs_group.addButton(self._rs485_radio, 1)
        self._rs485_radio.toggled.connect(self._on_rs_mode_changed)
        mode_row.addWidget(self._rs232_radio)
        mode_row.addWidget(self._rs485_radio)
        mode_row.addStretch()
        adv_form.addRow("Mode:", mode_row)

        # Address
        self._address_spin = QSpinBox()
        self._address_spin.setRange(0, 255)
        self._address_spin.setValue(254)
        adv_form.addRow("Address:", self._address_spin)

        layout.addWidget(self._adv_box)

        # ── Commands group ──
        cmd_group = QGroupBox("Commands to poll")
        cmd_layout = QVBoxLayout(cmd_group)
        self._cmd_list = QListWidget()
        self._cmd_list.setSelectionMode(QListWidget.SelectionMode.MultiSelection)
        self._cmd_list.setMaximumHeight(130)
        cmd_layout.addWidget(self._cmd_list)
        layout.addWidget(cmd_group)

        # ── Poll interval + experimental warning ──
        bottom_form = QFormLayout()
        bottom_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self._interval_spin = QDoubleSpinBox()
        self._interval_spin.setRange(0.1, 60.0)
        self._interval_spin.setSingleStep(0.1)
        self._interval_spin.setDecimals(1)
        self._interval_spin.setValue(1.0)
        self._interval_spin.setSuffix(" s")
        bottom_form.addRow("Poll interval:", self._interval_spin)
        layout.addLayout(bottom_form)

        self._exp_label = QLabel()
        self._exp_label.setStyleSheet("color: orange;")
        self._exp_label.hide()
        layout.addWidget(self._exp_label)

        # ── Buttons ──
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    # ------------------------------------------------------------------
    # Populate
    # ------------------------------------------------------------------

    def _populate_models(self) -> None:
        models = self._registry.stable_models()
        exp = self._registry.experimental_models()
        self._model_combo.clear()
        for m in models:
            self._model_combo.addItem(m)
        if exp:
            self._model_combo.insertSeparator(len(models))
            for m in exp:
                self._model_combo.addItem(f"{m} (experimental)")
        if models:
            self._model_combo.setCurrentIndex(0)

    def _populate_ports(self) -> None:
        ports = [p.device for p in serial.tools.list_ports.comports()]
        self._port_combo.clear()
        for p in ports:
            self._port_combo.addItem(p)
        if not ports:
            self._port_combo.addItem("(no ports found)")

    def _on_model_changed(self, text: str) -> None:
        model = text.replace(" (experimental)", "")
        try:
            self._spec = self._registry.get_spec(model)
        except Exception:
            logger.exception("Failed to load spec for model %r", model)
            self._spec = None
            return

        spec = self._spec
        self._address_spin.setValue(spec.default_address)
        self._baud_combo.setCurrentText(str(spec.default_baud))

        # RS mode default
        rs485_available = "RS485" in spec.rs_modes
        self._rs485_radio.setEnabled(rs485_available)
        if "RS485" in spec.rs_modes and spec.rs_modes[0] == "RS485":
            self._rs485_radio.setChecked(True)
        else:
            self._rs232_radio.setChecked(True)

        # Populate commands
        self._cmd_list.clear()
        for cmd_name, cmd in spec.commands.items():
            if cmd.read:
                item = QListWidgetItem(
                    f"{cmd_name}  —  {cmd.description}" if cmd.description else cmd_name
                )
                item.setData(Qt.ItemDataRole.UserRole, cmd_name)
                self._cmd_list.addItem(item)
                if cmd_name == "pressure":
                    item.setSelected(True)

        if spec.experimental:
            self._exp_label.setText(
                f"⚠  {spec.model} support is experimental — use with caution."
            )
            self._exp_label.show()
        else:
            self._exp_label.hide()

    def _on_rs_mode_changed(self, rs485: bool) -> None:
        if self._spec is None:
            return
        if rs485 and self._spec.rs485_address_range:
            lo, hi = self._spec.rs485_address_range
            self._address_spin.setRange(int(lo), int(hi))
            self._address_spin.setValue(int(lo))
        else:
            self._address_spin.setRange(0, 255)
            self._address_spin.setValue(
                self._spec.default_address if self._spec else 254
            )

    # ------------------------------------------------------------------
    # Advanced panel toggle
    # ------------------------------------------------------------------

    def _toggle_advanced(self) -> None:
        visible = self._adv_box.isVisible()
        self._adv_box.setVisible(not visible)
        self._adv_toggle.setText(
            ("▼" if not visible else "▶") + "  Advanced Settings"
        )
        self.adjustSize()

    # ------------------------------------------------------------------
    # Port scanning
    # ------------------------------------------------------------------

    @pyqtSlot()
    def _on_scan(self) -> None:
        # Stop any previous scan
        if self._scanner and self._scanner.isRunning():
            self._scanner.requestInterruption()
            self._scanner.wait(2000)

        ports = [p.device for p in serial.tools.list_ports.comports()]
        if not ports:
            self._scan_status.setText("No ports available to scan.")
            self._scan_status.show()
            return

        self._scan_list.clear()
        self._scan_list.show()
        self._scan_status.setText(f"Scanning {len(ports)} port(s)…")
        self._scan_status.show()
        self._scan_btn.setEnabled(False)

        self._scanner = PortScanner(ports=ports, parent=self)
        self._scanner.port_found.connect(self._on_port_found)
        self._scanner.scan_complete.connect(self._on_scan_complete)
        self._scanner.start()

    @pyqtSlot(str, str, str)
    def _on_port_found(self, port: str, description: str, model_hint: str) -> None:
        short_desc = description[:50].strip()
        item = QListWidgetItem(f"{port}  [{model_hint}]  {short_desc}")
        item.setData(Qt.ItemDataRole.UserRole, (port, model_hint))
        self._scan_list.addItem(item)

    @pyqtSlot()
    def _on_scan_complete(self) -> None:
        self._scan_btn.setEnabled(True)
        if self._scan_list.count() == 0:
            no_item = QListWidgetItem("No devices found")
            no_item.setFlags(Qt.ItemFlag.NoItemFlags)
            self._scan_list.addItem(no_item)
            self._scan_status.setText("Scan complete — no devices found.")
        else:
            n = self._scan_list.count()
            self._scan_status.setText(f"Scan complete — {n} device(s) found. Click to select.")

    @pyqtSlot(QListWidgetItem)
    def _on_scan_result_clicked(self, item: QListWidgetItem) -> None:
        from PyQt6.QtWidgets import QMessageBox
        data = item.data(Qt.ItemDataRole.UserRole)
        if not data:
            return
        port, model_hint = data

        # TC600 is a Pfeiffer turbo — not a gauge
        if "TC600" in model_hint.upper() or "(TURBO)" in model_hint.upper():
            QMessageBox.information(
                self, "Pfeiffer TC600 Turbo Detected",
                f"A Pfeiffer TC600 turbo controller was found on {port}.\n\n"
                "Use Devices → Open Turbo Controller to connect to it.\n"
                "The turbo controller has its own dedicated window.",
            )
            return

        # Set the port
        idx = self._port_combo.findText(port)
        if idx >= 0:
            self._port_combo.setCurrentIndex(idx)
        else:
            self._port_combo.insertItem(0, port)
            self._port_combo.setCurrentIndex(0)

        # Strip "INFICON " prefix, then match by scanning tokens against the model list.
        # e.g. "INFICON PPG570" → try to match "PPG570" then "PPG" in the combo.
        hint_clean = model_hint.upper().replace("INFICON", "").strip()
        tokens = [t for t in hint_clean.split() if t]

        for token in tokens:
            for i in range(self._model_combo.count()):
                if token in self._model_combo.itemText(i).upper():
                    self._model_combo.setCurrentIndex(i)
                    return

    # ------------------------------------------------------------------
    # Result
    # ------------------------------------------------------------------

    def result_config(self) -> dict | None:
        if self._spec is None:
            return None

        model_text = self._model_combo.currentText()
        model = model_text.replace(" (experimental)", "")
        spec = self._spec

        selected_cmds = [
            item.data(Qt.ItemDataRole.UserRole)
            for item in self._cmd_list.selectedItems()
        ]
        if not selected_cmds:
            selected_cmds = [n for n, c in spec.commands.items() if c.read]

        address = self._address_spin.value()
        protocol = self._registry.make_protocol(spec, address=address)

        return {
            "spec": spec,
            "protocol": protocol,
            "port": self._port_combo.currentText(),
            "commands": selected_cmds,
            "poll_interval": self._interval_spin.value(),
            "baud_override": int(self._baud_combo.currentText()),
            "rs485_enabled": self._rs485_radio.isChecked(),
        }
