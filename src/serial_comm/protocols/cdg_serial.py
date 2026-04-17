"""
CDG serial protocol codec — INFICON SKY CDG025D / CDG045D / CDG100D / CDG160D / CDG200D.

Wire format:
  Command (5 bytes):
    Byte 0  0x03 (fixed)
    Byte 1  Service command: 0x00=read, 0x10=write, 0x40=special
    Byte 2  Address / register number
    Byte 3  Data byte
    Byte 4  Checksum: sum(bytes[1:4]) mod 256

  Response (9 bytes, continuous output):
    Byte 0  0x07 (fixed)
    Byte 1  Page number
    Byte 2  Status
    Byte 3  Error byte (0 = OK)
    Byte 4  Measurement high byte
    Byte 5  Measurement low byte
    Byte 6  Read command echo
    Byte 7  Sensor type code
    Byte 8  Checksum: sum(bytes[1:8]) mod 256

  Pressure = signed_int16(bytes[4:6]) / 16384.0  [in mbar at full scale]
"""

from __future__ import annotations

import logging
from typing import Any

from serial_comm.models import GaugeReading
from serial_comm.protocols.base import GaugeProtocol

logger = logging.getLogger(__name__)

RESPONSE_LENGTH = 9
RESPONSE_SYNC = 0x07
COMMAND_START = 0x03

_GAUGE_TYPE_MAP: dict[int, str] = {
    0: "CDG025D",
    1: "CDG045D",
    2: "CDG100D",
    3: "CDG160D",
    4: "CDG200D",
}


class CDGProtocol(GaugeProtocol):
    """
    Codec for the INFICON SKY CDG capacitance diaphragm gauges.

    The CDG sends data continuously; the host sends 5-byte frames to configure
    or query the gauge.  This codec handles both directions.
    """

    def __init__(self, gauge_type: str = "CDG045D", address: int = 0) -> None:
        super().__init__(address)
        self.gauge_type = gauge_type

    # Command address table (service command byte 2 = register address)
    _COMMANDS: dict[str, dict[str, Any]] = {
        "pressure":       {"service": 0x00, "addr": 0x00, "write": False},
        "data_tx_mode":   {"service": 0x10, "addr": 0x01, "write": True},
        "reset":          {"service": 0x40, "addr": 0x00, "write": True},
        "factory_reset":  {"service": 0x40, "addr": 0x01, "write": True},
        "zero_adjust":    {"service": 0x40, "addr": 0x02, "write": True},
        "cdg_type":       {"service": 0x00, "addr": 59,   "write": False},
    }

    # ------------------------------------------------------------------
    # GaugeProtocol interface
    # ------------------------------------------------------------------

    def supports_continuous_output(self) -> bool:
        return True

    def build_request(self, command: str, value: Any = None) -> bytes:
        spec = self._COMMANDS.get(command)
        if spec is None:
            raise ValueError(f"CDGProtocol: unknown command '{command}'")

        service = spec["service"]
        addr = spec["addr"]
        data_byte = 0x00

        if command == "data_tx_mode" and value is not None:
            data_byte = int(value) & 0xFF
        elif value is not None and spec.get("write"):
            data_byte = int(value) & 0xFF

        msg = bytearray([COMMAND_START, service, addr, data_byte])
        csum = sum(msg[1:4]) & 0xFF
        msg.append(csum)
        return bytes(msg)

    def parse_response(self, raw: bytes, command: str) -> GaugeReading:
        return self.parse_continuous(raw)

    def parse_continuous(self, raw: bytes) -> GaugeReading:
        """Parse a 9-byte continuous-output frame from the CDG."""
        if not raw:
            return self._err("No response received")
        if len(raw) != RESPONSE_LENGTH:
            return self._err(f"Expected {RESPONSE_LENGTH} bytes, got {len(raw)}", raw)
        if raw[0] != RESPONSE_SYNC:
            return self._err(f"Invalid sync byte: 0x{raw[0]:02X}", raw)

        calc_csum = sum(raw[1:8]) & 0xFF
        if calc_csum != raw[8]:
            return self._err(
                f"Checksum error: expected {calc_csum:02X}, got {raw[8]:02X}", raw
            )

        err_byte = raw[3]
        if err_byte != 0:
            return self._err(f"Gauge error byte: 0x{err_byte:02X}", raw)

        meas = (raw[4] << 8) | raw[5]
        # Signed 16-bit interpretation
        if meas >= 0x8000:
            meas -= 0x10000
        pressure = meas / 16384.0  # mbar at full scale

        sensor_code = raw[7]
        gauge_type = _GAUGE_TYPE_MAP.get(sensor_code, f"unknown({sensor_code})")

        return GaugeReading(
            success=True,
            value=pressure,
            unit="mbar",
            formatted=f"{pressure:.4f} mbar",
            raw=raw,
            extra={"gauge_type": gauge_type, "page": raw[1], "status": raw[2]},
        )

    def detect_gauge_type(self, raw: bytes) -> str | None:
        """Return the model string encoded in byte 7 of a CDG response, or None."""
        if len(raw) == RESPONSE_LENGTH and raw[0] == RESPONSE_SYNC:
            return _GAUGE_TYPE_MAP.get(raw[7])
        return None
