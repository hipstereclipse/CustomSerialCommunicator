"""
Tests for :mod:`serial_comm.units`.

These exercise round-trip conversion for every supported pressure unit pair
— the invariant is that converting A → B → A recovers the original value to
floating-point precision for typical vacuum-pressure magnitudes.
"""

from __future__ import annotations

import math

import pytest

from serial_comm.units import (
    SUPPORTED_UNITS,
    convert_pressure,
    format_pressure,
)


@pytest.mark.parametrize("unit", SUPPORTED_UNITS)
def test_identity_is_noop(unit: str) -> None:
    assert convert_pressure(1.234e-5, unit, unit) == 1.234e-5
    assert convert_pressure(1013.0, unit, unit) == 1013.0


@pytest.mark.parametrize("a", SUPPORTED_UNITS)
@pytest.mark.parametrize("b", SUPPORTED_UNITS)
@pytest.mark.parametrize("value", [1e-6, 1e-3, 1.0, 1013.0])
def test_round_trip(a: str, b: str, value: float) -> None:
    """A → B → A must recover the original value to floating-point precision."""
    through = convert_pressure(value, a, b)
    back = convert_pressure(through, b, a)
    assert math.isclose(back, value, rel_tol=1e-12, abs_tol=0.0)


def test_mbar_to_hpa_identity() -> None:
    """mbar and hPa differ by *definition* zero — conversion must be exact."""
    assert convert_pressure(17.5, "mbar", "hPa") == 17.5
    assert convert_pressure(17.5, "hPa", "mbar") == 17.5


def test_torr_known_conversion() -> None:
    """1 Torr == 1.33322387415 mbar (SI definition)."""
    assert math.isclose(
        convert_pressure(1.0, "Torr", "mbar"),
        1.33322387415,
        rel_tol=1e-12,
    )


def test_pa_known_conversion() -> None:
    """1 mbar == 100 Pa (definition)."""
    assert math.isclose(convert_pressure(1.0, "mbar", "Pa"), 100.0, rel_tol=1e-12)


def test_unknown_unit_raises() -> None:
    with pytest.raises(ValueError, match="Unsupported pressure unit"):
        convert_pressure(1.0, "bar", "mbar")
    with pytest.raises(ValueError, match="Unsupported pressure unit"):
        convert_pressure(1.0, "mbar", "bar")


def test_format_pressure_picks_scientific_for_vacuum() -> None:
    formatted = format_pressure(1.23e-6, "mbar")
    assert "e-" in formatted.lower() and "mbar" in formatted


def test_format_pressure_compact_for_atm() -> None:
    formatted = format_pressure(1013.0, "mbar")
    assert "e" not in formatted.lower()
    assert "mbar" in formatted
