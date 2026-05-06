"""Helpers for classifying device commands for UI defaults and plotting."""

from __future__ import annotations

from serial_comm.models import CommandSpec, DeviceSpec


PRESSURE_UNITS = {"mbar", "Torr", "torr", "Pa", "hPa", "psi"}

_PRIMARY_PRESSURE_TOKENS = (
    "combined",
    "total",
    "master",
    "best",
    "primary",
)

_SUBSENSOR_TOKENS = (
    "pirani",
    "piezo",
    "capacitance",
    "diaphragm",
    "differential",
    "subsensor",
    "sub sensor",
    "sensor pressure",
)


def is_pressure_command(command: CommandSpec) -> bool:
    return command.unit in PRESSURE_UNITS


def is_primary_pressure_command(name: str, command: CommandSpec) -> bool:
    """Return True for combined/total pressure commands, not subsensor reads."""
    if not is_pressure_command(command):
        return False
    haystack = f"{name} {command.description}".replace("_", " ").lower()
    if name == "pressure":
        return True
    if any(token in haystack for token in _PRIMARY_PRESSURE_TOKENS):
        return True
    if any(token in haystack for token in _SUBSENSOR_TOKENS):
        return False
    return False


def default_poll_commands(spec: DeviceSpec) -> list[str]:
    """Default to combined/total pressure; fall back to readable pressure commands."""
    primary = [
        name for name, command in spec.commands.items()
        if command.read and is_primary_pressure_command(name, command)
    ]
    combined = [name for name in primary if "combined" in name.replace("_", " ").lower()]
    if combined:
        return combined
    if primary:
        return primary
    pressure = [
        name for name, command in spec.commands.items()
        if command.read and is_pressure_command(command)
    ]
    if pressure:
        return pressure
    return [name for name, command in spec.commands.items() if command.read]


def command_display_name(name: str) -> str:
    return name.replace("_", " ").title()
