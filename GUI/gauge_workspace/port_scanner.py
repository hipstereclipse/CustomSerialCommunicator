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

# ── CDG/HPG serial probe (INFICON SKY binary family) ───────────────────────
# Use an explicit read command so scanner classification is based on an active
# protocol probe, not only passive observation of a byte pattern.
_CDG_PRESSURE_READ = b"\x03\x00\x00\x00\x00"

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

    port_found   = pyqtSignal(str, str, str, object)  # port, description, model_hint, metadata
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
                # Discard RS-485 echo (device reflects our command back before
                # sending the actual response on half-duplex buses).
                if raw == _PPG_FV:
                    raw = self._read_until(ser, b"\\", 64)
                    # If *nothing* follows the echo, the port is likely in
                    # hardware loopback — no point in other protocol probes.
                    if not raw:
                        logger.info("Scanner: %s shows echo but no response — likely loopback/no gauge", port)
                        return

                if raw and self._ppg_is_ack(raw):
                    self._identify_ppg(ser, port, raw)
                    return

                # ── Pfeiffer ASCII probe ────────────────────────────────
                ser.reset_input_buffer()
                ser.write(_PFA_FW)
                raw = self._read_until(ser, b"\r", 64)
                # Reject an exact echo of our request (loopback)
                if raw == _PFA_FW:
                    logger.info("Scanner: %s echoed Pfeiffer probe — likely loopback/no gauge", port)
                    return

                if raw and self._valid_pfeiffer_frame(raw):
                    self._identify_pfeiffer(ser, port, raw)
                    return

                # ── CDG/HPG binary probe (active command verification) ──
                ser.reset_input_buffer()
                ser.write(_CDG_PRESSURE_READ)
                stream = ser.read(96)
                if self._looks_like_cdg(stream):
                    # Query type/range register to improve full-scale identification.
                    ser.reset_input_buffer()
                    ser.write(self._cdg_type_request())
                    type_stream = ser.read(96)
                    self._identify_cdg(port, stream, type_stream)
                    return

        except serial.SerialException as exc:
            logger.debug("Scanner: %s — %s", port, exc)

    # ------------------------------------------------------------------
    # CDG identification (continuous output — no probe required)
    # ------------------------------------------------------------------

    @staticmethod
    def _looks_like_cdg(stream: bytes) -> bool:
        """Return True if *stream* contains a valid 9-byte CDG frame."""
        from serial_comm.protocols.cdg_serial import CDGProtocol, RESPONSE_LENGTH
        if len(stream) < RESPONSE_LENGTH:
            return False
        for i in range(len(stream) - RESPONSE_LENGTH + 1):
            frame = stream[i : i + RESPONSE_LENGTH]
            if CDGProtocol.is_cdg_frame(frame):
                return True
        return False

    @staticmethod
    def _cdg_type_request() -> bytes:
        # service=0x00, addr=0x3B, data=0x00, checksum=sum([0x00,0x3B,0x00])=0x3B
        return b"\x03\x00\x3B\x00\x3B"

    @staticmethod
    def _first_cdg_frame(stream: bytes, read_echo: int | None = None) -> bytes:
        from serial_comm.protocols.cdg_serial import CDGProtocol, RESPONSE_LENGTH
        for i in range(len(stream) - RESPONSE_LENGTH + 1):
            candidate = stream[i : i + RESPONSE_LENGTH]
            if not CDGProtocol.is_cdg_frame(candidate):
                continue
            if read_echo is not None and candidate[6] != read_echo:
                continue
            return candidate
        return b""

    @staticmethod
    def _infer_cdg_full_scale_mbar(sensor_code: int, type_word: int | None) -> float | None:
        # Canonical CDG full-scale options across mbar-native and Torr-native heads.
        options = (0.1, 0.13332, 0.25, 0.3333, 1.0, 1.3332, 2.0, 2.6664,
                   10.0, 13.332, 20.0, 26.664, 100.0, 133.32, 200.0, 266.64,
                   500.0, 666.6, 1000.0, 1100.0, 1333.22)
        if type_word is None:
            return None

        candidates: list[float] = []
        base = float(type_word)
        for scale in (1.0, 0.1, 0.01, 0.001, 10.0):
            candidates.append(base * scale)
        for scale in (1.33322, 0.133322, 0.0133322):
            candidates.append(base * scale)

        best_val: float | None = None
        best_rel_err = 1.0
        for cand in candidates:
            if cand <= 0:
                continue
            nearest = min(options, key=lambda opt: abs(opt - cand))
            rel_err = abs(nearest - cand) / max(nearest, 1e-12)
            if rel_err < best_rel_err:
                best_rel_err = rel_err
                best_val = float(nearest)

        # Require a reasonably close match; otherwise avoid forcing a wrong scale.
        if best_val is not None and best_rel_err <= 0.05:
            return best_val

        logger.debug(
            "Scanner: could not confidently infer CDG full scale (sensor_code=0x%02X, type_word=%s)",
            sensor_code,
            type_word,
        )
        return None

    def _identify_cdg(self, port: str, stream: bytes, type_stream: bytes | None = None) -> None:
        """Emit a port_found signal for a detected CDG gauge."""
        frame = self._first_cdg_frame(stream)

        sensor_code = frame[7] if frame else 0
        raw_ratio = 0.0
        if frame:
            meas = int.from_bytes(frame[4:6], "big", signed=True)
            raw_ratio = meas / 16384.0

        type_frame = self._first_cdg_frame(type_stream or b"", read_echo=0x3B)
        type_word = int.from_bytes(type_frame[4:6], "big", signed=False) if type_frame else None
        full_scale_mbar = self._infer_cdg_full_scale_mbar(sensor_code, type_word)

        # Sensor-code byte is not a reliable model identifier on CDG025D
        # (it encodes the factory full-scale range, not the model).
        # Default to CDG025D and only special-case HPG400.
        if sensor_code == 0x0B:
            model_hint = "INFICON HPG400"
        else:
            model_hint = "INFICON CDG025D"
        parts = [f"ratio {raw_ratio:+.3f}", f"code 0x{sensor_code:02X}"]
        if full_scale_mbar is not None:
            parts.append(f"FS≈{full_scale_mbar:g} mbar")
        if type_word is not None:
            parts.append(f"type=0x{type_word:04X}")
        desc = "  |  ".join(parts)
        metadata = {
            "family": "cdg_serial",
            "sensor_code": sensor_code,
            "type_word": type_word,
            "full_scale_mbar": full_scale_mbar,
            "model": model_hint.replace("INFICON ", ""),
        }
        self.port_found.emit(port, desc, model_hint, metadata)
        logger.info(
            "Scanner: %s — detected SKY-binary gauge (code 0x%02X, model=%s, fs=%s)",
            port,
            sensor_code,
            metadata["model"],
            full_scale_mbar,
        )

    # ------------------------------------------------------------------
    # PPG identification
    # ------------------------------------------------------------------

    def _identify_ppg(self, ser: serial.Serial, port: str, fw_raw: bytes) -> None:
        firmware = self._ppg_data(fw_raw)

        # Serial number
        ser.reset_input_buffer()
        ser.write(_PPG_SN)
        sn_raw = self._read_until(ser, b"\\", 64)
        serial_num = self._ppg_data(sn_raw) if sn_raw and self._ppg_is_ack(sn_raw) else ""

        # Try combined pressure first (PPG570-specific), fall back to Pirani (PPG550)
        # Track whether PR1 got an ACK — this distinguishes PPG570 from PPG550
        pressure = ""
        pr1_acked = False
        for probe in (_PPG_PR1, _PPG_PR3):
            ser.reset_input_buffer()
            ser.write(probe)
            pr_raw = self._read_until(ser, b"\\", 64)
            if pr_raw and self._ppg_is_ack(pr_raw):
                if probe is _PPG_PR1:
                    pr1_acked = True
                val = self._ppg_data(pr_raw)
                try:
                    float(val)
                    pressure = val
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

        model_hint = self._guess_ppg_model(firmware, serial_num, pr1_acked)
        metadata = {
            "family": "ppg_ascii",
            "firmware": firmware,
            "serial_number": serial_num,
            "pr1_acked": pr1_acked,
            "model": "PPG550/570",
        }
        self.port_found.emit(port, desc, model_hint, metadata)

    @staticmethod
    def _ppg_is_ack(raw: bytes) -> bool:
        """True for '@ACK...\\' and address-prefixed '@253ACK...\\' variants."""
        if raw.startswith(b"@ACK"):
            return True
        return (
            len(raw) >= 8
            and raw.startswith(b"@")
            and raw[1:4].isdigit()
            and raw[4:7] == b"ACK"
        )

    @staticmethod
    def _ppg_data(raw: bytes) -> str:
        """Extract data from '@ACK...\\' or '@dddACK...\\' frames."""
        if raw.startswith(b"@ACK"):
            payload = raw[4:-1]
        elif (
            len(raw) >= 8
            and raw.startswith(b"@")
            and raw[1:4].isdigit()
            and raw[4:7] == b"ACK"
        ):
            payload = raw[7:-1]
        else:
            payload = b""
        return payload.decode("ascii", errors="replace").strip()

    @staticmethod
    def _guess_ppg_model(firmware: str, serial_num: str, pr1_acked: bool = False) -> str:
        _ = (firmware, serial_num, pr1_acked)
        # PPG550/PPG570 are protocol-compatible for the common read path; use
        # a combined selector to avoid false model splits from limited probes.
        return "INFICON PPG550/570"

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

        metadata = {
            "family": "pfeiffer_ascii",
            "firmware": firmware,
            "hardware": hw_ver,
            "model": model_hint.replace("INFICON ", ""),
        }
        self.port_found.emit(port, desc, model_hint, metadata)

    # ------------------------------------------------------------------
    # Pfeiffer frame validation
    # ------------------------------------------------------------------

    @staticmethod
    def _valid_pfeiffer_frame(raw: bytes) -> bool:
        """
        Return True only for a well-formed Pfeiffer ASCII *response* frame.

        Format: addr(3) action(2) param(3) len(2) data(len) chk(3) CR
        Total minimum length = 3+2+3+2+0+3+1 = 14 bytes (data_len = 0).

        Action codes: "00" = read request (host→device), "10" = read response
        (device→host), "20"/"30" = write variants. Only responses are valid
        here — this prevents an echoed TX (action "00") from validating.
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
        # Action code: must be a device→host response, not our own request
        if text[3:5] not in ("10", "11"):
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
