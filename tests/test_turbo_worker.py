"""
Tests for TurboWorker — Pfeiffer TC600 polling loop.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from serial_comm.turbos.tc600_protocol import TC600Protocol
from serial_comm.turbos.turbo_worker import TurboWorker, TurboStatus
from serial_comm.transport import TransportConfig, TransportError


@pytest.fixture
def tc600() -> TC600Protocol:
    return TC600Protocol(address=1)


@pytest.fixture
def cfg() -> TransportConfig:
    return TransportConfig(port="COM88", baud=9600)


def _make_response(addr: int, pid: int, data: str) -> bytes:
    from serial_comm.protocols.pfeiffer_ascii import _checksum
    action = "10"
    body = f"{addr:03d}{action}{pid:03d}{len(data):02d}{data}"
    csum = _checksum(body)
    return f"{body}{csum:03d}\r".encode("ascii")


def _default_mock_transport(read_response: bytes = b"") -> MagicMock:
    m = MagicMock()
    m.write.return_value = None
    m.flush_input.return_value = None
    m.read_until.return_value = read_response
    return m


class TestTurboWorkerLifecycle:
    def test_connected_on_open(self, qtbot, tc600, cfg):
        worker = TurboWorker(
            protocol=tc600,
            transport_config=cfg,
            poll_commands=["pump_on"],
            poll_interval=0.05,
        )
        with patch("serial_comm.turbos.turbo_worker.SerialTransport") as MockT:
            m = _default_mock_transport()
            # Return a valid response for pump_on (P023, boolean_old)
            m.read_until.return_value = _make_response(1, 23, "000000")
            MockT.return_value = m

            with qtbot.waitSignal(worker.connected, timeout=3000):
                worker.start()
            worker.stop()
            with qtbot.waitSignal(worker.finished, timeout=3000):
                pass

    def test_open_failure_emits_error(self, qtbot, tc600, cfg):
        worker = TurboWorker(
            protocol=tc600,
            transport_config=cfg,
            poll_commands=["pump_on"],
        )
        errors = []
        worker.error_occurred.connect(errors.append)

        with patch("serial_comm.turbos.turbo_worker.SerialTransport") as MockT:
            MockT.return_value.open.side_effect = TransportError("busy")
            with qtbot.waitSignal(worker.finished, timeout=3000):
                worker.start()

        assert errors
        assert not errors[0].recoverable

    def test_disconnected_on_stop(self, qtbot, tc600, cfg):
        worker = TurboWorker(
            protocol=tc600,
            transport_config=cfg,
            poll_commands=["pump_on"],
            poll_interval=0.05,
        )
        with patch("serial_comm.turbos.turbo_worker.SerialTransport") as MockT:
            m = _default_mock_transport()
            m.read_until.return_value = _make_response(1, 23, "000000")
            MockT.return_value = m

            with qtbot.waitSignal(worker.connected, timeout=3000):
                worker.start()
            worker.stop()
            with qtbot.waitSignal(worker.disconnected, timeout=3000):
                pass


class TestTurboStatusEmission:
    def test_emits_status_with_readings(self, qtbot, tc600, cfg):
        worker = TurboWorker(
            protocol=tc600,
            transport_config=cfg,
            poll_commands=["actual_speed_hz"],
            poll_interval=0.05,
        )
        statuses = []
        worker.status_ready.connect(statuses.append)

        with patch("serial_comm.turbos.turbo_worker.SerialTransport") as MockT:
            m = _default_mock_transport()
            # actual_speed_hz = P309, u_integer, value "027000" = 27000 Hz
            m.read_until.return_value = _make_response(1, 309, "027000")
            MockT.return_value = m

            with qtbot.waitSignal(worker.status_ready, timeout=3000):
                worker.start()
            worker.stop()
            with qtbot.waitSignal(worker.finished, timeout=3000):
                pass

        assert statuses
        s = statuses[0]
        assert isinstance(s, TurboStatus)
        assert "actual_speed_hz" in s.readings
        r = s.readings["actual_speed_hz"]
        assert r.success
        assert abs(r.value - 27000.0) < 1

    def test_status_has_timestamp(self, qtbot, tc600, cfg):
        from datetime import datetime, timezone
        worker = TurboWorker(
            protocol=tc600,
            transport_config=cfg,
            poll_commands=["pump_on"],
            poll_interval=0.05,
        )
        statuses = []
        worker.status_ready.connect(statuses.append)

        with patch("serial_comm.turbos.turbo_worker.SerialTransport") as MockT:
            m = _default_mock_transport()
            m.read_until.return_value = _make_response(1, 23, "000000")
            MockT.return_value = m

            before = datetime.now(tz=timezone.utc)
            with qtbot.waitSignal(worker.status_ready, timeout=3000):
                worker.start()
            worker.stop()
            with qtbot.waitSignal(worker.finished, timeout=3000):
                pass
            after = datetime.now(tz=timezone.utc)

        assert statuses
        assert before <= statuses[0].timestamp_wall <= after


class TestTurboWorkerErrors:
    def test_transport_error_emits_recoverable(self, qtbot, tc600, cfg):
        worker = TurboWorker(
            protocol=tc600,
            transport_config=cfg,
            poll_commands=["pump_on"],
            poll_interval=0.05,
        )
        errors = []
        worker.error_occurred.connect(errors.append)

        with patch("serial_comm.turbos.turbo_worker.SerialTransport") as MockT, \
             patch("serial_comm.turbos.turbo_worker._ERROR_RETRY_DELAY", 0.02):
            m = _default_mock_transport()
            m.write.side_effect = TransportError("write fail")
            MockT.return_value = m

            with qtbot.waitSignal(worker.error_occurred, timeout=3000):
                worker.start()
            worker.stop()
            with qtbot.waitSignal(worker.finished, timeout=3000):
                pass

        assert errors[0].recoverable

    def test_repeated_errors_become_fatal(self, qtbot, tc600, cfg):
        worker = TurboWorker(
            protocol=tc600,
            transport_config=cfg,
            poll_commands=["pump_on"],
            poll_interval=0.02,
        )
        errors = []
        worker.error_occurred.connect(errors.append)

        with patch("serial_comm.turbos.turbo_worker.SerialTransport") as MockT, \
             patch("serial_comm.turbos.turbo_worker._ERROR_RETRY_DELAY", 0.02):
            m = _default_mock_transport()
            m.write.side_effect = TransportError("gone")
            MockT.return_value = m

            with qtbot.waitSignal(worker.finished, timeout=5000):
                worker.start()

        assert any(not e.recoverable for e in errors)
