from __future__ import annotations

from GUI.gauge_workspace.add_gauge_dialog import AddGaugeDialog
from serial_comm.device_registry import DeviceRegistry


def test_scan_config_uses_detected_spec_default_address(qtbot) -> None:
    registry = DeviceRegistry()
    dialog = AddGaugeDialog(registry)
    qtbot.addWidget(dialog)

    dialog._address_spin.setValue(0)
    config = dialog._build_config_for("PPG550/570", "COM99", metadata={})

    assert config is not None
    assert config["spec"].model == "PPG570"
    assert config["protocol"].address == 254
    assert config["baud_override"] == 9600


def test_manual_config_honors_address_override(qtbot) -> None:
    registry = DeviceRegistry()
    dialog = AddGaugeDialog(registry)
    qtbot.addWidget(dialog)

    dialog._address_spin.setValue(12)
    config = dialog._build_config_for("PPG550/570", "COM99")

    assert config is not None
    assert config["protocol"].address == 12
