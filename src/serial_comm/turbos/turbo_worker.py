"""
TurboWorker — QThread for Pfeiffer turbo pump controllers (TC600).

Architecturally separate from GaugeWorker: turbos are Pfeiffer products
and have distinct operational concerns (pump control, error acknowledgement,
speed ramp monitoring) that don't fit the gauge polling model.

Signals:
  status_ready(TurboStatus)   — periodic status snapshot (all polled params)
  error_occurred(DeviceError) — transport or protocol errors
  connected()                 — transport opened OK
  disconnected()              — transport closed

TurboStatus is a flat dict of {command_name: GaugeReading} captured in
one polling round.  The GUI thread presents it as a dashboard.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Sequence

from PyQt6.QtCore import QThread, pyqtSignal

from serial_comm.models import DeviceError
from serial_comm.protocols.pfeiffer_ascii import PfeifferAsciiProtocol
from serial_comm.transport import SerialTransport, TransportConfig, TransportError

logger = logging.getLogger(__name__)

# Seconds to wait after a recoverable error
_ERROR_RETRY_DELAY = 3.0
# Consecutive errors before declaring the connection dead
_MAX_CONSECUTIVE_ERRORS = 5

# Default commands polled every cycle for a status snapshot
_DEFAULT_POLL_COMMANDS = (
    "pump_on",
    "actual_speed_hz",
    "motor_current_A",
    "motor_power_W",
    "error_code",
)


@dataclass
class TurboStatus:
    """One full polling cycle snapshot from the TC600."""

    device_id: str
    timestamp_mono: float
    timestamp_wall: datetime
    readings: dict[str, Any] = field(default_factory=dict)
    # readings[cmd] = GaugeReading


class TurboWorker(QThread):
    """
    Worker thread for one Pfeiffer turbo pump controller.

    Parameters
    ----------
    protocol:
        TC600Protocol (or any PfeifferAsciiProtocol subclass) already
        configured with the correct address.
    transport_config:
        Port and baud settings.
    poll_commands:
        Command names to read each cycle.
    poll_interval:
        Seconds between polling cycles.
    device_id:
        String identifier for logging and signal payloads.
    """

    status_ready = pyqtSignal(object)    # TurboStatus
    error_occurred = pyqtSignal(object)  # DeviceError
    connected = pyqtSignal()
    disconnected = pyqtSignal()

    def __init__(
        self,
        protocol: PfeifferAsciiProtocol,
        transport_config: TransportConfig,
        poll_commands: Sequence[str] = _DEFAULT_POLL_COMMANDS,
        poll_interval: float = 1.0,
        device_id: str = "",
    ) -> None:
        super().__init__()
        self._protocol = protocol
        self._transport_cfg = transport_config
        self._poll_commands = list(poll_commands)
        self._poll_interval = poll_interval
        self._device_id = device_id or f"{transport_config.port}:{protocol.address}"
        self._pending_command: tuple[str, Any] | None = None
        self._cmd_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public control  (call from GUI thread)
    # ------------------------------------------------------------------

    def stop(self) -> None:
        self.requestInterruption()

    def send_command(self, command: str, value: Any = None) -> None:
        """Queue a write command to be sent on the next loop iteration.

        Thread-safe: latest command wins if several are queued before the worker
        processes them (e.g. rapid Start → Stop clicks).
        """
        with self._cmd_lock:
            self._pending_command = (command, value)

    # ------------------------------------------------------------------
    # QThread.run
    # ------------------------------------------------------------------

    def run(self) -> None:
        transport = SerialTransport(self._transport_cfg)

        try:
            transport.open()
        except TransportError as exc:
            self._emit_error(str(exc), recoverable=False)
            return

        self.connected.emit()
        logger.info("[%s] TC600 connected", self._device_id)

        try:
            self._run_loop(transport)
        finally:
            transport.close()
            self.disconnected.emit()
            logger.info("[%s] TC600 disconnected", self._device_id)

    # ------------------------------------------------------------------
    # Poll loop
    # ------------------------------------------------------------------

    def _run_loop(self, transport: SerialTransport) -> None:
        from serial_comm.protocols.pfeiffer_ascii import TERMINATOR

        consecutive_errors = 0

        while not self.isInterruptionRequested():
            cycle_start = time.monotonic()

            # Send any pending write command first
            with self._cmd_lock:
                pending = self._pending_command
                self._pending_command = None
            if pending is not None:
                cmd, val = pending
                try:
                    request = self._protocol.build_request(cmd, value=val)
                    transport.flush_input()
                    transport.write(request)
                    # Read the echo/ACK (TC600 echoes writes)
                    transport.read_until(TERMINATOR)
                    logger.debug("[%s] sent %s=%s", self._device_id, cmd, val)
                except (TransportError, ValueError) as exc:
                    self._emit_error(f"Write failed ({cmd}): {exc}", recoverable=True)

            # Read all polled parameters
            readings: dict[str, Any] = {}
            for cmd in self._poll_commands:
                if self.isInterruptionRequested():
                    return
                try:
                    request = self._protocol.build_request(cmd)
                    transport.flush_input()
                    transport.write(request)
                    raw = transport.read_until(TERMINATOR)
                    result = self._protocol.parse_response(raw, cmd)
                    readings[cmd] = result
                    consecutive_errors = 0
                except TransportError as exc:
                    consecutive_errors += 1
                    recoverable = consecutive_errors < _MAX_CONSECUTIVE_ERRORS
                    self._emit_error(f"Transport error ({cmd}): {exc}",
                                     recoverable=recoverable)
                    if not recoverable:
                        return
                    self._sleep_interruptible(_ERROR_RETRY_DELAY)
                    break  # skip rest of cycle on transport error

            if readings:
                now = time.monotonic()
                status = TurboStatus(
                    device_id=self._device_id,
                    timestamp_mono=now,
                    timestamp_wall=datetime.now(tz=timezone.utc),
                    readings=readings,
                )
                self.status_ready.emit(status)

            elapsed = time.monotonic() - cycle_start
            remaining = self._poll_interval - elapsed
            if remaining > 0:
                self._sleep_interruptible(remaining)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _emit_error(self, message: str, *, recoverable: bool) -> None:
        now = time.monotonic()
        err = DeviceError(
            device_id=self._device_id,
            timestamp_mono=now,
            timestamp_wall=datetime.now(tz=timezone.utc),
            message=message,
            recoverable=recoverable,
        )
        self.error_occurred.emit(err)
        if recoverable:
            logger.warning("[%s] %s", self._device_id, message)
        else:
            logger.error("[%s] FATAL — %s", self._device_id, message)

    def _sleep_interruptible(self, seconds: float, chunk: float = 0.05) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if self.isInterruptionRequested():
                break
            time.sleep(min(chunk, end - time.monotonic()))
