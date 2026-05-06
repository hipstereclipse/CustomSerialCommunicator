"""
INFICON P3 V02 binary protocol codec.

Used by: INFICON Augent OPG550 (TIRB59E1) over RS-232 / RS-485.

Wire format (all integers big-endian unless noted):

    Byte  0       : ADDR        - 0x00 for RS-232, receiver address for RS-485
    Byte  1       : ID          - sender device ID (master = 0x00, OPG550 = 0x0B)
    Byte  2       : HDR         - VER(bits 7..4) | RES(bits 3..1) | ACK(bit 0)
                                  VER = 2, ACK = 0 from master, 1 from slave
    Bytes 3..4    : LEN         - big-endian 16-bit APDU length
                                  = 1 (CMD) + 2 (PID) + 2 (IDX) + len(DATA)
    Byte  5       : CMD         - 0x01 read_req, 0x02 read_resp,
                                  0x03 write_req, 0x04 write_resp
    Bytes 6..7    : PID         - big-endian 16-bit parameter ID
    Bytes 8..9    : IDX         - big-endian 16-bit index (always 0x0000)
    Bytes 10..    : DATA        - payload (LEN - 5 bytes)
    Last 2 bytes  : CRC         - CRC-16/MCRF4XX, transmitted LOW byte first

CRC parameters (CRC-16/MCRF4XX):
    poly = 0x1021, init = 0xFFFF, refin = true, refout = true, xorout = 0x0000

Error responses from the device use PID 0xFFFF with a single-byte error code.
"""

from __future__ import annotations

import logging
import struct
from typing import Any

from serial_comm.models import GaugeReading
from serial_comm.protocols.base import GaugeProtocol

logger = logging.getLogger(__name__)

# Header / protocol constants
PROTOCOL_VERSION = 2
MASTER_ID = 0x00
# Slave device IDs by product. OPG550 responds with 0x0B.
KNOWN_SLAVE_IDS = {0x0B}

# APDU commands
CMD_READ_REQ = 0x01
CMD_READ_RESP = 0x02
CMD_WRITE_REQ = 0x03
CMD_WRITE_RESP = 0x04

# Error PID returned by the device in place of the requested PID
ERROR_PID = 0xFFFF

_ERROR_DESCRIPTIONS: dict[int, str] = {
    0: "Application error (see error history)",
    1: "Access violation",
    2: "Parameter out of limits",
    3: "Parameter not found",
    4: "Data length error",
    5: "Wrong password",
    6: "Fatal EEPROM error",
    7: "Timeout",
    9: "Not in setup mode",
    100: "CRC mismatch",
    101: "Wrong command",
    102: "Acknowledge bit set but should not be",
    103: "Acknowledge bit not set but should be",
    104: "Wrong protocol version",
}


