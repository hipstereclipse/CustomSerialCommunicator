"""
protocol_factory.py
Provides a get_protocol() factory function so other code can instantiate
the correct protocol class for a given gauge type.
"""

# Imports protocol classes from the gauges folder
from serial_communication.gauges.protocols.cdg_protocol import CDGProtocol
from serial_communication.gauges.protocols.magmpg_protocol import MAGMPGProtocol
from serial_communication.gauges.protocols.pcg_protocol import PCGProtocol
from serial_communication.gauges.protocols.ppg_protocol import PPGProtocol
from serial_communication.gauges.protocols.bcg450_protocol import BCG450Protocol
from serial_communication.gauges.protocols.bcg552_protocol import BCG552Protocol
from serial_communication.gauges.protocols.bpg40x_protocol import BPG40xProtocol
from serial_communication.gauges.protocols.bpg552_protocol import BPG552Protocol
from serial_communication.turbos.protocols.tc600_protocol import TC600Protocol


def get_protocol(gauge_type: str, params: dict):
    """
    Factory function that returns an instance of the appropriate protocol class,
    based on the gauge_type string (e.g., 'PPG550', 'PCG550', etc.).
    This helps the rest of the system avoid referencing protocol classes directly,
    which keeps the architecture more flexible and modular.
    """
    # MEMS Pirani (ASCII-based)
    if gauge_type in ["PPG550", "PPG570"]:
        return PPGProtocol(
            address=params.get("address", 254),
            gauge_type=gauge_type
        )

    # Pirani/Capacitive combination
    elif gauge_type in ["PCG550", "PSG550"]:
        return PCGProtocol(device_id=params.get("device_id", 0x02))

    # Cold cathode
    elif gauge_type == "MAG500":
        return MAGMPGProtocol(device_id=params.get("device_id", 0x14))
    elif gauge_type == "MPG500":
        return MAGMPGProtocol(device_id=params.get("device_id", 0x04))

    # Capacitive
    elif gauge_type in ["CDG045D", "CDG025D"]:
        return CDGProtocol(gauge_type=gauge_type)

    # Hot cathode
    elif gauge_type == "BPG40x":
        return BPG40xProtocol()
    elif gauge_type == "BPG552":
        return BPG552Protocol()

    # Combination
    elif gauge_type == "BCG450":
        return BCG450Protocol()
    elif gauge_type == "BCG552":
        return BCG552Protocol()

    # Turbos
    elif gauge_type == "TC600":
        return TC600Protocol()

    # If none match, raise an error
    else:
        raise ValueError(f"Unsupported gauge type: {gauge_type}")
