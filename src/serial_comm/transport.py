"""
SerialTransport — thin pyserial wrapper with RS-485 direction control.

All serial I/O in the application goes through this class.  Protocol codecs
work with bytes; they never touch a serial.Serial object directly.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator

import serial

logger = logging.getLogger(__name__)


@dataclass
class RS485Config:
    """Timing parameters for RS-485 half-duplex direction control via RTS."""

    rts_level_for_tx: bool = True    # RTS high while transmitting
    rts_level_for_rx: bool = False   # RTS low while receiving
    delay_before_tx: float = 0.002   # seconds — let bus settle before write
    delay_before_rx: float = 0.002   # seconds — let last bit clock out before switching


@dataclass
class TransportConfig:
    """Full configuration for one serial port."""

    port: str
    baud: int = 9600
    parity: str = "N"          # "N", "E", "O"
    data_bits: int = 8
    stop_bits: int = 1
    timeout: float = 2.0       # read timeout in seconds
    rs485: RS485Config | None = None  # None → RS-232 (full-duplex)
    write_timeout: float = 1.0


class TransportError(OSError):
    """Raised when the transport cannot open, read, or write."""


class SerialTransport:
    """
    Wraps a single pyserial port.

    Thread-safety: instances are owned by a single worker thread.  The GUI
    thread never touches this object directly.
    """

    def __init__(self, config: TransportConfig) -> None:
        self._cfg = config
        self._ser: serial.Serial | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def open(self) -> None:
        if self._ser and self._ser.is_open:
            return
        try:
            self._ser = serial.Serial(
                port=self._cfg.port,
                baudrate=self._cfg.baud,
                parity=self._cfg.parity,
                bytesize=self._cfg.data_bits,
                stopbits=self._cfg.stop_bits,
                timeout=self._cfg.timeout,
                write_timeout=self._cfg.write_timeout,
            )
            logger.info("Opened %s @ %d baud", self._cfg.port, self._cfg.baud)
        except serial.SerialException as exc:
            raise TransportError(f"Cannot open {self._cfg.port}: {exc}") from exc

    def close(self) -> None:
        if self._ser and self._ser.is_open:
            try:
                self._ser.close()
            except serial.SerialException:
                pass
            logger.info("Closed %s", self._cfg.port)
        self._ser = None

    @property
    def is_open(self) -> bool:
        return self._ser is not None and self._ser.is_open

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def write(self, data: bytes) -> None:
        """Write bytes, handling RS-485 direction control around the write."""
        ser = self._require_open()
        cfg = self._cfg.rs485

        if cfg is not None:
            ser.rts = cfg.rts_level_for_tx
            time.sleep(cfg.delay_before_tx)

        try:
            ser.write(data)
            ser.flush()
            logger.debug("TX %s", data.hex(" "))
        except serial.SerialException as exc:
            raise TransportError(f"Write failed on {self._cfg.port}: {exc}") from exc
        finally:
            if cfg is not None:
                time.sleep(cfg.delay_before_rx)
                ser.rts = cfg.rts_level_for_rx

    # ------------------------------------------------------------------
    # Read helpers
    # ------------------------------------------------------------------

    def read_bytes(self, n: int) -> bytes:
        """Read exactly n bytes, returning fewer only on timeout."""
        ser = self._require_open()
        try:
            data = ser.read(n)
            if data:
                logger.debug("RX %s", data.hex(" "))
            return data
        except serial.SerialException as exc:
            raise TransportError(f"Read failed on {self._cfg.port}: {exc}") from exc

    def read_until(self, terminator: bytes, max_bytes: int = 256) -> bytes:
        """
        Read until *terminator* is seen (included) or *max_bytes* received.

        Returns whatever was read, including the terminator if found.
        Raises TransportError if the port errors; returns partial data on timeout.
        """
        ser = self._require_open()
        buf = bytearray()
        try:
            while len(buf) < max_bytes:
                b = ser.read(1)
                if not b:
                    break  # timeout
                buf.extend(b)
                if buf.endswith(terminator):
                    break
        except serial.SerialException as exc:
            raise TransportError(f"Read failed on {self._cfg.port}: {exc}") from exc
        if buf:
            logger.debug("RX %s", bytes(buf).hex(" "))
        return bytes(buf)

    def read_frame(self, sync_byte: int, frame_length: int) -> bytes:
        """
        Scan the incoming stream for *sync_byte*, then read *frame_length* - 1
        more bytes.  Returns the complete frame (sync byte included).

        Returns b'' if the sync byte is not found within 2 × frame_length reads.
        """
        ser = self._require_open()
        buf = bytearray()
        attempts = 0
        max_attempts = frame_length * 2

        try:
            while attempts < max_attempts:
                b = ser.read(1)
                if not b:
                    break
                attempts += 1
                if b[0] == sync_byte:
                    buf.extend(b)
                    rest = ser.read(frame_length - 1)
                    buf.extend(rest)
                    break
        except serial.SerialException as exc:
            raise TransportError(f"Read failed on {self._cfg.port}: {exc}") from exc

        data = bytes(buf)
        if data:
            logger.debug("RX frame %s", data.hex(" "))
        return data

    def flush_input(self) -> None:
        """Discard any bytes waiting in the OS receive buffer."""
        if self._ser and self._ser.is_open:
            self._ser.reset_input_buffer()

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    @contextmanager
    def session(self) -> Iterator[SerialTransport]:
        """Open on entry, close on exit (even if an exception is raised)."""
        self.open()
        try:
            yield self
        finally:
            self.close()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _require_open(self) -> serial.Serial:
        if self._ser is None or not self._ser.is_open:
            raise TransportError(f"{self._cfg.port} is not open")
        return self._ser