def crc16_mcrf4xx(data: bytes) -> int:
    """CRC-16/MCRF4XX (poly 0x1021, init 0xFFFF, refin=refout=true, xorout=0)."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0x8408  # reflected poly
            else:
                crc >>= 1
    return crc & 0xFFFF


def _make_header_byte(ack: int) -> int:
    return ((PROTOCOL_VERSION & 0x0F) << 4) | (ack & 0x01)


def build_frame(
    cmd: int,
    pid: int,
    data: bytes = b"",
    *,
    addr: int = 0x00,
    sender_id: int = MASTER_ID,
    ack: int = 0,
) -> bytes:
    """Build a complete, CRC-checked P3 V02 frame."""
    apdu_len = 5 + len(data)
    body = bytes([
        addr & 0xFF,
        sender_id & 0xFF,
        _make_header_byte(ack),
        (apdu_len >> 8) & 0xFF,
        apdu_len & 0xFF,
        cmd & 0xFF,
        (pid >> 8) & 0xFF,
        pid & 0xFF,
        0x00,
        0x00,
    ]) + data
    crc = crc16_mcrf4xx(body)
    return body + bytes([crc & 0xFF, (crc >> 8) & 0xFF])


def parse_frame(buf: bytes) -> dict[str, Any]:
    """
    Parse a complete frame buffer.

    Returns a dict with keys: addr, sender_id, ack, cmd, pid, idx, data,
    crc_ok, trailing (any bytes beyond one frame).
    Raises ValueError if the buffer is too short or malformed.
    """
    if len(buf) < 12:  # 10 header + 2 CRC minimum
        raise ValueError(f"frame too short ({len(buf)} bytes)")
    apdu_len = (buf[3] << 8) | buf[4]
    total = 5 + apdu_len + 2
    if len(buf) < total:
        raise ValueError(f"frame truncated (need {total}, have {len(buf)})")
    body = bytes(buf[:5 + apdu_len])
    crc_lo = buf[5 + apdu_len]
    crc_hi = buf[5 + apdu_len + 1]
    crc_rx = (crc_hi << 8) | crc_lo
    crc_calc = crc16_mcrf4xx(body)
    return {
        "addr": buf[0],
        "sender_id": buf[1],
        "ack": buf[2] & 0x01,
        "ver": (buf[2] >> 4) & 0x0F,
        "cmd": buf[5],
        "pid": (buf[6] << 8) | buf[7],
        "idx": (buf[8] << 8) | buf[9],
        "data": bytes(buf[10:5 + apdu_len]),
        "crc_ok": crc_rx == crc_calc,
        "crc_rx": crc_rx,
        "crc_calc": crc_calc,
        "total_len": total,
        "trailing": bytes(buf[total:]),
    }


# ---------------------------------------------------------------------------
# Data-type coders
# ---------------------------------------------------------------------------

def _encode_value(value: Any, data_type: str) -> bytes:
    if data_type == "opg_all_algorithms_off":
        return bytes([0])
    if data_type == "opg_spec_enable":
        if isinstance(value, (bytes, bytearray)):
            return bytes(value)
        if isinstance(value, dict):
            mode = int(value.get("mode", 1))
            count = int(value.get("count", value.get("number_of_spectra", 0)))
            integration_us = int(value.get("integration_us", 1000))
        elif isinstance(value, (list, tuple)):
            mode = int(value[0]) if len(value) > 0 else 1
            count = int(value[1]) if len(value) > 1 else 0
            integration_us = int(value[2]) if len(value) > 2 else 1000
        else:
            mode = int(value)
            count = 0
            integration_us = 1000
        return struct.pack(">BII", mode & 0xFF, count & 0xFFFFFFFF, integration_us & 0xFFFFFFFF)
    if data_type in ("opg_ror_enable", "opg_rgd_enable"):
        if isinstance(value, (bytes, bytearray)):
            return bytes(value)
        if isinstance(value, dict):
            mode = int(value.get("mode", 1))
            count = int(value.get("count", value.get("number_of_spectra", 0)))
            gas = int(value.get("gas", value.get("gas_number", 0)))
        elif isinstance(value, (list, tuple)):
            mode = int(value[0]) if len(value) > 0 else 1
            count = int(value[1]) if len(value) > 1 else 0
            gas = int(value[2]) if len(value) > 2 else 0
        else:
            mode = int(value)
            count = 0
            gas = 0
        return struct.pack(">BIB", mode & 0xFF, count & 0xFFFFFFFF, gas & 0xFF)
    if data_type in ("uint8", "enum_uint8", "bool_uint8"):
        return bytes([int(value) & 0xFF])
    if data_type == "uint16_be":
        return struct.pack(">H", int(value) & 0xFFFF)
    if data_type == "uint32_be":
        return struct.pack(">I", int(value) & 0xFFFFFFFF)
    if data_type == "int32_be":
        return struct.pack(">i", int(value))
    if data_type == "float32_be":
        return struct.pack(">f", float(value))
    if data_type == "string":
        if isinstance(value, bytes):
            return value
        return str(value).encode("ascii", errors="replace")
    raise ValueError(f"Unknown data_type for encode: {data_type!r}")


def _decode_opg_common_record_header(data: bytes, raw: bytes) -> tuple[dict[str, Any], int] | GaugeReading:
    if len(data) < 17:
        return GaugeReading(success=False, error="short OPG record header", raw=raw)
    record_id, time_ms, integration_us = struct.unpack(">III", data[:12])
    pressure = struct.unpack(">f", data[12:16])[0]
    ignition = data[16]
    return {
        "record_id": record_id,
        "time_ms": time_ms,
        "integration_us": integration_us,
        "total_pressure_mbar": pressure,
        "ignition_status": ignition,
        "ignition_active": bool(ignition),
    }, 17


def _scale_to_unit(values: list[float], unit: str) -> list[float]:
    if unit == "Torr":
        return [value / 750.062 for value in values]
    if unit == "Pascal":
        return [value / 100.0 for value in values]
    if unit == "micron":
        return [value / 750062.0 for value in values]
    return values


def _decode_opg_spec_record(data: bytes, unit: str, raw: bytes) -> GaugeReading:
    header = _decode_opg_common_record_header(data, raw)
    if isinstance(header, GaugeReading):
        return header
    extra, offset = header
    count = (len(data) - offset) // 4
    powers = list(struct.unpack(f">{count}I", data[offset:offset + count * 4])) if count else []
    pixel_data = [value / 10.0 for value in powers]
    extra.update({
        "pixel_count": len(pixel_data),
        "pixel_data": pixel_data,
        "raw_array": powers,
        "spectrum_power": pixel_data,
    })
    return GaugeReading(
        success=True,
        value=float(len(pixel_data)),
        unit=unit,
        formatted=f"SPEC record {extra['record_id']} ({len(pixel_data)} pixels)",
        raw=raw,
        extra=extra,
    )


def _decode_opg_ror_record(data: bytes, unit: str, raw: bytes) -> GaugeReading:
    header = _decode_opg_common_record_header(data, raw)
    if isinstance(header, GaugeReading):
        return header
    extra, offset = header
    if len(data) < offset + 4:
        return GaugeReading(success=False, error="short OPG RoR pressure-rise payload", raw=raw)
    pressure_rise = struct.unpack(">f", data[offset:offset + 4])[0]
    offset += 4
    remaining = len(data) - offset
    gas_count = 6 if remaining >= 12 else 0
    pixel_bytes = max(0, remaining - gas_count * 2)
    pixel_count = pixel_bytes // 2
    intensities = list(struct.unpack(f">{pixel_count}H", data[offset:offset + pixel_count * 2])) if pixel_count else []
    offset += pixel_count * 2
    leak_numbers = list(struct.unpack(f">{gas_count}h", data[offset:offset + gas_count * 2])) if gas_count else []
    leak_rates = [value / 100.0 for value in leak_numbers]
    extra.update({
        "pressure_rise_mtorr_per_min": pressure_rise,
        "pixel_count": len(intensities),
        "pixel_data": intensities,
        "raw_array": intensities,
        "spectrum_intensity": intensities,
        "leak_rate_numbers": leak_rates,
    })
    return GaugeReading(
        success=True,
        value=float(pressure_rise),
        unit="mTorr/min",
        formatted=f"RoR record {extra['record_id']} ({pressure_rise:.3g} mTorr/min)",
        raw=raw,
        extra=extra,
    )


def _decode_opg_rgd_record(data: bytes, unit: str, raw: bytes) -> GaugeReading:
    header = _decode_opg_common_record_header(data, raw)
    if isinstance(header, GaugeReading):
        return header
    extra, offset = header
    gas_count = 10
    ratio_count = 8
    tail_bytes = gas_count * 4 + gas_count * 4 + ratio_count * 4
    remaining = len(data) - offset
    pixel_bytes = max(0, remaining - tail_bytes)
    pixel_count = pixel_bytes // 4
    powers = list(struct.unpack(f">{pixel_count}I", data[offset:offset + pixel_count * 4])) if pixel_count else []
    offset += pixel_count * 4
    gas_intensities = list(struct.unpack(f">{gas_count}f", data[offset:offset + gas_count * 4])) if len(data) >= offset + gas_count * 4 else []
    offset += len(gas_intensities) * 4
    partial_pressures = list(struct.unpack(f">{gas_count}f", data[offset:offset + gas_count * 4])) if len(data) >= offset + gas_count * 4 else []
    offset += len(partial_pressures) * 4
    ratios = list(struct.unpack(f">{ratio_count}f", data[offset:offset + ratio_count * 4])) if len(data) >= offset + ratio_count * 4 else []
    pixel_data = [value / 10.0 for value in powers]
    extra.update({
        "pixel_count": len(pixel_data),
        "pixel_data": pixel_data,
        "raw_array": powers,
        "spectrum_power": pixel_data,
        "gas_intensities": gas_intensities,
        "partial_pressures": _scale_to_unit(partial_pressures, unit),
        "ratio_numbers": ratios,
    })
    return GaugeReading(
        success=True,
        value=float(len(pixel_data)),
        unit=unit,
        formatted=f"RGD record {extra['record_id']} ({len(pixel_data)} pixels)",
        raw=raw,
        extra=extra,
    )


def _decode_value(
    data: bytes,
    data_type: str,
    unit: str,
    raw: bytes,
    options: dict[int, str] | None = None,
) -> GaugeReading:
    try:
        if data_type == "uint8" or data_type == "bool_uint8":
            if len(data) < 1:
                return GaugeReading(success=False, error="empty uint8 payload", raw=raw)
            v = float(data[0])
            return GaugeReading(success=True, value=v, unit=unit,
                                formatted=f"{int(v)} {unit}".strip(), raw=raw)
        if data_type == "enum_uint8":
            if len(data) < 1:
                return GaugeReading(success=False, error="empty enum payload", raw=raw)
            code = data[0]
            label = (options or {}).get(code, f"code {code}")
            return GaugeReading(success=True, value=float(code), unit=unit,
                                formatted=label, raw=raw,
                                extra={"code": code, "label": label})
        if data_type == "uint16_be":
            if len(data) < 2:
                return GaugeReading(success=False, error="short uint16", raw=raw)
            v = float(struct.unpack(">H", data[:2])[0])
            return GaugeReading(success=True, value=v, unit=unit,
                                formatted=f"{int(v)} {unit}".strip(), raw=raw)
        if data_type == "uint32_be":
            if len(data) < 4:
                return GaugeReading(success=False, error="short uint32", raw=raw)
            v = float(struct.unpack(">I", data[:4])[0])
            return GaugeReading(success=True, value=v, unit=unit,
                                formatted=f"{int(v)} {unit}".strip(), raw=raw)
        if data_type == "int32_be":
            if len(data) < 4:
                return GaugeReading(success=False, error="short int32", raw=raw)
            v = float(struct.unpack(">i", data[:4])[0])
            return GaugeReading(success=True, value=v, unit=unit,
                                formatted=f"{int(v)} {unit}".strip(), raw=raw)
        if data_type == "float32_be":
            if len(data) < 4:
                return GaugeReading(success=False, error="short float32", raw=raw)
            v = float(struct.unpack(">f", data[:4])[0])
            return GaugeReading(success=True, value=v, unit=unit,
                                formatted=f"{v:.3E} {unit}".strip(), raw=raw)
        if data_type == "string":
            text = data.split(b"\x00", 1)[0].decode("ascii", errors="replace")
            return GaugeReading(success=True, formatted=text, raw=raw,
                                extra={"text": text})
        if data_type == "uint16_be_array":
            if len(data) < 2:
                return GaugeReading(success=False, error="empty uint16 array", raw=raw)
            n_pixels = len(data) // 2
            values = list(struct.unpack(f">{n_pixels}H", data[:n_pixels * 2]))
            peak = max(values, default=0)
            pixel_data = values
            if n_pixels > 288 and peak > 0:
                tail = values[-288:]
                if max(tail, default=0) > 0:
                    pixel_data = tail
            return GaugeReading(
                success=True,
                value=float(n_pixels),
                formatted=f"{n_pixels} pixels",
                raw=raw,
                extra={"pixel_count": len(pixel_data), "pixel_data": pixel_data, "raw_array": values},
            )
        if data_type == "opg_spec_record":
            return _decode_opg_spec_record(data, unit or "mbar", raw)
        if data_type == "opg_ror_record":
            return _decode_opg_ror_record(data, unit or "mbar", raw)
        if data_type == "opg_rgd_record":
            return _decode_opg_rgd_record(data, unit or "mbar", raw)
        if data_type == "error_record":
            # 4-byte combined error number + null-terminated description + null-terminated solution
            if len(data) < 4:
                return GaugeReading(success=False, error="short error record", raw=raw)
            code = struct.unpack(">I", data[:4])[0]
            parts = data[4:].split(b"\x00")
            desc = parts[0].decode("ascii", errors="replace") if len(parts) > 0 else ""
            sol = parts[1].decode("ascii", errors="replace") if len(parts) > 1 else ""
            return GaugeReading(success=True, value=float(code),
                                formatted=f"[{code}] {desc}", raw=raw,
                                extra={"code": code, "description": desc, "solution": sol})
        # Fallback: hex dump
        return GaugeReading(success=True, formatted=data.hex(), raw=raw,
                            extra={"hex": data.hex()})
    except Exception as exc:  # pragma: no cover - defensive
        return GaugeReading(success=False,
                            error=f"Decode error ({data_type}): {exc}", raw=raw)


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------

class InficonP3V02Protocol(GaugeProtocol):
    """
    Codec for the INFICON P3 V02 binary parameter protocol (OPG550).

    param_table entries may contain::

        {
            "pid": 14000,
            "read": True,
            "write": False,
            "data_type": "float32_be",
            "unit": "mbar",
            "request_data": [1],           # optional: fixed bytes sent with READ
            "request_data_type": "uint8",  # optional: encoder for write-only param
            "options": {0: "OFF", 1: "ON"} # optional: enum labels
        }
    """

    def __init__(
        self,
        address: int = 0,
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
            raise ValueError(f"InficonP3V02Protocol: unknown command '{command}'")
        pid = spec["pid"]

        if value is None:
            return self.build_read_request(command)

        # WRITE request
        if not spec.get("write", False):
            raise ValueError(f"Command '{command}' (PID {pid}) is read-only")
        data_type = spec.get("data_type", "uint8")
        payload = _encode_value(value, data_type)
        return build_frame(CMD_WRITE_REQ, pid, payload, addr=self.address)

    def build_read_request(
        self,
        command: str,
        request_data: bytes | bytearray | list[int] | tuple[int, ...] | str | int | None = None,
    ) -> bytes:
        spec = self._params.get(command)
        if spec is None:
            raise ValueError(f"InficonP3V02Protocol: unknown command '{command}'")
        pid = spec["pid"]
        if not spec.get("read", False):
            raise ValueError(f"Command '{command}' (PID {pid}) is write-only")
        data = self._coerce_request_data(request_data) if request_data is not None else self._encode_request_data(spec)
        return build_frame(CMD_READ_REQ, pid, data, addr=self.address)

    def parse_response(self, raw: bytes, command: str) -> GaugeReading:
        if not raw:
            return self._err("No response received", raw)
        try:
            parsed = parse_frame(raw)
        except ValueError as exc:
            return self._err(f"Malformed frame: {exc}", raw)

        if not parsed["crc_ok"]:
            return self._err(
                f"CRC mismatch (rx=0x{parsed['crc_rx']:04X} calc=0x{parsed['crc_calc']:04X})",
                raw,
            )
        if parsed["ack"] != 1:
            return self._err("Slave response missing ACK bit", raw)
        if parsed["pid"] == ERROR_PID:
            code = parsed["data"][0] if parsed["data"] else -1
            desc = _ERROR_DESCRIPTIONS.get(code, f"error code {code}")
            return self._err(f"Device error {code}: {desc}", raw)

        spec = self._params.get(command, {})
        expected_pid = spec.get("pid")
        if expected_pid is not None and parsed["pid"] != expected_pid:
            return self._err(
                f"PID mismatch: expected {expected_pid}, got {parsed['pid']}", raw
            )

        # Write response carries no payload; just return an OK reading
        if parsed["cmd"] == CMD_WRITE_RESP:
            return GaugeReading(success=True, formatted="OK", raw=raw)

        if parsed["cmd"] != CMD_READ_RESP:
            return self._err(f"Unexpected response CMD 0x{parsed['cmd']:02X}", raw)

        data_type = spec.get("data_type", "string")
        options = spec.get("options") or None
        unit = spec.get("unit", "")
        # Normalize options to int-keyed dict (YAML may give list/dict)
        norm_options = self._normalize_options(options)
        return _decode_value(parsed["data"], data_type, unit, raw, norm_options)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _encode_request_data(spec: dict[str, Any]) -> bytes:
        return InficonP3V02Protocol._coerce_request_data(spec.get("request_data"))

    @staticmethod
    def _coerce_request_data(req: Any) -> bytes:
        if req is None:
            return b""
        if isinstance(req, (bytes, bytearray)):
            return bytes(req)
        if isinstance(req, str):
            return req.encode("ascii")
        if isinstance(req, (list, tuple)):
            return bytes(int(b) & 0xFF for b in req)
        if isinstance(req, int):
            return bytes([req & 0xFF])
        raise ValueError(f"Unsupported request_data: {req!r}")

    @staticmethod
    def _normalize_options(options: Any) -> dict[int, str] | None:
        if options is None:
            return None
        if isinstance(options, dict):
            return {int(k): str(v) for k, v in options.items()}
        if isinstance(options, list):
            out: dict[int, str] = {}
            for item in options:
                if isinstance(item, dict) and "code" in item:
                    out[int(item["code"])] = str(item.get("label", item["code"]))
            return out or None
        return None
