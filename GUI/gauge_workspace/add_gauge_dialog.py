"""
AddGaugeDialog — dialog for connecting a new gauge.

Fields:
  - Model (combo, populated from DeviceRegistry)
  - Port (combo, from serial.tools.list_ports)
  - Address (spin, default from spec)
  - Poll interval (double spin, seconds)
  - Commands to poll (multi-select list)
"""

from __future__ import annotations

import logging

import serial.tools.list_ports
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog, QDialogButtonBox, QFormLayout, QComboBox,
    QSpinBox, QDoubleSpinBox, QListWidget, QListWidgetItem,
    QLabel, QVBoxLayout, QGroupBox,
)

from serial_comm.device_registry import DeviceRegistry
from serial_comm.models import DeviceSpec

logger = logging.getLogger(__name__)


class AddGaugeDialog(QDialog):
    """Modal dialog for selecting and configuring a gauge connection."""

    def __init__(self, registry: DeviceRegistry, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add Gauge")
        self.setMinimumWidth(380)
        self._registry = registry
        self._spec: DeviceSpec | None = None

        self._build_ui()
        self._populate_models()
        self._populate_ports()

    # ------------------------------------------------------------------
    # Build UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        # Model selector
        self._model_combo = QComboBox()
        self._model_combo.currentTextChanged.connect(self._on_model_changed)
        form.addRow("Model:", self._model_combo)

        # Port selector
        self._port_combo = QComboBox()
        form.addRow("Port:", self._port_combo)

        # Address spin
        self._address_spin = QSpinBox()
        self._address_spin.setRange(0, 255)
        self._address_spin.setValue(254)
        form.addRow("Address:", self._address_spin)

        # Poll interval
        self._interval_spin = QDoubleSpinBox()
        self._interval_spin.setRange(0.1, 60.0)
        self._interval_spin.setSingleStep(0.1)
        self._interval_spin.setDecimals(1)
        self._interval_spin.setValue(1.0)
        self._interval_spin.setSuffix(" s")
        form.addRow("Poll interval:", self._interval_spin)

        layout.addLayout(form)

        # Commands group
        cmd_group = QGroupBox("Commands to poll")
        cmd_layout = QVBoxLayout(cmd_group)
        self._cmd_list = QListWidget()
        self._cmd_list.setSelectionMode(QListWidget.SelectionMode.MultiSelection)
        self._cmd_list.setMaximumHeight(140)
        cmd_layout.addWidget(self._cmd_list)
        layout.addWidget(cmd_group)

        # Experimental warning label
        self._exp_label = QLabel()
        self._exp_label.setStyleSheet("color: orange;")
        self._exp_label.hide()
        layout.addWidget(self._exp_label)

        # Buttons
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

        # Populate commands — pre-select readable commands
        self._cmd_list.clear()
        for cmd_name, cmd in spec.commands.items():
            if cmd.read:
                item = QListWidgetItem(cmd_name)
                item.setData(Qt.ItemDataRole.UserRole, cmd_name)
                self._cmd_list.addItem(item)
                # Pre-select "pressure" and "temperature"
                if cmd_name in ("pressure", "temperature"):
                    item.setSelected(True)

        # Show experimental warning
        if spec.experimental:
            self._exp_label.setText(
                f"⚠  {spec.model} support is experimental — use with caution."
            )
            self._exp_label.show()
        else:
            self._exp_label.hide()

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
            # Fall back to all readable commands
            selected_cmds = [n for n, c in spec.commands.items() if c.read]

        address = self._address_spin.value()
        protocol = self._registry.make_protocol(spec, address=address)

        return {
            "spec": spec,
            "protocol": protocol,
            "port": self._port_combo.currentText(),
            "commands": selected_cmds,
            "poll_interval": self._interval_spin.value(),
        }
