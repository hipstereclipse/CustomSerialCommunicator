"""
cdg_protocol.py
Implements RS232C protocol logic for CDG (Capacitance Diaphragm Gauge) devices.

We keep local "special" commands (reset, factory_reset, zero_adjust)
while also merging user-defined commands from GAUGE_PARAMETERS (including data_tx_mode).
If the gauge rejects a command with err=0x80, that indicates the device does not accept it
in its current firmware state, not that the code is wrong.
"""

from typing import Dict, Any, Optional, List

# The base gauge protocol
from serial_communication.gauges.protocols.gauge_protocol import GaugeProtocol
# Standard data models
from serial_communication.models import GaugeCommand, GaugeResponse
# Pull gauge params from config
from serial_communication.config import GAUGE_PARAMETERS

class CDGProtocol(GaugeProtocol):
    """
    For CDG025D, CDG045D, etc.:
    - 9-byte frames from gauge (Byte0=7, Byte3=error, Byte8=checksum)
    - 5-byte receipt strings to gauge (Byte0=3, Byte1=service cmd, Byte2=address, Byte3=data, Byte4=checksum)
    We keep local 'special' commands & unify them with config-based commands like 'data_tx_mode'.
    """

    # Mapping of type codes if we do detection
    CDG_TYPES = {
        0: "CDG025D",
        1: "CDG045D",
        2: "CDG100D",
        3: "CDG160D",
        4: "CDG200D"
    }

    def __init__(self, gauge_type: str = "CDG045D", address: int = 254, logger: Optional[object] = None):
        self.gauge_type = gauge_type  # must be set before super().__init__ calls _initialize_commands
        super().__init__(address, logger)
        self.device_id = 0x00
        self._response_validation_enabled = True
        self._detected_type = None
        self._last_command = None
        self.logger.debug("Initialized CDGProtocol with local + config commands")

    def _initialize_commands(self):
        """
        Merges local 'special' commands with config-based commands from GAUGE_PARAMETERS,
        so none produce "Unknown command."
        """
        # Load config-based commands
        gauge_params = GAUGE_PARAMETERS.get(self.gauge_type, {})
        config_cmds = gauge_params.get("commands", {})

        # Local special commands
        special_cmds = {
            "reset": {
                "cmd": "special_reset",
                "desc": "Perform a gauge power reset"
            },
            "factory_reset": {
                "cmd": "special_factory_reset",
                "desc": "Restore factory defaults"
            },
            "zero_adjust": {
                "cmd": "special_zero",
                "desc": "Perform zero offset adjust"
            }
        }

        merged = dict(config_cmds)
        for key, val in special_cmds.items():
            if key not in merged:
                merged[key] = val

        self._command_defs = merged

    def test_commands(self) -> List[bytes]:
        """
        Returns some frames to test connectivity (e.g., reading address=59).
        Byte0=3, Byte1=0 => read, Byte2=address, Byte3=0, Byte4= checksum
        """
        frames = []

        # read address=59 => might detect gauge type
        detect_cmd = bytearray([0x03, 0x00, 59, 0x00])
        detect_cmd.append(sum(detect_cmd[1:4]) & 0xFF)
        frames.append(bytes(detect_cmd))

        # read address=0 => page0 or something
        page0_cmd = bytearray([0x03, 0x00, 0x00, 0x00])
        page0_cmd.append(sum(page0_cmd[1:4]) & 0xFF)
        frames.append(bytes(page0_cmd))

        return frames

    def detect_gauge_type(self, response: bytes) -> Optional[str]:
        """
        If gauge_type=CDGxxxD, interpret byte6 to see if it's 0 => CDG025D, 1 => CDG045D, etc.
        """
        if len(response) == 9 and response[0] == 0x07 and self._last_command == "cdg_type":
            code = response[6]
            return self.CDG_TYPES.get(code)
        return None

    def create_command(self, command: GaugeCommand) -> bytes:
        """
        Builds the 5-byte "receipt string":
         Byte0=3
         Byte1=service command (read=0x00, write=0x10, special=0x40, etc.)
         Byte2=address
         Byte3=data
         Byte4=checksum
        """
        cmd_info = self._command_defs.get(command.name)
        if not cmd_info:
            raise ValueError(f"Unknown command: {command.name}")

        self._last_command = command.name

        msg = bytearray([0x03, 0x00, 0x00, 0x00])  # base

        ctype = cmd_info["cmd"]
        if ctype == "special_reset":
            msg[1] = 0x40
            msg[2] = 0
            msg[3] = 0
        elif ctype == "special_factory_reset":
            msg[1] = 0x40
            msg[2] = 1
            msg[3] = 0
        elif ctype == "special_zero":
            msg[1] = 0x40
            msg[2] = 2
            msg[3] = 0
        else:
            # Normal read/write
            if command.command_type == "!":
                msg[1] = 0x10
            else:
                msg[1] = 0x00

            addr = cmd_info.get("address", 0)
            msg[2] = addr & 0xFF

            if command.command_type == "!":
                val = 0
                if command.parameters and "value" in command.parameters:
                    val = int(command.parameters["value"])
                msg[3] = val & 0xFF
            else:
                msg[3] = 0

        # sum bytes [1..3]
        checksum = sum(msg[1:4]) & 0xFF
        msg.append(checksum)

        return bytes(msg)

    def parse_response(self, response: bytes) -> GaugeResponse:
        """
        Interprets the 9-byte send string from the gauge:
         Byte0=7, Byte1=PageNo, Byte2=Status, Byte3=Error, Byte4..5=Measurement, Byte6=read cmd, Byte7=sensor type, Byte8=checksum
        If Byte3 != 0 => we have an error code.
        If last_command is None => it might be from continuous reading (we label it "continuous_output").
        """
        try:
            if not response:
                return self._error_resp("No response received")
            if len(response) != 9:
                return self._error_resp("Invalid response length")
            if response[0] != 0x07:
                return self._error_resp("Invalid start byte (should be 0x07)")

            calc_sum = sum(response[1:8]) & 0xFF
            if calc_sum != response[8] and self._response_validation_enabled:
                return self._error_resp("Checksum mismatch")

            err_byte = response[3]
            # If err_byte is nonzero => gauge signals error
            if err_byte != 0:
                # If self._last_command is None => we label it "continuous_output"
                cmd_name = self._last_command if self._last_command else "continuous_output"
                return self._error_resp(f"{cmd_name} error: err_byte=0x{err_byte:02X}")

            # Attempt auto-detection if last_command == "cdg_type"
            if self._last_command == "cdg_type":
                code = response[6]
                dt = self.CDG_TYPES.get(code)
                if dt:
                    self._detected_type = dt
                    self.logger.info(f"Detected gauge type: {dt}")

            # If user commanded data_tx_mode, reset, factory_reset, or zero_adjust => success
            if self._last_command in ["data_tx_mode", "reset", "factory_reset", "zero_adjust"]:
                return GaugeResponse(
                    raw_data=response,
                    formatted_data=f"{self._last_command} executed successfully",
                    success=True
                )

            # Otherwise just show hex
            hex_str = " ".join(f"{b:02X}" for b in response)
            return GaugeResponse(
                raw_data=response,
                formatted_data=f"Response: {hex_str}",
                success=True
            )
        except Exception as e:
            return self._error_resp(str(e))

    def _error_resp(self, msg: str) -> GaugeResponse:
        self.logger.error(msg)
        return GaugeResponse(
            raw_data=b"",
            formatted_data=f"Error: {msg}",
            success=False,
            error_message=msg
        )
