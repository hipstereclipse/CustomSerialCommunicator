"""
Tests for CDGProtocol — INFICON SKY CDG025D/CDG045D codec.
"""

import pytest

from serial_comm.protocols.cdg_serial import CDGProtocol, RESPONSE_LENGTH, RESPONSE_SYNC


@pytest.fixture
def cdg025() -> CDGProtocol:
    return CDGProtocol(gauge_type="CDG025D")


@pytest.fixture
def cdg045() -> CDGProtocol:
    return CDGProtocol(gauge_type="CDG045D")


def make_cdg_response(
    pressure_raw: int,  # signed 16-bit
    page: int = 0,
    status: int = 0,
    err: int = 0,
    sensor_type: int = 1,  # 1 = CDG045D
) -> bytes:
    """Build a valid 9-byte CDG response frame."""
    hi = (pressure_raw >> 8) & 0xFF
    lo = pressure_raw & 0xFF
    frame = bytearray([RESPONSE_SYNC, page, status, err, hi, lo, 0x00, sensor_type])
    csum = sum(frame[1:8]) & 0xFF
    frame.append(csum)
    return bytes(frame)


# ---------------------------------------------------------------------------
# build_request
# ---------------------------------------------------------------------------

class TestBuildRequest:
    def test_pressure_read_length(self, cdg025):
        cmd = cdg025.build_request("pressure")
        assert len(cmd) == 5

    def test_pressure_read_start_byte(self, cdg025):
        cmd = cdg025.build_request("pressure")
        assert cmd[0] == 0x03

    def test_pressure_read_service_cmd(self, cdg025):
        cmd = cdg025.build_request("pressure")
        assert cmd[1] == 0x00  # read

    def test_zero_adjust_service_cmd(self, cdg025):
        cmd = cdg025.build_request("zero_adjust")
        assert cmd[1] == 0x40
        assert cmd[2] == 0x02

    def test_reset_service_cmd(self, cdg025):
        cmd = cdg025.build_request("reset")
        assert cmd[1] == 0x40
        assert cmd[2] == 0x00

    def test_factory_reset_service_cmd(self, cdg025):
        cmd = cdg025.build_request("factory_reset")
        assert cmd[1] == 0x40
        assert cmd[2] == 0x01

    def test_checksum_correct(self, cdg025):
        cmd = cdg025.build_request("pressure")
        expected = sum(cmd[1:4]) & 0xFF
        assert cmd[4] == expected

    def test_unknown_command_raises(self, cdg025):
        with pytest.raises(ValueError, match="unknown command"):
            cdg025.build_request("nonexistent")


# ---------------------------------------------------------------------------
# parse_continuous / parse_response
# ---------------------------------------------------------------------------

class TestParseResponse:
    def test_zero_pressure(self, cdg025):
        frame = make_cdg_response(0)
        reading = cdg025.parse_continuous(frame)
        assert reading.success
        assert reading.value == pytest.approx(0.0)

    def test_positive_pressure(self, cdg025):
        # 8192 raw → 8192 / 16384 = 0.5 mbar
        frame = make_cdg_response(8192)
        reading = cdg025.parse_continuous(frame)
        assert reading.success
        assert reading.value == pytest.approx(0.5)
        assert reading.unit == "mbar"

    def test_full_scale(self, cdg025):
        # 16383 / 16384 ≈ 1.0 mbar
        frame = make_cdg_response(16383)
        reading = cdg025.parse_continuous(frame)
        assert reading.success
        assert reading.value == pytest.approx(1.0, rel=1e-4)

    def test_negative_pressure_signed(self, cdg025):
        # Signed representation: -1 → 0xFFFF
        frame = make_cdg_response(-1)
        reading = cdg025.parse_continuous(frame)
        assert reading.success
        assert reading.value < 0

    def test_wrong_length(self, cdg025):
        reading = cdg025.parse_continuous(b"\x07\x00\x00\x00")
        assert not reading.success

    def test_wrong_sync_byte(self, cdg025):
        frame = bytearray(make_cdg_response(0))
        frame[0] = 0x03
        reading = cdg025.parse_continuous(bytes(frame))
        assert not reading.success

    def test_error_byte_fatal(self, cdg025):
        # 0x40 = sensor fault bit → fatal
        frame = make_cdg_response(0, err=0x40)
        reading = cdg025.parse_continuous(frame)
        assert not reading.success
        assert "fault" in reading.error.lower() or "0x40" in reading.error

    def test_error_byte_warmup_still_valid(self, cdg025):
        # 0x80 = sensor-not-ready (heating) → reading is still usable
        frame = make_cdg_response(8192, err=0x80)
        reading = cdg025.parse_continuous(frame)
        assert reading.success
        assert "sensor not ready" in reading.extra["warnings"]

    def test_error_byte_0x18_still_returns_pressure(self):
        cdg200 = CDGProtocol(gauge_type="CDG200D", full_scale_mbar=1.3332)
        frame = make_cdg_response(8192, err=0x18, sensor_type=4)

        reading = cdg200.parse_continuous(frame)

        assert reading.success
        assert reading.value == pytest.approx(0.6666)
        assert reading.extra["gauge_type"] == "CDG200D"
        assert "fs adjust running" in reading.extra["warnings"]
        assert "extended status" in reading.extra["warnings"]

    def test_bad_checksum(self, cdg025):
        frame = bytearray(make_cdg_response(0))
        frame[8] ^= 0xFF
        reading = cdg025.parse_continuous(bytes(frame))
        assert not reading.success
        assert "checksum" in reading.error.lower()

    def test_empty_response(self, cdg025):
        reading = cdg025.parse_continuous(b"")
        assert not reading.success

    def test_sensor_type_in_extra(self, cdg025):
        frame = make_cdg_response(0, sensor_type=1)  # CDG045D = code 1
        reading = cdg025.parse_continuous(frame)
        assert reading.success
        assert reading.extra.get("gauge_type") == "CDG045D"


