"""
PortScanner — QThread that probes serial ports for known gauge protocols.

Emits ``port_found`` for each responsive device found, then ``scan_complete``
when all ports have been tried.  After an initial identification probe, the
scanner runs a small set of follow-up queries to gather model / firmware /
measurement data so the user has more context before selecting a device.
"""

from __future__ import annotations

import logging

import serial
from PyQt6.QtCore import QThread, pyqtSignal

logger = logging.getLogger(__name__)

_PROBE_TIMEOUT = 0.6   # seconds — slightly longer to handle slow gauge response

# ── PPG ASCII probes (INFICON PPG550 / PPG570) ─────────────────────────────
# Broadcast address 254 works in RS-232 mode for all PPG gauges
_PPG_FV  = b"@254FV?\\"    # firmware version
_PPG_SN  = b"@254SN?\\"    # serial number
_PPG_PR3 = b"@254PR3?\\"   # Pirani pressure
_PPG_PR1 = b"@254PR1?\\"   # combined (piezo+Pirani) pressure — PPG570 only

# ── Pfeiffer ASCII probes ───────────────────────────────────────────────────
# Frame format: addr(3) + action(2) + param(3) + len(2) + data + chk(3) + CR
# Checksum = sum(all chars before the 3-char checksum) % 256


def _pfa_checksum(body: str) -> int:
    return sum(ord(c) for c in body) % 256


def _pfa_read_frame(param: int) -> bytes:
    """Build a Pfeiffer ASCII read-request frame for *param*."""
    body = f"001003{param:02d}902=?"   # addr=001, action=00, param, len=02, data=?
    # Correct body: addr(3) + action(2) + param(3) + len(2) + data(2)
    body = f"001" + "00" + f"{param:03d}" + "02" + "=?"
    return (body + f"{_pfa_checksum(body):03d}\r").encode("ascii")


