"""
Tests for :class:`serial_comm.simulated_worker.SimulatedGaugeWorker`.

Strategy: replace the ``SimulationEngine`` with a hand-rolled stub that
returns a deterministic "real" pressure and gas type.  We then drive the
worker's response model directly and assert:

* Pirani-family gauges apply the configured gas-correction factor.
* CDG-family gauges are gas-species independent (factor-of-1 regardless of gas).
* Cold-cathode gauges emit a saturation sentinel for pressure > 1e-2 mbar
  and an underrange sentinel for pressure < 1e-9 mbar.
* Signal emissions: ``reading_ready`` fires once per poll cycle with a
  :class:`DeviceReading` carrying the right device_id and unit.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from serial_comm.models import CommandSpec, DeviceReading, DeviceSpec
from serial_comm.protocols.pfeiffer_ascii import PfeifferAsciiProtocol
from serial_comm.simulated_worker import SimulatedGaugeWorker
from serial_comm.simulation_models import (
    GaugeFamily,
    GasType,
    SimulatedGaugeConfig,
    SimulationPattern,
    classify_family,
)


# ── Fixtures ─────────────────────────────────────────────────────────────────

def _make_spec(model: str, protocol: str = "ppg_ascii") -> DeviceSpec:
    return DeviceSpec(
        model=model,
        family="test",
        protocol=protocol,
        default_baud=9600,
        parity="N",
        data_bits=8,
        stop_bits=1,
        rs_modes=["RS232"],
        default_address=254,
        rs485_address_range=None,
        commands={
            "pressure": CommandSpec(
                name="pressure", read=True, write=False, unit="mbar",
            ),
        },
    )


def _make_engine_stub(pressure_mbar: float, gas: GasType = GasType.N2) -> MagicMock:
    """A tiny engine stub with just the methods the worker reads."""
    m = MagicMock()
    m.current_real_pressure.return_value = pressure_mbar
    m.current_gas.return_value = gas
    m.current_pattern.return_value = SimulationPattern.PUMPDOWN
    return m


def _make_config(model: str) -> SimulatedGaugeConfig:
    return SimulatedGaugeConfig(
        model=model,
        display_name=f"SIM – {model}",
        pattern=SimulationPattern.PUMPDOWN,
        poll_interval_s=0.05,
    )


# ── Pirani gas-correction factor ─────────────────────────────────────────────

@pytest.mark.parametrize(
    "gas, expected_factor",
    [
        (GasType.N2, 1.0),
        (GasType.AR, 1.6),
        (GasType.HE, 0.8),
        (GasType.CO2, 0.89),
    ],
)
def test_pirani_applies_gas_correction(gas: GasType, expected_factor: float) -> None:
    """PPG Pirani gauges multiply by the configured gas correction factor."""
    real = 1e-3
    spec = _make_spec("PPG550")
    cfg = _make_config("PPG550")
    engine = _make_engine_stub(real, gas=gas)
    worker = SimulatedGaugeWorker(spec=spec, config=cfg, engine=engine)

    # Force noise=0 by replacing the rng.  The worker already holds a
    # Random() seeded by sim_id — overwrite with a deterministic no-noise
    # sampler so the factor is the only contribution.
    worker._rng = _NoNoise()
    worker._cal_bias_rel = 0.0

    value = worker._apply_response_model(real, gas, "pressure")
    assert value == pytest.approx(real * expected_factor, rel=1e-12)


# ── CDG is gas-species independent ───────────────────────────────────────────

@pytest.mark.parametrize("gas", list(GasType))
def test_cdg_is_gas_independent(gas: GasType) -> None:
    real = 0.5
    spec = _make_spec("CDG025D", protocol="cdg_serial")
    cfg = _make_config("CDG025D")
    engine = _make_engine_stub(real, gas=gas)
    worker = SimulatedGaugeWorker(spec=spec, config=cfg, engine=engine)
    worker._rng = _NoNoise()
    worker._cal_bias_rel = 0.0

    value = worker._apply_response_model(real, gas, "pressure")
    assert value == pytest.approx(real, rel=1e-12), (
        f"CDG reading must not change with gas species; got {value} for {gas}"
    )


def test_cdg_clamps_to_full_scale() -> None:
    """CDG should never report pressure above its full scale."""
    full_scale = 10.0  # 10 mbar full scale
    spec = _make_spec("CDG025D", protocol="cdg_serial")
    cfg = SimulatedGaugeConfig(
        model="CDG025D",
        display_name="SIM – CDG025D",
        pattern=SimulationPattern.PUMPDOWN,
        poll_interval_s=0.05,
        cdg_full_scale_mbar=full_scale,
    )
    
    # Test pressure well above full scale
    real = 100.0  # 10x full scale
    engine = _make_engine_stub(real, gas=GasType.N2)
    worker = SimulatedGaugeWorker(spec=spec, config=cfg, engine=engine)
    worker._rng = _NoNoise()
    worker._cal_bias_rel = 0.0
    
    value = worker._apply_response_model(real, GasType.N2, "pressure")
    assert value <= full_scale, f"CDG reading {value} exceeds full scale {full_scale}"



# ── Cold cathode saturation / underrange ─────────────────────────────────────

def test_cold_cathode_overrange_returns_saturation_sentinel() -> None:
    spec = _make_spec("MAG500")
    cfg = _make_config("MAG500")
    engine = _make_engine_stub(1.0)  # way above 1e-2 mbar
    worker = SimulatedGaugeWorker(spec=spec, config=cfg, engine=engine)
    worker._rng = _NoNoise()
    worker._cal_bias_rel = 0.0
    val = worker._apply_response_model(1.0, GasType.N2, "pressure")
    assert val > 1e8, "cold cathode must report a large sentinel when saturated"


def test_cold_cathode_underrange_returns_zero() -> None:
    spec = _make_spec("MAG500")
    cfg = _make_config("MAG500")
    engine = _make_engine_stub(1e-12)
    worker = SimulatedGaugeWorker(spec=spec, config=cfg, engine=engine)
    worker._rng = _NoNoise()
    worker._cal_bias_rel = 0.0
    val = worker._apply_response_model(1e-12, GasType.N2, "pressure")
    assert val == 0.0


# ── Combination branches per-command ─────────────────────────────────────────

def test_combination_pirani_command_uses_gas_correction() -> None:
    spec = _make_spec("BCG450")
    cfg = _make_config("BCG450")
    engine = _make_engine_stub(1e-3, gas=GasType.AR)
    worker = SimulatedGaugeWorker(spec=spec, config=cfg, engine=engine)
    worker._rng = _NoNoise()
    worker._cal_bias_rel = 0.0
    val = worker._apply_response_model(1e-3, GasType.AR, "pirani_pressure")
    assert val == pytest.approx(1e-3 * 1.6, rel=1e-12)


def test_combination_cc_command_uses_cold_cathode_model() -> None:
    spec = _make_spec("BCG450")
    cfg = _make_config("BCG450")
    engine = _make_engine_stub(1e-4, gas=GasType.AR)
    worker = SimulatedGaugeWorker(spec=spec, config=cfg, engine=engine)
    worker._rng = _NoNoise()
    # Cold-cathode at 1e-4 mbar is inside its valid range and should *not* be
    # gas-corrected (gas correction is Pirani-only).
    val = worker._apply_response_model(1e-4, GasType.AR, "cc_pressure")
    assert val == pytest.approx(1e-4, rel=1e-9)


# ── Signal emission via qtbot ────────────────────────────────────────────────

def test_reading_ready_emits_device_reading(qtbot) -> None:
    spec = _make_spec("PPG550")
    cfg = _make_config("PPG550")
    engine = _make_engine_stub(1e-3, gas=GasType.N2)
    worker = SimulatedGaugeWorker(spec=spec, config=cfg, engine=engine)

    # Start the worker and capture one reading.  We stop immediately after.
    with qtbot.waitSignal(worker.reading_ready, timeout=2000) as blocker:
        worker.start()
    reading: DeviceReading = blocker.args[0]
    worker.stop()
    worker.wait(2000)

    assert reading.device_id == cfg.sim_id
    assert reading.unit == "mbar"
    assert reading.command == "pressure"
    assert reading.value > 0.0


def test_terminal_command_produces_fake_response(qtbot) -> None:
    spec = _make_spec("PPG550")
    cfg = _make_config("PPG550")
    engine = _make_engine_stub(1e-3)
    worker = SimulatedGaugeWorker(spec=spec, config=cfg, engine=engine)

    with qtbot.waitSignal(worker.terminal_response, timeout=2000) as blocker:
        worker.start()
        worker.send_terminal_command(b"@254PR3?\\", command="pressure")
    entry = blocker.args[0]
    worker.stop()
    worker.wait(2000)

    assert entry.request == b"@254PR3?\\"
    assert entry.response, "simulated worker must synthesise a response"
    assert entry.command == "pressure"


def test_opg550_is_combination_family() -> None:
    assert classify_family("OPG550") is GaugeFamily.COMBINATION


def test_opg550_combination_tracks_pressure_across_accuracy_bands() -> None:
    spec = _make_spec("OPG550", protocol="pfeiffer_ascii")
    cfg = _make_config("OPG550")
    points = [120.0, 10.0, 5e-2, 8e-4, 7e-5]

    def _band_limit(p: float) -> float:
        if p >= 100.0:
            return 0.005
        if p >= 2.0:
            return 0.01
        if p >= 1e-4:
            return 0.05
        return 0.25

    for p in points:
        engine = _make_engine_stub(p, gas=GasType.N2)
        worker = SimulatedGaugeWorker(spec=spec, config=cfg, engine=engine)
        worker._rng = _NoNoise()
        worker._cal_bias_rel = 0.0
        value = worker._apply_response_model(p, GasType.N2, "pressure")
        rel_err = abs(value - p) / max(p, 1e-12)
        assert rel_err <= _band_limit(p), f"p={p} rel_err={rel_err}"


def test_pfeiffer_ascii_temperature_response_is_parseable(qtbot) -> None:
    spec = DeviceSpec(
        model="OPG550",
        family="inficon_ascii",
        protocol="inficon_ascii",
        default_baud=9600,
        parity="N",
        data_bits=8,
        stop_bits=1,
        rs_modes=["RS232"],
        default_address=1,
        rs485_address_range=(1, 253),
        commands={
            "pressure": CommandSpec(
                name="pressure",
                read=True,
                write=False,
                unit="mbar",
                pid=340,
                data_type="u_expo_new",
            ),
            "temperature": CommandSpec(
                name="temperature",
                read=True,
                write=False,
                unit="°C",
                pid=216,
                data_type="u_real",
            ),
        },
    )
    param_table = {
        "pressure": {"pid": 340, "data_type": "u_expo_new", "unit": "mbar", "read": True, "write": False},
        "temperature": {"pid": 216, "data_type": "u_real", "unit": "°C", "read": True, "write": False},
    }
    protocol = PfeifferAsciiProtocol(address=1, param_table=param_table)
    cfg = _make_config("OPG550")
    engine = _make_engine_stub(8e-4, gas=GasType.N2)
    worker = SimulatedGaugeWorker(spec=spec, config=cfg, engine=engine, protocol=protocol)

    with qtbot.waitSignal(worker.terminal_response, timeout=2000) as blocker:
        worker.start()
        frame = protocol.build_request("temperature")
        worker.send_terminal_command(frame, command="temperature")
    entry = blocker.args[0]
    worker.stop()
    worker.wait(2000)

    parsed = protocol.parse_response(entry.response, "temperature")
    assert parsed.success
    assert parsed.value is not None
    assert 20.0 <= float(parsed.value) <= 60.0


# ── Helpers ──────────────────────────────────────────────────────────────────

class _NoNoise:
    """Drop-in replacement for :class:`random.Random` that removes all noise."""

    def gauss(self, mu: float, sigma: float) -> float:
        return 0.0
