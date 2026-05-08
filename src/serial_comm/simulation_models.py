"""
Data models for the gauge-simulation subsystem.

Simulated gauges share a single process-wide ``SimulationEngine`` which owns
one "real" pressure that advances along the selected ``SimulationPattern``.
Each simulated-gauge worker reads that shared pressure and models its own
response (Pirani gas correction, CDG direct read, cold-cathode
underrange/saturation, etc.).  These are the pure data carriers used by the
engine, workers, dialogs, and session serialiser — no Qt dependencies here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal
from typing import NamedTuple
from uuid import uuid4


# ── Patterns & gas types ─────────────────────────────────────────────────────

class SimulationPattern(str, Enum):
    """The shape of pressure-vs-time the shared engine produces."""
    PUMPDOWN = "PUMPDOWN"
    LEAK = "LEAK"
    ARGON_ENVIRONMENT = "ARGON_ENVIRONMENT"
    CUSTOM = "CUSTOM"


class GasType(str, Enum):
    """Selectable ambient-gas species used by thermal/Pirani correction."""
    N2 = "N2"       # baseline (1.0)
    AR = "AR"       # argon     (~1.6)
    HE = "HE"       # helium    (~0.8)
    CO2 = "CO2"     # carbon dioxide (~0.89)


class HumidityLevel(str, Enum):
    """Ambient humidity level that slows water-vapour-loaded pumpdowns."""
    LOW = "LOW"       # dry/controlled environment — minimal outgassing
    MEDIUM = "MEDIUM" # typical lab — moderate outgassing load
    HIGH = "HIGH"     # humid / exposed chamber — heavy water-vapour load


#: Pumpdown time multiplier per humidity level.  Higher humidity means more
#: adsorbed water vapour on chamber walls, which out-gasses during pumping.
HUMIDITY_TIME_FACTOR: dict[HumidityLevel, float] = {
    HumidityLevel.LOW: 1.0,
    HumidityLevel.MEDIUM: 1.15,
    HumidityLevel.HIGH: 1.35,
}


#: Pirani correction factor relative to N₂ baseline.  A raw Pirani reads
#: ``true_pressure * factor`` when the ambient gas is not N₂.  Values are
#: approximate values widely cited for hot-wire Pirani gauges.
PIRANI_GAS_FACTOR: dict[GasType, float] = {
    GasType.N2: 1.0,
    GasType.AR: 1.6,
    GasType.HE: 0.8,
    GasType.CO2: 0.89,
}


# ── Gauge-family classification ──────────────────────────────────────────────

class GaugeFamily(str, Enum):
    """How a simulated gauge turns "real" pressure into an emitted reading."""
    PIRANI = "PIRANI"
    CDG = "CDG"
    COLD_CATHODE = "COLD_CATHODE"
    COMBINATION = "COMBINATION"   # Pirani + cold cathode (BCG, PCG)
    VGC = "VGC"                   # controller — passes through attached sensor


@dataclass(frozen=True)
class GaugeSimulationSpec:
    """Per-model simulation metadata used by :class:`SimulatedGaugeWorker`."""

    min_mbar: float
    max_mbar: float
    repeatability_rel: float
    accuracy_rel: float
    response_tau_s: float
    warmup_s: float = 0.0
    # Combination-gauge blend transition: Pirani above high, ion below low.
    blend_low_mbar: float | None = None
    blend_high_mbar: float | None = None


# Explicit per-model mapping drives response modelling in the worker.  Keep
# this as the single source of truth for "what kind of sensor is this".
_FAMILY_BY_MODEL: dict[str, GaugeFamily] = {
    # Pirani / thermal
    "PPG550": GaugeFamily.PIRANI,
    "PPG570": GaugeFamily.PIRANI,
    "PSG500": GaugeFamily.PIRANI,
    "PSG550": GaugeFamily.PIRANI,
    "MPG400": GaugeFamily.PIRANI,
    "MPG500": GaugeFamily.PIRANI,
    "PEG100": GaugeFamily.PIRANI,
    # Capacitance diaphragm
    "CDG025D": GaugeFamily.CDG,
    "CDG045D": GaugeFamily.CDG,
    # Cold cathode / ionisation only
    "MAG500": GaugeFamily.COLD_CATHODE,
    "BPG402": GaugeFamily.COLD_CATHODE,
    "BPG552": GaugeFamily.COLD_CATHODE,
    # Combination (Pirani + CC)
    "OPG550": GaugeFamily.COMBINATION,
    "BCG450": GaugeFamily.COMBINATION,
    "BCG552": GaugeFamily.COMBINATION,
    "PCG550": GaugeFamily.COMBINATION,
    # Controllers — pass-through
    "VGC083": GaugeFamily.VGC,
    "VGC094": GaugeFamily.VGC,
    "VGC40X": GaugeFamily.VGC,
    "VGC50X": GaugeFamily.VGC,
}


def classify_family(model: str) -> GaugeFamily:
    """Return the :class:`GaugeFamily` used to model ``model``'s response.

    Unknown models fall back to :attr:`GaugeFamily.PIRANI` which is a safe,
    non-destructive default (direct read with gas correction).
    """
    return _FAMILY_BY_MODEL.get(model.upper(), GaugeFamily.PIRANI)


# ── Recipe steps & config ────────────────────────────────────────────────────

Interpolation = Literal["linear", "exponential", "flat"]


@dataclass
class RecipeStep:
    """One segment of a CUSTOM pattern's pressure-vs-time recipe."""

    name: str
    duration_s: float
    start_pressure_mbar: float
    end_pressure_mbar: float
    interpolation: Interpolation = "linear"