_PFA_FW  = _pfa_read_frame(309)   # firmware version
_PFA_SWV = _pfa_read_frame(310)   # hardware version (fallback id)


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

    port_found   = pyqtSignal(str, str, str)  # port, description, model_hint
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

    # ------------------------------------------------------------------
    # Port probing
    # ------------------------------------------------------------------

    def _probe_port(self, port: str) -> None:
        try:
            with serial.Serial(
                port=port, baudrate=9600,
                bytesize=8, parity="N", stopbits=1,
                timeout=_PROBE_TIMEOUT, write_timeout=1.0,
            ) as ser:
                # ── PPG ASCII probe ─────────────────────────────────────
                ser.reset_input_buffer()
                ser.write(_PPG_FV)
                raw = self._read_until(ser, b"\\", 64)

                if raw and raw.startswith(b"@ACK"):
                    self._identify_ppg(ser, port, raw)
                    return

                # ── Pfeiffer ASCII probe ────────────────────────────────
                ser.reset_input_buffer()
                ser.write(_PFA_FW)
                raw = self._read_until(ser, b"\r", 64)

                if raw and self._valid_pfeiffer_frame(raw):
                    self._identify_pfeiffer(ser, port, raw)
                    return

        except serial.SerialException as exc:
            logger.debug("Scanner: %s — %s", port, exc)

    # ------------------------------------------------------------------
    # PPG identification
    # ------------------------------------------------------------------

    def _identify_ppg(self, ser: serial.Serial, port: str, fw_raw: bytes) -> None:
        firmware = self._ppg_data(fw_raw)

        # Serial number
        ser.reset_input_buffer()
        ser.write(_PPG_SN)
        sn_raw = self._read_until(ser, b"\\", 64)
        serial_num = self._ppg_data(sn_raw) if sn_raw and sn_raw.startswith(b"@ACK") else ""

        # Try combined pressure first (PPG570), fall back to Pirani (PPG550)
        pressure = ""
        for probe in (_PPG_PR1, _PPG_PR3):
            ser.reset_input_buffer()
            ser.write(probe)
            pr_raw = self._read_until(ser, b"\\", 64)
            if pr_raw and pr_raw.startswith(b"@ACK"):
                val = self._ppg_data(pr_raw)
                # Accept if it looks numeric (not a status string)
                try:
                    float(val)
                    pressure = val
                    break
                except ValueError:
                    pressure = val  # status like "UR" / "ATM" — still useful
                    break

        # Build rich description
        parts: list[str] = []
        if firmware:
            parts.append(f"FW: {firmware}")
        if serial_num:
            parts.append(f"SN: {serial_num}")
        if pressure:
            parts.append(f"P: {pressure} mbar")
        desc = "  |  ".join(parts) if parts else "PPG gauge"

        model_hint = self._guess_ppg_model(firmware, serial_num)
        self.port_found.emit(port, desc, model_hint)

    @staticmethod
    def _ppg_data(raw: bytes) -> str:
        """Extract the data payload from a ``@ACK{data}\\`` frame."""
        return raw[4:-1].decode("ascii", errors="replace").strip()

    @staticmethod
    def _guess_ppg_model(firmware: str, serial_num: str) -> str:
        combined = (firmware + serial_num).upper()
        if "570" in combined:
            return "INFICON PPG570"
        if "550" in combined:
            return "INFICON PPG550"
        return "INFICON PPG"

    # ------------------------------------------------------------------
    # Pfeiffer identification
    # ------------------------------------------------------------------

    def _identify_pfeiffer(self, ser: serial.Serial, port: str, fw_raw: bytes) -> None:
        firmware = self._pfeiffer_data(fw_raw).strip()

        # Try to read hardware version for additional context
        ser.reset_input_buffer()
        ser.write(_PFA_SWV)
        hw_raw = self._read_until(ser, b"\r", 64)
        hw_ver = ""
        if hw_raw and self._valid_pfeiffer_frame(hw_raw):
            hw_ver = self._pfeiffer_data(hw_raw).strip()

        parts: list[str] = []
        if firmware:
            parts.append(f"FW: {firmware}")
        if hw_ver:
            parts.append(f"HW: {hw_ver}")
        desc = "  |  ".join(parts) if parts else "Pfeiffer device"

        # Identify model — INFICON makes all gauges; Pfeiffer makes the TC600 turbo
        fw_upper = firmware.upper()
        if "TC600" in fw_upper or "TC 600" in fw_upper:
            model_hint = "TC600 (Turbo)"
        elif "BCG550" in fw_upper or "BCG552" in fw_upper:
            model_hint = "INFICON BCG552"
        elif "BCG" in fw_upper:
            model_hint = "INFICON BCG450"
        elif "BPG" in fw_upper:
            model_hint = f"INFICON {firmware[:6].strip()}"
        elif "MPG" in fw_upper or "MAG" in fw_upper:
            model_hint = f"INFICON {firmware[:6].strip()}"
        elif "PCG" in fw_upper or "PSG" in fw_upper or "OPG" in fw_upper:
            model_hint = f"INFICON {firmware[:6].strip()}"
        else:
            model_hint = f"INFICON Gauge ({firmware[:6].strip()})" if firmware else "Unknown Pfeiffer ASCII"

        self.port_found.emit(port, desc, model_hint)

    # ------------------------------------------------------------------
    # Pfeiffer frame validation
    # ------------------------------------------------------------------

    @staticmethod
    def _valid_pfeiffer_frame(raw: bytes) -> bool:
        """
        Return True only for a well-formed Pfeiffer ASCII response frame.

        Format: addr(3) action(2) param(3) len(2) data(len) chk(3) CR
        Total minimum length = 3+2+3+2+0+3+1 = 14 bytes (data_len = 0).
        """
        if len(raw) < 14:
            return False
        try:
            text = raw.decode("ascii")
        except UnicodeDecodeError:
            return False
        # First 3 chars must be the numeric address
        if not text[:3].isdigit():
            return False
        # Chars 8:10 encode the data length
        if not text[8:10].isdigit():
            return False
        data_len = int(text[8:10])
        # Expected total: addr(3)+action(2)+param(3)+len(2)+data+chk(3)+CR(1)
        expected = 14 + data_len
        if len(text) != expected:
            return False
        # Validate checksum
        body = text[:-4]   # everything before the 3-char checksum + CR
        expected_cs = sum(ord(c) for c in body) % 256
        try:
            actual_cs = int(text[-4:-1])
        except ValueError:
            return False
        return actual_cs == expected_cs

    @staticmethod
    def _pfeiffer_data(raw: bytes) -> str:
        """Extract the data field from a validated Pfeiffer ASCII response."""
        text = raw.decode("ascii", errors="replace")
        data_len = int(text[8:10])
        return text[10: 10 + data_len]

    # ------------------------------------------------------------------
    # Read helpers
    # ------------------------------------------------------------------

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
