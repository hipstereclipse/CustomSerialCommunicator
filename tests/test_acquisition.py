"""
Tests for GaugeWorker — acquisition loop logic.

We stub the transport layer entirely to avoid needing real hardware.
Signal timing: use `with qtbot.waitSignal(...): worker.start()` to avoid
the race between thread start and spy installation.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from PyQt6.QtCore import QCoreApplication

from serial_comm.acquisition import GaugeWorker, _MAX_CONSECUTIVE_ERRORS
from serial_comm.models import DeviceReading, DeviceError, DeviceSpec, CommandSpec
from serial_comm.protocols.ppg_ascii import PPGProtocol
from serial_comm.transport import TransportConfig, TransportError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def ppg_spec() -> DeviceSpec:
    return DeviceSpec(
        model="PPG550",
        family="ppg_ascii",
        protocol="ppg_ascii",
        default_baud=9600,
        parity="N",
        data_bits=8,
        stop_bits=1,
        rs_modes=["RS232"],
        default_address=254,
        rs485_address_range=None,
        commands={
            "pressure": CommandSpec(
                name="pressure", read=True, write=False, unit="mbar", mnemonic="PR3"
            )
        },
    )


@pytest.fixture
def ppg_protocol() -> PPGProtocol:
    return PPGProtocol(address=254, gauge_type="PPG550")


@pytest.fixture
def transport_config() -> TransportConfig:
    return TransportConfig(port="COM99", baud=9600)


def _make_worker(spec, protocol, cfg, commands=None, poll_interval=0.05):
    return GaugeWorker(
        spec=spec,
        protocol=protocol,
        transport_config=cfg,
        commands=commands or ["pressure"],
        poll_interval=poll_interval,
        device_id="COM99:254",
    )


def _good_transport_mock():
    """Return a mock transport that always gives a valid PPG pressure response."""
    m = MagicMock()
    m.read_until.return_value = b"@ACK1.23E-3\\"
    m.write.return_value = None
    m.flush_input.return_value = None
    return m


# ---------------------------------------------------------------------------
# Transport open failure
# ---------------------------------------------------------------------------

class TestWorkerOpenFailure:
    def test_emits_error_on_open_failure(self, qtbot, ppg_spec, ppg_protocol, transport_config):
        worker = _make_worker(ppg_spec, ppg_protocol, transport_config)
        errors = []
        worker.error_occurred.connect(errors.append)

        with patch("serial_comm.acquisition.SerialTransport") as MockTransport:
            MockTransport.return_value.open.side_effect = TransportError("port busy")
            with qtbot.waitSignal(worker.finished, timeout=3000):
                worker.start()

        assert len(errors) == 1
        assert not errors[0].recoverable
        assert "port busy" in errors[0].message

    def test_connected_not_emitted_on_open_failure(
        self, qtbot, ppg_spec, ppg_protocol, transport_config
    ):
        worker = _make_worker(ppg_spec, ppg_protocol, transport_config)
        connected_count = [0]
        worker.connected.connect(lambda: connected_count.__setitem__(0, 1))

        with patch("serial_comm.acquisition.SerialTransport") as MockTransport:
            MockTransport.return_value.open.side_effect = TransportError("no port")
            with qtbot.waitSignal(worker.finished, timeout=3000):
                worker.start()

        assert connected_count[0] == 0


# ---------------------------------------------------------------------------
# Successful poll
# ---------------------------------------------------------------------------

class TestWorkerPolledSuccess:
    def test_emits_reading_on_good_response(
        self, qtbot, ppg_spec, ppg_protocol, transport_config
    ):
        worker = _make_worker(ppg_spec, ppg_protocol, transport_config, poll_interval=0.05)
        readings = []
        worker.reading_ready.connect(readings.append)

        with patch("serial_comm.acquisition.SerialTransport") as MockTransport:
            MockTransport.return_value = _good_transport_mock()
            with qtbot.waitSignal(worker.reading_ready, timeout=3000):
                worker.start()
            worker.stop()
            with qtbot.waitSignal(worker.finished, timeout=3000):
                pass

        assert len(readings) >= 1
        r = readings[0]
        assert isinstance(r, DeviceReading)
        assert abs(r.value - 1.23e-3) < 1e-9
        assert r.unit == "mbar"
        assert r.command == "pressure"
        assert r.device_id == "COM99:254"

    def test_reading_has_wall_timestamp(
        self, qtbot, ppg_spec, ppg_protocol, transport_config
    ):
        worker = _make_worker(ppg_spec, ppg_protocol, transport_config, poll_interval=0.05)
        readings = []
        worker.reading_ready.connect(readings.append)

        with patch("serial_comm.acquisition.SerialTransport") as MockTransport:
            MockTransport.return_value = _good_transport_mock()
            before = datetime.now(tz=timezone.utc)
            with qtbot.waitSignal(worker.reading_ready, timeout=3000):
                worker.start()
            worker.stop()
            with qtbot.waitSignal(worker.finished, timeout=3000):
                pass
            after = datetime.now(tz=timezone.utc)

        assert readings
        assert before <= readings[0].timestamp_wall <= after

    def test_connected_then_disconnected(
        self, qtbot, ppg_spec, ppg_protocol, transport_config
    ):
        worker = _make_worker(ppg_spec, ppg_protocol, transport_config, poll_interval=0.05)

        with patch("serial_comm.acquisition.SerialTransport") as MockTransport:
            MockTransport.return_value = _good_transport_mock()
            with qtbot.waitSignal(worker.connected, timeout=3000):
                worker.start()
            worker.stop()
            with qtbot.waitSignal(worker.disconnected, timeout=3000):
                pass

    def test_device_id_in_reading(
        self, qtbot, ppg_spec, ppg_protocol, transport_config
    ):
        worker = GaugeWorker(
            spec=ppg_spec,
            protocol=ppg_protocol,
            transport_config=transport_config,
            commands=["pressure"],
            poll_interval=0.05,
            device_id="TESTPORT:12",
        )
        readings = []
        worker.reading_ready.connect(readings.append)

        with patch("serial_comm.acquisition.SerialTransport") as MockTransport:
            MockTransport.return_value = _good_transport_mock()
            with qtbot.waitSignal(worker.reading_ready, timeout=3000):
                worker.start()
            worker.stop()
            with qtbot.waitSignal(worker.finished, timeout=3000):
                pass

        assert readings[0].device_id == "TESTPORT:12"


# ---------------------------------------------------------------------------
# Parse error handling
# ---------------------------------------------------------------------------

class TestWorkerParseErrors:
    def test_nak_emits_error_not_crash(
        self, qtbot, ppg_spec, ppg_protocol, transport_config
    ):
        worker = _make_worker(ppg_spec, ppg_protocol, transport_config, poll_interval=0.05)
        errors = []
        worker.error_occurred.connect(errors.append)

        with patch("serial_comm.acquisition.SerialTransport") as MockTransport:
            m = MagicMock()
            m.read_until.return_value = b"@NAK\\"
            m.write.return_value = None
            m.flush_input.return_value = None
            MockTransport.return_value = m

            with qtbot.waitSignal(worker.error_occurred, timeout=3000):
                worker.start()
            worker.stop()
            with qtbot.waitSignal(worker.finished, timeout=3000):
                pass

        assert errors
        assert errors[0].recoverable

    def test_parse_error_does_not_stop_worker(
        self, qtbot, ppg_spec, ppg_protocol, transport_config
    ):
        """A single parse error should not kill the worker; it should keep polling."""
        worker = _make_worker(ppg_spec, ppg_protocol, transport_config, poll_interval=0.05)
        readings = []
        worker.reading_ready.connect(readings.append)

        call_count = [0]

        def read_side_effect(*_):
            call_count[0] += 1
            if call_count[0] == 1:
                return b"@NAK\\"     # first call → parse error
            return b"@ACK5.00E-2\\"  # subsequent → success

        with patch("serial_comm.acquisition.SerialTransport") as MockTransport:
            m = MagicMock()
            m.read_until.side_effect = read_side_effect
            m.write.return_value = None
            m.flush_input.return_value = None
            MockTransport.return_value = m

            with qtbot.waitSignal(worker.reading_ready, timeout=3000):
                worker.start()
            worker.stop()
            with qtbot.waitSignal(worker.finished, timeout=3000):
                pass

        assert len(readings) >= 1
        assert abs(readings[0].value - 5.0e-2) < 1e-9


# ---------------------------------------------------------------------------
# Transport error handling
# ---------------------------------------------------------------------------

class TestWorkerTransportErrors:
    def test_recoverable_transport_error_is_marked_recoverable(
        self, qtbot, ppg_spec, ppg_protocol, transport_config
    ):
        worker = _make_worker(ppg_spec, ppg_protocol, transport_config, poll_interval=0.05)
        errors = []
        worker.error_occurred.connect(errors.append)

        call_count = [0]

        def write_side_effect(*_):
            call_count[0] += 1
            if call_count[0] <= 1:
                raise TransportError("blip")
            # After the error, let reads proceed normally
            return None

        with patch("serial_comm.acquisition.SerialTransport") as MockTransport:
            m = MagicMock()
            m.flush_input.return_value = None
            m.write.side_effect = write_side_effect
            m.read_until.return_value = b"@ACK1.23E-3\\"
            MockTransport.return_value = m

            with qtbot.waitSignal(worker.error_occurred, timeout=3000):
                worker.start()
            with qtbot.waitSignal(worker.finished, timeout=3000):
                worker.stop()

        assert errors[0].recoverable

    def test_repeated_transport_errors_escalate_to_fatal(
        self, qtbot, ppg_spec, ppg_protocol, transport_config
    ):
        worker = _make_worker(ppg_spec, ppg_protocol, transport_config, poll_interval=0.02)
        errors = []
        worker.error_occurred.connect(errors.append)

        with patch("serial_comm.acquisition.SerialTransport") as MockTransport:
            m = MagicMock()
            m.flush_input.return_value = None
            m.write.side_effect = TransportError("port gone")
            MockTransport.return_value = m

            with qtbot.waitSignal(worker.finished, timeout=10000):
                worker.start()

        assert any(not e.recoverable for e in errors)


# ---------------------------------------------------------------------------
# Stop / interrupt
# ---------------------------------------------------------------------------

class TestWorkerStop:
    def test_stop_before_start_is_safe(self, ppg_spec, ppg_protocol, transport_config):
        worker = _make_worker(ppg_spec, ppg_protocol, transport_config)
        worker.stop()  # must not raise

    def test_sleep_interruptible_clamps_negative_duration(
        self, ppg_spec, ppg_protocol, transport_config
    ):
        worker = _make_worker(ppg_spec, ppg_protocol, transport_config)

        with (
            patch(
                "serial_comm.acquisition.time.monotonic",
                side_effect=[10.0, 10.05, 10.11, 10.11],
            ),
            patch("serial_comm.acquisition.time.sleep") as sleep_mock,
        ):
            worker._sleep_interruptible(0.1)

        sleep_mock.assert_called_once_with(0.0)

    def test_stop_exits_within_reasonable_time(
        self, qtbot, ppg_spec, ppg_protocol, transport_config
    ):
        worker = _make_worker(ppg_spec, ppg_protocol, transport_config, poll_interval=0.1)

        with patch("serial_comm.acquisition.SerialTransport") as MockTransport:
            MockTransport.return_value = _good_transport_mock()

            with qtbot.waitSignal(worker.connected, timeout=3000):
                worker.start()

            t0 = time.monotonic()
            worker.stop()
            with qtbot.waitSignal(worker.finished, timeout=5000):
                pass
            elapsed = time.monotonic() - t0

        # poll_interval=0.1, chunk=0.05 → worst case stop takes ~0.15 s
        assert elapsed < 2.0