# ---------------------------------------------------------------------------
# detect_gauge_type
# ---------------------------------------------------------------------------

class TestDetectGaugeType:
    def test_detects_cdg045d(self, cdg025):
        frame = make_cdg_response(0, sensor_type=1)
        assert cdg025.detect_gauge_type(frame) == "CDG045D"

    def test_detects_cdg025d(self, cdg025):
        frame = make_cdg_response(0, sensor_type=0)
        assert cdg025.detect_gauge_type(frame) == "CDG025D"

    def test_detects_hpg400(self, cdg025):
        frame = make_cdg_response(0, sensor_type=0x0B)
        assert cdg025.detect_gauge_type(frame) == "HPG400"

    def test_detects_cdg100d(self, cdg025):
        frame = make_cdg_response(0, sensor_type=2)
        assert cdg025.detect_gauge_type(frame) == "CDG100D"

    def test_detects_cdg160d(self, cdg025):
        frame = make_cdg_response(0, sensor_type=3)
        assert cdg025.detect_gauge_type(frame) == "CDG160D"

    def test_detects_cdg200d(self, cdg025):
        frame = make_cdg_response(0, sensor_type=4)
        assert cdg025.detect_gauge_type(frame) == "CDG200D"

    def test_unknown_sensor_type(self, cdg025):
        frame = make_cdg_response(0, sensor_type=99)
        assert cdg025.detect_gauge_type(frame) is None


    def test_scanner_infers_two_torr_full_scale_as_mbar(self):
        from GUI.gauge_workspace.port_scanner import PortScanner

        assert PortScanner._infer_cdg_full_scale_mbar(1, 2) == pytest.approx(2.6664)

    def test_scanner_decodes_cdg_model_from_frame(self):
        from GUI.gauge_workspace.port_scanner import PortScanner

        frame = make_cdg_response(8192, sensor_type=3)
        decoded = PortScanner([])._decode_cdg_identification(frame)
        assert decoded.model == "CDG160D"
        assert decoded.raw_ratio == pytest.approx(0.5)

    def test_scanner_does_not_turn_plain_full_scale_into_model(self):
        from GUI.gauge_workspace.port_scanner import PortScanner

        pressure_frame = make_cdg_response(0, sensor_type=1)
        type_reply = bytearray(make_cdg_response(2, sensor_type=1))
        type_reply[6] = 0x3B
        type_reply[8] = sum(type_reply[1:8]) & 0xFF

        decoded = PortScanner([])._decode_cdg_identification(pressure_frame, bytes(type_reply))
        assert decoded.model == "CDG045D"
        assert decoded.full_scale_mbar == pytest.approx(2.6664)

    def test_registry_has_cdg_scan_targets(self):
        from serial_comm.device_registry import DeviceRegistry

        registry = DeviceRegistry()
        for model in ("CDG025D", "CDG045D", "CDG100D", "CDG160D", "CDG200D"):
            spec = registry.get_spec(model)
            assert spec.protocol == "cdg_serial"
