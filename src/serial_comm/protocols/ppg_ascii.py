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

    # Fallback table: command -> (mnemonic, writable, unit)
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
        # _cmd_table entries: (mnemonic, writable, unit, query_param, write_prefix)
        # query_param is appended after '?' on a read, e.g. "P?CMB\" or "Q?CONFIG\"
        # write_prefix is prepended to write payloads, e.g. SPV!"1," + <value>
        self._cmd_table: dict[str, tuple[str, bool, str, str, str]] = {}
        self._runtime_mnemonic_override: dict[str, str] = {}
        self._runtime_query_param_override: dict[str, str] = {}
        self._pressure_fallback_tried: set[str] = set()
        if param_table:
            for name, info in param_table.items():
                mn = info.get("mnemonic")
                if mn:
                    self._cmd_table[name] = (
                        mn,
                        bool(info.get("write", False)),
                        info.get("unit", ""),
                        str(info.get("query_param", "") or ""),
                        str(info.get("write_prefix", "") or ""),
                    )
        else:
            for name, (mn, wr, un) in self._MNEMONIC_DEFAULTS.items():
                self._cmd_table[name] = (mn, wr, un, "", "")

    # ------------------------------------------------------------------
    # GaugeProtocol interface
    # ------------------------------------------------------------------

    def build_request(self, command: str, value: Any = None) -> bytes:
        entry = self._cmd_table.get(command)
        if entry is None:
            raise ValueError(f"PPGProtocol: unknown command '{command}'")
        mnemonic, writable, _, query_param, write_prefix = entry
        mnemonic = self._runtime_mnemonic_override.get(command, mnemonic)
        query_param = self._runtime_query_param_override.get(command, query_param)
        if value is not None and not writable:
            raise ValueError(f"PPGProtocol: command '{command}' is read-only")
        if value is None:
            action = "?"
            suffix = query_param
        else:
            action = "!"
            suffix = f"{write_prefix}{value}"
        frame = f"@{self.address:03d}{mnemonic}{action}{suffix}\\"
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

        # Some PPG firmware variants include the address in responses,
        # e.g. "@253ACK...\\". Normalize to "@ACK..." / "@NAK...".
        if (
            len(body) >= 7
            and body.startswith(b"@")
            and body[1:4].isdigit()
            and body[4:7] in (b"ACK", b"NAK")
        ):
            body = b"@" + body[4:]

        if body.startswith(NAK_PREFIX):
            reason = body[len(NAK_PREFIX):].decode("ascii", errors="replace").strip()
            self._maybe_adapt_on_unknown_command(command, reason)
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
            entry = self._cmd_table.get(command, ("", False, "", "", ""))
            unit = entry[2]
            # Route by unit or command name
            if unit in ("mbar", "Torr", "Pa", "hPa", "psi"):
                return self._decode_pressure(data, raw, unit)
            if unit in ("°C", "degC"):
                v = float(data)
                return self._ok(v, unit, f"{v:.1f} {unit}", raw)
            return GaugeReading(success=True, formatted=data or "OK", raw=raw,
                                extra={"text": data})
        except Exception as exc:
            return self._err(f"Parse error for '{command}': {exc}", raw)

    def _maybe_adapt_on_unknown_command(self, command: str, reason: str) -> None:
        """
        Auto-recover from pressure command variant mismatches.

        Recovery paths:
        - pressure: alternate mnemonic (PR3 <-> P)
        - pressure_combined: downgrade from P?CMB to P?
        """
        if command not in ("pressure", "pressure_combined"):
            return
        normalized = reason.replace(" ", "").upper()
        if "UNKNOWNCOMMAND" not in normalized:
            return

        if command == "pressure_combined":
            if self._runtime_query_param_override.get("pressure_combined") != "":
                self._runtime_query_param_override["pressure_combined"] = ""
                self._runtime_mnemonic_override["pressure_combined"] = "P"
                logger.warning(
                    "PPGProtocol switched pressure_combined from 'P?CMB' to 'P?' after UNKNOWNCOMMAND"
                )
            return

        base = self._cmd_table.get("pressure")
        if base is None:
            return
        current = self._runtime_mnemonic_override.get("pressure", base[0]).upper()

        if current == "PR3" and "P" not in self._pressure_fallback_tried:
            self._runtime_mnemonic_override["pressure"] = "P"
            self._pressure_fallback_tried.add("P")
            logger.warning("PPGProtocol switched pressure mnemonic to 'P' after UNKNOWNCOMMAND")
            return
        if current == "P" and "PR3" not in self._pressure_fallback_tried:
            self._runtime_mnemonic_override["pressure"] = "PR3"
            self._pressure_fallback_tried.add("PR3")
            logger.warning("PPGProtocol switched pressure mnemonic to 'PR3' after UNKNOWNCOMMAND")

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
