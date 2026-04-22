"""
Session persistence for Serial Communicator.

.scj — JSON: connection configs + plot styling, no measurement data
.scd — JSON: connection configs + plot styling + full DeviceReading time-series

Format (version 1):
{
    "version": 1,
    "saved_at": "<ISO-8601 UTC>",
    "gauges": [
        {
            "model": "PPG550",
            "port": "COM3",
            "address": 254,
            "commands": ["pressure"],
            "poll_interval": 1.0
        },
        ...
    ],
    "data": [               // .scd only — omitted in .scj
        {
            "device_id": "COM3:254",
            "timestamp_mono": 3.141,
            "timestamp_wall": "2024-01-01T12:00:00+00:00",
            "value": 1.23e-3,
            "unit": "mbar",
            "command": "pressure"
        },
        ...
    ]
}
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from serial_comm.models import DeviceReading
from serial_comm.simulation_models import SimulatedGaugeConfig

if TYPE_CHECKING:
    pass

_SESSION_VERSION = 1


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def save_session(
    path: str | Path,
    gauge_configs: list[dict[str, Any]],
    readings: list[DeviceReading] | None = None,
    simulated_configs: list[SimulatedGaugeConfig] | None = None,
) -> None:
    """Write a session file.

    Parameters
    ----------
    path:
        Destination path.  Extension must be ``.scj`` or ``.scd``.
    gauge_configs:
        List of dicts, each describing one real gauge's connection:
        ``{"model": str, "port": str, "address": int,
           "commands": list[str], "poll_interval": float}``.
    readings:
        If provided, included in the file (use for ``.scd``).
        If *None* or empty, no ``"data"`` key is written (for ``.scj``).
    simulated_configs:
        Optional list of :class:`SimulatedGaugeConfig`.  Stored under a
        separate ``"simulated_gauges"`` section so the existing real-gauge
        schema is unaffected.
    """
    path = Path(path)
    payload: dict[str, Any] = {
        "version": _SESSION_VERSION,
        "saved_at": datetime.now(tz=timezone.utc).isoformat(),
        "gauges": [_serialise_gauge_config(g) for g in gauge_configs],
    }
    if simulated_configs:
        payload["simulated_gauges"] = [c.to_dict() for c in simulated_configs]
    if readings:
        payload["data"] = [_serialise_reading(r) for r in readings]

    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def load_session(path: str | Path) -> dict[str, Any]:
    """Load a session file and return its contents.

    Returns
    -------
    dict with keys:
        ``version`` (int),
        ``saved_at`` (datetime, UTC),
        ``gauges`` (list[dict]) — same shape as supplied to :func:`save_session`,
        ``data`` (list[DeviceReading]) — present only in ``.scd`` files.

    Raises
    ------
    SessionError
        If the file cannot be parsed or is an unsupported version.
    """
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SessionError(f"Cannot read session file: {exc}") from exc

    version = raw.get("version")
    if version != _SESSION_VERSION:
        raise SessionError(
            f"Unsupported session version {version!r} "
            f"(this build supports version {_SESSION_VERSION})"
        )

    try:
        saved_at = datetime.fromisoformat(raw["saved_at"])
    except (KeyError, ValueError) as exc:
        raise SessionError(f"Bad 'saved_at' field: {exc}") from exc

    gauges = raw.get("gauges", [])
    for g in gauges:
        _validate_gauge_config(g)

    result: dict[str, Any] = {
        "version": version,
        "saved_at": saved_at,
        "gauges": gauges,
    }

    sim_raw = raw.get("simulated_gauges", [])
    if sim_raw:
        result["simulated_gauges"] = [
            SimulatedGaugeConfig.from_dict(s) for s in sim_raw
        ]

    if "data" in raw:
        result["data"] = [_deserialise_reading(d) for d in raw["data"]]

    return result


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class SessionError(Exception):
    """Raised when a session file cannot be read or is malformed."""


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _serialise_gauge_config(cfg: dict[str, Any]) -> dict[str, Any]:
    return {
        "model": str(cfg["model"]),
        "port": str(cfg["port"]),
        "address": int(cfg["address"]),
        "commands": list(cfg["commands"]),
        "poll_interval": float(cfg["poll_interval"]),
    }


def _validate_gauge_config(g: dict) -> None:
    for key in ("model", "port", "address", "commands", "poll_interval"):
        if key not in g:
            raise SessionError(f"Gauge config missing required key: {key!r}")


def _serialise_reading(r: DeviceReading) -> dict[str, Any]:
    return {
        "device_id": r.device_id,
        "timestamp_mono": r.timestamp_mono,
        "timestamp_wall": r.timestamp_wall.isoformat(),
        "value": r.value,
        "unit": r.unit,
        "command": r.command,
    }


def _deserialise_reading(d: dict[str, Any]) -> DeviceReading:
    try:
        ts_wall = datetime.fromisoformat(d["timestamp_wall"])
    except (KeyError, ValueError) as exc:
        raise SessionError(f"Bad reading timestamp_wall: {exc}") from exc
    return DeviceReading(
        device_id=d["device_id"],
        timestamp_mono=float(d["timestamp_mono"]),
        timestamp_wall=ts_wall,
        value=float(d["value"]),
        unit=d["unit"],
        command=d["command"],
    )
