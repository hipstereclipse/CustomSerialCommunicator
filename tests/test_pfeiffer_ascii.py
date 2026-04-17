"""
Tests for PfeifferAsciiProtocol — BCG450 / TC600 ASCII parameter codec.

Frame format: [Addr:3][Action:2][Param:3][DataLen:2][Data][Checksum:3]\\r
"""

import pytest

from serial_comm.protocols.pfeiffer_ascii import PfeifferAsciiProtocol, _checksum


# Minimal param table for testing
_PARAMS = {
    "pressure":        {"pid": 340, "data_type": "u_expo_new", "read": True,  "write": False, "unit": "mbar"},
    "standby":         {"pid": 2,   "data_type": "boolean_old", "read": True,  "write": True,  "unit": ""},
    "actual_speed_hz": {"pid": 309, "data_type": "u_integer",   "read": True,  "write": False, "unit": "Hz"},
    "motor_current_A": {"pid": 310, "data_type": "u_real",      "read": True,  "write": False, "unit": "A"},
    "firmware":        {"pid": 312, "data_type": "string",      "read": True,  "write": False, "unit": ""},
    "motor_on":        {"pid": 23,  "data_type": "boolean_old", "read": True,  "write": True,  "unit": ""},
}


@pytest.fixture
def proto() -> PfeifferAsciiProtocol:
    return PfeifferAsciiProtocol(address=1, param_table=_PARAMS)


# ---------------------------------------------------------------------------
# Checksum helper
# ---------------------------------------------------------------------------

class TestChecksum:
    def test_empty_string(self):
        assert _checksum("") == 0

    def test_known_value(self):
        # "00100030002=?" → sum of ASCII codes mod 256
        frame = "00100030002=?"
        expected = sum(ord(c) for c in frame) % 256
        assert _checksum(frame) == expected

    def test_roundtrip(self):
        # Build a frame manually and verify checksum field
        frame_body = "001003400 2=?"
        csum = _checksum(frame_body)
        assert 0 <= csum <= 255


# ---------------------------------------------------------------------------
# build_request
# ---------------------------------------------------------------------------

class TestBuildRequest:
    def test_read_request_format(self, proto):
        raw = proto.build_request("pressure")
        text = raw.decode("ascii")
        # addr=001, action=00, pid=340, len=02, data==?
        assert text.startswith("0010034002=?")  # some chars may vary
        assert text.endswith("\r")

    def test_read_request_addr(self, proto):
        raw = proto.build_request("pressure")
        assert raw[:3] == b"001"

    def test_read_request_action(self, proto):
        raw = proto.build_request("pressure")
        assert raw[3:5] == b"00"

    def test_read_request_pid(self, proto):
        raw = proto.build_request("pressure")
        assert raw[5:8] == b"340"

    def test_read_request_payload(self, proto):
        raw = proto.build_request("pressure")
        # DataLen=02, Data==?
        assert raw[8:10] == b"02"
        assert raw[10:12] == b"=?"

    def test_write_boolean_on(self, proto):
        raw = proto.build_request("standby", value=True)
        text = raw.decode("ascii").rstrip("\r")
        # data field should be 111111 (boolean_old ON)
        data_len = int(text[8:10])
        data = text[10 : 10 + data_len]
        assert data == "111111"

    def test_write_boolean_off(self, proto):
        raw = proto.build_request("standby", value=False)
        text = raw.decode("ascii").rstrip("\r")
        data_len = int(text[8:10])
        data = text[10 : 10 + data_len]
        assert data == "000000"

    def test_checksum_present_and_valid(self, proto):
        raw = proto.build_request("pressure")
        text = raw.decode("ascii").rstrip("\r")
        frame_body = text[:-3]
        csum_field = int(text[-3:])
        assert _checksum(frame_body) == csum_field

    def test_unknown_command_raises(self, proto):
        with pytest.raises(ValueError, match="unknown command"):
            proto.build_request("nonexistent")

    def test_write_to_readonly_raises(self, proto):
        with pytest.raises(ValueError, match="read-only"):
            proto.build_request("pressure", value=1e-6)

    def test_terminator_is_cr(self, proto):
        raw = proto.build_request("pressure")
        assert raw.endswith(b"\r")


# ---------------------------------------------------------------------------
# Construct a valid response frame helper
# ---------------------------------------------------------------------------

def make_response(addr: int, pid: int, data: str) -> bytes:
    """Build a syntactically correct response frame."""
    action = "10"
    pid_str = f"{pid:03d}"
    data_len = f"{len(data):02d}"
    body = f"{addr:03d}{action}{pid_str}{data_len}{data}"
    csum = _checksum(body)
    return f"{body}{csum:03d}\r".encode("ascii")


# ---------------------------------------------------------------------------
# parse_response — success
# ---------------------------------------------------------------------------

class TestParseResponseSuccess:
    def test_u_integer_speed(self, proto):
        # Actual speed 27000 Hz → "027000"
        raw = make_response(1, 309, "027000")
        reading = proto.parse_response(raw, "actual_speed_hz")
        assert reading.success
        assert reading.value == pytest.approx(27000.0)
        assert reading.unit == "Hz"

    def test_u_real_current(self, proto):
        # Motor current 1.23 A → fixed-point 4.2 → "000123"
        raw = make_response(1, 310, "000123")
        reading = proto.parse_response(raw, "motor_current_A")
        assert reading.success
        assert reading.value == pytest.approx(1.23)
        assert reading.unit == "A"

    def test_boolean_old_on(self, proto):
        raw = make_response(1, 23, "111111")
        reading = proto.parse_response(raw, "motor_on")
        assert reading.success
        assert reading.value == 1.0

    def test_boolean_old_off(self, proto):
        raw = make_response(1, 23, "000000")
        reading = proto.parse_response(raw, "motor_on")
        assert reading.success
        assert reading.value == 0.0

    def test_string_firmware(self, proto):
        raw = make_response(1, 312, "  1.07")
        reading = proto.parse_response(raw, "firmware")
        assert reading.success
        assert "1.07" in reading.formatted


# ---------------------------------------------------------------------------
# parse_response — errors
# ---------------------------------------------------------------------------

class TestParseResponseErrors:
    def test_empty_response(self, proto):
        reading = proto.parse_response(b"", "pressure")
        assert not reading.success

    def test_too_short(self, proto):
        reading = proto.parse_response(b"001003400", "pressure")
        assert not reading.success

    def test_bad_checksum(self, proto):
        raw = make_response(1, 309, "027000")
        # Corrupt last 3 bytes (checksum)
        corrupted = raw[:-4] + b"999\r"
        reading = proto.parse_response(corrupted, "actual_speed_hz")
        assert not reading.success
        assert "checksum" in reading.error.lower()

    def test_no_def_error(self, proto):
        raw = make_response(1, 309, "NO_DEF")
        reading = proto.parse_response(raw, "actual_speed_hz")
        assert not reading.success
        assert "NO_DEF" in reading.error

    def test_range_error(self, proto):
        raw = make_response(1, 309, "_RANGE")
        reading = proto.parse_response(raw, "actual_speed_hz")
        assert not reading.success

    def test_non_ascii_response(self, proto):
        reading = proto.parse_response(b"\xff\xfe\x00", "pressure")
        assert not reading.success
