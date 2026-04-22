"""
Pressure-unit helpers used by simulated gauges and the display-unit setting.

All internal arithmetic in the simulation subsystem is done in **mbar**.
:func:`convert_pressure` turns a stored mbar value into whatever the user has
selected under Settings → Display Units, and vice-versa.
"""

from __future__ import annotations


#: Multiplicative factor that converts a value *in that unit* to mbar.
#: e.g. ``1 Torr * 1.33322 = 1.33322 mbar``.
_TO_MBAR: dict[str, float] = {
    "mbar": 1.0,
    "hPa": 1.0,                 # 1 hPa == 1 mbar exactly
    "Pa": 0.01,                 # 1 Pa == 0.01 mbar
    "Torr": 1.33322387415,      # 1 Torr == 1.33322387415 mbar
    "psi": 68.9475729317831,    # 1 psi == 68.9475729 mbar
}

#: Canonical ordering for UI combos.
SUPPORTED_UNITS: tuple[str, ...] = ("mbar", "Torr", "Pa", "hPa", "psi")


def _factor(unit: str) -> float:
    try:
        return _TO_MBAR[unit]
    except KeyError as exc:
        raise ValueError(
            f"Unsupported pressure unit {unit!r}. "
            f"Supported: {', '.join(SUPPORTED_UNITS)}"
        ) from exc


def convert_pressure(value: float, from_unit: str, to_unit: str) -> float:
    """Convert ``value`` from ``from_unit`` to ``to_unit``.

    Parameters
    ----------
    value:
        Numeric pressure reading.
    from_unit, to_unit:
        Must be one of :data:`SUPPORTED_UNITS`.  Comparison is
        case-sensitive; pass the canonical form.

    Returns
    -------
    float
        ``value`` expressed in ``to_unit``.  Returns ``value`` unchanged when
        ``from_unit == to_unit`` — a useful fast-path since most readings are
        already in mbar.

    Raises
    ------
    ValueError
        If either unit is not one of :data:`SUPPORTED_UNITS`.
    """
    if from_unit == to_unit:
        return value
    mbar = value * _factor(from_unit)
    return mbar / _factor(to_unit)


def format_pressure(value: float, unit: str, sig: int = 4) -> str:
    """Format ``value`` in ``unit`` using scientific notation for
    sub-millibar readings and a compact ``%.{sig}g`` otherwise."""
    if abs(value) < 1e-2:
        return f"{value:.{sig}e} {unit}"
    return f"{value:.{sig}g} {unit}"
