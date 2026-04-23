"""Synthetic OPG optical-spectrum modelling utilities.

The OPG550 surfaces optical telemetry, so the synthetic spectrum is represented
over wavelength (nm) rather than mass/charge.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum


class SpectrumMode(str, Enum):
    AUTO = "Auto"
    AIR_LEAK = "Air Leak"
    WATER_LEAK = "Water Leak"
    HELIUM_LEAK = "Helium Leak"
    HYDROCARBON_BACKSTREAM = "Hydrocarbon Backstream"


@dataclass(frozen=True)
class MoleculeMatch:
    name: str
    score: float


# Practical pressure ceiling for meaningful optical gas-signature analysis.
# Above this pressure the broad background dominates and species-level
# interpretation is not realistic.
OPG_ANALYSIS_MAX_PRESSURE_MBAR: float = 1e-2


# Simplified optical emission/absorption signatures (nm -> relative weight).
_OPTICAL_SIGNATURES: dict[str, dict[float, float]] = {
    "N2": {391.0: 0.65, 428.0: 1.0, 662.0: 0.35},
    "O2": {577.0: 0.7, 630.0: 1.0, 762.0: 0.55},
    "Ar": {696.0: 0.7, 706.0: 1.0, 738.0: 0.8},
    "He": {447.0: 0.6, 588.0: 1.0, 668.0: 0.6},
    "H2O": {720.0: 0.8, 742.0: 1.0, 760.0: 0.9},
    "CO2": {690.0: 0.45, 720.0: 0.9, 760.0: 1.0},
    "CH4": {430.0: 0.7, 620.0: 0.55, 730.0: 1.0},
    "H2": {486.0: 0.65, 656.0: 1.0},
    "CO": {520.0: 0.5, 607.0: 1.0, 646.0: 0.45},
}

# Scenario baseline fractions for the synthetic partial-pressure model.
_SCENARIO_COMPOSITION: dict[SpectrumMode, dict[str, float]] = {
    SpectrumMode.AIR_LEAK: {
        "N2": 0.72,
        "O2": 0.20,
        "Ar": 0.03,
        "H2O": 0.04,
        "CO2": 0.01,
    },
    SpectrumMode.WATER_LEAK: {
        "H2O": 0.62,
        "N2": 0.18,
        "O2": 0.06,
        "CO2": 0.06,
        "H2": 0.08,
    },
    SpectrumMode.HELIUM_LEAK: {
        "He": 0.78,
        "N2": 0.14,
        "O2": 0.04,
        "Ar": 0.02,
        "H2O": 0.02,
    },
    SpectrumMode.HYDROCARBON_BACKSTREAM: {
        "CH4": 0.44,
        "H2O": 0.26,
        "CO": 0.14,
        "CO2": 0.08,
        "H2": 0.08,
    },
}


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _gaussian(x: float, mu: float, sigma: float) -> float:
    z = (x - mu) / max(sigma, 1e-6)
    return math.exp(-0.5 * z * z)


def _auto_composition(
    pressure_mbar: float,
    trend_mbar_per_s: float,
    elapsed_s: float,
) -> dict[str, float]:
    p = max(pressure_mbar, 1e-12)
    logp = math.log10(p)

    # Rising pressure strongly suggests ingress from ambient gases.
    rising = _clamp(math.log10(1.0 + max(trend_mbar_per_s, 0.0) * 1e5), 0.0, 1.0)

    # High pressure region is air-dominant, deep vacuum drifts to H2/H2O/CO.
    air_weight = _clamp((logp + 7.0) / 7.0, 0.0, 1.0)
    drydown = math.exp(-max(elapsed_s, 0.0) / 1400.0)

    comp = {
        "N2": 0.40 * air_weight + 0.18 * rising,
        "O2": 0.12 * air_weight + 0.05 * rising,
        "Ar": 0.018 * air_weight,
        "CO2": 0.020 + 0.028 * air_weight,
        "H2O": 0.22 * drydown + 0.05 * air_weight,
        "H2": 0.06 + 0.11 * (1.0 - air_weight),
        "CO": 0.05 + 0.06 * (1.0 - air_weight),
        "CH4": 0.03 + 0.02 * (1.0 - air_weight),
    }

    total = sum(max(v, 0.0) for v in comp.values())
    if total <= 0:
        return {"N2": 1.0}
    return {k: v / total for k, v in comp.items()}


def _scenario_composition_evolved(
    mode: SpectrumMode,
    elapsed_s: float,
    pressure_mbar: float,
    trend_mbar_per_s: float,
) -> dict[str, float]:
    """Return a time-evolving composition for a fixed scenario mode.

    For non-AUTO modes the base fraction table is modulated over time so that
    the spectrum changes realistically as the simulated event progresses.
    """
    base = dict(_SCENARIO_COMPOSITION[mode])
    t = max(elapsed_s, 0.0)
    p = max(pressure_mbar, 1e-12)
    rising = _clamp(math.log10(1.0 + max(trend_mbar_per_s, 0.0) * 1e5), 0.0, 1.0)

    if mode is SpectrumMode.AIR_LEAK:
        # As the leak progresses, outgassed H₂O grows; CO₂ builds slowly.
        # N₂/O₂ fraction dips slightly as H₂O fills the spectrum.
        h2o_growth = _clamp(t / 120.0, 0.0, 0.22)
        co2_growth = _clamp(t / 480.0, 0.0, 0.07)
        air_decay = h2o_growth * 0.55 + co2_growth * 0.35
        base["H2O"] = base.get("H2O", 0.0) + h2o_growth
        base["CO2"] = base.get("CO2", 0.0) + co2_growth
        base["N2"] = max(0.05, base.get("N2", 0.0) - air_decay * 0.65)
        base["O2"] = max(0.01, base.get("O2", 0.0) - air_decay * 0.35)
        # Rising pressure amplifies ingress signatures.
        base["N2"] = base["N2"] * (1.0 + rising * 0.25)
        base["O2"] = base["O2"] * (1.0 + rising * 0.18)

    elif mode is SpectrumMode.WATER_LEAK:
        # H₂O dominates early; thermal dissociation produces H₂ over time.
        h2_growth = _clamp(t / 240.0, 0.0, 0.15)
        co2_growth = _clamp(t / 600.0, 0.0, 0.08)
        h2o_decay = h2_growth * 0.6 + co2_growth * 0.3
        base["H2"] = base.get("H2", 0.0) + h2_growth
        base["CO2"] = base.get("CO2", 0.0) + co2_growth
        base["H2O"] = max(0.15, base.get("H2O", 0.0) - h2o_decay)

    elif mode is SpectrumMode.HELIUM_LEAK:
        # He dominates; over time residual N₂/O₂ from earlier pump-down diminish.
        n2_decay = _clamp(t / 300.0, 0.0, 0.08)
        he_growth = n2_decay * 0.7
        base["He"] = min(0.95, base.get("He", 0.0) + he_growth)
        base["N2"] = max(0.02, base.get("N2", 0.0) - n2_decay)
        base["O2"] = max(0.005, base.get("O2", 0.0) - n2_decay * 0.4)

    elif mode is SpectrumMode.HYDROCARBON_BACKSTREAM:
        # Heavier CH₄ builds initially; CO grows as decomposition proceeds.
        co_growth = _clamp(t / 360.0, 0.0, 0.12)
        h2_growth = _clamp(t / 300.0, 0.0, 0.10)
        ch4_decay = co_growth * 0.5 + h2_growth * 0.4
        base["CO"] = base.get("CO", 0.0) + co_growth
        base["H2"] = base.get("H2", 0.0) + h2_growth
        base["CH4"] = max(0.08, base.get("CH4", 0.0) - ch4_decay)

    total = sum(max(v, 0.0) for v in base.values())
    if total <= 0:
        return {"N2": 1.0}
    return {k: max(v, 0.0) / total for k, v in base.items()}


def simulate_optical_spectrum(
    pressure_mbar: float,
    trend_mbar_per_s: float,
    elapsed_s: float,
    mode: SpectrumMode,
    *,
    wavelength_min_nm: float = 380.0,
    wavelength_max_nm: float = 780.0,
    samples: int = 401,
) -> list[float]:
    """Return normalized optical intensities over wavelength.

    The returned vector has ``samples`` points spanning
    ``wavelength_min_nm..wavelength_max_nm``.
    """
    if mode is SpectrumMode.AUTO:
        composition = _auto_composition(pressure_mbar, trend_mbar_per_s, elapsed_s)
    else:
        composition = _scenario_composition_evolved(mode, elapsed_s, pressure_mbar, trend_mbar_per_s)

    # Increasing pressure trend amplifies ingress signatures.
    ingress = _clamp(math.log10(1.0 + max(trend_mbar_per_s, 0.0) * 2e5), 0.0, 1.0)
    samples = max(int(samples), 16)
    wavelengths = [
        wavelength_min_nm
        + (wavelength_max_nm - wavelength_min_nm) * (i / (samples - 1))
        for i in range(samples)
    ]
    spectrum = [0.0] * samples

    for molecule, frac in composition.items():
        signature = _OPTICAL_SIGNATURES.get(molecule, {})
        if not signature:
            continue
        dyn = frac * (1.0 + ingress * (1.2 if molecule in {"N2", "O2", "Ar", "He"} else 0.45))
        for line_nm, weight in signature.items():
            sigma_nm = 4.0 if line_nm < 550 else 5.5
            for i, wl in enumerate(wavelengths):
                spectrum[i] += dyn * weight * _gaussian(wl, line_nm, sigma_nm)

    # Small baseline for display continuity.
    for i in range(samples):
        spectrum[i] += 8e-4

    peak = max(spectrum, default=1.0)
    if peak <= 0:
        return spectrum
    return [v / peak for v in spectrum]


def identify_optical_species(
    spectrum: list[float],
    *,
    wavelength_min_nm: float = 380.0,
    wavelength_max_nm: float = 780.0,
    top_k: int = 4,
) -> list[MoleculeMatch]:
    """Score known gas signatures against a normalized optical spectrum."""
    if len(spectrum) < 12:
        return []

    scores: list[MoleculeMatch] = []
    n = len(spectrum)

    def _idx_from_nm(wavelength_nm: float) -> int:
        frac = (wavelength_nm - wavelength_min_nm) / max(wavelength_max_nm - wavelength_min_nm, 1e-9)
        return int(round(_clamp(frac, 0.0, 1.0) * (n - 1)))

    for name, signature in _OPTICAL_SIGNATURES.items():
        weight_sum = sum(signature.values())
        if weight_sum <= 0:
            continue

        acc = 0.0
        missing_penalty = 0.0
        for line_nm, expected in signature.items():
            center = _idx_from_nm(line_nm)
            lo = max(0, center - 3)
            hi = min(n - 1, center + 3)
            obs = max(spectrum[lo : hi + 1])
            acc += min(obs / max(expected, 1e-6), 1.0) * expected
            if obs < 0.04 and expected >= 0.6:
                missing_penalty += 0.16

        score = _clamp((acc / weight_sum) - missing_penalty, 0.0, 1.0)
        if score >= 0.2:
            scores.append(MoleculeMatch(name=name, score=score))

    scores.sort(key=lambda m: m.score, reverse=True)
    return scores[: max(1, top_k)]


# Backward-compatible aliases used by older call sites/tests.
def simulate_mass_spectrum(
    pressure_mbar: float,
    trend_mbar_per_s: float,
    elapsed_s: float,
    mode: SpectrumMode,
    *,
    max_mass: int = 401,
) -> list[float]:
    return simulate_optical_spectrum(
        pressure_mbar=pressure_mbar,
        trend_mbar_per_s=trend_mbar_per_s,
        elapsed_s=elapsed_s,
        mode=mode,
        samples=max(max_mass, 16),
    )


def identify_common_molecules(
    spectrum: list[float],
    *,
    top_k: int = 4,
) -> list[MoleculeMatch]:
    return identify_optical_species(spectrum, top_k=top_k)
