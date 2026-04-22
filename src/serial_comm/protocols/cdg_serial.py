"""
CDG serial protocol codec — INFICON SKY CDG025D / CDG045D / CDG100D / CDG160D / CDG200D.

Wire format (per INFICON "Communication Protocol RS232C Interface", TIRA49E1):

  Command (5 bytes, host -> gauge):
    Byte 0  0x03 (fixed)
    Byte 1  Service command: 0x00=read, 0x10=write, 0x40=special
    Byte 2  Address / register number
    Byte 3  Data byte
    Byte 4  Checksum: sum(bytes[1:4]) mod 256

  Response (9 bytes, gauge -> host, streamed continuously):
    Byte 0  0x07 (fixed)
    Byte 1  Page number
    Byte 2  Status byte (setpoint/unit/zero bits)
    Byte 3  Error byte (bit field, see _STATUS_FATAL_MASK)
    Byte 4  Measurement high byte
    Byte 5  Measurement low byte
    Byte 6  Read command echo
    Byte 7  Sensor type code (also full-scale range code)
    Byte 8  Checksum: sum(bytes[1:8]) mod 256

  Pressure (engineering units):

        p = signed_int16(bytes[4:6]) / 16384 * Full_Scale

  Callers supply ``full_scale_mbar`` when the factory range is known.

Error byte bits:
    0x01  underrange          (soft)
    0x02  overrange           (soft)
    0x04  zero adjust running (soft)
    0x08  fs adjust running   (soft)
    0x10  sync / protocol     FATAL
    0x20  bad measurement     FATAL
    0x40  sensor fault        FATAL
    0x80  sensor not ready    (soft — normal for unheated variants)
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

# Error-byte bits that invalidate a reading
_STATUS_FATAL_MASK = 0x70  # sync | bad measurement | sensor fault

# Sensor-type byte values -> nominal gauge family (advisory only).
_GAUGE_TYPE_MAP: dict[int, str] = {
    0: "CDG025D",
    1: "CDG045D",
    2: "CDG100D",
    3: "CDG160D",
    4: "CDG200D",
    0x0B: "HPG400",
}


class CDGProtocol(GaugeProtocol):
    """Codec for the INFICON SKY CDG capacitance diaphragm gauges."""

    # Full command table covering every RS232 command in TIRA49E1.
    _COMMANDS: dict[str, dict[str, Any]] = {
        # Measurement / streaming
        "pressure":        {"service": 0x00, "addr": 0x00, "write": False,
                            "description": "Start continuous pressure output"},
        "data_tx_mode":    {"service": 0x10, "addr": 0x01, "write": True,
                            "description": "Set continuous-output data-TX mode"},
        # Identification
        "cdg_type":        {"service": 0x00, "addr": 0x3B, "write": False,
                            "description": "Query gauge full-scale / type code"},
        "firmware":        {"service": 0x00, "addr": 0x10, "write": False,
                            "description": "Query firmware revision"},
        "serial_number":   {"service": 0x00, "addr": 0x11, "write": False,
                            "description": "Query serial number"},
        # Unit & setpoints
        "unit":            {"service": 0x10, "addr": 0x04, "write": True,
                            "description": "Set pressure unit (0=mbar,1=Torr,2=Pa)"},
        "setpoint_1_low":  {"service": 0x10, "addr": 0x20, "write": True,
                            "description": "Set Setpoint 1 lower threshold"},
        "setpoint_1_high": {"service": 0x10, "addr": 0x21, "write": True,
                            "description": "Set Setpoint 1 upper threshold"},
        "setpoint_2_low":  {"service": 0x10, "addr": 0x22, "write": True,
                            "description": "Set Setpoint 2 lower threshold"},
        "setpoint_2_high": {"service": 0x10, "addr": 0x23, "write": True,
                            "description": "Set Setpoint 2 upper threshold"},
        "setpoint_1_read": {"service": 0x00, "addr": 0x20, "write": False,
                            "description": "Read Setpoint 1 thresholds"},
        "setpoint_2_read": {"service": 0x00, "addr": 0x22, "write": False,
                            "description": "Read Setpoint 2 thresholds"},
        # Service (code 0x40)
        "reset":           {"service": 0x40, "addr": 0x00, "write": True,
                            "description": "Power-cycle reset"},
        "factory_reset":   {"service": 0x40, "addr": 0x01, "write": True,
                            "description": "Restore factory defaults"},
        "zero_adjust":     {"service": 0x40, "addr": 0x02, "write": True,
                            "description": "Execute zero-offset adjustment"},
        "fs_adjust":       {"service": 0x40, "addr": 0x03, "write": True,
                            "description": "Execute full-scale adjustment"},
        "clear_zero":      {"service": 0x40, "addr": 0x04, "write": True,
                            "description": "Clear user zero adjustment"},
    }

    def __init__(
        self,
        gauge_type: str = "CDG045D",
        address: int = 0,
        full_scale_mbar: float = 1.0,
    ) -> None:
        super().__init__(address)
        self.gauge_type = gauge_type
        self.full_scale_mbar = float(full_scale_mbar) if full_scale_mbar else 1.0

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
        if value is not None and spec.get("write"):
            data_byte = int(value) & 0xFF

        msg = bytearray([COMMAND_START, service, addr, data_byte])
        csum = sum(msg[1:4]) & 0xFF
        msg.append(csum)
        return bytes(msg)

    def parse_response(self, raw: bytes, command: str) -> GaugeReading:
        if command == "pressure":
            return self.parse_continuous(raw)

        frame = self._validate_frame(raw)
        if isinstance(frame, GaugeReading):
            return frame

        raw = frame
        data_u16 = int.from_bytes(raw[4:6], byteorder="big", signed=False)

        if command in {"setpoint_1_read", "setpoint_2_read"}:
            low = raw[4]
            high = raw[5]
            return GaugeReading(
                success=True,
                value=float(data_u16),
                unit="raw",
                formatted=f"low={low}, high={high}",
                raw=raw,
                extra={"low": low, "high": high},
            )

        if command in {"firmware", "software_version"}:
            major = raw[4]
            minor = raw[5]
            return GaugeReading(
                success=True,
                formatted=f"v{major}.{minor:02d}",
                raw=raw,
            )

        if command == "serial_number":
            serial = int.from_bytes(raw[4:8], byteorder="big", signed=False)
            return GaugeReading(
                success=True,
                formatted=str(serial),
                raw=raw,
            )

        # For write/service commands, treat a valid frame as acknowledgement.
        return GaugeReading(
            success=True,
            formatted="ACK",
            raw=raw,
            extra={"status": raw[2], "error_byte": raw[3]},
        )

    def parse_continuous(self, raw: bytes) -> GaugeReading:
        """Parse a 9-byte continuous-output frame from the CDG."""
        frame = self._validate_frame(raw)
        if isinstance(frame, GaugeReading):
            return frame

        raw = frame

        page = raw[1]
        status = raw[2]
        err_byte = raw[3]

        # Only fatal error bits block the reading
        if err_byte & _STATUS_FATAL_MASK:
            return self._err(f"Gauge fault (error byte 0x{err_byte:02X})", raw)

        meas = int.from_bytes(raw[4:6], byteorder="big", signed=True)
        ratio = meas / 16384.0
        pressure = ratio * self.full_scale_mbar

        sensor_code = raw[7]
        gauge_type = _GAUGE_TYPE_MAP.get(sensor_code, f"code_0x{sensor_code:02X}")

        warnings: list[str] = []
        if err_byte & 0x01:
            warnings.append("underrange")
        if err_byte & 0x02:
            warnings.append("overrange")
        if err_byte & 0x04:
            warnings.append("zero adjust running")
        if err_byte & 0x08:
            warnings.append("fs adjust running")
        if err_byte & 0x80:
            warnings.append("sensor not ready")

        return GaugeReading(
            success=True,
            value=pressure,
            unit="mbar",
            formatted=f"{pressure:.4E} mbar",
            raw=raw,
            extra={
                "gauge_type": gauge_type,
                "sensor_code": sensor_code,
                "page": page,
                "status": status,
                "error_byte": err_byte,
                "ratio": ratio,
                "full_scale_mbar": self.full_scale_mbar,
                "warnings": warnings,
            },
        )

    def _validate_frame(self, raw: bytes) -> bytes | GaugeReading:
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
        return raw

    # ------------------------------------------------------------------
    # Identification helpers
    # ------------------------------------------------------------------

    @staticmethod
    def detect_gauge_type(raw: bytes) -> str | None:
        if len(raw) == RESPONSE_LENGTH and raw[0] == RESPONSE_SYNC:
            return _GAUGE_TYPE_MAP.get(raw[7])
        return None

    @staticmethod
    def is_cdg_frame(raw: bytes) -> bool:
        """Structural sanity check: 9 bytes, sync byte, checksum matches."""
        if len(raw) != RESPONSE_LENGTH or raw[0] != RESPONSE_SYNC:
            return False
        return (sum(raw[1:8]) & 0xFF) == raw[8]

