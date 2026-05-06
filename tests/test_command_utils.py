from __future__ import annotations

from serial_comm.command_utils import default_poll_commands, is_primary_pressure_command
from serial_comm.device_registry import DeviceRegistry


def test_ppg570_defaults_to_combined_pressure_commands() -> None:
    spec = DeviceRegistry().get_spec("PPG570")

    assert default_poll_commands(spec) == ["pressure_combined"]


def test_ppg570_subsensor_pressure_is_not_primary_default() -> None:
    spec = DeviceRegistry().get_spec("PPG570")

    assert not is_primary_pressure_command("pressure_pirani", spec.commands["pressure_pirani"])
    assert not is_primary_pressure_command("pressure_piezo_vac", spec.commands["pressure_piezo_vac"])


def test_opg550_defaults_to_verified_total_pressure_only() -> None:
    spec = DeviceRegistry().get_spec("OPG550")

    assert default_poll_commands(spec) == ["pressure"]