def _default_recipe() -> list[RecipeStep]:
    return [
        RecipeStep("Roughing Pumpdown", 90.0, 1013.0, 2e-1, "exponential"),
        RecipeStep("High-Vac Pumpdown", 180.0, 2e-1, 8e-6, "exponential"),
        RecipeStep("Process Hold", 120.0, 8e-6, 1.2e-5, "linear"),
        RecipeStep("Gas Burst / Load", 12.0, 1.2e-5, 6e-4, "linear"),
        RecipeStep("Recovery", 90.0, 6e-4, 1e-5, "exponential"),
        RecipeStep("Slow Leak Drift", 300.0, 1e-5, 2.5e-5, "linear"),
    ]


def _new_sim_id() -> str:
    return f"sim:{uuid4().hex[:8]}"


@dataclass
class SimulatedGaugeConfig:
    """User-supplied configuration for one simulated gauge.

    ``sim_id`` is auto-generated and immutable for the lifetime of the config.
    ``recipe_steps`` is only consulted when the engine's current pattern is
    :attr:`SimulationPattern.CUSTOM`.
    """

    model: str
    display_name: str
    pattern: SimulationPattern = SimulationPattern.PUMPDOWN
    recipe_steps: list[RecipeStep] = field(default_factory=_default_recipe)
    base_pressure_mbar: float = 1e-6
    leak_rate_mbar_l_s: float = 0.0
    poll_interval_s: float = 0.1
    cdg_full_scale_mbar: float | None = None
    humidity_level: HumidityLevel = HumidityLevel.MEDIUM
    gas_type: GasType = GasType.N2
    sim_id: str = field(default_factory=_new_sim_id)

    def to_dict(self) -> dict:
        """JSON-friendly representation used by the session serialiser."""
        return {
            "sim_id": self.sim_id,
            "model": self.model,
            "display_name": self.display_name,
            "pattern": self.pattern.value,
            "recipe_steps": [
                {
                    "name": s.name,
                    "duration_s": s.duration_s,
                    "start_pressure_mbar": s.start_pressure_mbar,
                    "end_pressure_mbar": s.end_pressure_mbar,
                    "interpolation": s.interpolation,
                }
                for s in self.recipe_steps
            ],
            "base_pressure_mbar": self.base_pressure_mbar,
            "leak_rate_mbar_l_s": self.leak_rate_mbar_l_s,
            "poll_interval_s": self.poll_interval_s,
            "cdg_full_scale_mbar": self.cdg_full_scale_mbar,
            "humidity_level": self.humidity_level.value,
            "gas_type": self.gas_type.value,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SimulatedGaugeConfig":
        """Inverse of :meth:`to_dict`."""
        steps = [
            RecipeStep(
                name=s["name"],
                duration_s=float(s["duration_s"]),
                start_pressure_mbar=float(s["start_pressure_mbar"]),
                end_pressure_mbar=float(s["end_pressure_mbar"]),
                interpolation=s.get("interpolation", "linear"),
            )
            for s in data.get("recipe_steps", [])
        ]
        return cls(
            sim_id=data.get("sim_id") or _new_sim_id(),
            model=str(data["model"]),
            display_name=str(data["display_name"]),
            pattern=SimulationPattern(data.get("pattern", "PUMPDOWN")),
            recipe_steps=steps or _default_recipe(),
            base_pressure_mbar=float(data.get("base_pressure_mbar", 1e-6)),
            leak_rate_mbar_l_s=float(data.get("leak_rate_mbar_l_s", 0.0)),
            poll_interval_s=float(data.get("poll_interval_s", 0.1)),
            cdg_full_scale_mbar=(
                float(data["cdg_full_scale_mbar"])
                if data.get("cdg_full_scale_mbar") is not None
                else None
            ),
            humidity_level=HumidityLevel(data.get("humidity_level", HumidityLevel.MEDIUM.value)),
            gas_type=GasType(data.get("gas_type", GasType.N2.value)),
        )


_MODEL_SIM_SPECS: dict[str, GaugeSimulationSpec] = {
    # Pirani / thermal
    "PPG550": GaugeSimulationSpec(5e-4, 1.2e3, 0.003, 0.03, 0.8),
    "PPG570": GaugeSimulationSpec(5e-4, 1.2e3, 0.0025, 0.025, 0.7),
    "PSG500": GaugeSimulationSpec(5e-4, 1.1e3, 0.0035, 0.035, 0.9),
    "PSG550": GaugeSimulationSpec(5e-4, 1.2e3, 0.003, 0.03, 0.85),
    "PEG100": GaugeSimulationSpec(8e-4, 1.1e3, 0.006, 0.05, 1.2),
    # Combination sensors modeled as Pirani + ion/cold-cathode blend
    "BCG450": GaugeSimulationSpec(5e-10, 1.1e3, 0.008, 0.12, 0.9, warmup_s=2.0, blend_low_mbar=6e-4, blend_high_mbar=2e-3),
    "BCG552": GaugeSimulationSpec(5e-10, 1.1e3, 0.008, 0.1, 0.85, warmup_s=2.0, blend_low_mbar=5e-4, blend_high_mbar=1.5e-3),
    "BPG402": GaugeSimulationSpec(1e-10, 1e-2, 0.01, 0.2, 1.6, warmup_s=2.5),
    "BPG552": GaugeSimulationSpec(1e-10, 1e-2, 0.01, 0.2, 1.6, warmup_s=2.5),
    "MAG500": GaugeSimulationSpec(1e-9, 1e-2, 0.012, 0.2, 1.6, warmup_s=2.0),
    # OPG550 simulated as Pirani + cold-cathode with pressure-dependent blending.
    "OPG550": GaugeSimulationSpec(
        1e-9, 1.3e3, 0.003, 0.03, 0.8,
        warmup_s=1.5, blend_low_mbar=7e-4, blend_high_mbar=2e-3,
    ),
    # Capacitance / piezo combinations
    "MPG400": GaugeSimulationSpec(5e-4, 1.3e3, 0.003, 0.03, 0.75),
    "MPG500": GaugeSimulationSpec(5e-4, 1.3e3, 0.003, 0.03, 0.75),
    "PCG550": GaugeSimulationSpec(1e-4, 1.3e3, 0.002, 0.02, 0.6),
    # CDG family uses dynamic full-scale logic and custom accuracy model.
    "CDG025D": GaugeSimulationSpec(1e-5, 1.333e3, 0.0004, 0.0025, 0.4),
    "CDG045D": GaugeSimulationSpec(1e-5, 1.333e3, 0.0004, 0.0025, 0.4),
    # Controllers (pass-through of attached transducer)
    "VGC083": GaugeSimulationSpec(1e-10, 1.3e3, 0.01, 0.15, 0.8),
    "VGC094": GaugeSimulationSpec(1e-10, 1.3e3, 0.01, 0.15, 0.8),
    "VGC40X": GaugeSimulationSpec(1e-10, 1.3e3, 0.01, 0.15, 0.8),
    "VGC50X": GaugeSimulationSpec(1e-10, 1.3e3, 0.01, 0.15, 0.8),
}


_DEFAULT_SIM_SPEC = GaugeSimulationSpec(5e-4, 1e3, 0.005, 0.05, 0.9)


# ── Per-model CDG full-scale option tables ──────────────────────────────────
# CDG025D is INFICON's lower-range capacitance diaphragm gauge — available in
# 0.1, 1, 10, 100 Torr heads (most sensitive models go down to 0.001 Torr).
# CDG heads are calibrated at the factory to a specific full-scale pressure.
# INFICON offers heads in *both* Torr-native and mbar-native calibrations as
# distinct SKUs — e.g. a "10 mbar" head and a "10 Torr (≈13.33 mbar)" head
# are separate, non-equivalent products with different transfer functions.
#
# Each entry is a CDGFullScaleOption(label, mbar) where:
#   label — human-readable string for UI combo boxes
#   mbar  — full-scale in mbar for all internal maths
#
# Sources: INFICON ordering documentation, confirmed 2026-04.


class CDGFullScaleOption(NamedTuple):
    """A single CDG full-scale head option."""
    label: str    # display string for UI combo boxes
    mbar: float   # full-scale in mbar for all internal calculations


def _torr(torr: float) -> CDGFullScaleOption:
    """Torr-native head — label shows both units."""
    mbar_val = torr * 1.33322
    if mbar_val >= 10:
        mbar_str = f"{mbar_val:.1f}"
    elif mbar_val >= 1:
        mbar_str = f"{mbar_val:.2f}"
    else:
        mbar_str = f"{mbar_val:.4g}"
    return CDGFullScaleOption(f"{torr:g} Torr ({mbar_str} mbar)", mbar_val)


def _mbar(mbar: float) -> CDGFullScaleOption:
    """mbar-native head."""
    return CDGFullScaleOption(f"{mbar:g} mbar", float(mbar))


# CDG025D — INFICON's lower-range CDG.
# Torr heads (base variant):  0.1, 1, 10, 100, 1000 Torr
# mbar heads:                 0.1, 1, 10, 100, 1100 mbar
# (RS232 variant also offers 0.25, 20, 200 Torr.)
CDG025D_FULL_SCALE_OPTIONS: tuple[CDGFullScaleOption, ...] = (
    _mbar(0.1),
    _torr(0.1),
    _mbar(1),
    _torr(1),
    _mbar(10),
    _torr(10),
    _mbar(100),
    _torr(100),
    _mbar(1100),
    _torr(1000),
)

# CDG045D — INFICON's wider-range CDG.
# Torr heads:  0.1, 0.25, 1, 2, 5, 10, 20, 50, 100, 200, 500, 1000 Torr
# mbar heads:  0.1, 0.25, 1, 2, 5, 10, 20, 50, 100, 200, 1100 mbar
CDG045D_FULL_SCALE_OPTIONS: tuple[CDGFullScaleOption, ...] = (
    _mbar(0.1),
    _torr(0.1),
    _mbar(0.25),
    _torr(0.25),
    _mbar(1),
    _torr(1),
    _mbar(2),
    _torr(2),
    _mbar(5),
    _torr(5),
    _mbar(10),
    _torr(10),
    _mbar(20),
    _torr(20),
    _mbar(50),
    _torr(50),
    _mbar(100),
    _torr(100),
    _mbar(200),
    _torr(200),
    _mbar(1100),
    _torr(500),
    _torr(1000),
)

# Fallback for unknown CDG models.
CDG_FULL_SCALE_OPTIONS: tuple[CDGFullScaleOption, ...] = (
    _mbar(0.1),
    _torr(0.1),
    _mbar(1),
    _torr(1),
    _mbar(10),
    _torr(10),
    _mbar(100),
    _torr(100),
    _torr(1000),
)

# Backwards-compatible mbar-only tuple for callers not yet updated.
CDG_FULL_SCALE_OPTIONS_MBAR: tuple[float, ...] = tuple(
    o.mbar for o in CDG_FULL_SCALE_OPTIONS
)

# Map model name → labeled option table.
_CDG_FS_OPTIONS_BY_MODEL: dict[str, tuple[CDGFullScaleOption, ...]] = {
    "CDG025D": CDG025D_FULL_SCALE_OPTIONS,
    "CDG045D": CDG045D_FULL_SCALE_OPTIONS,
}


def cdg_full_scale_options(model: str) -> tuple[CDGFullScaleOption, ...]:
    """Return the ordered full-scale options for a CDG model."""
    return _CDG_FS_OPTIONS_BY_MODEL.get(model.upper(), CDG_FULL_SCALE_OPTIONS)


def get_simulation_spec(model: str) -> GaugeSimulationSpec:
    """Return the per-model simulation metadata, falling back safely."""
    return _MODEL_SIM_SPECS.get(model.upper(), _DEFAULT_SIM_SPEC)
