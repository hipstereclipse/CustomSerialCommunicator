"""
pcg_protocol.py
Implements protocol logic for PCG/PSG gauges (Pirani/Capacitive combination).
These devices typically use a binary protocol with a certain PID structure.
"""

import struct   # Used for packing/unpacking binary data
from typing import Dict, Any, Optional

# Imports the base class for all protocols
from serial_communication.gauges.protocols.gauge_protocol import GaugeProtocol
from serial_communication.models import GaugeCommand, GaugeResponse
from serial_communication.param_types import CommandDefinition, ParamType


class PCGProtocol(GaugeProtocol):
    """
    Manages PCG/PSG gauge commands using a binary protocol structure.
    Commonly, each command has a PID (parameter ID) and a command code (1=read, 3=write).
    """

    def __init__(self, device_id: int = 0x02, logger: Optional[object] = None):
        """
        Sets the device ID (default 0x02 for PCG/PSG).
        Also creates a logger if none is provided.
        """
        super().__init__(address=254, logger=logger)
        self.device_id = device_id

    def _initialize_commands(self):
        """
        Populates self._command_defs with basic read/write definitions that the PCG can handle.
        In practice, you might load them from a config or commands file.
        """
        self._command_defs = {
            "pressure": CommandDefinition(221, "pressure", "Read pressure measurement", read=True),
            "temperature": CommandDefinition(222, "temperature", "Read temperature", read=True),
            "zero_adjust": CommandDefinition(417, "zero_adjust", "Execute zero adjustment", write=True),
            "software_version": CommandDefinition(218, "software_version", "Read software version", read=True),
            "serial_number": CommandDefinition(207, "serial_number", "Read serial number", read=True),
            "error_status": CommandDefinition(228, "error_status", "Read error status", read=True)
        }

    def create_command(self, command: GaugeCommand) -> bytes:
        """
        Builds a command frame:
         [Addr, device_id, 0x00, length, cmd_code, pid_msb, pid_lsb, 0x00, 0x00, CRC16]
        """
        # Retrieves the command definition from our dict
        cmd_def = self._command_defs.get(command.name)
        if not cmd_def:
            raise ValueError(f"Unknown command: {command.name}")

        # The basic frame always starts with [address, device_id, ackbit(?), length, command_type, pid, pid, 0,0]
        msg = bytearray([
            0x00 if not self.rs485_mode else self.address,  # 0 for RS232, else address
            self.device_id,
            0x00,         # ACK bit or message count (often 0)
            0x05,         # length byte (to be adjusted if we add parameters)
            0x01 if cmd_def.read else 0x03,  # 0x01=read, 0x03=write
            (cmd_def.pid >> 8) & 0xFF,
            cmd_def.pid & 0xFF,
            0x00, 0x00    # placeholders
        ])

        # If user wants to write (!), and the command allows writing, we embed parameters
        if command.command_type == "!":
            if cmd_def.write and command.parameters:
                param_bytes = self._encode_param(command.parameters.get('value', 0), cmd_def.param_type)
                # Extends the message with the parameter bytes
                msg.extend(param_bytes)
                # Adjusts the length field (index=3)
                msg[3] = len(msg) - 4

        # Finally, we compute a CRC16 across everything but the final 2 bytes, then append
        crc = self.calculate_crc16(msg)
        msg.extend([crc & 0xFF, (crc >> 8) & 0xFF])

        return bytes(msg)

    def parse_response(self, response: bytes) -> GaugeResponse:
        """
        Verifies length, checks CRC, and interprets the data portion.
        The gauge typically returns a message structured similarly to the command frame.
        """
        if len(response) < 7:
            return self._error_response("Response too short")

        device_id = response[1]
        msg_length = response[3]
        pid = (response[5] << 8) | response[6]

        # Checks if device ID matches
        if device_id != self.device_id:
            return self._error_response("Invalid device ID")

        # Verifies message length vs actual length
        if len(response) != msg_length + 6:
            return self._error_response("Invalid message length")

        # Validates CRC
        received_crc = (response[-1] << 8) | response[-2]
        calculated_crc = self.calculate_crc16(response[:-2])
        if received_crc != calculated_crc:
            return self._error_response("CRC mismatch")

        # Extracts the data portion after the first 7 bytes, minus the last 2 for CRC
        data = response[7:-2]
        return self._parse_data(pid, data, response)

    def _parse_data(self, pid: int, data: bytes, raw: bytes) -> GaugeResponse:
        """
        Interprets the 'data' bytes depending on which PID was used (e.g., read pressure).
        """
        if pid == 221:
            # Fixs32en20: 32-bit signed fixed-point, fraction = 2^20
            value = int.from_bytes(data, byteorder='big', signed=True)
            pressure = 10 ** (value / (2 ** 20))
            return GaugeResponse(raw_data=raw, formatted_data=f"{pressure:.3E} mbar", success=True)

        elif pid == 222:
            if len(data) == 4:
                temp = struct.unpack('>f', data)[0]
                return GaugeResponse(raw_data=raw, formatted_data=f"{temp:.1f} °C", success=True)
            else:
                return GaugeResponse(raw_data=raw, formatted_data=data.hex(), success=True)

        elif pid == 228:
            error_flags = int.from_bytes(data, byteorder='big')
            flags = self._parse_error_flags(error_flags)
            active = [k for k, v in flags.items() if v] or ["none"]
            return GaugeResponse(raw_data=raw, formatted_data=f"Errors: {', '.join(active)}", success=True)

        return GaugeResponse(raw_data=raw, formatted_data=data.hex(), success=True)

    def _parse_error_flags(self, flags: int) -> Dict[str, bool]:
        return {
            "sensor_error": bool(flags & 0x01),
            "electronics_error": bool(flags & 0x02),
            "calibration_error": bool(flags & 0x04),
            "memory_error": bool(flags & 0x08),
        }

    def _encode_param(self, value: Any, param_type: Optional[ParamType]) -> bytes:
        """
        Converts a value to bytes, e.g., if param_type=FLOAT, we pack into 4 bytes big-endian.
        """
        if param_type == ParamType.UINT8:
            return bytes([int(value) & 0xFF])
        elif param_type == ParamType.UINT16:
            return int(value).to_bytes(2, byteorder='big')
        elif param_type == ParamType.FLOAT:
            return struct.pack('>f', float(value))
        return bytes()

    def _error_response(self, message: str) -> GaugeResponse:
        self.logger.error(message)
        return GaugeResponse(raw_data=b"", formatted_data=f"Error: {message}", success=False, error_message=message)
