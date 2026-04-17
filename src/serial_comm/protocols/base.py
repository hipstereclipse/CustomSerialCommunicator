"""Abstract base class for all gauge protocol codecs."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from serial_comm.models import GaugeReading


class GaugeProtocol(ABC):
    """
    Protocol codec interface.

    Each subclass knows how to:
    - build a request frame for a named command
    - parse a response frame into a GaugeReading

    Codecs are stateless with respect to the transport — they work only with
    bytes.  The acquisition layer owns the transport and calls the codec.
    """

    def __init__(self, address: int = 254) -> None:
        self.address = address

    @abstractmethod
    def build_request(self, command: str, value: Any = None) -> bytes:
        """Return the raw bytes to send for the given command."""

    @abstractmethod
    def parse_response(self, raw: bytes, command: str) -> GaugeReading:
        """
        Parse *raw* bytes received from the device for *command*.

        Always returns a GaugeReading; never raises.  Errors are expressed
        as GaugeReading(success=False, error=...).
        """

    def supports_continuous_output(self) -> bool:
        """Return True if the device sends unsolicited frames (e.g. CDG)."""
        return False

    def parse_continuous(self, raw: bytes) -> GaugeReading:
        """Parse an unsolicited frame.  Only called if supports_continuous_output()."""
        return GaugeReading(success=False, error="continuous output not supported")

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _ok(value: float, unit: str, formatted: str, raw: bytes = b"") -> GaugeReading:
        return GaugeReading(success=True, value=value, unit=unit, formatted=formatted, raw=raw)

    @staticmethod
    def _err(message: str, raw: bytes = b"") -> GaugeReading:
        return GaugeReading(success=False, error=message, raw=raw)
