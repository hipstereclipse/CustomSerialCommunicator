"""
PPG ASCII protocol codec — INFICON PPG550 / PPG570.

Wire format:
  Request:  @{addr:03d}{mnemonic}{action}{value}\\
  Response: @ACK{data}\\  or  @NAK\\

Where:
  addr      3-digit decimal device address (254 = broadcast / RS-232 default)
  mnemonic  ASCII command string, e.g. "PR3", "T", "FV"
  action    "?" for read, "!" for write
  value     present only on write; omitted on read
  \\         ASCII backslash (0x5C) — frame terminator, no checksum
"""

from __future__ import annotations

import logging
from typing import Any

from serial_comm.models import GaugeReading
from serial_comm.protocols.base import GaugeProtocol

logger = logging.getLogger(__name__)

TERMINATOR = b"\\"
ACK_PREFIX = b"@ACK"
NAK_PREFIX = b"@NAK"

# PPG570 adds atmospheric-sensor commands
_PPG570_EXTRAS = {"atm_pressure", "atm_zero", "atm_full_scale"}


class PPGProtocol(GaugeProtocol):
    """
    Codec for the INFICON PPG-series Pirani/Piezo gauges.

    The command table is driven by the device spec loaded by DeviceRegistry;
    this class only handles framing, not the command list.
    """

    def __init__(self, address: int = 254, gauge_type: str = "PPG550") -> None:
        super().__init__(address)
        self.gauge_type = gauge_type

    # Mnemonic map: command name → (mnemonic, supports_write)
    # Keep in sync with device_specs/gauges/ppg550.yaml
    _MNEMONIC: dict[str, tuple[str, bool]] = {
        "pressure":         ("PR3", False),
        "temperature":      ("T",   False),
        "software_version": ("FV",  False),
        "serial_number":    ("SN",  False),
        "unit":             ("U",   True),
        "zero_adjust":      ("VAC", True),
        "piezo_adjust":     ("FS",  True),
        # PPG570 extras
        "atm_pressure":     ("PR4", False),
        "combined_pressure":("PR1", False),
        "atm_zero":         ("ATZ", True),
        "atm_full_scale":   ("ATD", True),
    }

    # ------------------------------------------------------------------
    # GaugeProtocol interface
    # ------------------------------------------------------------------

    def build_request(self, command: str, value: Any = None) -> bytes:
        entry = self._MNEMONIC.get(command)
        if entry is None:
            raise ValueError(f"PPGProtocol: unknown command '{command}'")
        mnemonic, writable = entry
        if value is not None and not writable:
            raise ValueError(f"PPGProtocol: command '{command}' is read-only")

        action = "?" if value is None else "!"
        val_str = "" if value is None else str(value)
        frame = f"@{self.address:03d}{mnemonic}{action}{val_str}\\"
        return frame.encode("ascii")

    def parse_response(self, raw: bytes, command: str) -> GaugeReading:
        if not raw:
            return self._err("No response received", raw)
        if not raw.endswith(TERMINATOR):
            return self._err(f"Missing terminator in response: {raw!r}", raw)

        body = raw[:-1]  # strip backslash

        if body.startswith(NAK_PREFIX):
            return self._err(f"NAK from device: {raw!r}", raw)
        if not body.startswith(ACK_PREFIX):
            return self._err(f"Unexpected response prefix: {raw!r}", raw)

        data_str = body[len(ACK_PREFIX):].decode("ascii", errors="replace").strip()
        return self._decode(command, data_str, raw)

    # ------------------------------------------------------------------
    # Decoding
    # ------------------------------------------------------------------

    def _decode(self, command: str, data: str, raw: bytes) -> GaugeReading:
        try:
            if command in ("pressure", "atm_pressure", "combined_pressure"):
                return self._decode_pressure(data, raw)
            if command == "temperature":
                v = float(data)
                return self._ok(v, "°C", f"{v:.1f} °C", raw)
            if command == "unit":
                return GaugeReading(success=True, formatted=data, raw=raw,
                                    extra={"unit_code": data})
            if command in ("software_version", "serial_number"):
                return GaugeReading(success=True, formatted=data, raw=raw,
                                    extra={"text": data})
            if command in ("zero_adjust", "piezo_adjust", "atm_zero", "atm_full_scale"):
                return GaugeReading(success=True, formatted=data or "OK", raw=raw)
            # Fallback: return raw string
            return GaugeReading(success=True, formatted=data, raw=raw)
        except Exception as exc:
            return self._err(f"Parse error for '{command}': {exc}", raw)

    @staticmethod
    def _decode_pressure(data: str, raw: bytes) -> GaugeReading:
        """
        PPG pressure data is a float in scientific notation, e.g. "1.23E-3".
        The device also returns status strings like "UR" (under-range) or
        "OR" (over-range); these are reported as errors.
        """
        upper = data.strip().upper()
        if upper in ("UR", "UNDERRANGE", "NO SENSOR"):
            return GaugeReading(success=False, error=f"Gauge status: {data}", raw=raw)
        if upper in ("OR", "OVERRANGE"):
            return GaugeReading(success=False, error=f"Gauge status: {data}", raw=raw)
        try:
            v = float(data)
        except ValueError:
            return GaugeReading(success=False, error=f"Cannot parse pressure: {data!r}", raw=raw)
        return GaugeReading(success=True, value=v, unit="mbar",
                            formatted=f"{v:.3E} mbar", raw=raw)
