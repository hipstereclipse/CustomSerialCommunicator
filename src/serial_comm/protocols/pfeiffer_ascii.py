"""
Pfeiffer ASCII protocol codec.

Used by: INFICON BCG450, BCG552 (gauge side), OPG550, and all
Pfeiffer Electronic Drive Units (TC600, etc.).

Wire format (PM 800 488 BN / TIRA89E1):
  [Addr:3][Action:2][Param:3][DataLen:2][Data:n][Checksum:3]\\r

  Addr      3-digit ASCII device address (001–255)
  Action    2 chars: "00" = read request, "10" = write/response
  Param     3-digit ASCII parameter number
  DataLen   2-digit ASCII byte count of Data field
  Data      ASCII payload; "=?" (len 02) for reads
  Checksum  (sum of all preceding bytes) % 256, zero-padded to 3 digits
  \\r        carriage return (0x0D) — frame terminator

Error responses from device: Data = "NO_DEF", "_RANGE", "_LOGIC"
"""

from __future__ import annotations

import logging
import struct
from typing import Any

from serial_comm.models import GaugeReading
from serial_comm.protocols.base import GaugeProtocol

logger = logging.getLogger(__name__)

TERMINATOR = b"\r"
READ_ACTION = "00"
WRITE_ACTION = "10"
READ_PAYLOAD = "=?"

# Error substrings returned in the data field
_ERROR_SUBSTRINGS = ("NO_DEF", "_RANGE", "_LOGIC")


def _checksum(frame_str: str) -> int:
    """Sum of ASCII ordinals of every character in frame_str, mod 256."""
    return sum(ord(c) for c in frame_str) % 256


