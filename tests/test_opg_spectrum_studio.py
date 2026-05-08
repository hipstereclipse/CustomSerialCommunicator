from __future__ import annotations

import math
from collections import deque

import pytest

from GUI.gauge_workspace.gauge_tab import OPG550SpectrumStudio
from serial_comm.device_registry import DeviceRegistry


class _FakeWorker:
    _protocol = None


def test_advanced_plot_series_arrays_preserves_all_collected_points() -> None:
    time_values = deque(float(index) for index in range(25))
    data_values = deque(float(index * 10) for index in range(25))

    time_array, data_array = OPG550SpectrumStudio._plot_series_arrays(
        time_values,
        data_values,
    )

    assert time_array.tolist() == [float(index) for index in range(25)]
    assert data_array.tolist() == [float(index * 10) for index in range(25)]


def test_peer_pressure_history_is_not_sample_capped(qtbot) -> None:
    spec = DeviceRegistry().get_spec("OPG550")
    studio = OPG550SpectrumStudio(spec, _FakeWorker())
    qtbot.addWidget(studio)

    for index in range(50):
        studio._store_peer_pressure("ppg", "PPG", float(index), 1.0e-6, "mbar")

    peer = studio._peer_pressures["ppg"]
    assert peer["t"].maxlen is None
    assert peer["p"].maxlen is None
    assert len(peer["t"]) == 50


def test_pressure_delta_percent_uses_compare_b_as_reference() -> None:
    assert OPG550SpectrumStudio._pressure_delta_percent(
        5.0e-6,
        4.0e-6,
    ) == pytest.approx(25.0)
    assert OPG550SpectrumStudio._pressure_delta_percent(
        3.0e-6,
        4.0e-6,
    ) == pytest.approx(-25.0)


def test_pressure_delta_percent_returns_none_for_invalid_reference() -> None:
    assert OPG550SpectrumStudio._pressure_delta_percent(5.0e-6, 0.0) is None
    assert OPG550SpectrumStudio._pressure_delta_percent(math.nan, 4.0e-6) is None


def test_auto_plasma_threshold_controls_follow_display_unit(qtbot) -> None:
    spec = DeviceRegistry().get_spec("OPG550")
    studio = OPG550SpectrumStudio(spec, _FakeWorker())
    qtbot.addWidget(studio)

    persisted: list[tuple[str, object]] = []
    studio._persist_setting = lambda key, value: persisted.append((key, value))  # type: ignore[method-assign]
    studio._display_unit = "mbar"
    studio._auto_plasma_threshold_unit = "mbar"
    studio._auto_plasma_min_mbar = 1.33322387415
    studio._auto_plasma_max_mbar = 13.3322387415
    studio._refresh_auto_plasma_threshold_controls()

    studio.set_display_unit("Torr")

    assert studio._auto_plasma_min_spin.suffix() == " Torr"
    assert studio._auto_plasma_min_spin.value() == pytest.approx(1.0)

    studio._auto_plasma_min_spin.setValue(2.0)

    assert studio._auto_plasma_min_mbar == pytest.approx(2.6664477483)
    assert persisted[-1][0] == "opg/min_ignition_pressure_mbar"
    assert persisted[-1][1] == pytest.approx(2.6664477483)


def test_advanced_refresh_requests_are_coalesced(qtbot) -> None:
    spec = DeviceRegistry().get_spec("OPG550")
    studio = OPG550SpectrumStudio(spec, _FakeWorker())
    qtbot.addWidget(studio)

    refresh_calls: list[bool] = []

    def record_refresh() -> None:
        refresh_calls.append(True)

    studio._refresh_advanced_plot = record_refresh  # type: ignore[method-assign]

    studio._schedule_advanced_refresh()
    studio._schedule_advanced_refresh()
    studio._schedule_advanced_refresh()

    assert refresh_calls == []
    qtbot.waitUntil(lambda: len(refresh_calls) == 1, timeout=1000)


def test_advanced_delta_label_includes_pressure_and_percent_delta(qtbot) -> None:
    spec = DeviceRegistry().get_spec("OPG550")
    studio = OPG550SpectrumStudio(spec, _FakeWorker())
    qtbot.addWidget(studio)

    studio._store_peer_pressure("a", "Gauge A", 0.0, 5.0e-6, "mbar")
    studio._store_peer_pressure("b", "Gauge B", 0.0, 4.0e-6, "mbar")
    studio._compare_a_combo.setCurrentIndex(studio._compare_a_combo.findData("a"))
    studio._compare_b_combo.setCurrentIndex(studio._compare_b_combo.findData("b"))

    studio._refresh_advanced_plot()

    label = studio._delta_label.text()
    assert "\u0394 = +1.0000E-06 mbar" in label
    assert "\u0394% = +25.00%" in label
