"""
GaugeWorker — QThread that owns the serial transport for one gauge.

All serial I/O happens inside run(); the GUI thread communicates only
through Qt signals.  One GaugeWorker per connected gauge.

Lifecycle:
  1. Caller creates GaugeWorker, moves it to a new QThread if desired,
     or lets start() create the thread automatically.
  2. start() → run() opens the port, enters the poll loop.
  3. stop() → sets _stop flag, which causes run() to exit cleanly.
  4. finished signal (from QThread) fires when run() returns.

Signals emitted (all queued across thread boundary):
  reading_ready(DeviceReading)   — one measurement
  error_occurred(DeviceError)    — transport or parse error
  connected()                    — transport opened OK
  disconnected()                 — transport closed
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Sequence

from PyQt6.QtCore import QThread, pyqtSignal

from serial_comm.models import DeviceError, DeviceReading, DeviceSpec
from serial_comm.protocols.base import GaugeProtocol
from serial_comm.transport import SerialTransport, TransportConfig, TransportError

logger = logging.getLogger(__name__)

# How long to sleep after a recoverable error before retrying (seconds)
_ERROR_RETRY_DELAY = 2.0
# Maximum consecutive errors before treating the connection as dead
_MAX_CONSECUTIVE_ERRORS = 5


class GaugeWorker(QThread):
    """
    Worker thread for one gauge connection.

    Parameters
    ----------
    spec:
        DeviceSpec loaded from the device registry.
    protocol:
        Instantiated GaugeProtocol codec (created by DeviceRegistry.make_protocol).
    transport_config:
        Port, baud, parity, etc.
    commands:
        Ordered list of command names to poll each cycle (e.g. ["pressure"]).
    poll_interval:
        Seconds between poll cycles.  Actual interval ≥ this value.
    device_id:
        Human-readable ID used in DeviceReading/DeviceError (e.g. "COM3:254").
    """

    reading_ready = pyqtSignal(object)   # DeviceReading
    error_occurred = pyqtSignal(object)  # DeviceError
    connected = pyqtSignal()
    disconnected = pyqtSignal()

    def __init__(
        self,
        spec: DeviceSpec,
        protocol: GaugeProtocol,
        transport_config: TransportConfig,
        commands: Sequence[str],
        poll_interval: float = 1.0,
        device_id: str = "",
    ) -> None:
        super().__init__()
        self._spec = spec
        self._protocol = protocol
        self._transport_cfg = transport_config
        self._commands = list(commands)
        self._poll_interval = poll_interval
        self._device_id = device_id or f"{transport_config.port}:{protocol.address}"
        self._transport: SerialTransport | None = None

    # ------------------------------------------------------------------
    # Public control API  (call from GUI thread)
    # ------------------------------------------------------------------

    def stop(self) -> None:
        """Request the worker to exit cleanly.  Returns immediately."""
        self.requestInterruption()

    # ------------------------------------------------------------------
    # QThread.run — everything below runs in the worker thread
    # ------------------------------------------------------------------

    def run(self) -> None:
        transport = SerialTransport(self._transport_cfg)
        self._transport = transport

        try:
            transport.open()
        except TransportError as exc:
            self._emit_error(str(exc), recoverable=False)
            return

        self.connected.emit()
        logger.info("[%s] connected", self._device_id)

        try:
            if self._protocol.supports_continuous_output():
                self._run_continuous(transport)
            else:
                self._run_polled(transport)
        finally:
            transport.close()
            self.disconnected.emit()
            logger.info("[%s] disconnected", self._device_id)

    # ------------------------------------------------------------------
    # Poll loop (request–response gauges: PPG, BCG, PCG, …)
    # ------------------------------------------------------------------

    def _run_polled(self, transport: SerialTransport) -> None:
        consecutive_errors = 0

        while not self.isInterruptionRequested():
            cycle_start = time.monotonic()

            for command in self._commands:
                if self.isInterruptionRequested():
                    break
                try:
                    reading = self._poll_one(transport, command)
                    if reading is not None:
                        self.reading_ready.emit(reading)
                        consecutive_errors = 0
                except TransportError as exc:
                    consecutive_errors += 1
                    recoverable = consecutive_errors < _MAX_CONSECUTIVE_ERRORS
                    self._emit_error(f"Transport error ({command}): {exc}",
                                     recoverable=recoverable)
                    if not recoverable:
                        return
                    self._sleep_interruptible(_ERROR_RETRY_DELAY)

            elapsed = time.monotonic() - cycle_start
            remaining = self._poll_interval - elapsed
            if remaining > 0:
                self._sleep_interruptible(remaining)

    def _poll_one(
        self, transport: SerialTransport, command: str
    ) -> DeviceReading | None:
        """Send one request–response pair. Returns a DeviceReading or None on parse error."""
        request = self._protocol.build_request(command)
        transport.flush_input()
        transport.write(request)

        raw = self._read_response(transport, command)
        result = self._protocol.parse_response(raw, command)

        if not result.success:
            self._emit_error(f"Parse error ({command}): {result.error}", recoverable=True)
            return None

        if result.value is None:
            return None

        now_mono = time.monotonic()
        return DeviceReading(
            device_id=self._device_id,
            timestamp_mono=now_mono,
            timestamp_wall=datetime.now(tz=timezone.utc),
            value=result.value,
            unit=result.unit,
            command=command,
            raw=result.raw,
        )

    def _read_response(self, transport: SerialTransport, command: str) -> bytes:
        """Read one response frame from the transport."""
        from serial_comm.protocols.ppg_ascii import TERMINATOR as PPG_TERM
        from serial_comm.protocols.pfeiffer_ascii import TERMINATOR as PA_TERM
        from serial_comm.protocols.pfeiffer_binary import PfeifferBinaryProtocol
        from serial_comm.protocols.cdg_serial import RESPONSE_LENGTH, RESPONSE_SYNC

        proto = self._protocol
        if isinstance(proto, PfeifferBinaryProtocol):
            # Read fixed 11-byte frames (or variable — peek length byte)
            header = transport.read_bytes(4)
            if len(header) < 4:
                return header
            msg_len = header[3]
            rest = transport.read_bytes(msg_len + 2)  # payload + 2 CRC bytes
            return header + rest
        if self._spec.protocol == "ppg_ascii":
            return transport.read_until(PPG_TERM)
        if self._spec.protocol in ("pfeiffer_ascii",):
            return transport.read_until(PA_TERM)
        # CDG: fixed-length frame (handled in _run_continuous)
        return transport.read_bytes(RESPONSE_LENGTH)

    # ------------------------------------------------------------------
    # Continuous-output loop (CDG series)
    # ------------------------------------------------------------------

    def _run_continuous(self, transport: SerialTransport) -> None:
        """
        For gauges that stream frames continuously (CDG025D/CDG045D).
        The host sends a single "pressure" request to start streaming, then
        just reads frames as they arrive.
        """
        from serial_comm.protocols.cdg_serial import RESPONSE_LENGTH, RESPONSE_SYNC

        # Send initial read request to begin continuous output
        try:
            request = self._protocol.build_request("pressure")
            transport.write(request)
        except TransportError as exc:
            self._emit_error(f"Failed to start continuous output: {exc}", recoverable=False)
            return

        consecutive_errors = 0

        while not self.isInterruptionRequested():
            try:
                raw = transport.read_frame(RESPONSE_SYNC, RESPONSE_LENGTH)
                if not raw:
                    consecutive_errors += 1
                    if consecutive_errors >= _MAX_CONSECUTIVE_ERRORS:
                        self._emit_error("No data from CDG — giving up", recoverable=False)
                        return
                    continue

                result = self._protocol.parse_continuous(raw)
                if result.success and result.value is not None:
                    now = time.monotonic()
                    self.reading_ready.emit(DeviceReading(
                        device_id=self._device_id,
                        timestamp_mono=now,
                        timestamp_wall=datetime.now(tz=timezone.utc),
                        value=result.value,
                        unit=result.unit,
                        command="pressure",
                        raw=raw,
                    ))
                    consecutive_errors = 0
                elif not result.success:
                    self._emit_error(result.error or "CDG parse error", recoverable=True)
                    consecutive_errors += 1

            except TransportError as exc:
                consecutive_errors += 1
                recoverable = consecutive_errors < _MAX_CONSECUTIVE_ERRORS
                self._emit_error(f"CDG transport error: {exc}", recoverable=recoverable)
                if not recoverable:
                    return

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
        """Sleep for *seconds* total, waking every *chunk* to check for stop request."""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if self.isInterruptionRequested():
                break
            time.sleep(min(chunk, end - time.monotonic()))
