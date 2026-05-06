"""Unit tests for the INFICON P3 V02 binary protocol codec."""

from __future__ import annotations

import struct

import pytest

from serial_comm.protocols.inficon_p3_v02 import (
    CMD_READ_REQ,
    CMD_READ_RESP,
    CMD_WRITE_REQ,
    CMD_WRITE_RESP,
    ERROR_PID,
    InficonP3V02Protocol,
    build_frame,
    crc16_mcrf4xx,
    parse_frame,
)


# --------------------------------------------------------------------------
# CRC vectors (from the OPG550 communication manual TIRB59E1)
# --------------------------------------------------------------------------

# Read request for PID 10000 (manufacturer_name), master → slave, addr=0, id=0
# Body = 00 00 20 00 05 01 27 10 00 00
READ_MFG_BODY = bytes.fromhex("00 00 20 00 05 01 27 10 00 00".replace(" ", ""))
READ_MFG_CRC = 0x6853


def test_crc16_mcrf4xx_read_manufacturer_vector():
    assert crc16_mcrf4xx(READ_MFG_BODY) == READ_MFG_CRC


def test_crc16_empty_input_is_initial_value():
    # Empty input reflected-in/out of 0xFFFF is 0xFFFF
    assert crc16_mcrf4xx(b"") == 0xFFFF


# --------------------------------------------------------------------------
# build_frame / parse_frame
# --------------------------------------------------------------------------

def test_build_frame_matches_manual_vector():
    """Read request for PID 10000 should equal the manual's canonical bytes."""
    frame = build_frame(CMD_READ_REQ, 10000)
    # 10-byte body + 2-byte CRC (low byte first)
    assert frame[:10] == READ_MFG_BODY
    assert frame[10] == READ_MFG_CRC & 0xFF
    assert frame[11] == (READ_MFG_CRC >> 8) & 0xFF


def test_parse_frame_round_trip_read_request():
    frame = build_frame(CMD_READ_REQ, 14000, b"\x01")  # pressure request, unit=1
    p = parse_frame(frame)
    assert p["crc_ok"] is True
    assert p["ver"] == 2
    assert p["ack"] == 0
    assert p["cmd"] == CMD_READ_REQ
    assert p["pid"] == 14000
    assert p["idx"] == 0
    assert p["data"] == b"\x01"
    assert p["trailing"] == b""


def test_parse_frame_detects_bad_crc():
    frame = bytearray(build_frame(CMD_READ_REQ, 10000))
    frame[-1] ^= 0xFF  # corrupt CRC hi byte
    p = parse_frame(bytes(frame))
    assert p["crc_ok"] is False


def test_parse_frame_truncated_raises():
    frame = build_frame(CMD_READ_REQ, 10000)
    with pytest.raises(ValueError):
        parse_frame(frame[:8])


def test_parse_frame_returns_trailing_bytes():
    frame = build_frame(CMD_READ_REQ, 10000)
    buf = frame + b"\xAA\xBB"
    p = parse_frame(buf)
    assert p["crc_ok"] is True
    assert p["trailing"] == b"\xAA\xBB"


# --------------------------------------------------------------------------
# Protocol.build_request
# --------------------------------------------------------------------------

@pytest.fixture
def protocol() -> InficonP3V02Protocol:
    params = {
        "manufacturer_name": {
            "pid": 10000,
            "read": True,
            "data_type": "string",
        },
        "pressure": {
            "pid": 14000,
            "read": True,
            "data_type": "float32_be",
            "unit": "mbar",
            "request_data": [1],
        },
        "self_diagnostic": {
            "pid": 11000,
            "read": True,
            "data_type": "enum_uint8",
            "options": {0: "OK", 1: "Service", 2: "Failure"},
        },
        "plasma_enable": {
            "pid": 12002,
            "read": False,
            "write": True,
            "data_type": "bool_uint8",
        },
        "serial_number": {
            "pid": 10002,
            "read": True,
            "data_type": "string",
        },
        "spec_record": {
            "pid": 20004,
            "read": True,
            "data_type": "opg_spec_record",
            "request_data": [0, 0, 0, 0, 0, 1, 1, 32, 0],
            "unit": "mbar",
        },
        "legacy_array": {
            "pid": 20005,
            "read": True,
            "data_type": "uint16_be_array",
        },
        "spec_enable": {
            "pid": 20000,
            "read": False,
            "write": True,
            "data_type": "opg_spec_enable",
        },
        "ror_enable": {
            "pid": 21000,
            "read": False,
            "write": True,
            "data_type": "opg_ror_enable",
        },
        "ror_record": {
            "pid": 21004,
            "read": True,
            "data_type": "opg_ror_record",
            "request_data": [0, 0, 0, 0, 0, 1, 1, 32, 0, 1, 0, 6, 0],
            "unit": "mbar",
        },
        "rgd_record": {
            "pid": 22004,
            "read": True,
            "data_type": "opg_rgd_record",
            "request_data": [0, 0, 0, 0, 0, 1, 1, 32, 0, 1, 0, 10, 0, 1, 0, 8, 0],
            "unit": "mbar",
        },
    }
    return InficonP3V02Protocol(address=0, param_table=params)


