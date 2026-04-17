"""
PortScanner — QThread that probes serial ports for known gauge protocols.

Emits ``port_found`` for each responsive device found, then ``scan_complete``
when all ports have been tried.  The scan is non-blocking from the GUI thread's
perspective.
"""

from __future__ import annotations

import logging

import serial
from PyQt6.QtCore import QThread, pyqtSignal

logger = logging.getLogger(__name__)

# How long to wait for a response from each port (seconds)
_PROBE_TIMEOUT = 0.5

# PPG ASCII broadcast firmware-version query (addr 254, read-only)
_PPG_PROBE = b"@254FV?\\"

# Pfeiffer ASCII: read param 309 (firmware) from address 001
# Frame: addr(3) + action(2) + param(3) + len(2) + data + checksum(3) + CR
# "001" + "00" + "309" + "02" + "=?" + checksum + "\r"
_PFA_BODY = "001003090 2=?"


def _pfeiffer_checksum(body: str) -> int:
    return sum(ord(c) for c in body) % 256


_PFA_PROBE = (_PFA_BODY + f"{_pfeiffer_checksum(_PFA_BODY):03d}\r").encode("ascii")


class PortScanner(QThread):
    """
    Probes a list of serial ports for recognisable gauge responses.

    Signals
    -------
    port_found(port, description, suggested_model)
        Emitted for each port that returned a recognisable response.
    scan_complete()
        Emitted when all ports have been probed.
    """

    port_found = pyqtSignal(str, str, str)   # port, description, suggested_model
    scan_complete = pyqtSignal()

    def __init__(self, ports: list[str], parent=None) -> None:
        super().__init__(parent)
        self._ports = ports

    def run(self) -> None:
        for port in self._ports:
            if self.isInterruptionRequested():
                break
            self._probe_port(port)
        self.scan_complete.emit()

    def _probe_port(self, port: str) -> None:
        try:
            with serial.Serial(
                port=port,
                baudrate=9600,
                bytesize=8,
                parity="N",
                stopbits=1,
                timeout=_PROBE_TIMEOUT,
                write_timeout=1.0,
            ) as ser:
                # --- PPG probe ---
                ser.reset_input_buffer()
                ser.write(_PPG_PROBE)
                raw = self._read_until(ser, b"\\", 64)
                if raw and raw.startswith(b"@ACK"):
                    desc = raw.decode("ascii", errors="replace").strip()
                    self.port_found.emit(port, desc, "PPG family")
                    return

                # --- Pfeiffer ASCII probe ---
                ser.reset_input_buffer()
                ser.write(_PFA_PROBE)
                raw = self._read_until(ser, b"\r", 64)
                if raw and len(raw) >= 13:
                    desc = raw.decode("ascii", errors="replace").strip()
                    self.port_found.emit(port, desc, "Pfeiffer ASCII")
                    return

        except serial.SerialException as exc:
            logger.debug("Scanner: %s — %s", port, exc)

    @staticmethod
    def _read_until(ser: serial.Serial, term: bytes, max_bytes: int) -> bytes:
        buf = bytearray()
        while len(buf) < max_bytes:
            b = ser.read(1)
            if not b:
                break
            buf.extend(b)
            if buf.endswith(term):
                break
        return bytes(buf)
