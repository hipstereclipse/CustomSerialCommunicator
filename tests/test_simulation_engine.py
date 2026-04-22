"""
Tests for :class:`serial_comm.simulation_engine.SimulationEngine`.

Exercises the three pattern maths paths called out by the spec:
* PUMPDOWN: pressure at t=0, t=τ, t=5·τ
* LEAK: linear rate accumulation
* CUSTOM: linear + exponential interpolation across steps
"""

from __future__ import annotations

import math

import pytest

from serial_comm.simulation_engine import (
    P_ATM_MBAR,
    SimulationEngine,
    _interpolate,
)
from serial_comm.simulation_models import (
    RecipeStep,
    SimulationPattern,
)


# ── PUMPDOWN ─────────────────────────────────────────────────────────────────

def test_pumpdown_starts_at_atmosphere() -> None:
    eng = SimulationEngine()
    eng.set_pattern(
        SimulationPattern.PUMPDOWN,
        base_pressure_mbar=1e-6,
    )
    # At t=0 the curve must start at atmospheric pressure.
    assert math.isclose(eng.current_real_pressure(), P_ATM_MBAR, rel_tol=1e-3)


def test_pumpdown_at_key_times() -> None:
    """Two-stage decay: starts at atmosphere and approaches base monotonically."""
    eng = SimulationEngine()
    base = 1e-6
    eng.set_pattern(SimulationPattern.PUMPDOWN, base_pressure_mbar=base)
    with eng._lock:
        p_at_0 = eng._pumpdown_locked(0.0)
        p_1m = eng._pumpdown_locked(60.0)
        p_10m = eng._pumpdown_locked(600.0)
    # P(0) == P_atm exactly.
    assert p_at_0 == pytest.approx(P_ATM_MBAR, rel=1e-12)
    # Curve decreases monotonically and remains bounded above base.
    assert base < p_10m < p_1m < P_ATM_MBAR


def test_pumpdown_stays_at_base_after_completion() -> None:
    """Far into the future, pressure asymptotically clamps to base."""
    eng = SimulationEngine()
    base = 1e-3
    eng.set_pattern(SimulationPattern.PUMPDOWN, base_pressure_mbar=base)
    with eng._lock:
        p = eng._pumpdown_locked(24 * 3600.0)  # far in the future
    assert p == pytest.approx(base, rel=1e-12)


# ── LEAK ─────────────────────────────────────────────────────────────────────

def test_leak_accumulates_linearly() -> None:
    eng = SimulationEngine()
    eng.set_pattern(
        SimulationPattern.LEAK,
        base_pressure_mbar=1e-6,
        leak_rate_mbar_l_s=0.1,
    )
    eng.set_volume(10.0)  # 10 L default
    with eng._lock:
        p0 = eng._leak_locked(0.0)
        p10 = eng._leak_locked(10.0)
        p100 = eng._leak_locked(100.0)
    assert math.isclose(p0, 1e-6, rel_tol=1e-12)
    # 0.1 mbar·L/s / 10 L * 10 s = 0.1 mbar accumulated on top of base.
    assert math.isclose(p10, 1e-6 + 0.1, rel_tol=1e-9)
    assert math.isclose(p100, 1e-6 + 1.0, rel_tol=1e-9)


def test_leak_zero_rate_holds_base() -> None:
    eng = SimulationEngine()
    eng.set_pattern(
        SimulationPattern.LEAK,
        base_pressure_mbar=5e-4,
        leak_rate_mbar_l_s=0.0,
    )
    assert eng.current_real_pressure() == pytest.approx(5e-4, rel=1e-12)


# ── ARGON_ENVIRONMENT ────────────────────────────────────────────────────────

def test_argon_environment_is_flat() -> None:
    eng = SimulationEngine()
    eng.set_pattern(
        SimulationPattern.ARGON_ENVIRONMENT,
        base_pressure_mbar=3e-4,
    )
    p1 = eng.current_real_pressure()
    p2 = eng.current_real_pressure()
    assert p1 == p2 == pytest.approx(3e-4, rel=1e-12)


