"""Reusable simulation behavior scenarios used by simulation UI surfaces."""

from __future__ import annotations

from dataclasses import dataclass

from serial_comm.simulation_models import GasType, HumidityLevel, RecipeStep, SimulationPattern


@dataclass(frozen=True)
class SimulationScenario:
    key: str
    title: str
    description: str
    pattern: SimulationPattern
    base_pressure_mbar: float
    leak_rate_mbar_l_s: float
    humidity: HumidityLevel
    gas: GasType
    recipe_steps: tuple[RecipeStep, ...] = ()


SCENARIOS: tuple[SimulationScenario, ...] = (
    SimulationScenario(
        key="pumpdown_realistic",
        title="General Pumpdown (Humidity Aware)",
        description="Atmosphere to high-vac with humidity-dependent outgassing and realistic timing.",
        pattern=SimulationPattern.PUMPDOWN,
        base_pressure_mbar=8e-6,
        leak_rate_mbar_l_s=0.0,
        humidity=HumidityLevel.MEDIUM,
        gas=GasType.N2,
    ),
    SimulationScenario(
        key="semi_cleaning",
        title="Semiconductor Cleaning Cycle",
        description="Pump, O2/N2 clean pulse, hold, and post-clean recovery envelope.",
        pattern=SimulationPattern.CUSTOM,
        base_pressure_mbar=2e-5,
        leak_rate_mbar_l_s=0.0,
        humidity=HumidityLevel.MEDIUM,
        gas=GasType.N2,
        recipe_steps=(
            RecipeStep("Initial Pumpdown", 180.0, 1013.0, 2e-4, "exponential"),
            RecipeStep("Plasma Clean Backfill", 35.0, 2e-4, 2.5e-2, "linear"),
            RecipeStep("Clean Hold", 180.0, 2.5e-2, 2.2e-2, "linear"),
            RecipeStep("Gas Shutoff", 20.0, 2.2e-2, 4e-4, "linear"),
            RecipeStep("Recovery", 140.0, 4e-4, 1.5e-5, "exponential"),
        ),
    ),
    SimulationScenario(
        key="pvd_process",
        title="PVD / Sputter Process",
        description="Pumpdown, argon process pressure control, and recovery to base.",
        pattern=SimulationPattern.CUSTOM,
        base_pressure_mbar=1.2e-5,
        leak_rate_mbar_l_s=0.0,
        humidity=HumidityLevel.MEDIUM,
        gas=GasType.AR,
        recipe_steps=(
            RecipeStep("Chamber Pumpdown", 210.0, 1013.0, 8e-6, "exponential"),
            RecipeStep("Argon Backfill", 18.0, 8e-6, 4.8e-3, "linear"),
            RecipeStep("Process Stabilize", 120.0, 4.8e-3, 3.5e-3, "linear"),
            RecipeStep("Deposition Drift", 300.0, 3.5e-3, 4.1e-3, "linear"),
            RecipeStep("Purge + Pump", 120.0, 4.1e-3, 1.2e-5, "exponential"),
        ),
    ),
    SimulationScenario(
        key="rac_leak_test",
        title="RAC Leak Test",
        description="Automotive HVAC evacuation, isolation hold, and leak event response.",
        pattern=SimulationPattern.CUSTOM,
        base_pressure_mbar=2e-2,
        leak_rate_mbar_l_s=0.0015,
        humidity=HumidityLevel.HIGH,
        gas=GasType.N2,
        recipe_steps=(
            RecipeStep("Rough Evacuation", 140.0, 1013.0, 1.8, "exponential"),
            RecipeStep("Deep Pull", 160.0, 1.8, 2.2e-2, "exponential"),
            RecipeStep("Isolation Hold", 240.0, 2.2e-2, 2.8e-2, "linear"),
            RecipeStep("Leak Spike", 20.0, 2.8e-2, 8.5e-2, "linear"),
            RecipeStep("Post-test Decay", 90.0, 8.5e-2, 2.5e-2, "exponential"),
        ),
    ),
    SimulationScenario(
        key="slow_leak_watch",
        title="Long Hold with Slow Leak",
        description="Commissioning hold where small leaks and moisture drive pressure creep.",
        pattern=SimulationPattern.LEAK,
        base_pressure_mbar=8e-6,
        leak_rate_mbar_l_s=5e-4,
        humidity=HumidityLevel.HIGH,
        gas=GasType.N2,
    ),
)


SCENARIOS_BY_KEY: dict[str, SimulationScenario] = {s.key: s for s in SCENARIOS}


def scenario_by_key(key: str) -> SimulationScenario | None:
    return SCENARIOS_BY_KEY.get(key)
