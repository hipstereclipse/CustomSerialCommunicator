"""Tests for serial_comm.session — save/load session files."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from serial_comm.models import DeviceReading
from serial_comm.session import SessionError, load_session, save_session


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _reading(device_id="COM3:254", command="pressure", value=1.23e-3) -> DeviceReading:
    return DeviceReading(
        device_id=device_id,
        timestamp_mono=100.0,
        timestamp_wall=datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
        value=value,
        unit="mbar",
        command=command,
    )


def _gauge_cfg(
    model="PPG550", port="COM3", address=254,
    commands=None, poll_interval=1.0
) -> dict:
    return {
        "model": model,
        "port": port,
        "address": address,
        "commands": commands or ["pressure"],
        "poll_interval": poll_interval,
    }


# ---------------------------------------------------------------------------
# .scj round-trips
# ---------------------------------------------------------------------------


def test_save_scj_no_data(tmp_path):
    path = tmp_path / "test.scj"
    cfg = _gauge_cfg()
    save_session(path, [cfg])

    raw = json.loads(path.read_text())
    assert raw["version"] == 1
    assert len(raw["gauges"]) == 1
    assert "data" not in raw


def test_load_scj_round_trip(tmp_path):
    path = tmp_path / "test.scj"
    cfg = _gauge_cfg(model="BCG450", port="COM5", address=2, commands=["pressure", "status"])
    save_session(path, [cfg])

    result = load_session(path)
    assert result["version"] == 1
    assert isinstance(result["saved_at"], datetime)
    assert len(result["gauges"]) == 1
    g = result["gauges"][0]
    assert g["model"] == "BCG450"
    assert g["port"] == "COM5"
    assert g["address"] == 2
    assert g["commands"] == ["pressure", "status"]
    assert g["poll_interval"] == 1.0
    assert "data" not in result


def test_save_multiple_gauges(tmp_path):
    path = tmp_path / "multi.scj"
    cfgs = [
        _gauge_cfg(model="PPG550", port="COM3"),
        _gauge_cfg(model="BCG450", port="COM5", address=2),
    ]
    save_session(path, cfgs)

    result = load_session(path)
    assert len(result["gauges"]) == 2
    assert result["gauges"][0]["model"] == "PPG550"
    assert result["gauges"][1]["model"] == "BCG450"


def test_save_empty_gauges(tmp_path):
    path = tmp_path / "empty.scj"
    save_session(path, [])
    result = load_session(path)
    assert result["gauges"] == []


# ---------------------------------------------------------------------------
# .scd round-trips (with data)
# ---------------------------------------------------------------------------


def test_save_scd_with_readings(tmp_path):
    path = tmp_path / "test.scd"
    cfg = _gauge_cfg()
    readings = [_reading(), _reading(value=2.5e-4)]
    save_session(path, [cfg], readings=readings)

    raw = json.loads(path.read_text())
    assert "data" in raw
    assert len(raw["data"]) == 2


def test_load_scd_readings_round_trip(tmp_path):
    path = tmp_path / "test.scd"
    cfg = _gauge_cfg()
    r = _reading(device_id="COM3:254", command="pressure", value=1.5e-3)
    save_session(path, [cfg], readings=[r])

    result = load_session(path)
    assert "data" in result
    assert len(result["data"]) == 1
    loaded = result["data"][0]
    assert isinstance(loaded, DeviceReading)
    assert loaded.device_id == "COM3:254"
    assert loaded.command == "pressure"
    assert abs(loaded.value - 1.5e-3) < 1e-15
    assert loaded.unit == "mbar"
    assert loaded.timestamp_wall == datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def test_scd_preserves_timezone(tmp_path):
    path = tmp_path / "tz.scd"
    r = _reading()
    save_session(path, [_gauge_cfg()], readings=[r])
    result = load_session(path)
    ts = result["data"][0].timestamp_wall
    assert ts.tzinfo is not None


def test_scd_empty_readings_omits_data_key(tmp_path):
    """Passing an empty readings list should produce a .scj-equivalent (no data key)."""
    path = tmp_path / "test.scd"
    save_session(path, [_gauge_cfg()], readings=[])
    raw = json.loads(path.read_text())
    assert "data" not in raw


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


def test_load_missing_file_raises(tmp_path):
    with pytest.raises(SessionError, match="Cannot read"):
        load_session(tmp_path / "nonexistent.scj")


def test_load_bad_json_raises(tmp_path):
    path = tmp_path / "bad.scj"
    path.write_text("not json at all", encoding="utf-8")
    with pytest.raises(SessionError, match="Cannot read"):
        load_session(path)


def test_load_wrong_version_raises(tmp_path):
    path = tmp_path / "v99.scj"
    path.write_text(json.dumps({"version": 99, "saved_at": "2024-01-01T00:00:00+00:00", "gauges": []}),
                    encoding="utf-8")
    with pytest.raises(SessionError, match="Unsupported session version"):
        load_session(path)


def test_load_missing_gauge_key_raises(tmp_path):
    path = tmp_path / "bad_gauge.scj"
    payload = {
        "version": 1,
        "saved_at": "2024-01-01T00:00:00+00:00",
        "gauges": [{"model": "PPG550"}],  # missing port, address, etc.
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(SessionError, match="missing required key"):
        load_session(path)


def test_load_bad_timestamp_wall_raises(tmp_path):
    path = tmp_path / "bad_reading.scd"
    payload = {
        "version": 1,
        "saved_at": "2024-01-01T00:00:00+00:00",
        "gauges": [],
        "data": [
            {
                "device_id": "COM3:254",
                "timestamp_mono": 1.0,
                "timestamp_wall": "not-a-date",
                "value": 1.0,
                "unit": "mbar",
                "command": "pressure",
            }
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(SessionError, match="Bad reading timestamp_wall"):
        load_session(path)
