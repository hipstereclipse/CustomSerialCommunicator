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
from copy import deepcopy

import serial.tools.list_ports
from PyQt6.QtCore import Qt, pyqtSlot
from PyQt6.QtWidgets import (
    QButtonGroup, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QFormLayout, QComboBox, QGroupBox, QHBoxLayout, QLabel,
    QLayout, QListWidget, QListWidgetItem, QPushButton, QRadioButton,
    QSizePolicy, QSpinBox, QVBoxLayout, QWidget,
)

from serial_comm.device_registry import DeviceRegistry
from serial_comm.models import DeviceSpec
from serial_comm.command_utils import (
    default_poll_commands,
    is_pressure_command,
    is_primary_pressure_command,
)
from GUI.gauge_workspace.port_scanner import PortScanner
from GUI.theme import current_theme, list_style

logger = logging.getLogger(__name__)

_BAUD_RATES = ["1200", "2400", "4800", "9600", "19200", "38400", "57600", "115200"]
_MODEL_ALIASES = {
    "PPG550/570": "PPG570",
}


class _ScanResultWidget(QWidget):
    """Compact two-line scan result card."""

    def __init__(self, port: str, model_hint: str, description: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._port = port
        self._model_hint = model_hint
        self._description = description

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(3)

        self._model_label = QLabel(model_hint or "Unknown gauge")
        self._model_label.setTextFormat(Qt.TextFormat.PlainText)
        self._model_label.setWordWrap(False)
        layout.addWidget(self._model_label)

        detail = f"{port}  \u00b7  {description}" if description else port
        self._detail_label = QLabel(detail)
        self._detail_label.setTextFormat(Qt.TextFormat.PlainText)
        self._detail_label.setWordWrap(True)
        layout.addWidget(self._detail_label)

        self._model_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._detail_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(50)
        self.apply_theme()

    def apply_theme(self) -> None:
        theme = current_theme(self)
        self.setStyleSheet("background: transparent;")
        self._model_label.setStyleSheet(f"font-weight:700; font-size:13px; color:{theme.text};")
        self._detail_label.setStyleSheet(f"font-size:11px; color:{theme.muted};")

class AddGaugeDialog(QDialog):
    """Modal dialog for selecting and configuring a gauge connection."""

    def __init__(self, registry: DeviceRegistry, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add Gauge")
        self.setMinimumWidth(500)
        self.resize(520, 420)
        self._registry = registry
        self._spec: DeviceSpec | None = None
        self._scanner: PortScanner | None = None

        self._build_ui()
        self._populate_models()
        self._populate_ports()
        self.apply_theme()

    # ------------------------------------------------------------------
    # Build UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(4)

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
        self._scan_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self._scan_list.setMinimumHeight(96)
        self._scan_list.setMaximumHeight(170)
        self._scan_list.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._scan_list.hide()
        self._scan_list.itemSelectionChanged.connect(self._on_scan_selection_changed)
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

        # CDG full scale (only shown for CDG models)
        self._cdg_fs_combo = QComboBox()
        self._cdg_fs_combo.setEditable(False)
        self._cdg_fs_combo.hide()
        self._cdg_fs_label = QLabel("CDG Full Scale:")
        self._cdg_fs_label.hide()
        adv_form.addRow(self._cdg_fs_label, self._cdg_fs_combo)

        layout.addWidget(self._adv_box)

        # ── Commands group ──
        cmd_group = QGroupBox("Commands to poll")
        cmd_layout = QVBoxLayout(cmd_group)
        cmd_layout.setContentsMargins(8, 8, 8, 8)
        cmd_layout.setSpacing(4)

        # Advanced toggle: show non-pressure commands
        adv_cmd_row = QHBoxLayout()
        adv_cmd_row.addWidget(QLabel("Pressure commands are selected by default."))
        self._adv_cmd_check = QPushButton("Advanced Command Menu")
        self._adv_cmd_check.setCheckable(True)
        self._adv_cmd_check.setFlat(True)
        self._adv_cmd_check.setStyleSheet("font-weight: bold; color: #4C9BE8;")
        self._adv_cmd_check.toggled.connect(self._on_adv_cmd_toggled)
        adv_cmd_row.addWidget(self._adv_cmd_check)
        adv_cmd_row.addStretch()
        cmd_layout.addLayout(adv_cmd_row)

        self._cmd_list = QListWidget()
        self._cmd_list.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        self._cmd_list.setMaximumHeight(150)
        self._cmd_list.hide()  # hidden until advanced is toggled
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
        self._exp_label.hide()
        layout.addWidget(self._exp_label)

        # ── Buttons ──
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        layout.setSizeConstraint(QLayout.SizeConstraint.SetFixedSize)

    # ------------------------------------------------------------------
    # Populate
    # ------------------------------------------------------------------

    def _populate_models(self) -> None:
        models = self._registry.stable_models()
        exp = self._registry.experimental_models()
        self._model_combo.clear()
        for m in models:
            self._model_combo.addItem(m)
        # Combined selector for protocol-compatible PPG models.
        self._model_combo.addItem("PPG550/570")
        if exp:
            self._model_combo.insertSeparator(self._model_combo.count())
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
        model = _MODEL_ALIASES.get(model, model)
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
        default_commands = set(default_poll_commands(spec))
        for cmd_name, cmd in spec.commands.items():
            if cmd.read:
                kind = self._command_kind_label(cmd_name, cmd)
                item = QListWidgetItem(
                    f"[{kind}]  {cmd_name.replace('_', ' ')}"
                    + (f"  -  {cmd.description}" if cmd.description else "")
                )
                item.setData(Qt.ItemDataRole.UserRole, cmd_name)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                self._cmd_list.addItem(item)
                item.setCheckState(
                    Qt.CheckState.Checked
                    if cmd_name in default_commands
                    else Qt.CheckState.Unchecked
                )

        # Reset advanced toggle when model changes
        self._adv_cmd_check.setChecked(False)
        self._cmd_list.hide()

        if spec.experimental:
            self._exp_label.setText(
                f"⚠  {spec.model} support is experimental — use with caution."
            )
            self._exp_label.show()
        else:
            self._exp_label.hide()

        self._refresh_cdg_full_scale_controls(spec)

    def _refresh_cdg_full_scale_controls(self, spec: DeviceSpec) -> None:
        raw_extra = spec.__dict__.get("_raw_extra", {})
        options = raw_extra.get("full_scale_options_mbar")
        default_fs = raw_extra.get("full_scale_mbar")
        is_cdg = bool(spec.protocol == "cdg_serial" and options)
        self._cdg_fs_label.setVisible(is_cdg)
        self._cdg_fs_combo.setVisible(is_cdg)
        if not is_cdg:
            self._cdg_fs_combo.clear()
            return

        parsed_options: list[float] = []
        for opt in options:
            try:
                val = float(opt)
            except (TypeError, ValueError):
                continue
            if val > 0:
                parsed_options.append(val)
        parsed_options = sorted(set(parsed_options))

        self._cdg_fs_combo.clear()
        for fs in parsed_options:
            self._cdg_fs_combo.addItem(f"{fs:g} mbar", fs)

        try:
            target = float(default_fs)
        except (TypeError, ValueError):
            target = parsed_options[0] if parsed_options else 1.0
        idx = self._index_for_fs(target)
        if idx >= 0:
            self._cdg_fs_combo.setCurrentIndex(idx)

    def _index_for_fs(self, fs_mbar: float) -> int:
        for i in range(self._cdg_fs_combo.count()):
            try:
                val = float(self._cdg_fs_combo.itemData(i))
            except (TypeError, ValueError):
                continue
            if abs(val - fs_mbar) <= max(1e-6, fs_mbar * 1e-3):
                return i
        return -1

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
    # Advanced commands toggle
    # ------------------------------------------------------------------

    def _on_adv_cmd_toggled(self, checked: bool) -> None:
        self._adv_cmd_check.setText(
            "Hide Advanced Commands" if checked else "Advanced Command Menu"
        )
        self._cmd_list.setVisible(checked)
        self.adjustSize()

    @staticmethod
    def _command_kind_label(cmd_name: str, cmd) -> str:
        if is_primary_pressure_command(cmd_name, cmd):
            return "Combined Pressure"
        if is_pressure_command(cmd):
            return "Subsensor Pressure"
        data_type = (cmd.data_type or "").lower()
        if "spectrum" in cmd_name.lower() or "array" in data_type:
            return "Spectrum"
        if cmd.unit or data_type in {"uint8", "uint16_be", "uint32_be", "int32_be", "float32_be", "u_real", "u_integer"}:
            return "Numeric"
        return "Status"

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
        # If a scan is already running, act as a cancel button.
        if self._scanner is not None and self._scanner.isRunning():
            self._stop_scanner()
            self._scan_btn.setText("Scan Ports")
            self._scan_btn.setEnabled(True)
            self._set_scan_status("Scan Stopped", "#C62828")
            return

        # Stop any stale scanner so its signals don't race with the new scan.
        self._stop_scanner()

        ports = [p.device for p in serial.tools.list_ports.comports()]
        if not ports:
            self._set_scan_status(
                "No serial ports are available. Check the USB/serial adapter connection, then scan again.",
                "#C62828",
            )
            self._scan_status.show()
            return

        self._scan_list.clear()
        self._scan_list.show()
        self._set_scan_status(
            f"Starting scan across {len(ports)} serial port(s). The scanner will try PPG ASCII, Pfeiffer ASCII, CDG/HPG binary, and OPG550 P3 V02 probes.",
            "#333333",
        )
        self._scan_status.show()
        self._scan_btn.setText("Stop")
        self._scan_btn.setEnabled(True)

        self._scanner = PortScanner(ports=ports, parent=self)
        self._scanner.port_found.connect(self._on_port_found)
        self._scanner.scan_complete.connect(self._on_scan_complete)
        self._scanner.port_scanning.connect(self._on_port_scanning)
        self._scanner.start()

    @pyqtSlot(str, str, str, object)
    def _on_port_found(self, port: str, description: str, model_hint: str, metadata: object) -> None:
        details = description.strip()
        item = QListWidgetItem()
        payload = {
            "port": port,
            "model_hint": model_hint,
            "metadata": metadata if isinstance(metadata, dict) else {},
        }
        item.setData(Qt.ItemDataRole.UserRole, payload)
        widget = _ScanResultWidget(port, model_hint, details, self._scan_list)
        item.setSizeHint(widget.sizeHint())
        self._scan_list.addItem(item)
        self._scan_list.setItemWidget(item, widget)

    @pyqtSlot()
    def _on_scan_complete(self) -> None:
        self._scan_btn.setEnabled(True)
        self._scan_btn.setText("Scan Ports")
        if self._scan_list.count() == 0:
            no_item = QListWidgetItem("No devices found")
            no_item.setFlags(Qt.ItemFlag.NoItemFlags)
            self._scan_list.addItem(no_item)
            self._set_scan_status(
                "Scan complete. No gauges answered the probe sequence on the available ports.",
                "#2E7D32",
            )
        else:
            n = self._scan_list.count()
            self._set_scan_status(
                f"Scan complete. Found {n} device(s). Select one or more results, then click OK to add them.",
                "#2E7D32",
            )
        self._scanner = None

    @pyqtSlot(str, int, int, str)
    def _on_port_scanning(self, port: str, current: int, total: int, detail: str) -> None:
        found = self._scan_list.count()
        self._set_scan_status(
            f"Scanning {port} ({current} of {total}): {detail}. Devices found so far: {found}.",
            "#333333",
        )

    def _set_scan_status(self, text: str, color: str) -> None:
        self._scan_status.setText(f"<span style='color: {color};'>{text}</span>")
        self._scan_status.show()

    def apply_theme(self) -> None:
        theme = current_theme(self)
        self._adv_toggle.setStyleSheet("text-align: left; font-weight: bold;")
        self._adv_cmd_check.setStyleSheet("font-weight: bold; color: #2878B8;")
        self._cmd_list.setStyleSheet(list_style(radius=8))
        self._scan_list.setStyleSheet(list_style())
        self._exp_label.setStyleSheet("color:#C77C02;" if theme.name == "light" else "color:#FFB454;")
        for row in range(self._scan_list.count()):
            widget = self._scan_list.itemWidget(self._scan_list.item(row))
            hook = getattr(widget, "apply_theme", None)
            if callable(hook):
                hook()

    @pyqtSlot()
    def _on_scan_selection_changed(self) -> None:
        item = self._scan_list.currentItem()
        if item is None or not item.isSelected():
            return
        self._apply_scan_item(item)

    def _resolve_model_hint(self, model_hint: str, metadata: dict | None = None) -> str | None:
        hint_upper = (model_hint or "").upper().replace("INFICON", "").strip()
        metadata = metadata or {}
        explicit = str(metadata.get("model", "")).strip().upper()
        if explicit == "PPG550/570" or "PPG550/570" in hint_upper:
            return "PPG550/570"

        for i in range(self._model_combo.count()):
            item_text = self._model_combo.itemText(i)
            item_upper = item_text.upper()
            if not item_text or "---" in item_text:
                continue
            if explicit and explicit in item_upper:
                return item_text
            if hint_upper and hint_upper in item_upper:
                return item_text

        tokens = [t for t in hint_upper.replace("/", " ").split() if t]
        for token in tokens:
            for i in range(self._model_combo.count()):
                item_text = self._model_combo.itemText(i)
                if token and token in item_text.upper():
                    return item_text
        return None

    @pyqtSlot(QListWidgetItem)
    def _apply_scan_item(self, item: QListWidgetItem) -> None:
        from PyQt6.QtWidgets import QMessageBox
        try:
            data = item.data(Qt.ItemDataRole.UserRole)
        except Exception:
            logger.exception("Failed to read scan-result data")
            return
        if not data or not isinstance(data, dict):
            return
        port = str(data.get("port", ""))
        model_hint = str(data.get("model_hint", ""))
        metadata = data.get("metadata", {}) if isinstance(data.get("metadata", {}), dict) else {}
        if not port:
            return

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

        matched = self._resolve_model_hint(model_hint, metadata)
        if matched:
            idx = self._model_combo.findText(matched)
            if idx >= 0:
                self._model_combo.setCurrentIndex(idx)

        if self._cdg_fs_combo.isVisible() and metadata.get("full_scale_mbar") is not None:
            try:
                fs = float(metadata.get("full_scale_mbar"))
            except (TypeError, ValueError):
                fs = None
            if fs and fs > 0:
                idx = self._index_for_fs(fs)
                if idx >= 0:
                    self._cdg_fs_combo.setCurrentIndex(idx)

    def _clone_spec_with_scan_overrides(self, spec: DeviceSpec, metadata: dict) -> DeviceSpec:
        full_scale = metadata.get("full_scale_mbar")
        if full_scale is None:
            return spec
        try:
            fs = float(full_scale)
        except (TypeError, ValueError):
            return spec
        if fs <= 0:
            return spec

        spec_copy = deepcopy(spec)
        raw_extra = dict(spec_copy.__dict__.get("_raw_extra", {}))
        raw_extra["full_scale_mbar"] = fs
        spec_copy.__dict__["_raw_extra"] = raw_extra
        return spec_copy

    def _build_config_for(
        self,
        model_display: str,
        port: str,
        metadata: dict | None = None,
    ) -> dict | None:
        # When this method is called from the scan-selection loop, ``metadata``
        # is supplied (possibly as an empty dict). The dialog UI controls
        # (baud combo, command checklist, RS-485 radios) only reflect the
        # *currently active* model in ``self._model_combo`` — applying them to
        # every scan-selected port would, for example, force a CDG opened
        # alongside an OPG550 to use 115200 baud. So treat the presence of
        # ``metadata`` as the signal to take per-spec defaults instead.
        from_scan = metadata is not None
        metadata = metadata or {}
        normalized_model = model_display.replace(" (experimental)", "").strip()
        model_name = _MODEL_ALIASES.get(normalized_model, normalized_model)
        try:
            spec = self._registry.get_spec(model_name)
        except Exception:
            logger.exception("Unknown model in scanned selection: %s", model_display)
            return None

        spec = self._clone_spec_with_scan_overrides(spec, metadata)
        spec = self._apply_cdg_full_scale_override(spec)

        if from_scan:
            selected_cmds = list(default_poll_commands(spec))
        else:
            selected_cmds = [
                self._cmd_list.item(row).data(Qt.ItemDataRole.UserRole)
                for row in range(self._cmd_list.count())
                if self._cmd_list.item(row).checkState() == Qt.CheckState.Checked
            ]
            selected_cmds = [c for c in selected_cmds if c and c in spec.commands and spec.commands[c].read]
            if not selected_cmds:
                selected_cmds = default_poll_commands(spec)

        if from_scan:
            baud = spec.default_baud
        else:
            try:
                baud = int(self._baud_combo.currentText())
            except (TypeError, ValueError):
                baud = spec.default_baud

        if from_scan:
            rs485_enabled = bool(spec.rs_modes) and spec.rs_modes[0] == "RS485"
        else:
            rs485_enabled = self._rs485_radio.isChecked()

        address = self._address_spin.value()
        protocol = self._registry.make_protocol(spec, address=address)
        return {
            "spec": spec,
            "protocol": protocol,
            "port": port,
            "commands": selected_cmds,
            "poll_interval": float(self._interval_spin.value()),
            "baud_override": baud,
            "rs485_enabled": rs485_enabled,
        }

    def _apply_cdg_full_scale_override(self, spec: DeviceSpec) -> DeviceSpec:
        if not self._cdg_fs_combo.isVisible():
            return spec
        try:
            fs = float(self._cdg_fs_combo.currentData())
        except (TypeError, ValueError):
            return spec
        if fs <= 0:
            return spec

        spec_copy = deepcopy(spec)
        raw_extra = dict(spec_copy.__dict__.get("_raw_extra", {}))
        raw_extra["full_scale_mbar"] = fs
        spec_copy.__dict__["_raw_extra"] = raw_extra
        return spec_copy

    def result_configs(self) -> list[dict]:
        configs: list[dict] = []

        selected = self._scan_list.selectedItems()
        if selected:
            seen_ports: set[str] = set()
            for item in selected:
                data = item.data(Qt.ItemDataRole.UserRole)
                if not isinstance(data, dict):
                    continue
                port = str(data.get("port", "")).strip()
                if not port or port in seen_ports:
                    continue
                seen_ports.add(port)

                model_hint = str(data.get("model_hint", "")).strip()
                metadata = data.get("metadata", {}) if isinstance(data.get("metadata", {}), dict) else {}
                model_display = self._resolve_model_hint(model_hint, metadata)
                if not model_display:
                    model_display = self._model_combo.currentText().replace(" (experimental)", "")

                cfg = self._build_config_for(model_display, port, metadata)
                if cfg:
                    configs.append(cfg)

            if configs:
                return configs

        # Fallback: single manual config from combo selections.
        model_display = self._model_combo.currentText().replace(" (experimental)", "")
        port = self._port_combo.currentText().strip()
        if not port or port.startswith("("):
            return []
        cfg = self._build_config_for(model_display, port)
        return [cfg] if cfg else []

    # ------------------------------------------------------------------
    # Result
    # ------------------------------------------------------------------

    def result_config(self) -> dict | None:
        try:
            configs = self.result_configs()
            return configs[0] if configs else None
        except Exception:
            logger.exception("Failed to build gauge connection config")
            return None

    # ------------------------------------------------------------------
    # Cleanup: make sure the scanner QThread is fully stopped before the
    # dialog is destroyed.  Without this, closing the dialog while a scan is
    # still running produces  "QThread: Destroyed while thread is still
    # running"  and can crash the process on Windows.
    # ------------------------------------------------------------------

    def _stop_scanner(self) -> None:
        sc = self._scanner
        if sc is None:
            return
        # Disconnect signals first so queued 'scan_complete' / 'port_found'
        # events that are in flight cannot touch slots on a dialog that may
        # be in the middle of closing.
        try:
            sc.port_found.disconnect(self._on_port_found)
        except (TypeError, RuntimeError):
            pass
        try:
            sc.scan_complete.disconnect(self._on_scan_complete)
        except (TypeError, RuntimeError):
            pass
        try:
            sc.port_scanning.disconnect(self._on_port_scanning)
        except (TypeError, RuntimeError):
            pass
        try:
            if sc.isRunning():
                sc.requestInterruption()
                # With short per-read timeouts (50 ms) the thread exits quickly.
                if not sc.wait(2000):
                    logger.warning("PortScanner did not stop in time; forcing thread termination")
                    sc.terminate()
                    sc.wait(1000)
        except RuntimeError:
            # scanner may have already been destroyed by Qt
            pass
        self._scanner = None

    def done(self, result) -> None:  # type: ignore[override]
        self._stop_scanner()
        super().done(result)

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self._stop_scanner()
        super().closeEvent(event)
