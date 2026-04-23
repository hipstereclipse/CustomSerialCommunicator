"""
SimulationEngine — the shared "real pressure" clock for all simulated gauges.

This is the pure model.  Each :class:`SimulatedGaugeWorker` reads
``current_real_pressure()`` once per poll and then applies its own family-
specific response model (Pirani gas correction, CDG direct read, cold-cathode
range, …).

Thread-safety
-------------
The engine is shared between N worker threads + the GUI thread.  All public
methods are serialised by a single ``threading.Lock``.  Reading current
pressure is short and non-blocking, so this coarse lock is fine for the
expected <20 workers updating at ≤1 Hz.

Time model
----------
Elapsed time is measured in monotonic seconds since the last ``restart()`` or
``set_pattern()`` call, minus any time spent paused.  All pattern maths use
this virtual timeline — real wall-clock jumps (DST, NTP) do not disturb it.
"""

from __future__ import annotations

import math
import threading
import time
from typing import Optional

from serial_comm.simulation_models import (
    GasType,
    HumidityLevel,
    HUMIDITY_TIME_FACTOR,
    RecipeStep,
    SimulatedGaugeConfig,
    SimulationPattern,
)


# Atmospheric base pressure, in mbar.  Used as the starting pressure for
# :attr:`SimulationPattern.PUMPDOWN`.
P_ATM_MBAR: float = 1013.0

# Default chamber volume (litres) used by the leak-rate model.
_DEFAULT_VOLUME_L: float = 10.0

# Two-stage pumpdown time constants (at LOW humidity, typical 10 L chamber).
# Stage 1: roughing pump  atm → 1..10 Torr region (turbo spin-up threshold)
# Stage 2: turbo pump     crossover → deep vacuum
# Stage 3: molecular flow region where conductance limits effective speed
# and pumpdown naturally slows.
_ROUGHING_TAU_S: float = 9.0         # e-folding time, roughing stage
_TURBO_TAU_S: float = 3.8            # e-folding time, turbo viscous/transitional stage
_MOLECULAR_TAU_S: float = 18.0       # slower deep-vac tail in molecular flow
_CROSSOVER_MBAR: float = 13.0        # 10 Torr (turbo enable threshold)
_MOLECULAR_TRANSITION_MBAR: float = 3e-6

# Moisture-loaded surfaces outgas after pump start. This adds a long tail that
# is especially visible at medium/high humidity and keeps real pumpdowns from
# unrealistically reaching deep vacuum too quickly.
_OUTGASSING_TAU_S: dict[HumidityLevel, float] = {
    HumidityLevel.LOW: 180.0,
    HumidityLevel.MEDIUM: 240.0,
    HumidityLevel.HIGH: 320.0,
}
_OUTGASSING_START_FRACTION: dict[HumidityLevel, float] = {
    HumidityLevel.LOW: 2e-9,
    HumidityLevel.MEDIUM: 8e-9,
    HumidityLevel.HIGH: 3e-8,
}


