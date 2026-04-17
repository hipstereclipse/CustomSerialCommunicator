"""
GaugeTab — one tab in the main window's QTabWidget, per connected gauge.

Contains:
  - Live pyqtgraph time-series plot (multiple traces for multi-command gauges)
  - Readings table: command | value | unit | timestamp
  - Status / error label
"""

from __future__ import annotations

import logging
import time
from collections import deque
from datetime import datetime, timezone

import pyqtgraph as pg
from PyQt6.QtCore import Qt, pyqtSlot
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QTableWidget, QTableWidgetItem, QSplitter,
    QSizePolicy,
)

from serial_comm.models import DeviceReading, DeviceError, DeviceSpec

logger = logging.getLogger(__name__)

# Colours for traces — one per command polled
_TRACE_COLOURS = [
    "#4C9BE8",  # blue
    "#E8954C",  # orange
    "#4CE87A",  # green
    "#E84C6F",  # red
    "#9B4CE8",  # purple
    "#E8D74C",  # yellow
]

# How many seconds of history to show in the plot
_PLOT_HISTORY_S = 120.0
# Maximum data points kept per trace (circular buffer)
_MAX_POINTS = 2000


class GaugeTab(QWidget):
    """One gauge's live view tab."""

    def __init__(
        self,
        device_id: str,
        spec: DeviceSpec,
        worker,           # GaugeWorker — imported lazily to avoid circular
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.device_id = device_id
        self._spec = spec
        self.worker = worker

        # Per-command circular buffers: command → deque of (t_rel, value)
        self._time_buffers: dict[str, deque] = {}
        self._val_buffers: dict[str, deque] = {}
        self._all_readings: list[DeviceReading] = []
        self._t0: float | None = None  # monotonic start time

        self._build_ui()

    # ------------------------------------------------------------------
    # Build UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

        # Header
        header = QHBoxLayout()
        self._title_label = QLabel(f"<b>{self._spec.model}</b>  {self.device_id}")
        header.addWidget(self._title_label)

        self._status_label = QLabel("Connecting…")
        self._status_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        header.addWidget(self._status_label)
        layout.addLayout(header)

        # Splitter: plot on top, table below
        splitter = QSplitter(Qt.Orientation.Vertical)

        # --- Plot ---
        self._plot_widget = pg.PlotWidget(background="#1E1E1E")
        self._plot_widget.setLabel("left", "Pressure", units="mbar")
        self._plot_widget.setLabel("bottom", "Time", units="s")
        self._plot_widget.showGrid(x=True, y=True, alpha=0.3)
        self._plot_widget.setLogMode(x=False, y=True)  # log scale for pressure
        self._plot_widget.addLegend()
        self._plot_curves: dict[str, pg.PlotDataItem] = {}
        self._plot_widget.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        splitter.addWidget(self._plot_widget)

        # --- Table ---
        self._table = QTableWidget(0, 4)
        self._table.setHorizontalHeaderLabels(["Command", "Value", "Unit", "Time"])
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.setMaximumHeight(180)
        splitter.addWidget(self._table)

        splitter.setSizes([400, 150])
        layout.addWidget(splitter)

    # ------------------------------------------------------------------
    # Slots (called from GaugeWorker signals — cross-thread)
    # ------------------------------------------------------------------

    @pyqtSlot(object)
    def on_reading(self, reading: DeviceReading) -> None:
        self._all_readings.append(reading)
        self._update_plot(reading)
        self._update_table(reading)
        self._status_label.setText(
            f"OK  |  {reading.timestamp_wall.strftime('%H:%M:%S')}"
        )

    @pyqtSlot(object)
    def on_error(self, error: DeviceError) -> None:
        level = "WARN" if error.recoverable else "ERROR"
        self._status_label.setText(f"[{level}] {error.message}")
        logger.warning("[%s] %s", self.device_id, error.message)

    # ------------------------------------------------------------------
    # Plot update
    # ------------------------------------------------------------------

    def _update_plot(self, reading: DeviceReading) -> None:
        if reading.value is None:
            return

        cmd = reading.command
        t = reading.timestamp_mono

        if self._t0 is None:
            self._t0 = t
        t_rel = t - self._t0

        if cmd not in self._time_buffers:
            self._time_buffers[cmd] = deque(maxlen=_MAX_POINTS)
            self._val_buffers[cmd] = deque(maxlen=_MAX_POINTS)
            colour = _TRACE_COLOURS[len(self._plot_curves) % len(_TRACE_COLOURS)]
            curve = self._plot_widget.plot(
                pen=pg.mkPen(colour, width=2),
                name=cmd,
            )
            self._plot_curves[cmd] = curve

        self._time_buffers[cmd].append(t_rel)
        self._val_buffers[cmd].append(reading.value)

        # Trim to window
        cutoff = t_rel - _PLOT_HISTORY_S
        ts = self._time_buffers[cmd]
        vs = self._val_buffers[cmd]
        while ts and ts[0] < cutoff:
            ts.popleft()
            vs.popleft()

        import numpy as np
        t_arr = np.array(ts, dtype=float)
        v_arr = np.array(vs, dtype=float)

        # Guard against non-positive values on log scale
        v_arr = np.where(v_arr > 0, v_arr, 1e-12)
        self._plot_curves[cmd].setData(t_arr, v_arr)

    # ------------------------------------------------------------------
    # Table update
    # ------------------------------------------------------------------

    def _update_table(self, reading: DeviceReading) -> None:
        # Find existing row for this command, or add a new one
        for row in range(self._table.rowCount()):
            if self._table.item(row, 0) and self._table.item(row, 0).text() == reading.command:
                self._set_table_row(row, reading)
                return
        # New command — add row
        row = self._table.rowCount()
        self._table.insertRow(row)
        self._set_table_row(row, reading)

    def _set_table_row(self, row: int, reading: DeviceReading) -> None:
        val_str = f"{reading.value:.4g}" if reading.value is not None else "—"
        ts_str = reading.timestamp_wall.strftime("%H:%M:%S")
        for col, text in enumerate([reading.command, val_str, reading.unit, ts_str]):
            item = QTableWidgetItem(text)
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._table.setItem(row, col, item)

    # ------------------------------------------------------------------
    # Data access (for export)
    # ------------------------------------------------------------------

    def get_readings(self) -> list[DeviceReading]:
        return list(self._all_readings)

    def get_session_config(self) -> dict:
        """Return connection config dict suitable for session serialisation."""
        w = self.worker
        return {
            "model": self._spec.model,
            "port": w._transport_cfg.port,
            "address": w._protocol.address,
            "commands": list(w._commands),
            "poll_interval": w._poll_interval,
        }