def test_build_request_read_no_data(protocol):
    frame = protocol.build_request("manufacturer_name")
    p = parse_frame(frame)
    assert p["cmd"] == CMD_READ_REQ
    assert p["pid"] == 10000
    assert p["data"] == b""


def test_build_request_read_with_request_data(protocol):
    frame = protocol.build_request("pressure")
    p = parse_frame(frame)
    assert p["cmd"] == CMD_READ_REQ
    assert p["pid"] == 14000
    assert p["data"] == b"\x01"


def test_build_read_request_allows_runtime_request_data(protocol):
    frame = protocol.build_read_request("spec_record", [0xFF, 0xFF, 0xFF, 0xFF])
    p = parse_frame(frame)
    assert p["cmd"] == CMD_READ_REQ
    assert p["pid"] == 20004
    assert p["data"] == b"\xff\xff\xff\xff"


def test_build_request_uses_opg_spec_record_default_request_data(protocol):
    frame = protocol.build_request("spec_record")
    p = parse_frame(frame)
    assert p["cmd"] == CMD_READ_REQ
    assert p["pid"] == 20004
    assert p["data"] == bytes([0, 0, 0, 0, 0, 1, 1, 32, 0])


def test_build_request_opg_spec_enable_defaults_to_endless_1000us(protocol):
    frame = protocol.build_request("spec_enable", 1)
    p = parse_frame(frame)
    assert p["cmd"] == CMD_WRITE_REQ
    assert p["pid"] == 20000
    assert p["data"] == struct.pack(">BII", 1, 0, 1000)


def test_build_request_opg_ror_enable_defaults_to_all_gases(protocol):
    frame = protocol.build_request("ror_enable", 1)
    p = parse_frame(frame)
    assert p["cmd"] == CMD_WRITE_REQ
    assert p["pid"] == 21000
    assert p["data"] == struct.pack(">BIB", 1, 0, 0)


def test_build_request_write(protocol):
    frame = protocol.build_request("plasma_enable", 1)
    p = parse_frame(frame)
    assert p["cmd"] == CMD_WRITE_REQ
    assert p["pid"] == 12002
    assert p["data"] == b"\x01"


def test_build_request_unknown_raises(protocol):
    with pytest.raises(ValueError):
        protocol.build_request("not_a_command")


def test_build_request_write_on_readonly_raises(protocol):
    with pytest.raises(ValueError):
        protocol.build_request("manufacturer_name", "foo")


def test_build_request_read_on_writeonly_raises(protocol):
    with pytest.raises(ValueError):
        protocol.build_request("plasma_enable")


# --------------------------------------------------------------------------
# Protocol.parse_response
# --------------------------------------------------------------------------

def _make_response(pid: int, data: bytes, cmd: int = CMD_READ_RESP) -> bytes:
    return build_frame(cmd, pid, data, addr=0, sender_id=0x0B, ack=1)


def test_parse_response_string(protocol):
    frame = _make_response(10000, b"INFICON AG\x00")
    r = protocol.parse_response(frame, "manufacturer_name")
    assert r.success
    assert r.formatted == "INFICON AG"


def test_parse_response_float_pressure(protocol):
    payload = struct.pack(">f", 1.234e-3)
    frame = _make_response(14000, payload)
    r = protocol.parse_response(frame, "pressure")
    assert r.success
    assert r.unit == "mbar"
    assert r.value == pytest.approx(1.234e-3)


def test_parse_response_uint16_array_keeps_pixel_data(protocol):
    payload = struct.pack(">4H", 100, 200, 300, 400)
    frame = _make_response(20005, payload)
    r = protocol.parse_response(frame, "legacy_array")
    assert r.success
    assert r.value == 4
    assert r.extra["pixel_count"] == 4
    assert r.extra["pixel_data"] == [100, 200, 300, 400]


