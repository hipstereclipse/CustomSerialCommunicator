"""
Tests for PPGProtocol — INFICON PPG550/PPG570 ASCII codec.

Fixtures represent real byte sequences per the INFICON protocol spec.
"""

import pytest

from serial_comm.protocols.ppg_ascii import PPGProtocol, TERMINATOR


@pytest.fixture
def ppg() -> PPGProtocol:
    return PPGProtocol(address=254, gauge_type="PPG550")


@pytest.fixture
def ppg_rs485() -> PPGProtocol:
    return PPGProtocol(address=12, gauge_type="PPG550")


@pytest.fixture
def ppg570() -> PPGProtocol:
    return PPGProtocol(
        address=254,
        gauge_type="PPG570",
        param_table={
            "pressure": {
                "mnemonic": "P",
                "read": True,
                "write": False,
                "unit": "mbar",
            },
            "pressure_combined": {
                "mnemonic": "P",
                "query_param": "CMB",
                "read": True,
                "write": False,
                "unit": "mbar",
            },
            "setpoint_1": {
                "mnemonic": "SPV",
                "query_param": "1",
                "write_prefix": "1,",
                "read": True,
                "write": True,
                "unit": "mbar",
            },
        },
    )


# ---------------------------------------------------------------------------
# build_request
# ---------------------------------------------------------------------------

class TestBuildRequest:
    def test_pressure_read(self, ppg):
        cmd = ppg.build_request("pressure")
        assert cmd == b"@254PR3?\\"

    def test_temperature_read(self, ppg):
        cmd = ppg.build_request("temperature")
        assert cmd == b"@254T?\\"

    def test_firmware_read(self, ppg):
        cmd = ppg.build_request("software_version")
        assert cmd == b"@254FV?\\"

    def test_zero_adjust_write(self, ppg):
        cmd = ppg.build_request("zero_adjust", value="")
        assert cmd == b"@254VAC!\\"

    def test_unit_write(self, ppg):
        cmd = ppg.build_request("unit", value="Torr")
        assert cmd == b"@254U!Torr\\"

    def test_rs485_address_in_frame(self, ppg_rs485):
        cmd = ppg_rs485.build_request("pressure")
        assert cmd == b"@012PR3?\\"

    def test_unknown_command_raises(self, ppg):
        with pytest.raises(ValueError, match="unknown command"):
            ppg.build_request("nonexistent")

    def test_write_to_readonly_raises(self, ppg):
        with pytest.raises(ValueError, match="read-only"):
            ppg.build_request("pressure", value="1E-3")

    def test_ppg570_query_param_is_encoded(self, ppg570):
        cmd = ppg570.build_request("pressure_combined")
        assert cmd == b"@254P?CMB\\"

    def test_ppg570_write_prefix_is_encoded(self, ppg570):
        cmd = ppg570.build_request("setpoint_1", value="2.00E-2")
        assert cmd == b"@254SPV!1,2.00E-2\\"


# ---------------------------------------------------------------------------
# parse_response — success cases
# ---------------------------------------------------------------------------

class TestParseResponseSuccess:
    def test_pressure_nominal(self, ppg):
        raw = b"@ACK1.23E-3\\"
        reading = ppg.parse_response(raw, "pressure")
        assert reading.success
        assert abs(reading.value - 1.23e-3) < 1e-10
        assert reading.unit == "mbar"

    def test_pressure_high(self, ppg):
        raw = b"@ACK9.99E+2\\"
        reading = ppg.parse_response(raw, "pressure")
        assert reading.success
        assert abs(reading.value - 999.0) < 0.5

    def test_temperature(self, ppg):
        raw = b"@ACK25.3\\"
        reading = ppg.parse_response(raw, "temperature")
        assert reading.success
        assert abs(reading.value - 25.3) < 1e-6
        assert reading.unit == "°C"

    def test_firmware_version(self, ppg):
        raw = b"@ACK1.07\\"
        reading = ppg.parse_response(raw, "software_version")
        assert reading.success
        assert reading.extra.get("text") == "1.07"

    def test_serial_number(self, ppg):
        raw = b"@ACK12345678\\"
        reading = ppg.parse_response(raw, "serial_number")
        assert reading.success
        assert "12345678" in reading.extra.get("text", "")


# ---------------------------------------------------------------------------
# parse_response — error cases
# ---------------------------------------------------------------------------

class TestParseResponseErrors:
    def test_nak_response(self, ppg):
        raw = b"@NAK\\"
        reading = ppg.parse_response(raw, "pressure")
        assert not reading.success
        assert "NAK" in reading.error

    def test_empty_response(self, ppg):
        reading = ppg.parse_response(b"", "pressure")
        assert not reading.success

    def test_missing_terminator(self, ppg):
        reading = ppg.parse_response(b"@ACK1.23E-3", "pressure")
        assert not reading.success
        assert "terminator" in reading.error.lower()

    def test_under_range(self, ppg):
        raw = b"@ACKUR\\"
        reading = ppg.parse_response(raw, "pressure")
        assert not reading.success
        assert "UR" in reading.error or "under" in reading.error.lower()

    def test_over_range(self, ppg):
        raw = b"@ACKOR\\"
        reading = ppg.parse_response(raw, "pressure")
        assert not reading.success

    def test_garbage_prefix(self, ppg):
        reading = ppg.parse_response(b"GARBAGE1.23E-3\\", "pressure")
        assert not reading.success

    def test_unparseable_float(self, ppg):
        raw = b"@ACKnotanumber\\"
        reading = ppg.parse_response(raw, "pressure")
        assert not reading.success

    def test_address_prefixed_ack_is_accepted(self, ppg):
        raw = b"@253ACK1.00E-3\\"
        reading = ppg.parse_response(raw, "pressure")
        assert reading.success
        assert abs(reading.value - 1.0e-3) < 1e-12

    def test_pressure_unknown_command_switches_to_ppg570_mnemonic(self, ppg):
        assert ppg.build_request("pressure") == b"@254PR3?\\"
        reading = ppg.parse_response(b"@NAKUNKNOWNCOMMAND\\", "pressure")
        assert not reading.success
        assert ppg.build_request("pressure") == b"@254P?\\"

    def test_non_unknown_nak_does_not_switch_mnemonic(self, ppg):
        assert ppg.build_request("pressure") == b"@254PR3?\\"
        reading = ppg.parse_response(b"@NAKWAIT\\", "pressure")
        assert not reading.success
        assert ppg.build_request("pressure") == b"@254PR3?\\"

    def test_pressure_combined_unknown_command_falls_back_to_plain_pressure(self, ppg570):
        assert ppg570.build_request("pressure_combined") == b"@254P?CMB\\"
        reading = ppg570.parse_response(b"@NAKUNKNOWNCOMMAND\\", "pressure_combined")
        assert not reading.success
        assert ppg570.build_request("pressure_combined") == b"@254P?\\"
