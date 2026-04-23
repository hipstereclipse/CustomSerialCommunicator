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
    QListWidget, QListWidgetItem, QPushButton, QRadioButton,
    QSpinBox, QVBoxLayout, QWidget,
)

from serial_comm.device_registry import DeviceRegistry
from serial_comm.models import DeviceSpec
from GUI.gauge_workspace.port_scanner import PortScanner

logger = logging.getLogger(__name__)

_BAUD_RATES = ["1200", "2400", "4800", "9600", "19200", "38400", "57600", "115200"]
_MODEL_ALIASES = {
    "PPG550/570": "PPG570",
}


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
        self._scan_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self._scan_list.setMaximumHeight(90)
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
        for cmd_name, cmd in spec.commands.items():
            if cmd.read:
                item = QListWidgetItem(
                    f"{cmd_name}  —  {cmd.description}" if cmd.description else cmd_name
                )
                item.setData(Qt.ItemDataRole.UserRole, cmd_name)
                self._cmd_list.addItem(item)
                if cmd_name == "pressure" or spec.model.upper() == "OPG550":
                    item.setSelected(True)

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
        # Stop any previous scan (and disconnect its signals so stale
        # 'scan_complete' events don't race with a new scan).
        self._stop_scanner()

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

    @pyqtSlot(str, str, str, object)
    def _on_port_found(self, port: str, description: str, model_hint: str, metadata: object) -> None:
        short_desc = description[:50].strip()
        item = QListWidgetItem(f"{port}  [{model_hint}]  {short_desc}")
        payload = {
            "port": port,
            "model_hint": model_hint,
            "metadata": metadata if isinstance(metadata, dict) else {},
        }
        item.setData(Qt.ItemDataRole.UserRole, payload)
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
            self._scan_status.setText(
                f"Scan complete — {n} device(s) found. Select one or more, then click OK."
            )

    @pyqtSlot()
    def _on_scan_selection_changed(self) -> None:
        selected = self._scan_list.selectedItems()
        if not selected:
            return
        self._apply_scan_item(selected[0])

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
        metadata = metadata or {}
        model_name = _MODEL_ALIASES.get(model_display, model_display)
        try:
            spec = self._registry.get_spec(model_name)
        except Exception:
            logger.exception("Unknown model in scanned selection: %s", model_display)
            return None

        spec = self._clone_spec_with_scan_overrides(spec, metadata)
        spec = self._apply_cdg_full_scale_override(spec)

        selected_cmds = [
            item.data(Qt.ItemDataRole.UserRole)
            for item in self._cmd_list.selectedItems()
        ]
        selected_cmds = [c for c in selected_cmds if c and c in spec.commands and spec.commands[c].read]
        if not selected_cmds:
            selected_cmds = [name for name, cmd in spec.commands.items() if cmd.read]

        try:
            baud = int(self._baud_combo.currentText())
        except (TypeError, ValueError):
            baud = spec.default_baud

        address = self._address_spin.value()
        protocol = self._registry.make_protocol(spec, address=address)
        return {
            "spec": spec,
            "protocol": protocol,
            "port": port,
            "commands": selected_cmds,
            "poll_interval": float(self._interval_spin.value()),
            "baud_override": baud,
            "rs485_enabled": self._rs485_radio.isChecked(),
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
            if sc.isRunning():
                sc.requestInterruption()
                # Some serial drivers can block a probe for several seconds.
                # Wait generously, then force-stop as a last resort so the
                # dialog cannot be destroyed while the thread is still alive.
                if not sc.wait(8000):
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