def test_parse_response_opg_spec_record_extracts_metadata_and_pixels(protocol):
    payload = (
        struct.pack(">III", 7, 1234, 1000)
        + struct.pack(">f", 1.25e-3)
        + bytes([1])
        + struct.pack(">3I", 100, 250, 400)
    )
    frame = _make_response(20004, payload)
    r = protocol.parse_response(frame, "spec_record")
    assert r.success
    assert r.extra["record_id"] == 7
    assert r.extra["total_pressure_mbar"] == pytest.approx(1.25e-3)
    assert r.extra["pixel_data"] == [10.0, 25.0, 40.0]


def test_parse_response_opg_ror_record_extracts_pressure_rise(protocol):
    payload = (
        struct.pack(">III", 11, 15000, 565227)
        + struct.pack(">f", 2.5e-3)
        + bytes([1])
        + struct.pack(">f", 3.75)
        + struct.pack(">4H", 10, 20, 30, 40)
        + struct.pack(">6h", -130, 0, 45, 100, -344, 12)
    )
    frame = _make_response(21004, payload)
    r = protocol.parse_response(frame, "ror_record")
    assert r.success
    assert r.extra["pressure_rise_mtorr_per_min"] == pytest.approx(3.75)
    assert r.extra["pixel_data"] == [10, 20, 30, 40]
    assert r.extra["leak_rate_numbers"] == pytest.approx([-1.30, 0.0, 0.45, 1.0, -3.44, 0.12])


def test_parse_response_opg_rgd_record_extracts_partials(protocol):
    powers = struct.pack(">3I", 100, 200, 300)
    gas_intensities = struct.pack(">10f", *[float(i) for i in range(10)])
    partials = struct.pack(">10f", *[1e-6 * (i + 1) for i in range(10)])
    ratios = struct.pack(">8f", *[0.1 * i for i in range(8)])
    payload = (
        struct.pack(">III", 8, 66023, 481693)
        + struct.pack(">f", 1.5e-3)
        + bytes([1])
        + powers
        + gas_intensities
        + partials
        + ratios
    )
    frame = _make_response(22004, payload)
    r = protocol.parse_response(frame, "rgd_record")
    assert r.success
    assert r.extra["pixel_data"] == [10.0, 20.0, 30.0]
    assert r.extra["partial_pressures"][:3] == pytest.approx([1e-6, 2e-6, 3e-6])
    assert r.extra["ratio_numbers"][3] == pytest.approx(0.3)


def test_parse_response_enum_labels(protocol):
    frame = _make_response(11000, bytes([1]))
    r = protocol.parse_response(frame, "self_diagnostic")
    assert r.success
    assert r.formatted == "Service"
    assert r.extra["code"] == 1


def test_parse_response_write_ack(protocol):
    frame = _make_response(12002, b"", cmd=CMD_WRITE_RESP)
    r = protocol.parse_response(frame, "plasma_enable")
    assert r.success
    assert r.formatted == "OK"


def test_parse_response_error_pid(protocol):
    # Device replies with PID 0xFFFF and 1-byte error code
    frame = _make_response(ERROR_PID, bytes([3]))  # 3 = Parameter not found
    r = protocol.parse_response(frame, "manufacturer_name")
    assert not r.success
    assert "Parameter not found" in (r.error or "")


def test_parse_response_missing_ack_bit(protocol):
    # Build a "response" with ack=0 — should be rejected
    frame = build_frame(CMD_READ_RESP, 10000, b"X", addr=0, sender_id=0x0B, ack=0)
    r = protocol.parse_response(frame, "manufacturer_name")
    assert not r.success
    assert "ACK" in (r.error or "")


def test_parse_response_pid_mismatch(protocol):
    # PID returned doesn't match expected
    frame = _make_response(9999, b"X")
    r = protocol.parse_response(frame, "manufacturer_name")
    assert not r.success
    assert "PID mismatch" in (r.error or "")


def test_parse_response_crc_corruption(protocol):
    frame = bytearray(_make_response(10000, b"AB"))
    frame[-2] ^= 0xFF
    r = protocol.parse_response(bytes(frame), "manufacturer_name")
    assert not r.success
    assert "CRC" in (r.error or "")


def test_parse_response_empty_buffer(protocol):
    r = protocol.parse_response(b"", "manufacturer_name")
    assert not r.success
