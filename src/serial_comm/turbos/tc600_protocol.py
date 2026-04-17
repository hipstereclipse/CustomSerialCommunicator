"""
TC600 Pfeiffer ASCII protocol — convenience wrapper around PfeifferAsciiProtocol.

This class loads the TC600 command table from the device spec YAML so that
the protocol stays aligned with the spec file without duplication.

Wire format: identical to pfeiffer_ascii — see pfeiffer_ascii.py for details.
"""

from __future__ import annotations

import logging
from typing import Any

from serial_comm.protocols.pfeiffer_ascii import PfeifferAsciiProtocol

logger = logging.getLogger(__name__)

# Complete TC600 parameter table sourced from Pfeiffer PM 800 547 BE.
# Keep in sync with device_specs/turbos/tc600.yaml.
_TC600_PARAMS: dict[str, dict[str, Any]] = {
    "standby":          {"pid": 2,   "data_type": "boolean_old", "read": True,  "write": True,  "unit": ""},
    "error_ack":        {"pid": 9,   "data_type": "boolean_old", "read": False, "write": True,  "unit": ""},
    "pump_on":          {"pid": 10,  "data_type": "boolean_old", "read": True,  "write": True,  "unit": ""},
    "vent_enable":      {"pid": 12,  "data_type": "boolean_old", "read": True,  "write": True,  "unit": ""},
    "motor_on":         {"pid": 23,  "data_type": "boolean_old", "read": True,  "write": True,  "unit": ""},
    "op_mode":          {"pid": 26,  "data_type": "u_short_int", "read": True,  "write": True,  "unit": ""},
    "vent_mode":        {"pid": 30,  "data_type": "u_short_int", "read": True,  "write": True,  "unit": ""},
    "error_code":       {"pid": 303, "data_type": "string",      "read": True,  "write": False, "unit": ""},
    "set_speed_hz":     {"pid": 308, "data_type": "u_integer",   "read": True,  "write": False, "unit": "Hz"},
    "actual_speed_hz":  {"pid": 309, "data_type": "u_integer",   "read": True,  "write": False, "unit": "Hz"},
    "motor_current_A":  {"pid": 310, "data_type": "u_real",      "read": True,  "write": False, "unit": "A"},
    "op_hours_TMP":     {"pid": 311, "data_type": "u_integer",   "read": True,  "write": False, "unit": "h"},
    "firmware":         {"pid": 312, "data_type": "string",      "read": True,  "write": False, "unit": ""},
    "final_speed_hz":   {"pid": 315, "data_type": "u_integer",   "read": True,  "write": False, "unit": "Hz"},
    "motor_power_W":    {"pid": 316, "data_type": "u_integer",   "read": True,  "write": False, "unit": "W"},
    "runup_time_min":   {"pid": 700, "data_type": "u_integer",   "read": True,  "write": True,  "unit": "min"},
    "speed_setpoint_pct": {"pid": 707, "data_type": "u_real",   "read": True,  "write": True,  "unit": "%"},
    "standby_speed_pct": {"pid": 717, "data_type": "u_integer", "read": True,  "write": True,  "unit": "%"},
    "vent_freq_pct":    {"pid": 720, "data_type": "u_integer",   "read": True,  "write": True,  "unit": "%"},
    "vent_time_s":      {"pid": 721, "data_type": "u_integer",   "read": True,  "write": True,  "unit": "s"},
    "rs485_address":    {"pid": 797, "data_type": "u_integer",   "read": True,  "write": True,  "unit": ""},
    # Temperature sensors (read-only, u_short_int = 3-char decimal)
    "bearing_temp_C":       {"pid": 342, "data_type": "u_short_int", "read": True,  "write": False, "unit": "°C"},
    "motor_temp_C":         {"pid": 346, "data_type": "u_short_int", "read": True,  "write": False, "unit": "°C"},
    "electronics_temp_C":   {"pid": 347, "data_type": "u_short_int", "read": True,  "write": False, "unit": "°C"},
    # Warning code (parallel to error_code, pid 302)
    "warning_code":         {"pid": 302, "data_type": "string",      "read": True,  "write": False, "unit": ""},
}

# Human-readable descriptions for error codes from the DCU parameter list
ERROR_DESCRIPTIONS: dict[str, str] = {
    "no Err": "No error",
    "Err001": "TMP excess rotation speed",
    "Err002": "Power pack unit error",
    "Err006": "Start-up time error — check run-up time, fore-vacuum pressure, leaks",
    "Err007": "Operating fluid deficiency (TC600 only)",
    "Err008": "Connection between TC and pump",
    "Err015": "Error in TC controller — power-cycle with pump at standstill",
    "Err021": "Incorrect pump identification resistance",
    "Err025": "Error in temperature monitoring TC",
    "Err026": "Error of temperature sensor inside TC",
    "Err037": "Error in motor stages or control",
    "Err040": "Hardware error: external RAM defective",
    "Err042": "Hardware error: EPROM checksum",
    "Err043": "Hardware error: E2PROM erratum",
    "Err090": "Insufficient RAM",
    "Err144": "Heating type changed",
    "Err698": "TC does not respond — check DCU↔TC connection",
    "Err913": "Error during self-test or start-up",
    "Wrn011": "TMS heating start-up time elapsed",
    "Wrn022": "TMS limit temperature (TMP > 100°C)",
    "Wrn033": "TMS heating circuit temperature sensor fault",
    "Wrn007": "Mains power failure",
    "Wrn039": "Protective conductor warning — DANGER: disconnect immediately",
    "Wrn110": "Pressure gauge defective",
    "Wrn777": "Pump nominal speed not set — set P777 (PumpRotMax)",
}


class TC600Protocol(PfeifferAsciiProtocol):
    """
    Pfeiffer TC600 Electronic Drive Unit protocol.

    Subclasses PfeifferAsciiProtocol with the TC600 parameter table pre-loaded.
    Add convenience methods for common TC600 operations.
    """

    def __init__(self, address: int = 1) -> None:
        super().__init__(address=address, param_table=_TC600_PARAMS)

    def describe_error(self, code: str) -> str:
        """Return a human-readable description for an error code string."""
        code = code.strip()
        return ERROR_DESCRIPTIONS.get(code, f"Unknown code: {code}")

    @property
    def all_commands(self) -> list[str]:
        return list(_TC600_PARAMS)

    @property
    def readable_commands(self) -> list[str]:
        return [k for k, v in _TC600_PARAMS.items() if v.get("read")]

    @property
    def writable_commands(self) -> list[str]:
        return [k for k, v in _TC600_PARAMS.items() if v.get("write")]
