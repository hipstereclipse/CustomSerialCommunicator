"""Core data models shared across the whole library."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class DeviceReading:
    """A single timestamped measurement from one gauge."""

    device_id: str          # unique per session: "<port>:<address>"
    timestamp_mono: float   # time.monotonic() — for interval maths
    timestamp_wall: datetime  # UTC wall clock — for export
    value: float
    unit: str               # display unit, e.g. "mbar"
    command: str            # command that produced this reading, e.g. "pressure"
    raw: bytes = field(default=b"", repr=False)


@dataclass(frozen=True)
class DeviceError:
    """An error or warning from a device or the transport layer."""

    device_id: str
    timestamp_mono: float
    timestamp_wall: datetime
    message: str
    recoverable: bool = True  # False → disconnect; True → log and retry


@dataclass(frozen=True)
class GaugeReading:
    """Parsed result from a protocol codec's parse_response()."""

    success: bool
    value: float | None = None
    unit: str = ""
    formatted: str = ""
    error: str | None = None
    raw: bytes = field(default=b"", repr=False)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class DeviceSpec:
    """
    Resolved device specification loaded from a YAML file.

    This is what the device registry returns; callers should not construct
    DeviceSpec directly — use DeviceRegistry.get_spec(model).
    """

    model: str
    family: str
    protocol: str
    default_baud: int
    parity: str           # "N", "E", "O"
    data_bits: int
    stop_bits: int
    rs_modes: list[str]   # ["RS232"] or ["RS232", "RS485"]
    default_address: int
    rs485_address_range: tuple[int, int] | None
    commands: dict[str, "CommandSpec"]
    experimental: bool = False


@dataclass(frozen=True)
class TerminalEntry:
    """One request/response exchange captured by the terminal."""

    request: bytes
    response: bytes
    timestamp: datetime
    command: str = ""        # empty for raw custom frames
    error: str | None = None


@dataclass
class CommandSpec:
    """Specification for a single command on a device."""

    name: str
    read: bool
    write: bool
    unit: str = ""
    description: str = ""
    # Binary/pfeiffer_ascii protocols:
    pid: int | None = None
    data_type: str | None = None  # boolean_old | u_integer | u_real | u_expo | string | ...
    # PPG ASCII protocol:
    mnemonic: str | None = None
    # Constraints:
    min_value: float | None = None
    max_value: float | None = None
    scale: float = 1.0            # multiply raw value before display
    options: list[dict[str, Any]] = field(default_factory=list)
    experimental: bool = False
