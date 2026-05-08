from __future__ import annotations

from GUI.gauge_workspace.combined_tab import CombinedTab


def test_combined_tab_keeps_full_history_for_mismatched_poll_rates(qtbot) -> None:
    tab = CombinedTab()
    qtbot.addWidget(tab)
    tab.add_gauge("ppg", "PPG", "#4C9BE8")
    tab.add_gauge("cdg", "CDG", "#E8954C")

    for index in range(25):
        tab.feed("ppg", float(index), 2.0e-6)
    for index in range(250):
        tab.feed("cdg", float(index) / 10.0, 1.0e-6)

    ppg = tab._gauges[tab._series_id("ppg", "pressure")]
    cdg = tab._gauges[tab._series_id("cdg", "pressure")]

    assert ppg["time_buf"].maxlen is None
    assert cdg["time_buf"].maxlen is None
    assert len(ppg["time_buf"]) == 25
    assert len(cdg["time_buf"]) == 250