class PfeifferAsciiProtocol(GaugeProtocol):
    """
    Generic codec for any device that speaks the Pfeiffer ASCII parameter
    protocol.  The command table (param numbers, data types) is injected
    at construction time via *param_table*.

    param_table format::

        {
            "pressure": {
                "pid": 340,
                "data_type": "u_expo_new",  # see _decode_value
                "unit": "mbar",
                "read": True,
                "write": False,
            },
            ...
        }
    """

    def __init__(
        self,
        address: int = 1,
        param_table: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(address)
        self._params: dict[str, dict[str, Any]] = param_table or {}

    # ------------------------------------------------------------------
    # GaugeProtocol interface
    # ------------------------------------------------------------------

    def build_request(self, command: str, value: Any = None) -> bytes:
        spec = self._params.get(command)
        if spec is None:
            raise ValueError(f"PfeifferAsciiProtocol: unknown command '{command}'")

        pid = spec["pid"]
        if value is None:
            action = READ_ACTION
            data = READ_PAYLOAD
        else:
            if not spec.get("write", False):
                raise ValueError(f"Command '{command}' (PID {pid}) is read-only")
            action = WRITE_ACTION
            data = self._encode_value(value, spec.get("data_type", "string"))

        data_len = f"{len(data):02d}"
        frame_body = f"{self.address:03d}{action}{pid:03d}{data_len}{data}"
        csum = _checksum(frame_body)
        frame = f"{frame_body}{csum:03d}\r"
        return frame.encode("ascii")

    def parse_response(self, raw: bytes, command: str) -> GaugeReading:
        if not raw:
            return self._err("No response received", raw)

        try:
            text = raw.rstrip(b"\r").decode("ascii")
        except UnicodeDecodeError:
            return self._err(f"Non-ASCII response: {raw!r}", raw)

        # Minimum frame length: 3+2+3+2+0+3 = 13 chars (empty data)
        if len(text) < 13:
            return self._err(f"Response too short: {text!r}", raw)

        # Verify checksum
        frame_body = text[:-3]
        csum_field = text[-3:]
        try:
            expected = _checksum(frame_body)
            received = int(csum_field)
        except ValueError:
            return self._err(f"Malformed checksum field: {csum_field!r}", raw)
        if expected != received:
            return self._err(
                f"Checksum mismatch: expected {expected:03d}, got {received:03d}", raw
            )

        # Parse fields
        # addr = text[0:3]
        # action = text[3:5]
        # pid_str = text[5:8]
        data_len_str = text[8:10]
        try:
            data_len = int(data_len_str)
        except ValueError:
            return self._err(f"Bad data length field: {data_len_str!r}", raw)
        data = text[10 : 10 + data_len]

        # Check for device error
        for err in _ERROR_SUBSTRINGS:
            if err in data:
                return self._err(f"Device error: {data}", raw)

        # Decode
        spec = self._params.get(command, {})
        return self._decode_value(data, spec.get("data_type", "string"),
                                  spec.get("unit", ""), raw)

    # ------------------------------------------------------------------
    # Value encoding / decoding
    # ------------------------------------------------------------------

    @staticmethod
    def _encode_value(value: Any, data_type: str) -> str:
        """Encode *value* to the ASCII string that goes in the Data field."""
        if data_type == "boolean_old":
            return "111111" if value else "000000"
        if data_type == "boolean_new":
            return "1" if value else "0"
        if data_type == "u_integer":
            return f"{int(value):06d}"
        if data_type == "u_short_int":
            return f"{int(value):03d}"
        if data_type == "u_real":
            # Fixed-point 4.2: e.g. 1234.56 → "123456"
            return f"{round(float(value) * 100):06d}"
        if data_type in ("u_expo", "u_expo_new"):
            # 6-char exponential: mantissa + exponent, e.g. "1.2E-6"
            return f"{float(value):.2E}"
        # Default: string, padded/truncated to 6
        return str(value)[:6].ljust(6)

    @staticmethod
    def _decode_value(data: str, data_type: str, unit: str, raw: bytes) -> GaugeReading:
        """Decode the Data field string into a GaugeReading."""
        try:
            if data_type == "boolean_old":
                v = 1.0 if data.strip("0") else 0.0
                return GaugeReading(success=True, value=v, formatted=str(bool(v)), raw=raw)
            if data_type == "boolean_new":
                v = float(data.strip())
                return GaugeReading(success=True, value=v, formatted=str(bool(v)), raw=raw)
            if data_type == "u_integer":
                v = float(int(data))
                return GaugeReading(success=True, value=v, unit=unit,
                                    formatted=f"{int(v)} {unit}".strip(), raw=raw)
            if data_type == "u_short_int":
                v = float(int(data))
                return GaugeReading(success=True, value=v, unit=unit,
                                    formatted=f"{int(v)} {unit}".strip(), raw=raw)
            if data_type == "u_real":
                # Fixed-point 4.2
                v = int(data) / 100.0
                return GaugeReading(success=True, value=v, unit=unit,
                                    formatted=f"{v:.2f} {unit}".strip(), raw=raw)
            if data_type == "u_expo":
                v = float(data)
                return GaugeReading(success=True, value=v, unit=unit,
                                    formatted=f"{v:.3E} {unit}".strip(), raw=raw)
            if data_type == "u_expo_new":
                # e.g. "456711" → 4.567 × 10^(11-20) = 4.567e-9
                # Format: first 4 digits are mantissa (/1000), last 2 are exponent offset by 20
                if len(data) == 6 and data.isdigit():
                    mantissa = int(data[:4]) / 1000.0
                    exponent = int(data[4:]) - 20
                    v = mantissa * (10 ** exponent)
                else:
                    v = float(data)
                return GaugeReading(success=True, value=v, unit=unit,
                                    formatted=f"{v:.3E} {unit}".strip(), raw=raw)
            # string / fallback
            return GaugeReading(success=True, formatted=data.strip(), raw=raw,
                                extra={"text": data.strip()})
        except Exception as exc:
            return GaugeReading(success=False,
                                error=f"Decode error ({data_type}): {exc}", raw=raw)