class SimulationEngine:
    """Process-wide shared clock/pressure source for simulated gauges.

    Construct lazily via :func:`get_engine`; do not instantiate directly.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pattern: SimulationPattern = SimulationPattern.PUMPDOWN
        self._base_pressure_mbar: float = 1e-6
        self._leak_rate_mbar_l_s: float = 0.0
        self._volume_l: float = _DEFAULT_VOLUME_L
        self._recipe_steps: list[RecipeStep] = []
        self._loop_custom: bool = True
        self._gas: GasType = GasType.N2

        self._humidity: HumidityLevel = HumidityLevel.MEDIUM

        self._t0: float = time.monotonic()
        self._paused_at: Optional[float] = None
        self._paused_offset: float = 0.0

        self._registered: dict[str, SimulatedGaugeConfig] = {}

    # ------------------------------------------------------------------
    # Time helpers (must be called with _lock held)
    # ------------------------------------------------------------------

    def _elapsed_locked(self) -> float:
        now = time.monotonic() if self._paused_at is None else self._paused_at
        return max(0.0, now - self._t0 - self._paused_offset)

    # ------------------------------------------------------------------
    # Pattern / parameter control
    # ------------------------------------------------------------------

    def set_pattern(
        self,
        pattern: SimulationPattern,
        *,
        base_pressure_mbar: float | None = None,
        leak_rate_mbar_l_s: float | None = None,
        recipe_steps: list[RecipeStep] | None = None,
        reset_clock: bool = True,
    ) -> None:
        """Atomically switch the active pattern and (optionally) its params.

        When ``reset_clock`` is true (default) the elapsed-time counter is
        reset so the new pattern starts from t=0.  Pass False to continue the
        previous virtual timeline — useful for live pattern-switch UI where
        you want the gauges to "flow" smoothly across the change.
        """
        with self._lock:
            self._pattern = pattern
            if base_pressure_mbar is not None:
                self._base_pressure_mbar = max(float(base_pressure_mbar), 1e-12)
            if leak_rate_mbar_l_s is not None:
                self._leak_rate_mbar_l_s = float(leak_rate_mbar_l_s)
            if recipe_steps is not None:
                self._recipe_steps = list(recipe_steps)
            if reset_clock:
                self._t0 = time.monotonic()
                self._paused_at = None
                self._paused_offset = 0.0

    def set_recipe_steps(self, steps: list[RecipeStep]) -> None:
        """Replace the CUSTOM recipe.  No effect unless the current pattern is
        :attr:`SimulationPattern.CUSTOM`."""
        with self._lock:
            self._recipe_steps = list(steps)

    def set_base_pressure(self, mbar: float) -> None:
        with self._lock:
            self._base_pressure_mbar = max(float(mbar), 1e-12)

    def set_leak_rate(self, mbar_l_s: float) -> None:
        with self._lock:
            self._leak_rate_mbar_l_s = float(mbar_l_s)

    def set_gas(self, gas: GasType) -> None:
        with self._lock:
            self._gas = gas

    def set_volume(self, litres: float) -> None:
        with self._lock:
            self._volume_l = max(float(litres), 1e-6)

    def set_humidity(self, level: HumidityLevel) -> None:
        """Set the humidity level that affects pumpdown speed."""
        with self._lock:
            self._humidity = level

    def current_humidity(self) -> HumidityLevel:
        with self._lock:
            return self._humidity

    # ------------------------------------------------------------------
    # Clock control
    # ------------------------------------------------------------------

    def restart(self) -> None:
        """Reset the virtual clock to t=0 and clear any pause."""
        with self._lock:
            self._t0 = time.monotonic()
            self._paused_at = None
            self._paused_offset = 0.0

    def pause(self) -> None:
        with self._lock:
            if self._paused_at is None:
                self._paused_at = time.monotonic()

    def resume(self) -> None:
        with self._lock:
            if self._paused_at is not None:
                self._paused_offset += time.monotonic() - self._paused_at
                self._paused_at = None

    def is_paused(self) -> bool:
        with self._lock:
            return self._paused_at is not None

    # ------------------------------------------------------------------
    # Registration (for UI listing / cap enforcement)
    # ------------------------------------------------------------------

    def register(self, config: SimulatedGaugeConfig) -> None:
        with self._lock:
            self._registered[config.sim_id] = config

    def unregister(self, sim_id: str) -> None:
        with self._lock:
            self._registered.pop(sim_id, None)

    def registered(self) -> list[SimulatedGaugeConfig]:
        with self._lock:
            return list(self._registered.values())

    def count(self) -> int:
        with self._lock:
            return len(self._registered)

    # ------------------------------------------------------------------
    # State snapshot (for UI — returns a plain dict)
    # ------------------------------------------------------------------

    def snapshot(self) -> dict:
        """Point-in-time view of the engine state for the control tab."""
        with self._lock:
            elapsed = self._elapsed_locked()
            pressure = self._compute_pressure_locked(elapsed)
            step_idx, step_frac = self._current_step_locked(elapsed)
            return {
                "pattern": self._pattern,
                "gas": self._gas,
                "pressure_mbar": pressure,
                "elapsed_s": elapsed,
                "paused": self._paused_at is not None,
                "base_pressure_mbar": self._base_pressure_mbar,
                "leak_rate_mbar_l_s": self._leak_rate_mbar_l_s,
                "volume_l": self._volume_l,
                "recipe_steps": list(self._recipe_steps),
                "current_step_index": step_idx,
                "current_step_fraction": step_frac,
                "registered_count": len(self._registered),
            "humidity": self._humidity,
            }

    # ------------------------------------------------------------------
    # Core pressure computation
    # ------------------------------------------------------------------

    def current_real_pressure(self) -> float:
        """Return the shared "true" pressure in mbar for the current t."""
        with self._lock:
            return self._compute_pressure_locked(self._elapsed_locked())

    def current_gas(self) -> GasType:
        with self._lock:
            return self._gas

    def current_pattern(self) -> SimulationPattern:
        with self._lock:
            return self._pattern

    def _compute_pressure_locked(self, t: float) -> float:
        """Dispatch to the per-pattern maths.  Lock must be held."""
        pat = self._pattern
        if pat is SimulationPattern.PUMPDOWN:
            return self._pumpdown_locked(t)
        if pat is SimulationPattern.LEAK:
            return self._leak_locked(t)
        if pat is SimulationPattern.ARGON_ENVIRONMENT:
            return self._base_pressure_mbar
        if pat is SimulationPattern.CUSTOM:
            return self._custom_locked(t)
        return self._base_pressure_mbar  # defensive

    # --- individual pattern implementations -------------------------------

    def _pumpdown_locked(self, t: float) -> float:
        """Realistic staged pumpdown: roughing, turbo, then molecular-flow tail.

        Behavior targets:
        - Roughing stage handles atmosphere down to the 1..10 Torr region.
        - Turbo stage then quickly reaches high vacuum.
        - Below a deep-vac threshold, effective speed drops (molecular flow),
          so the final decades flatten naturally.
        """
        base = self._base_pressure_mbar
        if base >= P_ATM_MBAR:
            return base

        h = HUMIDITY_TIME_FACTOR.get(self._humidity, 1.0)
        tau1 = _ROUGHING_TAU_S * h
        tau_turbo = _TURBO_TAU_S * h
        tau_mol = _MOLECULAR_TAU_S * h

        # Stage 1: roughing decay
        p_rough = P_ATM_MBAR * math.exp(-t / tau1)

        # Find when roughing reaches crossover so Stage 2 can begin.
        if P_ATM_MBAR > _CROSSOVER_MBAR:
            t_cross = tau1 * math.log(P_ATM_MBAR / _CROSSOVER_MBAR)
        else:
            t_cross = 0.0

        if t <= t_cross:
            # Still in roughing stage — high-vac pump not yet effective.
            return max(p_rough, base)

        # Stage 2: turbo pump dominates down to molecular-flow threshold.
        t2 = t - t_cross
        p_turbo = _CROSSOVER_MBAR * math.exp(-t2 / tau_turbo)

        if _CROSSOVER_MBAR > _MOLECULAR_TRANSITION_MBAR:
            t_mol = tau_turbo * math.log(_CROSSOVER_MBAR / _MOLECULAR_TRANSITION_MBAR)
        else:
            t_mol = 0.0

        if t2 <= t_mol:
            p_flow_limited = p_turbo
        else:
            # Stage 3: deep-vac molecular regime slows further decay.
            t3 = t2 - t_mol
            p_flow_limited = _MOLECULAR_TRANSITION_MBAR * math.exp(-t3 / tau_mol)

        out_tau = _OUTGASSING_TAU_S.get(self._humidity, 780.0)
        out_frac = _OUTGASSING_START_FRACTION.get(self._humidity, 0.03)
        p_out = (P_ATM_MBAR * out_frac) * math.exp(-t / out_tau)

        # After crossover, roughing no longer governs chamber pressure.
        pressure = max(p_flow_limited, p_out, base)
        # Clamp to base once we’re close enough.
        if pressure <= base * 1.0001:
            return base
        return pressure

    def _leak_locked(self, t: float) -> float:
        """Linear rise: P(t) = base + leak_rate · t / volume."""
        return self._base_pressure_mbar + (
            self._leak_rate_mbar_l_s * t / self._volume_l
        )

    def _custom_locked(self, t: float) -> float:
        """Piecewise interpolation across the current recipe steps."""
        steps = self._recipe_steps
        if not steps:
            return self._base_pressure_mbar
        total = sum(s.duration_s for s in steps)
        if total <= 0:
            return steps[0].start_pressure_mbar
        t_mod = t % total if self._loop_custom else min(t, total)
        cursor = 0.0
        for step in steps:
            if t_mod <= cursor + step.duration_s or step is steps[-1]:
                frac = 0.0 if step.duration_s <= 0 else (t_mod - cursor) / step.duration_s
                frac = max(0.0, min(1.0, frac))
                return _interpolate(
                    step.start_pressure_mbar,
                    step.end_pressure_mbar,
                    frac,
                    step.interpolation,
                )
            cursor += step.duration_s
        return steps[-1].end_pressure_mbar

    def _current_step_locked(self, t: float) -> tuple[int, float]:
        """Return ``(step_index, fraction_through_step)`` for CUSTOM pattern.

        Returns ``(-1, 0.0)`` when the pattern is not CUSTOM or there are no
        recipe steps.
        """
        if self._pattern is not SimulationPattern.CUSTOM:
            return -1, 0.0
        steps = self._recipe_steps
        if not steps:
            return -1, 0.0
        total = sum(s.duration_s for s in steps)
        if total <= 0:
            return 0, 0.0
        t_mod = t % total if self._loop_custom else min(t, total)
        cursor = 0.0
        for idx, step in enumerate(steps):
            if t_mod <= cursor + step.duration_s or step is steps[-1]:
                frac = 0.0 if step.duration_s <= 0 else (t_mod - cursor) / step.duration_s
                return idx, max(0.0, min(1.0, frac))
            cursor += step.duration_s
        return len(steps) - 1, 1.0


def _interpolate(a: float, b: float, frac: float, mode: str) -> float:
    """Interpolate between two pressures according to ``mode``.

    ``exponential`` uses log-space interpolation and therefore requires both
    endpoints to be strictly positive; if either is <= 0 we transparently
    fall back to linear interpolation.
    """
    if mode == "flat":
        return a
    if mode == "exponential" and a > 0 and b > 0:
        return a * (b / a) ** frac
    return a + (b - a) * frac


# ── Module-level singleton accessor ──────────────────────────────────────────

_ENGINE: SimulationEngine | None = None
_ENGINE_LOCK = threading.Lock()


def get_engine() -> SimulationEngine:
    """Return the process-wide :class:`SimulationEngine` instance.

    The instance is created lazily on first call and is safe to import from
    both the GUI and worker threads.
    """
    global _ENGINE
    with _ENGINE_LOCK:
        if _ENGINE is None:
            _ENGINE = SimulationEngine()
        return _ENGINE


def reset_engine_for_tests() -> None:
    """Drop the cached singleton so each test starts with a clean engine.

    Do not call from production code.
    """
    global _ENGINE
    with _ENGINE_LOCK:
        _ENGINE = None
