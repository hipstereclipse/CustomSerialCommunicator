"""
GaugeTab — one tab in the main window's QTabWidget, per connected gauge.

Contains two sub-tabs:
  "Live View"  — pyqtgraph multi-plot panel + readings table
  "Terminal"   — interactive serial terminal with format selector
"""

from __future__ import annotations

import logging
import time
from collections import deque

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import Qt, pyqtSlot
from PyQt6.QtWidgets import (
    QComboBox, QHBoxLayout, QLabel, QSizePolicy,
    QSplitter, QTabWidget, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from serial_comm.models import DeviceError, DeviceReading, DeviceSpec
from GUI.gauge_workspace.terminal_widget import TerminalWidget

logger = logging.getLogger(__name__)

_TRACE_COLOURS = [
    "#4C9BE8",  # blue
    "#E8954C",  # orange
    "#4CE87A",  # green
    "#E84C6F",  # red
    "#9B4CE8",  # purple
    "#E8D74C",  # yellow
]
_PRESSURE_UNITS = {"mbar", "Torr", "torr", "Pa", "hPa", "psi"}
_PLOT_HISTORY_S = 120.0
_MAX_POINTS = 2000


# ---------------------------------------------------------------------------
# PlotPanel
# ---------------------------------------------------------------------------

class PlotPanel(QWidget):
    """
    Multi-plot panel backed by a pyqtgraph GraphicsLayoutWidget.

    Layout modes
    ------------
    overlay  — all commands share one plot (different coloured traces)
    stacked  — one plot per command, arranged vertically
    grid     — one plot per command, two-column grid
    """

    def __init__(self, spec: DeviceSpec, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._spec = spec
        self._layout_mode = "overlay"

        self._time_bufs: dict[str, deque] = {}
        self._val_bufs: dict[str, deque] = {}
        self._t0: float | None = None
        self._commands: list[str] = []  # ordered as first seen

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(2)

        # Layout selector
        ctrl = QHBoxLayout()
        ctrl.addWidget(QLabel("Plot layout:"))
        self._layout_combo = QComboBox()
        self._layout_combo.addItems(["Overlay", "Stacked", "Grid"])
        self._layout_combo.setFixedWidth(100)
        self._layout_combo.currentTextChanged.connect(self._on_layout_changed)
        ctrl.addWidget(self._layout_combo)
        ctrl.addStretch()
        root.addLayout(ctrl)

        self._glw = pg.GraphicsLayoutWidget(background="#1E1E1E")
        self._glw.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        root.addWidget(self._glw)

        self._plots: dict[str, pg.PlotItem] = {}   # command → PlotItem
        self._curves: dict[str, pg.PlotDataItem] = {}  # command → curve

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def feed(self, command: str, t_mono: float, value: float) -> None:
        if self._t0 is None:
            self._t0 = t_mono
        t_rel = t_mono - self._t0

        if command not in self._time_bufs:
            self._time_bufs[command] = deque(maxlen=_MAX_POINTS)
            self._val_bufs[command] = deque(maxlen=_MAX_POINTS)
            self._commands.append(command)
            self._rebuild_plots()

        tb = self._time_bufs[command]
        vb = self._val_bufs[command]
        tb.append(t_rel)
        vb.append(value)

        cutoff = t_rel - _PLOT_HISTORY_S
        while tb and tb[0] < cutoff:
            tb.popleft()
            vb.popleft()

        curve = self._curves.get(command)
        if curve is None:
            return

        t_arr = np.array(tb, dtype=float)
        v_arr = np.array(vb, dtype=float)
        if self._is_pressure(command):
            v_arr = np.where(v_arr > 0, v_arr, 1e-12)
        curve.setData(t_arr, v_arr)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _on_layout_changed(self, mode: str) -> None:
        self._layout_mode = mode.lower()
        self._rebuild_plots()
        self._replay()

    def _rebuild_plots(self) -> None:
        self._glw.clear()
        self._plots.clear()
        self._curves.clear()

        cmds = self._commands
        mode = self._layout_mode

        if not cmds:
            pi = self._glw.addPlot(row=0, col=0)
            self._setup_plot_item(pi, "")
            return

        if mode == "overlay":
            pi = self._glw.addPlot(row=0, col=0)
            self._setup_plot_item(pi, cmds[0])
            legend = pi.addLegend()
            legend.setOffset((10, 10))
            for i, cmd in enumerate(cmds):
                colour = _TRACE_COLOURS[i % len(_TRACE_COLOURS)]
                curve = pi.plot(pen=pg.mkPen(colour, width=2), name=cmd)
                self._plots[cmd] = pi
                self._curves[cmd] = curve

        elif mode == "stacked":
            for i, cmd in enumerate(cmds):
                pi = self._glw.addPlot(row=i, col=0)
                self._setup_plot_item(pi, cmd)
                colour = _TRACE_COLOURS[i % len(_TRACE_COLOURS)]
                curve = pi.plot(pen=pg.mkPen(colour, width=2), name=cmd)
                self._plots[cmd] = pi
                self._curves[cmd] = curve
                if i < len(cmds) - 1:
                    pi.getAxis("bottom").setStyle(showValues=False)

        else:  # grid
            for i, cmd in enumerate(cmds):
                pi = self._glw.addPlot(row=i // 2, col=i % 2)
                self._setup_plot_item(pi, cmd)
                colour = _TRACE_COLOURS[i % len(_TRACE_COLOURS)]
                curve = pi.plot(pen=pg.mkPen(colour, width=2), name=cmd)
                self._plots[cmd] = pi
                self._curves[cmd] = curve

    def _setup_plot_item(self, pi: pg.PlotItem, command: str) -> None:
        pi.showGrid(x=True, y=True, alpha=0.3)
        pi.setLabel("bottom", "Time", units="s")

        cmd_spec = self._spec.commands.get(command)
        unit = cmd_spec.unit if cmd_spec else ""

        if self._is_pressure(command):
            pi.setLogMode(x=False, y=True)
            pi.setLabel("left", "Pressure", units=unit or "mbar")
        else:
            label = command.replace("_", " ").title() if command else "Value"
            pi.setLabel("left", label, units=unit)

    def _replay(self) -> None:
        for cmd in self._commands:
            curve = self._curves.get(cmd)
            if curve is None:
                continue
            tb = self._time_bufs.get(cmd)
            vb = self._val_bufs.get(cmd)
            if not tb or not vb:
                continue
            t_arr = np.array(tb, dtype=float)
            v_arr = np.array(vb, dtype=float)
            if self._is_pressure(cmd):
                v_arr = np.where(v_arr > 0, v_arr, 1e-12)
            curve.setData(t_arr, v_arr)

    def _is_pressure(self, command: str) -> bool:
        cmd_spec = self._spec.commands.get(command)
        if cmd_spec and cmd_spec.unit in _PRESSURE_UNITS:
            return True
        return False


# ---------------------------------------------------------------------------
# GaugeTab
# ---------------------------------------------------------------------------

class GaugeTab(QWidget):
    """One gauge's live view + terminal tab."""

    def __init__(
        self,
        device_id: str,
        spec: DeviceSpec,
        worker,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.device_id = device_id
        self._spec = spec
        self.worker = worker

        self._all_readings: list[DeviceReading] = []

        self._build_ui()

        # Wire terminal signal
        worker.terminal_response.connect(self._terminal.on_terminal_response)

    # ------------------------------------------------------------------
    # Build UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(4)

        # Header
        header = QHBoxLayout()
        self._title_label = QLabel(
            f"<b>{self._spec.model}</b>&nbsp;&nbsp;<span style='color:#888'>{self.device_id}</span>"
        )
        self._title_label.setTextFormat(Qt.TextFormat.RichText)
        header.addWidget(self._title_label)

        self._status_label = QLabel("Connecting…")
        self._status_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        header.addWidget(self._status_label)
        root.addLayout(header)

        # Main tab widget
        self._tabs = QTabWidget()
        root.addWidget(self._tabs)

        # ── Live View tab ──
        live = QWidget()
        live_layout = QVBoxLayout(live)
        live_layout.setContentsMargins(0, 4, 0, 0)

        splitter = QSplitter(Qt.Orientation.Vertical)

        self._plot_panel = PlotPanel(spec=self._spec)
        splitter.addWidget(self._plot_panel)

        # Readings table
        self._table = QTableWidget(0, 4)
        self._table.setHorizontalHeaderLabels(["Command", "Value", "Unit", "Time"])
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.setMaximumHeight(160)
        splitter.addWidget(self._table)

        splitter.setSizes([400, 130])
        live_layout.addWidget(splitter)
        self._tabs.addTab(live, "Live View")

        # ── Terminal tab ──
        self._terminal = TerminalWidget(spec=self._spec, worker=self.worker)
        self._tabs.addTab(self._terminal, "Terminal")

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

    @pyqtSlot(object)
    def on_reading(self, reading: DeviceReading) -> None:
        self._all_readings.append(reading)
        if reading.value is not None:
            self._plot_panel.feed(reading.command, reading.timestamp_mono, reading.value)
        self._update_table(reading)
        self._status_label.setText(
            f"<span style='color:#4CE87A'>●</span>&nbsp;"
            f"{reading.timestamp_wall.strftime('%H:%M:%S')}"
        )
        self._status_label.setTextFormat(Qt.TextFormat.RichText)

    @pyqtSlot(object)
    def on_error(self, error: DeviceError) -> None:
        level_col = "#E8D74C" if error.recoverable else "#E84C4C"
        level = "WARN" if error.recoverable else "ERROR"
        self._status_label.setText(
            f"<span style='color:{level_col}'>●</span>&nbsp;[{level}] {error.message}"
        )
        self._status_label.setTextFormat(Qt.TextFormat.RichText)

    # ------------------------------------------------------------------
    # Table
    # ------------------------------------------------------------------

    def _update_table(self, reading: DeviceReading) -> None:
        for row in range(self._table.rowCount()):
            if (
                self._table.item(row, 0)
                and self._table.item(row, 0).text() == reading.command
            ):
                self._set_table_row(row, reading)
                return
        row = self._table.rowCount()
        self._table.insertRow(row)
        self._set_table_row(row, reading)

    def _set_table_row(self, row: int, reading: DeviceReading) -> None:
        val_str = f"{reading.value:.4g}" if reading.value is not None else "—"
        ts_str = reading.timestamp_wall.strftime("%H:%M:%S")
        for col, text in enumerate(
            [reading.command, val_str, reading.unit, ts_str]
        ):
            item = QTableWidgetItem(text)
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._table.setItem(row, col, item)

    # ------------------------------------------------------------------
    # Data access
    # ------------------------------------------------------------------

    def get_readings(self) -> list[DeviceReading]:
        return list(self._all_readings)

    def get_session_config(self) -> dict:
        w = self.worker
        return {
            "model": self._spec.model,
            "port": w._transport_cfg.port,
            "address": w._protocol.address,
            "commands": list(w._commands),
            "poll_interval": w._poll_interval,
            "baud_override": w._transport_cfg.baud,
            "rs485_enabled": w._transport_cfg.rs485 is not None,
        }
