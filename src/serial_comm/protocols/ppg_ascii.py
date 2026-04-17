"""
PPG ASCII protocol codec — INFICON PPG550 / PPG570.

Wire format:
  Request:  @{addr:03d}{mnemonic}{action}{value}\\
  Response: @ACK{data}\\  or  @NAK{reason}\\

Where:
  addr      3-digit decimal device address (254 = broadcast / RS-232 default)
  mnemonic  ASCII command string, e.g. "PR3", "T", "FV"
  action    "?" for read, "!" for write
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

# Status strings the gauge returns instead of a numeric pressure value
_PRESSURE_STATUS = {
    "UR", "UNDERRANGE",
    "OR", "OVERRANGE",
    "NO SENSOR", "NS",
    "WAIT",
    "HV OFF",
    "LO SN",
    "ATM",
    "ERR",
    "PROG",
}


class PPGProtocol(GaugeProtocol):
    """
    Codec for the INFICON PPG-series Pirani/Piezo gauges.

    The command table is driven by the device spec loaded by DeviceRegistry
    via *param_table*.  The hardcoded ``_MNEMONIC_DEFAULTS`` are used as a
    fallback when no table is supplied (e.g. in tests).
    """

    # Fallback table: command → (mnemonic, writable, unit)
    _MNEMONIC_DEFAULTS: dict[str, tuple[str, bool, str]] = {
        "pressure":          ("PR3", False, "mbar"),
        "temperature":       ("T",   False, "°C"),
        "software_version":  ("FV",  False, ""),
        "serial_number":     ("SN",  False, ""),
        "unit":              ("U",   True,  ""),
        "zero_adjust":       ("VAC", True,  ""),
        "piezo_adjust":      ("FS",  True,  ""),
        "atm_pressure":      ("PR4", False, "mbar"),
        "combined_pressure": ("PR1", False, "mbar"),
        "atm_zero":          ("ATZ", True,  ""),
        "atm_full_scale":    ("ATD", True,  ""),
    }

    def __init__(
        self,
        address: int = 254,
        gauge_type: str = "PPG550",
        param_table: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(address)
        self.gauge_type = gauge_type
        if param_table:
            self._cmd_table: dict[str, tuple[str, bool, str]] = {}
            for name, info in param_table.items():
                mn = info.get("mnemonic")
                if mn:
                    self._cmd_table[name] = (
                        mn,
                        bool(info.get("write", False)),
                        info.get("unit", ""),
                    )
        else:
            self._cmd_table = dict(self._MNEMONIC_DEFAULTS)

    # ------------------------------------------------------------------
    # GaugeProtocol interface
    # ------------------------------------------------------------------

    def build_request(self, command: str, value: Any = None) -> bytes:
        entry = self._cmd_table.get(command)
        if entry is None:
            raise ValueError(f"PPGProtocol: unknown command '{command}'")
        mnemonic, writable, _ = entry
        if value is not None and not writable:
            raise ValueError(f"PPGProtocol: command '{command}' is read-only")
        action = "?" if value is None else "!"
        val_str = "" if value is None else str(value)
        frame = f"@{self.address:03d}{mnemonic}{action}{val_str}\\"
        logger.debug("TX %r", frame)
        return frame.encode("ascii")

    def parse_response(self, raw: bytes, command: str) -> GaugeReading:
        if not raw:
            return self._err("No response received", raw)

        # Strip any trailing CR/LF before checking the terminator
        stripped = raw.rstrip(b"\r\n")
        if not stripped.endswith(TERMINATOR):
            return self._err(
                f"Missing terminator; got {raw!r}", raw
            )

        body = stripped[:-1]  # remove trailing backslash

        if body.startswith(NAK_PREFIX):
            reason = body[len(NAK_PREFIX):].decode("ascii", errors="replace").strip()
            return self._err(f"NAK: {reason or '(no reason)'}", raw)
        if not body.startswith(ACK_PREFIX):
            return self._err(f"Unexpected prefix: {raw!r}", raw)

        data_str = body[len(ACK_PREFIX):].decode("ascii", errors="replace").strip()
        return self._decode(command, data_str, raw)

    # ------------------------------------------------------------------
    # Decoding
    # ------------------------------------------------------------------

    def _decode(self, command: str, data: str, raw: bytes) -> GaugeReading:
        try:
            _, _, unit = self._cmd_table.get(command, ("", False, ""))
            # Route by unit or command name
            if unit in ("mbar", "Torr", "Pa", "hPa", "psi"):
                return self._decode_pressure(data, raw, unit)
            if unit == "°C":
                v = float(data)
                return self._ok(v, "°C", f"{v:.1f} °C", raw)
            # Write-confirm commands return empty data or "OK"
            if not self._cmd_table.get(command, ("", True, ""))[1] is False:
                # If writable with no unit — treat as acknowledgement
                pass
            return GaugeReading(success=True, formatted=data or "OK", raw=raw,
                                extra={"text": data})
        except Exception as exc:
            return self._err(f"Parse error for '{command}': {exc}", raw)

    @staticmethod
    def _decode_pressure(data: str, raw: bytes, unit: str = "mbar") -> GaugeReading:
        upper = data.strip().upper()
        if upper in _PRESSURE_STATUS:
            return GaugeReading(success=False, error=f"Gauge status: {data}", raw=raw)
        try:
            v = float(data)
        except ValueError:
            # Unknown non-numeric string — report it without crashing
            return GaugeReading(
                success=False,
                error=f"Unexpected response: {data!r}",
                raw=raw,
            )
        return GaugeReading(
            success=True,
            value=v,
            unit=unit,
            formatted=f"{v:.3E} {unit}",
            raw=raw,
        )
