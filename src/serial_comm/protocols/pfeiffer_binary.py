"""
Pfeiffer binary protocol codec (INFICON "PKR" / "PCG" / "BPG" / "BCG" / "MAG" / "MPG" family).

Wire format (11-byte request, variable-length response):
  Byte  0    Address (0x00 for RS-232, device address for RS-485)
  Byte  1    Device ID (model-specific constant, e.g. 0x02 for PCG)
  Byte  2    0x00
  Byte  3    Message length (number of following bytes before CRC, default 0x05)
  Byte  4    Command code: 0x01 = read, 0x03 = write
  Byte  5    PID high byte
  Byte  6    PID low byte
  Byte  7-8  Parameter bytes (0x00 0x00 for reads)
  Byte  9-10 CRC-16-CCITT (init=0xFFFF, poly=0x1021), little-endian

Response mirrors request framing; data replaces parameter bytes.

Pressure encoding (varies by sub-family):
  Fixs32en20 (PCG/PSG):  pressure = 10 ** (signed_int32 / 2**20)
  LogFixs32en26 (MAG/BPG/BCG): pressure = 10 ** (signed_int32 / 2**26)
"""

from __future__ import annotations

import logging
import struct
from typing import Any

from serial_comm.models import GaugeReading
from serial_comm.protocols.base import GaugeProtocol

logger = logging.getLogger(__name__)

_CMD_READ = 0x01
_CMD_WRITE = 0x03


def _crc16(data: bytes | bytearray) -> int:
    """CRC-16-CCITT: init=0xFFFF, poly=0x1021, MSB-first."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = (crc << 1) ^ 0x1021
            else:
                crc <<= 1
            crc &= 0xFFFF
    return crc


class PfeifferBinaryProtocol(GaugeProtocol):
    """
    Generic codec for the Pfeiffer/INFICON 11-byte binary protocol.

    *device_id*       model-specific constant (0x02, 0x04, 0x14, ...)
    *pressure_enc*    "fixs32en20" or "logfixs32en26"
    *param_table*     dict mapping command names to PID integers and metadata
    """

    def __init__(
        self,
        address: int = 0,          # 0 for RS-232
        device_id: int = 0x02,
        pressure_enc: str = "fixs32en20",
        param_table: dict[str, dict[str, Any]] | None = None,
        rs485_mode: bool = False,
    ) -> None:
        super().__init__(address)
        self.device_id = device_id
        self.pressure_enc = pressure_enc
        self.rs485_mode = rs485_mode
        self._params: dict[str, dict[str, Any]] = param_table or {}

    # ------------------------------------------------------------------
    # GaugeProtocol interface
    # ------------------------------------------------------------------

    def build_request(self, command: str, value: Any = None) -> bytes:
        spec = self._params.get(command)
        if spec is None:
            raise ValueError(f"PfeifferBinaryProtocol: unknown command '{command}'")

        pid: int = spec["pid"]
        addr = self.address if self.rs485_mode else 0x00
        cmd_code = _CMD_READ if value is None else _CMD_WRITE

        msg = bytearray([
            addr, self.device_id, 0x00, 0x05, cmd_code,
            (pid >> 8) & 0xFF, pid & 0xFF, 0x00, 0x00,
        ])

        if value is not None and not spec.get("write", False):
            raise ValueError(f"Command '{command}' (PID {pid}) is read-only")

        if value is not None:
            param_bytes = self._encode_param(value, spec.get("param_type"))
            msg.extend(param_bytes)
            msg[3] = len(msg) - 4  # update length field

        crc = _crc16(msg)
        msg.extend([crc & 0xFF, (crc >> 8) & 0xFF])
        return bytes(msg)

    def parse_response(self, raw: bytes, command: str) -> GaugeReading:
        if len(raw) < 7:
            return self._err(f"Response too short ({len(raw)} bytes)", raw)

        dev_id = raw[1]
        msg_len = raw[3]
        pid = (raw[5] << 8) | raw[6]

        if dev_id != self.device_id:
            return self._err(
                f"Device ID mismatch: expected 0x{self.device_id:02X}, got 0x{dev_id:02X}", raw
            )
        if len(raw) != msg_len + 6:
            return self._err(
                f"Length mismatch: header says {msg_len + 6} bytes, got {len(raw)}", raw
            )

        received_crc = (raw[-1] << 8) | raw[-2]
        calculated_crc = _crc16(raw[:-2])
        if received_crc != calculated_crc:
            return self._err(
                f"CRC mismatch: expected {calculated_crc:04X}, got {received_crc:04X}", raw
            )

        data = raw[7:-2]
        spec = self._params.get(command, {})
        return self._decode(pid, data, spec, raw)

    # ------------------------------------------------------------------
    # Decode
    # ------------------------------------------------------------------

    def _decode(
        self, pid: int, data: bytes, spec: dict[str, Any], raw: bytes
    ) -> GaugeReading:
        meas_type = spec.get("measurement", "")

        if meas_type == "pressure":
            return self._decode_pressure(data, raw)
        if meas_type == "temperature":
            return self._decode_temperature(data, raw)
        if meas_type == "error_flags":
            return self._decode_error_flags(data, spec.get("flag_names", []), raw)
        if len(data) == 4:
            # Try float
            try:
                v = struct.unpack(">f", data)[0]
                unit = spec.get("unit", "")
                return self._ok(v, unit, f"{v:.4g} {unit}".strip(), raw)
            except struct.error:
                pass
        return GaugeReading(success=True, formatted=data.hex(), raw=raw,
                            extra={"hex": data.hex()})

    def _decode_pressure(self, data: bytes, raw: bytes) -> GaugeReading:
        if len(data) < 4:
            return self._err(f"Pressure data too short: {data!r}", raw)
        raw_int = int.from_bytes(data[:4], byteorder="big", signed=True)
        if self.pressure_enc == "fixs32en20":
            # PCG / PSG family
            pressure = 10.0 ** (raw_int / (2 ** 20))
        else:
            # LogFixs32en26: MAG / MPG / BPG / BCG family
            pressure = 10.0 ** (raw_int / (2 ** 26))
        return self._ok(pressure, "mbar", f"{pressure:.3E} mbar", raw)

    @staticmethod
    def _decode_temperature(data: bytes, raw: bytes) -> GaugeReading:
        if len(data) == 4:
            v = struct.unpack(">f", data)[0]
            return GaugeReading(success=True, value=v, unit="°C",
                                formatted=f"{v:.1f} °C", raw=raw)
        return GaugeReading(success=True, formatted=data.hex(), raw=raw)

    @staticmethod
    def _decode_error_flags(
        data: bytes, flag_names: list[str], raw: bytes
    ) -> GaugeReading:
        flags = int.from_bytes(data, byteorder="big")
        active = [
            flag_names[i] if i < len(flag_names) else f"bit{i}"
            for i in range(len(flag_names))
            if flags & (1 << i)
        ] or ["none"]
        return GaugeReading(success=True, formatted=", ".join(active), raw=raw,
                            extra={"flags": flags, "active": active})

    @staticmethod
    def _encode_param(value: Any, param_type: str | None) -> bytes:
        if param_type == "uint8":
            return bytes([int(value) & 0xFF])
        if param_type == "uint16":
            return int(value).to_bytes(2, byteorder="big")
        if param_type == "float":
            return struct.pack(">f", float(value))
        return b""