# ── CUSTOM ───────────────────────────────────────────────────────────────────

def test_custom_linear_interpolation() -> None:
    eng = SimulationEngine()
    eng.set_pattern(
        SimulationPattern.CUSTOM,
        recipe_steps=[
            RecipeStep("Ramp", 10.0, 1.0, 11.0, "linear"),
        ],
    )
    with eng._lock:
        p_mid = eng._custom_locked(5.0)
    assert math.isclose(p_mid, 6.0, rel_tol=1e-9)  # halfway 1 → 11


def test_custom_exponential_interpolation() -> None:
    eng = SimulationEngine()
    eng.set_pattern(
        SimulationPattern.CUSTOM,
        recipe_steps=[
            RecipeStep("Decay", 4.0, 100.0, 1.0, "exponential"),
        ],
    )
    with eng._lock:
        p_mid = eng._custom_locked(2.0)
    # Log-space midpoint of [100, 1] is sqrt(100 * 1) = 10.
    assert math.isclose(p_mid, 10.0, rel_tol=1e-9)


def test_custom_flat_segment() -> None:
    eng = SimulationEngine()
    eng.set_pattern(
        SimulationPattern.CUSTOM,
        recipe_steps=[RecipeStep("Hold", 5.0, 7.0, 99.0, "flat")],
    )
    with eng._lock:
        assert eng._custom_locked(0.0) == 7.0
        assert eng._custom_locked(2.5) == 7.0


def test_custom_multi_step_traversal() -> None:
    eng = SimulationEngine()
    eng.set_pattern(
        SimulationPattern.CUSTOM,
        recipe_steps=[
            RecipeStep("A", 10.0, 0.0, 10.0, "linear"),
            RecipeStep("B", 10.0, 10.0, 20.0, "linear"),
        ],
    )
    with eng._lock:
        assert math.isclose(eng._custom_locked(5.0), 5.0, rel_tol=1e-9)   # middle of A
        assert math.isclose(eng._custom_locked(15.0), 15.0, rel_tol=1e-9)  # middle of B


def test_interpolate_falls_back_when_endpoint_non_positive() -> None:
    """Exponential interpolation requires both endpoints > 0; otherwise linear."""
    assert _interpolate(0.0, 10.0, 0.5, "exponential") == pytest.approx(5.0)
    assert _interpolate(10.0, 0.0, 0.5, "exponential") == pytest.approx(5.0)


# ── Clock / pause / resume ───────────────────────────────────────────────────

def test_restart_resets_virtual_clock() -> None:
    eng = SimulationEngine()
    eng.set_pattern(SimulationPattern.PUMPDOWN, base_pressure_mbar=1e-6)
    eng.restart()
    # Immediately after restart elapsed should be essentially zero and the
    # pressure should equal atmospheric.
    snap = eng.snapshot()
    assert snap["elapsed_s"] < 0.05
    assert math.isclose(snap["pressure_mbar"], P_ATM_MBAR, rel_tol=1e-3)


def test_pause_resume_idempotency() -> None:
    eng = SimulationEngine()
    assert not eng.is_paused()
    eng.pause()
    assert eng.is_paused()
    eng.pause()  # second pause is a no-op
    assert eng.is_paused()
    eng.resume()
    assert not eng.is_paused()
    eng.resume()  # second resume is a no-op
    assert not eng.is_paused()


# ── Registration / cap ───────────────────────────────────────────────────────

def test_registration_roundtrip() -> None:
    from serial_comm.simulation_models import SimulatedGaugeConfig

    eng = SimulationEngine()
    cfg = SimulatedGaugeConfig(
        model="PPG550",
        display_name="SIM – PPG550",
    )
    eng.register(cfg)
    assert eng.count() == 1
    assert cfg in eng.registered()
    eng.unregister(cfg.sim_id)
    assert eng.count() == 0
