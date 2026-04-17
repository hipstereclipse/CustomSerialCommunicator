"""
Tests for PfeifferBinaryProtocol — PCG/PSG/MAG/BPG/BCG binary codec.
"""

import struct

import pytest

from serial_comm.protocols.pfeiffer_binary import PfeifferBinaryProtocol, _crc16


# Minimal param tables for each sub-family
_PCG_PARAMS = {
    "pressure":    {"pid": 221, "read": True, "write": False, "measurement": "pressure", "unit": "mbar"},
    "temperature": {"pid": 222, "read": True, "write": False, "measurement": "temperature", "unit": "°C"},
    "error_status":{"pid": 228, "read": True, "write": False, "measurement": "error_flags",
                    "flag_names": ["sensor_error", "electronics_error", "calibration_error", "memory_error"]},
}

_MAG_PARAMS = {
    "pressure": {"pid": 340, "read": True, "write": False, "measurement": "pressure", "unit": "mbar"},
}


@pytest.fixture
def pcg() -> PfeifferBinaryProtocol:
    return PfeifferBinaryProtocol(
        address=0, device_id=0x02, pressure_enc="fixs32en20", param_table=_PCG_PARAMS
    )


@pytest.fixture
def mag() -> PfeifferBinaryProtocol:
    return PfeifferBinaryProtocol(
        address=0, device_id=0x14, pressure_enc="logfixs32en26", param_table=_MAG_PARAMS
    )


# ---------------------------------------------------------------------------
# CRC-16 helper
# ---------------------------------------------------------------------------

class TestCRC16:
    def test_empty(self):
        assert _crc16(b"") == 0xFFFF

    def test_known_byte(self):
        # CRC of single 0x00 byte
        crc = _crc16(b"\x00")
        assert isinstance(crc, int)
        assert 0 <= crc <= 0xFFFF

    def test_different_inputs_differ(self):
        assert _crc16(b"\x01") != _crc16(b"\x02")

    def test_bytearray_input(self):
        data = bytearray([0x01, 0x02, 0x03])
        assert _crc16(data) == _crc16(bytes(data))


# ---------------------------------------------------------------------------
# build_request
# ---------------------------------------------------------------------------

class TestBuildRequest:
    def test_read_pressure_length(self, pcg):
        cmd = pcg.build_request("pressure")
        assert len(cmd) == 11  # standard read frame is always 11 bytes

    def test_read_pressure_device_id(self, pcg):
        cmd = pcg.build_request("pressure")
        assert cmd[1] == 0x02

    def test_read_pressure_cmd_code(self, pcg):
        cmd = pcg.build_request("pressure")
        assert cmd[4] == 0x01  # read

    def test_read_pressure_pid(self, pcg):
        cmd = pcg.build_request("pressure")
        pid = (cmd[5] << 8) | cmd[6]
        assert pid == 221

    def test_crc_appended(self, pcg):
        cmd = pcg.build_request("pressure")
        payload = cmd[:-2]
        expected_crc = _crc16(payload)
        received_crc = (cmd[-1] << 8) | cmd[-2]  # little-endian stored
        assert received_crc == expected_crc

    def test_mag_device_id(self, mag):
        cmd = mag.build_request("pressure")
        assert cmd[1] == 0x14

    def test_rs485_address(self):
        proto = PfeifferBinaryProtocol(
            address=5, device_id=0x02, rs485_mode=True, param_table=_PCG_PARAMS
        )
        cmd = proto.build_request("pressure")
        assert cmd[0] == 5

    def test_rs232_address_zero(self, pcg):
        cmd = pcg.build_request("pressure")
        assert cmd[0] == 0x00

    def test_unknown_command_raises(self, pcg):
        with pytest.raises(ValueError):
            pcg.build_request("nonexistent")


# ---------------------------------------------------------------------------
# Build a valid response
# ---------------------------------------------------------------------------

def make_pcg_pressure_response(pressure_raw: int, device_id: int = 0x02) -> bytes:
    """Build a valid 13-byte PCG pressure response frame.

    Byte 3 = message length = bytes following byte-3 before CRC = 7
    (cmd + pid_hi + pid_lo + 4 data bytes).
    """
    data = pressure_raw.to_bytes(4, byteorder="big", signed=True)
    msg = bytearray([
        0x00, device_id, 0x00, 0x07, 0x01,
        (221 >> 8) & 0xFF, 221 & 0xFF,
    ])
    msg.extend(data)
    crc = _crc16(msg)
    msg.extend([crc & 0xFF, (crc >> 8) & 0xFF])
    return bytes(msg)


# ---------------------------------------------------------------------------
# parse_response — success
# ---------------------------------------------------------------------------

class TestParseResponseSuccess:
    def test_pcg_pressure_fixs32en20(self, pcg):
        # 1e-3 mbar → raw = log10(1e-3) * 2**20
        import math
        raw_int = int(math.log10(1e-3) * (2 ** 20))
        frame = make_pcg_pressure_response(raw_int)
        reading = pcg.parse_response(frame, "pressure")
        assert reading.success
        assert reading.value == pytest.approx(1e-3, rel=1e-3)
        assert reading.unit == "mbar"

    def test_error_flag_none(self, pcg):
        # Build response for error_status, all bits zero
        data = (0).to_bytes(4, byteorder="big", signed=False)
        msg = bytearray([0x00, 0x02, 0x00, 0x07, 0x01, 0x00, 228])
        msg.extend(data)
        crc = _crc16(msg)
        msg.extend([crc & 0xFF, (crc >> 8) & 0xFF])
        reading = pcg.parse_response(bytes(msg), "error_status")
        assert reading.success
        assert "none" in reading.formatted

    def test_error_flag_sensor_error(self, pcg):
        data = (1).to_bytes(4, byteorder="big", signed=False)
        msg = bytearray([0x00, 0x02, 0x00, 0x07, 0x01, 0x00, 228])
        msg.extend(data)
        crc = _crc16(msg)
        msg.extend([crc & 0xFF, (crc >> 8) & 0xFF])
        reading = pcg.parse_response(bytes(msg), "error_status")
        assert reading.success
        assert "sensor_error" in reading.formatted


# ---------------------------------------------------------------------------
# parse_response — errors
# ---------------------------------------------------------------------------

class TestParseResponseErrors:
    def test_too_short(self, pcg):
        reading = pcg.parse_response(b"\x00\x02", "pressure")
        assert not reading.success

    def test_wrong_device_id(self, pcg):
        frame = make_pcg_pressure_response(0, device_id=0x14)
        reading = pcg.parse_response(frame, "pressure")
        assert not reading.success
        assert "device id" in reading.error.lower()

    def test_bad_crc(self, pcg):
        frame = bytearray(make_pcg_pressure_response(0))
        frame[-1] ^= 0xFF  # corrupt CRC
        reading = pcg.parse_response(bytes(frame), "pressure")
        assert not reading.success
        assert "crc" in reading.error.lower()

    def test_length_mismatch(self, pcg):
        frame = bytearray(make_pcg_pressure_response(0))
        frame[3] = 0x09  # claim wrong length
        reading = pcg.parse_response(bytes(frame), "pressure")
        assert not reading.success
