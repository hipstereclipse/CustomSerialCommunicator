"""
GaugeTab — one tab in the main window's QTabWidget, per connected gauge.

Contains sub-tabs:
  "Live View"  — pyqtgraph multi-plot panel with interactive crosshair +
                 per-trace toggle buttons
    "Command Displays" — dedicated displays for non-pressure polled commands
  "Terminal"   — interactive serial terminal with format selector
"""

from __future__ import annotations

import csv
import logging
import math
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import Qt, QSettings, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFrame, QGridLayout,
    QGroupBox, QHBoxLayout, QHeaderView, QLabel, QPushButton, QScrollArea, QSizePolicy,
    QProgressBar, QSlider, QSplitter, QTabWidget, QTableWidget, QTableWidgetItem,
    QFileDialog, QMessageBox,
    QVBoxLayout, QWidget,
)

from serial_comm.models import DeviceError, DeviceReading, DeviceSpec, TerminalEntry
from serial_comm.opg_spectrum import (
    OPG_ANALYSIS_MAX_PRESSURE_MBAR,
    SpectrumMode,
    identify_optical_species,
    optical_signature_wavelengths,
    simulate_optical_spectrum,
)
from serial_comm.command_utils import command_display_name
from serial_comm.units import SUPPORTED_UNITS, convert_pressure
from GUI.gauge_workspace.command_display_panel import CommandDisplayPanel
from GUI.gauge_workspace.export_dialog import ExportDialog
from GUI.gauge_workspace.poll_commands_dialog import PollCommandsDialog
from GUI.gauge_workspace.terminal_widget import TerminalWidget
from GUI.settings_dialog import (
    display_signals,
    get_display_unit,
    get_opg_spectrum_verbose_diagnostics,
    get_setting,
)
from GUI.theme import current_theme, style_plot_item, themed_graphics_layout, themed_plot, value_bar_style

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
# Maximum samples retained per peer-pressure trace inside the OPG550
# Spectrum Studio panel. Streaming gauges such as the CDG can emit >100
# frames per second, so an unbounded deque previously grew large enough
# to make every advanced-plot redraw block the GUI thread.
_PEER_PRESSURE_MAXLEN = 30000


# ---------------------------------------------------------------------------
# PlotPanel
# ---------------------------------------------------------------------------

class PlotPanel(QWidget):
    """
    Multi-plot panel backed by a pyqtgraph GraphicsLayoutWidget.

    Features
    --------
    - Overlay / Stacked / Grid layout modes
    - Per-command toggle pill buttons — click to show/hide individual traces
    - Linked crosshair: hover any plot to show a vertical line across all
      charts at the same time-axis position, with Y values displayed above
    """

    def __init__(
        self,
        spec: DeviceSpec,
        parent: QWidget | None = None,
        *,
        trace_base_color: str = "#4C9BE8",
    ) -> None:
        super().__init__(parent)
        self._spec = spec
        self._layout_mode = "overlay"
        self._trace_base_color = trace_base_color
        # Display unit for pressure plots.  Values fed into feed() are
        # assumed to already be in this unit (GaugeTab converts before feed).
        self._display_unit = "mbar"

        self._time_bufs: dict[str, deque] = {}
        self._val_bufs:  dict[str, deque] = {}
        self._t0: float | None = None
        self._commands: list[str] = []

        # Crosshair state
        self._crosshair_lines: list[pg.InfiniteLine] = []
        self._proxy: pg.SignalProxy | None = None

        # Toggle-button state (persists across layout rebuilds)
        self._toggle_states: dict[str, bool] = {}
        self._cmd_toggles: dict[str, QPushButton] = {}
        self._plot_paused: bool = False

        self._build_ui()

    # ------------------------------------------------------------------
    # Build UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(2)

        # ── Trace toggle pill buttons (scrollable) ─────────────────────
        self._toggle_inner = QWidget()
        self._toggle_layout = QHBoxLayout(self._toggle_inner)
        self._toggle_layout.setContentsMargins(2, 0, 2, 0)
        self._toggle_layout.setSpacing(5)
        self._toggle_layout.addStretch()

        toggle_scroll = QScrollArea()
        toggle_scroll.setWidget(self._toggle_inner)
        toggle_scroll.setWidgetResizable(True)
        toggle_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        toggle_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        toggle_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        toggle_scroll.setFixedHeight(30)
        root.addWidget(toggle_scroll)

        # ── Crosshair value bar (hidden until mouse hovers) ────────────
        self._value_bar = QLabel()
        self._value_bar.setTextFormat(Qt.TextFormat.RichText)
        self._value_bar.setStyleSheet(value_bar_style())
        self._value_bar.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self._value_bar.setFixedHeight(18)
        self._value_bar.hide()
        root.addWidget(self._value_bar)

        # ── Plot widget ────────────────────────────────────────────────
        self._glw = pg.GraphicsLayoutWidget()
        themed_graphics_layout(self._glw)
        self._glw.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        root.addWidget(self._glw)

        # ── Control bar (layout selector) — fixed at bottom ────────────
        ctrl = QHBoxLayout()
        ctrl.setContentsMargins(4, 2, 4, 2)
        ctrl.addWidget(QLabel("Layout:"))
        self._layout_combo = QComboBox()
        self._layout_combo.addItems(["Overlay", "Stacked", "Grid"])
        self._layout_combo.setFixedWidth(90)
        self._layout_combo.currentTextChanged.connect(self._on_layout_changed)
        ctrl.addWidget(self._layout_combo)
        self._pause_plot_btn = QPushButton("Pause Plot")
        self._pause_plot_btn.setFixedHeight(24)
        self._pause_plot_btn.clicked.connect(self._on_pause_plot_clicked)
        ctrl.addWidget(self._pause_plot_btn)
        ctrl.addStretch()
        root.addLayout(ctrl)

        self._plots:  dict[str, pg.PlotItem]     = {}
        self._curves: dict[str, pg.PlotDataItem] = {}

    def apply_theme(self) -> None:
        themed_graphics_layout(self._glw)
        self._value_bar.setStyleSheet(value_bar_style())
        for plot in set(self._plots.values()):
            style_plot_item(plot)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def feed(self, command: str, t_mono: float, value: float) -> None:
        if self._t0 is None:
            self._t0 = t_mono
        t_rel = t_mono - self._t0

        if command not in self._time_bufs:
            self._time_bufs[command] = deque(maxlen=_MAX_POINTS)
            self._val_bufs[command]  = deque(maxlen=_MAX_POINTS)
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

        if self._plot_paused:
            return

        curve = self._curves.get(command)
        if curve is None or not curve.isVisible():
            return

        t_arr = np.array(tb, dtype=float)
        v_arr = np.array(vb, dtype=float)
        if self._is_pressure(command):
            v_arr = np.where(v_arr > 0, v_arr, 1e-12)
        curve.setData(t_arr, v_arr)

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def _on_layout_changed(self, mode: str) -> None:
        self._layout_mode = mode.lower()
        self._rebuild_plots()
        self._replay()

    def _on_pause_plot_clicked(self) -> None:
        self._plot_paused = not self._plot_paused
        if self._plot_paused:
            self._pause_plot_btn.setText("Resume Plot")
            return
        self._pause_plot_btn.setText("Pause Plot")
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
        elif mode == "overlay":
            pi = self._glw.addPlot(row=0, col=0)
            self._setup_plot_item(pi, cmds[0])
            legend = pi.addLegend()
            legend.setOffset((10, 10))
            for i, cmd in enumerate(cmds):
                colour = self._trace_colour_for_index(i)
                curve = pi.plot(pen=pg.mkPen(colour, width=2), name=cmd)
                self._plots[cmd]  = pi
                self._curves[cmd] = curve
        elif mode == "stacked":
            for i, cmd in enumerate(cmds):
                pi = self._glw.addPlot(row=i, col=0)
                self._setup_plot_item(pi, cmd)
                colour = self._trace_colour_for_index(i)
                curve = pi.plot(pen=pg.mkPen(colour, width=2), name=cmd)
                self._plots[cmd]  = pi
                self._curves[cmd] = curve
                if i < len(cmds) - 1:
                    pi.getAxis("bottom").setStyle(showValues=False)
        else:  # grid
            for i, cmd in enumerate(cmds):
                pi = self._glw.addPlot(row=i // 2, col=i % 2)
                self._setup_plot_item(pi, cmd)
                colour = self._trace_colour_for_index(i)
                curve = pi.plot(pen=pg.mkPen(colour, width=2), name=cmd)
                self._plots[cmd]  = pi
                self._curves[cmd] = curve

        self._setup_crosshairs()
        self._rebuild_toggles()

    def _setup_plot_item(self, pi: pg.PlotItem, command: str) -> None:
        pi.showGrid(x=True, y=True, alpha=0.3)
        pi.setLabel("bottom", "Time", units="s")

        cmd_spec = self._spec.commands.get(command)
        unit = cmd_spec.unit if cmd_spec else ""

        if self._is_pressure(command):
            pi.setLogMode(x=False, y=True)
            # Don't pass units to setLabel—pyqtgraph auto-scales with SI prefixes (G, M, etc.)
            # Instead, build our own label to avoid "GTorr" / "Gmbar" artifacts
            unit_str = self._display_unit or unit or "mbar"
            pi.setLabel("left", f"Pressure ({unit_str})", units="")
        else:
            label = command.replace("_", " ").title() if command else "Value"
            pi.setLabel("left", label, units=unit)
        style_plot_item(pi)

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

    # ------------------------------------------------------------------
    # Crosshair
    # ------------------------------------------------------------------

    def _setup_crosshairs(self) -> None:
        """Add an InfiniteLine to each unique PlotItem and wire mouse proxy."""
        self._crosshair_lines.clear()

        seen_plots: list[pg.PlotItem] = []
        for pi in self._plots.values():
            if pi not in seen_plots:
                seen_plots.append(pi)
                vline = pg.InfiniteLine(
                    angle=90, movable=False,
                    pen=pg.mkPen(color=(220, 220, 220, 160), width=1),
                )
                vline.setVisible(False)
                pi.addItem(vline, ignoreBounds=True)
                self._crosshair_lines.append(vline)

        # Disconnect old proxy before creating a new one
        if self._proxy is not None:
            try:
                self._proxy.disconnect()
            except (AttributeError, RuntimeError):
                pass
            self._proxy = None

        scene = self._glw.scene()
        if scene is not None:
            self._proxy = pg.SignalProxy(
                scene.sigMouseMoved,
                rateLimit=60,
                slot=self._on_mouse_moved,
            )

    def _on_mouse_moved(self, event: tuple) -> None:
        pos = event[0]
        x: float | None = None

        seen_plots: list[pg.PlotItem] = []
        for pi in self._plots.values():
            if pi in seen_plots:
                continue
            seen_plots.append(pi)
            if pi.vb.sceneBoundingRect().contains(pos):
                mp = pi.vb.mapSceneToView(pos)
                x = mp.x()
                break

        if x is None:
            for vline in self._crosshair_lines:
                vline.setVisible(False)
            self._value_bar.hide()
            return

        for vline in self._crosshair_lines:
            vline.setValue(x)
            vline.setVisible(True)

        self._update_value_bar(x)

    def _update_value_bar(self, x: float) -> None:
        parts: list[str] = []
        for i, cmd in enumerate(self._commands):
            toggle = self._cmd_toggles.get(cmd)
            if toggle and not toggle.isChecked():
                continue
            v = self._value_at_x(cmd, x)
            if v is None:
                continue
            cmd_spec = self._spec.commands.get(cmd)
            unit = cmd_spec.unit if cmd_spec else ""
            colour = self._trace_colour_for_index(i)
            label = cmd.replace("_", " ")
            if self._is_pressure(cmd):
                formatted = f"{v:.3E}"
            else:
                formatted = f"{v:.4g}"
            parts.append(
                f"<span style='color:{colour}'><b>{label}</b></span>: {formatted} {unit}"
            )

        if parts:
            self._value_bar.setText("  |  ".join(parts))
            self._value_bar.show()
        else:
            self._value_bar.hide()

    def _value_at_x(self, cmd: str, x: float) -> float | None:
        tb = self._time_bufs.get(cmd)
        vb = self._val_bufs.get(cmd)
        if not tb:
            return None
        ta = np.array(tb, dtype=float)
        idx = int(np.searchsorted(ta, x))
        idx = min(max(idx, 0), len(ta) - 1)
        return float(np.array(vb, dtype=float)[idx])

    # ------------------------------------------------------------------
    # Toggle buttons
    # ------------------------------------------------------------------

    def _rebuild_toggles(self) -> None:
        """Sync pill-button row with current command list, preserving states."""
        # Save existing states before clearing
        for cmd, btn in self._cmd_toggles.items():
            self._toggle_states[cmd] = btn.isChecked()

        # Clear layout
        while self._toggle_layout.count():
            item = self._toggle_layout.takeAt(0)
            if item.widget():
                item.widget().setParent(None)

        self._cmd_toggles.clear()

        for i, cmd in enumerate(self._commands):
            colour = self._trace_colour_for_index(i)
            label = cmd.replace("_", " ")
            checked = self._toggle_states.get(cmd, True)

            btn = QPushButton(f"● {label}")
            btn.setCheckable(True)
            btn.setChecked(checked)
            btn.setFixedHeight(22)
            btn.setToolTip(f"Show / hide {label} trace")
            btn.setStyleSheet(self._pill_style(colour))
            btn.toggled.connect(
                lambda vis, c=cmd: self._toggle_command(c, vis)
            )
            self._cmd_toggles[cmd] = btn
            self._toggle_layout.addWidget(btn)

        self._toggle_layout.addStretch()

        # Apply current visibility to freshly-created curves
        for cmd, btn in self._cmd_toggles.items():
            self._toggle_command(cmd, btn.isChecked())

    @staticmethod
    def _pill_style(colour: str) -> str:
        """Return a CSS stylesheet for a pill-shaped toggle button."""
        r, g, b = (
            int(colour[1:3], 16),
            int(colour[3:5], 16),
            int(colour[5:7], 16),
        )
        return f"""
            QPushButton {{
                background-color: rgba({r},{g},{b},60);
                border: 1px solid {colour};
                border-radius: 10px;
                color: white;
                padding: 0px 10px;
                font-size: 11px;
                font-weight: bold;
            }}
            QPushButton:checked {{
                background-color: rgba({r},{g},{b},90);
            }}
            QPushButton:!checked {{
                background-color: rgba(50,50,50,180);
                border-color: #555555;
                color: #777777;
            }}
            QPushButton:hover {{
                border-width: 2px;
            }}
        """

    def _toggle_command(self, cmd: str, visible: bool) -> None:
        curve = self._curves.get(cmd)
        if curve is not None:
            curve.setVisible(visible)

    def set_trace_base_color(self, color: str) -> None:
        self._trace_base_color = color
        for i, cmd in enumerate(self._commands):
            curve = self._curves.get(cmd)
            if curve is not None:
                curve.setPen(pg.mkPen(self._trace_colour_for_index(i), width=2))
        self._rebuild_toggles()
        self._value_bar.hide()

    def _trace_colour_for_index(self, idx: int) -> str:
        base = QColor(self._trace_base_color)
        if not base.isValid():
            return _TRACE_COLOURS[idx % len(_TRACE_COLOURS)]
        if idx == 0:
            return base.name()
        # Keep all command traces tied to the selected gauge color while
        # still visually separating multi-command overlays.
        factor = 115 + (idx % 4) * 18
        variant = QColor(base)
        if idx % 2 == 0:
            variant = variant.lighter(min(factor, 170))
        else:
            variant = variant.darker(min(factor, 170))
        return variant.name()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _is_pressure(self, command: str) -> bool:
        cmd_spec = self._spec.commands.get(command)
        return bool(cmd_spec and cmd_spec.unit in _PRESSURE_UNITS)

    def set_display_unit(self, unit: str) -> None:
        """Switch the displayed pressure unit for all pressure traces.

        Rescales previously-buffered pressure samples into ``unit`` so the
        user sees a continuous trace across the unit change, and refreshes
        the pressure-plot Y-axis labels.  Non-pressure traces are untouched.
        """
        if unit not in SUPPORTED_UNITS or unit == self._display_unit:
            return
        old_unit = self._display_unit
        self._display_unit = unit

        # Rescale buffered values for pressure-only commands.
        for cmd in self._commands:
            if not self._is_pressure(cmd):
                continue
            buf = self._val_bufs.get(cmd)
            if not buf:
                continue
            rescaled = [convert_pressure(v, old_unit, unit) for v in buf]
            buf.clear()
            buf.extend(rescaled)

        # Relabel pressure plots (avoid SI prefix scaling by building our own label).
        for cmd, pi in self._plots.items():
            if self._is_pressure(cmd):
                pi.setLabel("left", f"Pressure ({unit})", units="")

        self._replay()


class GaugeSettingsPanel(QWidget):
    """Per-gauge settings: polling controls, auto-query, and setpoint editor.

    The setpoint editor shows an S-shaped pressure envelope across the
    gauge's effective range, alongside draggable horizontal threshold
    lines. Mouse hover uses a horizontal guide line on the Y axis so users can
    inspect the corresponding pressure and whether SP1 / SP2 would be
    triggered at that level. Setpoints can be adjusted three ways: drag the
    line on the plot, slide the slider, or type the exact value in the spinbox;
    all three stay in sync.
    """

    _SP_COLORS = {"1": "#E84C6F", "2": "#4CE87A"}

    def __init__(self, spec: DeviceSpec, worker, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._spec = spec
        self._worker = worker
        self._rows: dict[str, dict] = {}
        self._timer = QTimer(self)
        self._timer.setInterval(200)
        self._timer.timeout.connect(self._on_auto_tick)

        # Setpoint editor state
        self._setpoint_spins: dict[str, QDoubleSpinBox] = {}
        self._setpoint_sliders: dict[str, QSlider] = {}
        self._setpoint_mbar_labels: dict[str, QLabel] = {}
        self._threshold_lines: dict[str, pg.InfiniteLine] = {}
        self._sp_regions: dict[str, pg.LinearRegionItem] = {}
        self._setpoint_status_labels: dict[str, QLabel] = {}
        self._setpoint_status_dots: dict[str, QLabel] = {}
        self._setpoint_plot: pg.PlotWidget | None = None
        self._sim_curve: pg.PlotDataItem | None = None
        self._hover_marker: pg.InfiniteLine | None = None
        self._setpoint_hover_proxy: pg.SignalProxy | None = None
        self._live_pressure_line: pg.InfiniteLine | None = None
        self._raw_max: float = 255.0
        proto = getattr(self._worker, "_protocol", None)
        self._full_scale_mbar: float = float(
            getattr(proto, "full_scale_mbar", 1.0) or 1.0
        )
        self._display_unit: str = get_display_unit()
        self._setpoint_state: dict[str, bool] = {}
        self._suppress_sync: bool = False
        self._ppg_setpoint_controls: dict[str, QWidget] = {}
        self._ppg_setpoint_rows: list[int] = []
        self._pin_setpoint_plot: pg.PlotWidget | None = None
        self._pin_setpoint_lines: dict[int, pg.InfiniteLine] = {}
        self._pin_setpoint_hyst_regions: dict[int, pg.LinearRegionItem] = {}
        self._pin_setpoint_status = QLabel("Setpoint states update from the live pressure reading.")
        self._last_pressure_mbar: float | None = None
        self._setpoint_preview_unit: str = self._display_unit

        self._build_ui()

    # ------------------------------------------------------------------
    # Build UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ── Fixed top section: polling + auto-query (never scrolls away) ─
        fixed_top = QWidget()
        self._fixed_top = fixed_top
        top_layout = QVBoxLayout(fixed_top)
        top_layout.setContentsMargins(8, 6, 8, 6)
        top_layout.setSpacing(6)

        # Polling + Auto-query controls — one compact row
        poll_row = QHBoxLayout()
        poll_row.setSpacing(6)
        poll_row.addWidget(QLabel("Polling:"))

        self._poll_toggle = QPushButton("Pause")
        self._poll_toggle.setCheckable(True)
        self._poll_toggle.setFixedHeight(26)
        self._poll_toggle.setToolTip("Pause/resume background polling for this gauge")
        self._poll_toggle.toggled.connect(self._on_poll_toggled)
        poll_row.addWidget(self._poll_toggle)

        poll_row.addSpacing(14)
        poll_row.addWidget(QLabel("Auto-query:"))

        self._auto_toggle = QPushButton("Start")
        self._auto_toggle.setCheckable(True)
        self._auto_toggle.setFixedHeight(26)
        self._auto_toggle.toggled.connect(self._on_auto_toggled)
        poll_row.addWidget(self._auto_toggle)

        self._query_once_btn = QPushButton("Query now")
        self._query_once_btn.setFixedHeight(26)
        self._query_once_btn.setToolTip("Query all currently-enabled commands once")
        self._query_once_btn.clicked.connect(self._query_selected_once)
        poll_row.addWidget(self._query_once_btn)
        poll_row.addStretch()
        top_layout.addLayout(poll_row)

        # Auto Query Schedule table
        auto_box = QFrame()
        auto_box.setFrameShape(QFrame.Shape.StyledPanel)
        auto_layout = QVBoxLayout(auto_box)
        auto_layout.setContentsMargins(8, 6, 8, 8)
        auto_layout.setSpacing(4)

        heading = QLabel("Auto Query Schedule")
        heading.setStyleSheet("font-weight: 600;")
        auto_layout.addWidget(heading)

        self._table = QTableWidget(0, 6)
        self._table.setHorizontalHeaderLabels(
            ["Command", "Auto", "Interval (s)", "Last Value", "Updated", "Query"]
        )
        self._table.verticalHeader().setVisible(False)
        self._table.setMinimumHeight(120)
        self._table.setMaximumHeight(240)
        self._table.setAlternatingRowColors(True)
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self._table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self._table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        auto_layout.addWidget(self._table)
        top_layout.addWidget(auto_box)

        outer.addWidget(fixed_top)

        # ── Scrollable area: analysis / setpoint controls ─────────────
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        outer.addWidget(scroll, 1)

        content = QWidget()
        scroll.setWidget(content)

        root = QVBoxLayout(content)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)

        self._build_query_rows()
        self._build_setpoint_editor(root)
        root.addStretch()

    def _build_query_rows(self) -> None:
        read_commands = [
            (name, spec) for name, spec in self._spec.commands.items()
            if spec.read and name != "pressure"
        ]
        if not read_commands:
            self._table.setRowCount(1)
            msg = QTableWidgetItem("No additional read commands available")
            msg.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._table.setItem(0, 0, msg)
            self._table.setSpan(0, 0, 1, 6)
            return

        self._table.setRowCount(len(read_commands))
        now = time.monotonic()
        for row, (name, cmd_spec) in enumerate(read_commands):
            self._table.setRowHeight(row, 36)

            name_item = QTableWidgetItem(name)
            name_item.setToolTip(cmd_spec.description or name)
            self._table.setItem(row, 0, name_item)

            auto_check = QCheckBox()
            auto_check.setChecked(False)
            auto_check.setToolTip("Enable periodic query for this command")
            self._table.setCellWidget(row, 1, auto_check)

            interval = QDoubleSpinBox()
            interval.setRange(0.2, 300.0)
            interval.setDecimals(1)
            interval.setSingleStep(0.2)
            interval.setValue(2.0)
            self._table.setCellWidget(row, 2, interval)

            last_value = QLabel("-")
            last_value.setStyleSheet("font-size: 13px; font-weight: 600;")
            self._table.setCellWidget(row, 3, last_value)

            last_ts = QLabel("-")
            self._table.setCellWidget(row, 4, last_ts)

            query_btn = QPushButton("Now")
            query_btn.clicked.connect(
                lambda _checked=False, command=name: self._send_query(command)
            )
            self._table.setCellWidget(row, 5, query_btn)

            self._rows[name] = {
                "auto": auto_check,
                "interval": interval,
                "last": last_value,
                "updated": last_ts,
                "next_due": now + interval.value(),
            }

    def _build_setpoint_editor(self, root: QVBoxLayout) -> None:
        setpoint_names = [
            "setpoint_1_low", "setpoint_1_high", "setpoint_2_low", "setpoint_2_high",
        ]
        if all(name in self._spec.commands for name in setpoint_names):
            self._build_cdg_setpoint_editor(root)
            return

        ppg_rows = self._detect_indexed_setpoint_rows()
        if ppg_rows:
            self._build_ppg_setpoint_editor(root, ppg_rows)

    def _detect_indexed_setpoint_rows(self) -> list[int]:
        rows: list[int] = []
        for name in self._spec.commands:
            if not name.startswith("setpoint_"):
                continue
            parts = name.split("_")
            if len(parts) < 2:
                continue
            if parts[1].isdigit():
                rows.append(int(parts[1]))
        if not rows:
            return []
        return sorted(set(rows))

    def _build_cdg_setpoint_editor(self, root: QVBoxLayout) -> None:

        box = QFrame()
        box.setFrameShape(QFrame.Shape.StyledPanel)
        v = QVBoxLayout(box)
        v.setContentsMargins(8, 6, 8, 8)
        v.setSpacing(6)

        # ── Heading row: title + live sim indicator + play/pause ─────
        head_row = QHBoxLayout()
        head_row.setSpacing(8)
        title = QLabel("Setpoint Configuration")
        title.setStyleSheet("font-size: 13px; font-weight: 700;")
        head_row.addWidget(title)

        hint = QLabel("hysteresis preview — drag lines, slide, or type")
        hint.setStyleSheet("color: #888; font-size: 11px;")
        head_row.addWidget(hint)
        head_row.addStretch()

        self._sim_pressure_label = QLabel("Hover plot to inspect Y / pressure / SP status")
        self._sim_pressure_label.setStyleSheet(
            "color:#EDEDED; font-family: Consolas, monospace; font-size:11px;"
            "padding: 2px 8px; background:#2A2A2A; border-radius:3px;"
        )
        self._sim_pressure_label.setMinimumWidth(420)
        self._sim_pressure_label.setAlignment(
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignCenter
        )
        head_row.addWidget(self._sim_pressure_label)

        v.addLayout(head_row)

        # ── Split pane: controls (compact) | animated plot (expanding) ─
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(6)

        controls = QWidget()
        controls.setMinimumWidth(280)
        controls.setMaximumWidth(380)
        controls.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred
        )
        c = QVBoxLayout(controls)
        c.setContentsMargins(0, 0, 0, 0)
        c.setSpacing(6)

        c.addWidget(self._make_setpoint_group(
            "Setpoint 1", "setpoint_1_low", "setpoint_1_high", self._SP_COLORS["1"]
        ))
        c.addWidget(self._make_setpoint_group(
            "Setpoint 2", "setpoint_2_low", "setpoint_2_high", self._SP_COLORS["2"]
        ))

        c.addStretch()

        btn_row = QHBoxLayout()
        btn_row.setSpacing(6)
        read_btn = QPushButton("Read")
        read_btn.setToolTip("Read current setpoint values from the gauge")
        read_btn.clicked.connect(self._read_setpoints_once)
        btn_row.addWidget(read_btn)

        apply_btn = QPushButton("Apply to Gauge")
        apply_btn.setStyleSheet("font-weight: 600;")
        apply_btn.setToolTip("Write all four thresholds to the gauge")
        apply_btn.clicked.connect(self._apply_setpoints)
        btn_row.addWidget(apply_btn, 1)
        c.addLayout(btn_row)

        splitter.addWidget(controls)

        # Plot
        self._setpoint_plot = pg.PlotWidget()
        self._setpoint_plot.setMinimumHeight(260)
        self._setpoint_plot.setMinimumWidth(320)
        themed_plot(self._setpoint_plot)
        # Avoid SI prefix scaling (GTorr/Gmbar)
        self._setpoint_plot.setLabel("left", f"Pressure ({self._display_unit})", units="")
        self._setpoint_plot.setLabel(
            "bottom", "Pumpdown progress", units="",
        )
        self._setpoint_plot.showGrid(x=True, y=True, alpha=0.2)
        self._setpoint_plot.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        splitter.addWidget(self._setpoint_plot)

        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([300, 700])
        v.addWidget(splitter, 1)

        root.addWidget(box, 1)

        self._build_setpoint_plot_items()
        self._refresh_regions_and_lines()
        self._rebuild_sim_envelope()

    # ------------------------------------------------------------------
    # Setpoint control group (per setpoint): header + two rows (low/high)
    # ------------------------------------------------------------------

    def _build_ppg_setpoint_editor(self, root: QVBoxLayout, rows: list[int]) -> None:
        box = QFrame()
        box.setFrameShape(QFrame.Shape.StyledPanel)
        v = QVBoxLayout(box)
        v.setContentsMargins(8, 6, 8, 8)
        v.setSpacing(6)

        title = QLabel("Setpoint Configuration")
        title.setStyleSheet("font-size: 13px; font-weight: 700;")
        subtitle = QLabel("PPG setpoints: value, hysteresis, direction, and enable")
        subtitle.setStyleSheet("color:#888; font-size:11px;")
        v.addWidget(title)
        v.addWidget(subtitle)

        grid = QGridLayout()
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(6)
        grid.addWidget(QLabel("Setpoint"), 0, 0)
        grid.addWidget(QLabel("Value"), 0, 1)
        grid.addWidget(QLabel("Hysteresis"), 0, 2)
        grid.addWidget(QLabel("Direction"), 0, 3)
        grid.addWidget(QLabel("Enable"), 0, 4)

        self._ppg_setpoint_rows = list(rows)
        for row_idx, sp_idx in enumerate(rows, start=1):
            grid.addWidget(QLabel(f"SP{sp_idx}"), row_idx, 0)

            value_spin = QDoubleSpinBox()
            value_spin.setRange(0.0, 1e9)
            value_spin.setDecimals(6)
            value_spin.setSingleStep(0.001)
            value_spin.setSuffix(f" {self._display_unit}")
            grid.addWidget(value_spin, row_idx, 1)
            self._ppg_setpoint_controls[f"setpoint_{sp_idx}"] = value_spin
            value_spin.valueChanged.connect(lambda _v, self=self: self._refresh_pin_setpoint_plot())

            hyst_spin = QDoubleSpinBox()
            hyst_spin.setRange(0.0, 1e9)
            hyst_spin.setDecimals(6)
            hyst_spin.setSingleStep(0.001)
            hyst_spin.setSuffix(f" {self._display_unit}")
            grid.addWidget(hyst_spin, row_idx, 2)
            self._ppg_setpoint_controls[f"setpoint_{sp_idx}_hysteresis"] = hyst_spin
            hyst_spin.valueChanged.connect(lambda _v, self=self: self._refresh_pin_setpoint_plot())

            direction = QComboBox()
            direction.addItems(["ABOVE", "BELOW"])
            grid.addWidget(direction, row_idx, 3)
            self._ppg_setpoint_controls[f"setpoint_{sp_idx}_direction"] = direction
            direction.currentIndexChanged.connect(lambda _i, self=self: self._refresh_pin_setpoint_status())

            enabled = QCheckBox("ON")
            grid.addWidget(enabled, row_idx, 4)
            self._ppg_setpoint_controls[f"setpoint_{sp_idx}_enable"] = enabled
            enabled.toggled.connect(lambda _on, self=self: self._refresh_pin_setpoint_status())

        v.addLayout(grid)

        btn_row = QHBoxLayout()
        read_btn = QPushButton("Read")
        read_btn.clicked.connect(self._read_ppg_setpoints_once)
        btn_row.addWidget(read_btn)

        apply_btn = QPushButton("Apply to Gauge")
        apply_btn.setStyleSheet("font-weight: 600;")
        apply_btn.clicked.connect(self._apply_ppg_setpoints)
        btn_row.addWidget(apply_btn)
        btn_row.addStretch()
        v.addLayout(btn_row)

        self._build_pin_setpoint_preview(v)

        root.addWidget(box)

    def _build_pin_setpoint_preview(self, root: QVBoxLayout) -> None:
        self._pin_setpoint_plot = pg.PlotWidget()
        self._pin_setpoint_plot.setMinimumHeight(200)
        themed_plot(self._pin_setpoint_plot)
        self._pin_setpoint_plot.setLabel("bottom", "Index")
        # Avoid SI prefix scaling (GTorr/Gmbar)
        self._pin_setpoint_plot.setLabel("left", f"Pressure ({self._setpoint_preview_unit})", units="")
        self._pin_setpoint_plot.setLogMode(x=False, y=True)
        self._pin_setpoint_plot.showGrid(x=True, y=True, alpha=0.25)

        self._pin_setpoint_status.setStyleSheet(
            "color:#AFC7D6; font-size:11px; font-family: Consolas, monospace;"
        )
        root.addWidget(self._pin_setpoint_plot)
        root.addWidget(self._pin_setpoint_status)

        self._refresh_pin_setpoint_plot()

    def apply_theme(self) -> None:
        theme = current_theme(self)
        fixed_top = getattr(self, "_fixed_top", None)
        if fixed_top is not None:
            fixed_top.setStyleSheet(f"QWidget {{ background: {theme.panel}; border-bottom: 1px solid {theme.border}; }}")
        if self._setpoint_plot is not None:
            themed_plot(self._setpoint_plot)
        if self._pin_setpoint_plot is not None:
            themed_plot(self._pin_setpoint_plot)
        self._pin_setpoint_status.setStyleSheet(
            f"color:{theme.muted}; font-size:11px; font-family: Consolas, monospace;"
        )

    def _refresh_pin_setpoint_plot(self) -> None:
        if self._pin_setpoint_plot is None:
            return
        plot_item = self._pin_setpoint_plot.getPlotItem()
        plot_item.clear()
        self._pin_setpoint_lines.clear()
        self._pin_setpoint_hyst_regions.clear()

        if not self._ppg_setpoint_rows:
            return

        values_x: list[float] = []
        values_y: list[float] = []
        all_y: list[float] = []

        palette = ["#4C9BE8", "#E8954C", "#4CE87A", "#E84C6F"]
        for idx, sp_idx in enumerate(self._ppg_setpoint_rows):
            value_widget = self._ppg_setpoint_controls.get(f"setpoint_{sp_idx}")
            if not isinstance(value_widget, QDoubleSpinBox):
                continue
            p_disp = max(1e-12, float(value_widget.value()))
            x = float(sp_idx)
            values_x.append(x)
            values_y.append(p_disp)
            all_y.append(p_disp)

            color = palette[idx % len(palette)]
            line = pg.InfiniteLine(
                pos=p_disp,
                angle=0,
                movable=False,
                pen=pg.mkPen(color, width=2),
                label=f"SP{sp_idx}",
                labelOpts={"position": 0.96, "color": "#EDEDED"},
            )
            plot_item.addItem(line)
            self._pin_setpoint_lines[sp_idx] = line

            hyst_widget = self._ppg_setpoint_controls.get(f"setpoint_{sp_idx}_hysteresis")
            if isinstance(hyst_widget, QDoubleSpinBox):
                hyst = max(0.0, float(hyst_widget.value()))
                lo = max(1e-12, p_disp - hyst)
                hi = max(lo, p_disp + hyst)
                region = pg.LinearRegionItem(
                    values=[lo, hi],
                    orientation="horizontal",
                    brush=self._band_brush(color),
                    pen=pg.mkPen(color, width=0),
                    movable=False,
                )
                region.setZValue(-8)
                plot_item.addItem(region)
                self._pin_setpoint_hyst_regions[sp_idx] = region
                all_y.extend([lo, hi])

        if values_x and values_y:
            curve = plot_item.plot(
                np.array(values_x, dtype=float),
                np.array(values_y, dtype=float),
                pen=pg.mkPen("#70D2FF", width=1, style=Qt.PenStyle.DotLine),
                symbol="o",
                symbolSize=7,
                symbolBrush="#70D2FF",
            )
            curve.setZValue(6)
            xmin = min(values_x) - 0.6
            xmax = max(values_x) + 0.6
            plot_item.setXRange(xmin, xmax, padding=0.02)

        if all_y:
            ymin = max(1e-12, min(all_y) * 0.5)
            ymax = max(ymin * 1.2, max(all_y) * 1.8)
            plot_item.setYRange(ymin, ymax, padding=0.0)

        self._refresh_pin_setpoint_status()

    def _refresh_pin_setpoint_status(self) -> None:
        if self._last_pressure_mbar is None:
            self._pin_setpoint_status.setText("Setpoint states update from the live pressure reading.")
            return
        p_disp = float(convert_pressure(self._last_pressure_mbar, "mbar", self._setpoint_preview_unit))
        parts = [f"P={p_disp:.3E} {self._setpoint_preview_unit}"]
        for sp_idx in self._ppg_setpoint_rows:
            value_widget = self._ppg_setpoint_controls.get(f"setpoint_{sp_idx}")
            direction_widget = self._ppg_setpoint_controls.get(f"setpoint_{sp_idx}_direction")
            enable_widget = self._ppg_setpoint_controls.get(f"setpoint_{sp_idx}_enable")
            if not isinstance(value_widget, QDoubleSpinBox):
                continue
            enabled = True
            if isinstance(enable_widget, QCheckBox):
                enabled = enable_widget.isChecked()
            if not enabled:
                parts.append(f"SP{sp_idx}=OFF")
                continue
            direction = "ABOVE"
            if isinstance(direction_widget, QComboBox):
                direction = direction_widget.currentText().strip().upper() or "ABOVE"
            threshold = float(value_widget.value())
            active = p_disp >= threshold if direction == "ABOVE" else p_disp <= threshold
            parts.append(f"SP{sp_idx}:{'ON' if active else 'OFF'}")
        self._pin_setpoint_status.setText(" | ".join(parts))

    def _make_setpoint_group(
        self, title: str, low_key: str, high_key: str, color: str,
    ) -> QFrame:
        group = QFrame()
        group.setFrameShape(QFrame.Shape.StyledPanel)
        group.setStyleSheet(
            f"QFrame {{ border:1px solid #3A3A3A; border-left:3px solid {color}; "
            f"border-radius:4px; }}"
        )
        gl = QVBoxLayout(group)
        gl.setContentsMargins(8, 6, 8, 8)
        gl.setSpacing(4)

        header = QHBoxLayout()
        header.setSpacing(6)
        name = QLabel(f"<b>{title}</b>")
        header.addWidget(name)

        dot = QLabel("●")
        dot.setStyleSheet("color: #555; font-size: 14px;")
        header.addWidget(dot)

        status = QLabel("Inactive")
        status.setStyleSheet("color: #888; font-size: 11px;")
        header.addWidget(status)
        header.addStretch()
        gl.addLayout(header)

        self._setpoint_status_labels[title] = status
        self._setpoint_status_dots[title] = dot

        gl.addLayout(self._make_threshold_row(low_key, "Low", color))
        gl.addLayout(self._make_threshold_row(high_key, "High", color))

        # Hysteresis band hint
        hint = QLabel(
            "Shaded band on the plot is this setpoint's hysteresis region."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #777; font-size: 10px;")
        gl.addWidget(hint)
        return group

    def _make_threshold_row(
        self, key: str, label_text: str, color: str,
    ) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(4)

        lbl = QLabel(label_text)
        lbl.setFixedWidth(32)
        lbl.setStyleSheet(f"color: {color}; font-weight: 600;")
        row.addWidget(lbl)

        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(0, int(self._raw_max))
        slider.setTracking(True)
        default_val = 0 if "low" in key else int(self._raw_max)
        slider.setValue(default_val)
        slider.setToolTip(f"Slide to adjust {label_text.lower()} threshold")
        row.addWidget(slider, 1)

        spin = QDoubleSpinBox()
        spin.setRange(0.0, self._raw_max)
        spin.setDecimals(0)
        spin.setSingleStep(1.0)
        spin.setValue(float(default_val))
        spin.setFixedWidth(58)
        spin.setAlignment(Qt.AlignmentFlag.AlignRight)
        spin.setToolTip("Exact threshold value (raw byte 0–255)")
        row.addWidget(spin)

        mbar = QLabel("")
        mbar.setFixedWidth(90)
        mbar.setStyleSheet("color: #AAA; font-size: 10px; font-family: Consolas, monospace;")
        mbar.setToolTip("Approximate pressure equivalent using gauge full-scale")
        row.addWidget(mbar)

        slider.valueChanged.connect(
            lambda v, k=key: self._on_slider_changed(k, float(v))
        )
        spin.valueChanged.connect(
            lambda v, k=key: self._on_spin_changed(k, v)
        )

        self._setpoint_spins[key] = spin
        self._setpoint_sliders[key] = slider
        self._setpoint_mbar_labels[key] = mbar
        self._update_mbar_label(key)
        return row

    # ------------------------------------------------------------------
    # Plot items: curve, draggable threshold lines, hysteresis bands, marker
    # ------------------------------------------------------------------

    def _build_setpoint_plot_items(self) -> None:
        assert self._setpoint_plot is not None
        plot_item = self._setpoint_plot.getPlotItem()

        # Hysteresis bands — drawn first so curve paints over them
        for sp_num, color in self._SP_COLORS.items():
            low_key = f"setpoint_{sp_num}_low"
            high_key = f"setpoint_{sp_num}_high"
            lo = self._raw_to_display_pressure(self._setpoint_spins[low_key].value())
            hi = self._raw_to_display_pressure(self._setpoint_spins[high_key].value())
            brush = self._band_brush(color)
            region = pg.LinearRegionItem(
                values=[min(lo, hi), max(lo, hi)],
                orientation="horizontal",
                brush=brush,
                pen=pg.mkPen(color, width=0),
                movable=False,
            )
            region.setZValue(-10)
            plot_item.addItem(region, ignoreBounds=True)
            self._sp_regions[sp_num] = region

        # Cube-root transfer curve across the full detected gauge range.
        self._sim_curve = plot_item.plot(pen=pg.mkPen("#4C9BE8", width=2))
        self._rebuild_sim_envelope()

        # Hover indicator — horizontal line following Y-axis cursor position.
        self._hover_marker = pg.InfiniteLine(
            pos=self._raw_to_display_pressure(self._raw_max / 2.0), angle=0, movable=False,
            pen=pg.mkPen("#EDEDED", width=1, style=Qt.PenStyle.DashLine),
        )
        self._hover_marker.setVisible(False)
        plot_item.addItem(self._hover_marker, ignoreBounds=True)

        # Live pressure indicator — solid white dotted line that tracks the
        # most-recently received pressure reading.  Automatically updates so
        # the user can see at a glance where the chamber pressure sits relative
        # to the configured setpoint bands.
        self._live_pressure_line = pg.InfiniteLine(
            angle=0, movable=False,
            pen=pg.mkPen("#FFD700", width=2, style=Qt.PenStyle.DotLine),
            label="Live P",
            labelOpts={
                "position": 0.90,
                "color": "#FFD700",
                "fill": QColor("#2A2000"),
                "movable": False,
            },
        )
        self._live_pressure_line.setVisible(False)
        plot_item.addItem(self._live_pressure_line, ignoreBounds=True)

        # Draggable threshold lines (one per spinbox)
        for sp_num, color in self._SP_COLORS.items():
            for edge in ("low", "high"):
                key = f"setpoint_{sp_num}_{edge}"
                style = Qt.PenStyle.DashLine if edge == "low" else Qt.PenStyle.SolidLine
                line = pg.InfiniteLine(
                    pos=self._raw_to_display_pressure(self._setpoint_spins[key].value()),
                    angle=0, movable=True,
                    pen=pg.mkPen(color, width=2, style=style),
                    hoverPen=pg.mkPen(color, width=3),
                    label=f"SP{sp_num} {edge}",
                    labelOpts={
                        "color": "#FFFFFF", "position": 0.03,
                        "fill": QColor(color),
                        "movable": False,
                    },
                )
                p_min = self._raw_to_display_pressure(0.0)
                p_max = self._raw_to_display_pressure(self._raw_max)
                line.setBounds([min(p_min, p_max), max(p_min, p_max)])
                line.sigPositionChanged.connect(
                    lambda ln=line, k=key: self._on_line_dragged(k, ln.value())
                )
                plot_item.addItem(line, ignoreBounds=True)
                self._threshold_lines[key] = line

        scene = self._setpoint_plot.scene()
        if scene is not None:
            self._setpoint_hover_proxy = pg.SignalProxy(
                scene.sigMouseMoved,
                rateLimit=60,
                slot=self._on_setpoint_plot_mouse_moved,
            )

    # ------------------------------------------------------------------
    # Sync: all three input methods (drag, slider, spinbox) route through
    # _set_threshold(), which is the single source of truth.  _suppress_sync
    # prevents feedback loops between the three widgets.
    # ------------------------------------------------------------------

    def _set_threshold(self, key: str, value: float, source: str) -> None:
        value = max(0.0, min(self._raw_max, value))
        if self._suppress_sync:
            return
        self._suppress_sync = True
        try:
            spin = self._setpoint_spins.get(key)
            slider = self._setpoint_sliders.get(key)
            line = self._threshold_lines.get(key)
            if spin is not None and source != "spin":
                spin.setValue(value)
            if slider is not None and source != "slider":
                slider.setValue(int(round(value)))
            if line is not None and source != "line":
                line.setValue(self._raw_to_display_pressure(value))
        finally:
            self._suppress_sync = False
        self._update_mbar_label(key)
        self._refresh_regions_and_lines()
        self._rebuild_sim_envelope()

    def _on_spin_changed(self, key: str, value: float) -> None:
        self._set_threshold(key, float(value), "spin")

    def _on_slider_changed(self, key: str, value: float) -> None:
        self._set_threshold(key, float(value), "slider")

    def _on_line_dragged(self, key: str, value: float) -> None:
        raw = self._mbar_to_raw(self._display_to_mbar(float(value)))
        self._set_threshold(key, raw, "line")

    def _update_mbar_label(self, key: str) -> None:
        spin = self._setpoint_spins.get(key)
        lbl = self._setpoint_mbar_labels.get(key)
        if spin is None or lbl is None:
            return
        p_disp = self._raw_to_display_pressure(spin.value())
        lbl.setText(f"~ {p_disp:.3g} {self._display_unit}")

    def _refresh_regions_and_lines(self) -> None:
        """Sync the two hysteresis band overlays to the current low/high values."""
        for sp_num, region in self._sp_regions.items():
            low_key = f"setpoint_{sp_num}_low"
            high_key = f"setpoint_{sp_num}_high"
            lo = self._raw_to_display_pressure(self._setpoint_spins[low_key].value())
            hi = self._raw_to_display_pressure(self._setpoint_spins[high_key].value())
            region.setRegion([min(lo, hi), max(lo, hi)])

    def _rebuild_sim_envelope(self) -> None:
        """Rebuild a yin-yang pumpdown pressure envelope.

        Shape (left → right = high pressure → low pressure):
          1. Flat high-pressure plateau
          2. First descent — curve drops from plateau toward the setpoint region
          3. Upward hump — curve rises back to just above the highest SP-High value
          4. Downward trough — curve dips to just below the lowest SP-Low value
          5. Final descent — curve drops to the low-pressure plateau
          6. Flat low-pressure plateau

        The hump and trough are placed close together (yin-yang style) so the
        oscillation looks like one smooth 'S' reversal rather than two distant bumps.
        """
        if self._sim_curve is None:
            return
        N = 2048
        x = np.linspace(0.0, 1.0, N, dtype=float)

        hi_vals = [
            float(self._raw_to_mbar(self._setpoint_spins["setpoint_1_high"].value())),
            float(self._raw_to_mbar(self._setpoint_spins["setpoint_2_high"].value())),
        ]
        lo_vals = [
            float(self._raw_to_mbar(self._setpoint_spins["setpoint_1_low"].value())),
            float(self._raw_to_mbar(self._setpoint_spins["setpoint_2_low"].value())),
        ]
        highest_high = max(hi_vals)
        lowest_low = min(lo_vals)

        # ── Pressure levels ─────────────────────────────────────────────
        # High/low plateaus are well outside the setpoint band.
        p_top = highest_high * 8.0
        p_bot = max(1e-12, lowest_low * 0.04)
        # Keep the reversal visibly outside the hysteresis bands so the
        # shaded setpoint regions read as an interior operating window.
        p_up_hump = highest_high * 2.0
        p_dn_trough = max(1e-12, lowest_low * 0.5)

        # ── X-axis waypoints (yin-yang peaks are close together) ─────────
        #   A: high plateau ends
        #   B: first descent completes (curve reaches trough level)
        #   C: upward hump peaks  ← twin peaks are B..D, close together
        #   D: curve back down through trough level
        #   E: low plateau begins
        A = 0.12
        B = 0.40
        C = 0.50   # hump peak (B and C are only 0.10 apart)
        D = 0.60   # back down through the trough level
        E = 0.88

        # ── Smooth interpolation via control-point table ─────────────────
        # Use a fine grid of waypoints so np.interp gives a recognisable shape,
        # then smooth with a Gaussian kernel for C∞ continuity.
        xw = np.array([0.00,  A,      A*1.4, B*0.85, B,      C,      D,      D*1.15, E,      1.00])
        yw = np.array([p_top, p_top,  p_top * 0.6,
                       p_dn_trough * 1.5,
                       p_dn_trough,
                       p_up_hump,
                       p_dn_trough,
                       p_dn_trough * 1.5,
                       p_bot, p_bot])

        p_raw = np.interp(x, xw, yw)

        # Gaussian smoothing (pure numpy — no scipy needed)
        sigma_x = 0.030  # smoothing width in x-axis units
        sigma_n = max(1, int(sigma_x * N))
        half = sigma_n * 3
        k = np.arange(-half, half + 1, dtype=float)
        kernel = np.exp(-0.5 * (k / sigma_n) ** 2)
        kernel /= kernel.sum()
        # Pad with edge values so borders stay stable
        padded = np.pad(p_raw, half, mode="edge")
        p_curve = np.convolve(padded, kernel, mode="valid")[:N]
        p_curve = np.maximum(p_curve, 1e-12)

        p_disp = np.asarray(
            convert_pressure(p_curve, "mbar", self._display_unit),
            dtype=float,
        )
        self._sim_curve.setData(x, p_disp)

        if self._setpoint_plot is not None:
            plot_item = self._setpoint_plot.getPlotItem()
            y_lo, y_hi = float(np.min(p_disp)), float(np.max(p_disp))
            pad = max((y_hi - y_lo) * 0.06, max(abs(y_hi), 1.0) * 0.02)
            plot_item.setXRange(0.0, 1.0, padding=0.01)
            plot_item.setYRange(y_lo - pad, y_hi + pad, padding=0.0)

    def _on_setpoint_plot_mouse_moved(self, event: tuple) -> None:
        if self._setpoint_plot is None or self._hover_marker is None:
            return
        pos = event[0]
        vb = self._setpoint_plot.getPlotItem().vb
        if not vb.sceneBoundingRect().contains(pos):
            self._hover_marker.setVisible(False)
            self._sim_pressure_label.setText("Hover plot to inspect Y / pressure / SP status")
            return

        mp = vb.mapSceneToView(pos)
        p_disp = float(mp.y())
        p_mbar = self._display_to_mbar(p_disp)
        y_raw = float(self._mbar_to_raw(p_mbar))
        p_disp = float(convert_pressure(p_mbar, "mbar", self._display_unit))

        self._hover_marker.setValue(p_disp)
        self._hover_marker.setVisible(True)

        sp1_trig, sp1_region = self._setpoint_triggered_at_raw("1", y_raw)
        sp2_trig, sp2_region = self._setpoint_triggered_at_raw("2", y_raw)
        self._apply_status_ui("Setpoint 1", sp1_trig, "1", sp1_region)
        self._apply_status_ui("Setpoint 2", sp2_trig, "2", sp2_region)

        sp1_text = "Triggered" if sp1_trig else "Not triggered"
        sp2_text = "Triggered" if sp2_trig else "Not triggered"
        sp1_col = "#4CE87A" if sp1_trig else "#E84C4C"
        sp2_col = "#4CE87A" if sp2_trig else "#E84C4C"
        def _sp_label(trig: bool, region: str, col: str) -> str:
            badge = ""
            if region == "hysteresis":
                badge = (
                    " <span style='color:#FFD74C; font-size:9px;"
                    "border:1px solid #FFD74C; border-radius:2px;"
                    "padding:0 2px;'>HYST</span>"
                )
            state_text = "Triggered" if trig else "Not triggered"
            return (
                f"<span style='color:{col}; font-weight:700'>{state_text}</span>"
                f"{badge}"
            )

        self._sim_pressure_label.setText(
            f"P~{p_disp:.4E} {self._display_unit}  |  "
            f"SP1: {_sp_label(sp1_trig, sp1_region, sp1_col)}  |  "
            f"SP2: {_sp_label(sp2_trig, sp2_region, sp2_col)}"
        )

    def _setpoint_triggered_at_raw(self, sp_num: str, y_raw: float) -> tuple[bool, str]:
        lo = self._setpoint_spins[f"setpoint_{sp_num}_low"].value()
        hi = self._setpoint_spins[f"setpoint_{sp_num}_high"].value()
        low, high = min(lo, hi), max(lo, hi)

        # CDG setpoint manual: relay/LED is energized when pressure is lower
        # than the setpoint; low/high act as hysteresis re-arm/release levels.
        if y_raw <= low:
            self._setpoint_state[sp_num] = True
            return True, "triggered"
        if y_raw >= high:
            self._setpoint_state[sp_num] = False
            return False, "released"

        state = self._setpoint_state.get(sp_num, y_raw <= high)
        self._setpoint_state[sp_num] = state
        return state, "hysteresis"

    def _raw_to_mbar(self, raw: float | np.ndarray) -> float | np.ndarray:
        """Convert raw setpoint byte value(s) to pressure via cube-root law.

        Forward relation:
            p = full_scale_mbar * (raw / 255)^3
        """
        ratio = np.clip(np.asarray(raw, dtype=float) / self._raw_max, 0.0, 1.0)
        out = (ratio ** 3.0) * self._full_scale_mbar
        if np.isscalar(raw):
            return float(out)
        return out

    def _effective_min_mbar(self) -> float:
        cmd = self._spec.commands.get("pressure")
        if cmd is None or cmd.min_value is None:
            return max(self._full_scale_mbar * 1e-6, 1e-12)
        unit = cmd.unit or "mbar"
        if unit in SUPPORTED_UNITS:
            return max(float(convert_pressure(float(cmd.min_value), unit, "mbar")), 1e-12)
        return max(self._full_scale_mbar * 1e-6, 1e-12)

    def _raw_to_display_pressure(self, raw: float) -> float:
        p_mbar = float(self._raw_to_mbar(raw))
        return float(convert_pressure(p_mbar, "mbar", self._display_unit))

    def _display_to_mbar(self, value: float) -> float:
        return float(convert_pressure(float(value), self._display_unit, "mbar"))

    def _mbar_to_raw(self, p_mbar: float) -> float:
        p = max(0.0, min(float(p_mbar), self._full_scale_mbar))
        ratio = p / max(self._full_scale_mbar, 1e-12)
        return max(0.0, min(self._raw_max, self._raw_max * (ratio ** (1.0 / 3.0))))

    def set_display_unit(self, unit: str) -> None:
        if unit not in SUPPORTED_UNITS or unit == self._display_unit:
            return
        old_unit = self._display_unit
        self._display_unit = unit
        self._setpoint_preview_unit = unit
        if self._setpoint_plot is not None:
            # Avoid SI prefix scaling (GTorr/Gmbar)
            self._setpoint_plot.setLabel("left", f"Pressure ({unit})", units="")
        if self._pin_setpoint_plot is not None:
            # Avoid SI prefix scaling (GTorr/Gmbar)
            self._pin_setpoint_plot.setLabel("left", f"Pressure ({unit})", units="")
        for key in self._setpoint_mbar_labels:
            self._update_mbar_label(key)
        for widget in self._ppg_setpoint_controls.values():
            if isinstance(widget, QDoubleSpinBox):
                old_value = float(widget.value())
                widget.blockSignals(True)
                widget.setValue(float(convert_pressure(old_value, old_unit, unit)))
                widget.setSuffix(f" {unit}")
                widget.blockSignals(False)
        self._refresh_regions_and_lines()
        self._rebuild_sim_envelope()
        self._refresh_pin_setpoint_plot()
        # Reposition live pressure line in the new unit.
        if self._live_pressure_line is not None and self._last_pressure_mbar is not None:
            p_disp = float(convert_pressure(self._last_pressure_mbar, "mbar", self._display_unit))
            self._live_pressure_line.setValue(p_disp)

    def _apply_status_ui(self, title: str, active: bool, sp_num: str, region: str = "") -> None:
        status = self._setpoint_status_labels.get(title)
        dot = self._setpoint_status_dots.get(title)
        if status is None or dot is None:
            return
        if region == "hysteresis":
            state_text = "Triggered" if active else "Not triggered"
            status.setText(f"Hysteresis band ({state_text})")
            status.setStyleSheet("color: #E8D74C; font-weight: 700; font-size: 11px;")
            dot.setStyleSheet("color: #E8D74C; font-size: 14px;")
            return
        if active:
            status.setText("Triggered")
            status.setStyleSheet("color: #4CE87A; font-weight: 700; font-size: 11px;")
            dot.setStyleSheet("color: #4CE87A; font-size: 14px;")
        else:
            status.setText("Not triggered")
            status.setStyleSheet("color: #E84C4C; font-weight: 700; font-size: 11px;")
            dot.setStyleSheet("color: #E84C4C; font-size: 14px;")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _band_brush(hex_color: str) -> pg.mkBrush:
        c = QColor(hex_color)
        c.setAlpha(36)
        return pg.mkBrush(c)

    @pyqtSlot(bool)
    def _on_poll_toggled(self, paused: bool) -> None:
        self._poll_toggle.setText("Resume" if paused else "Pause")
        setter = getattr(self._worker, "set_polling_enabled", None)
        if callable(setter):
            setter(not paused)

    @pyqtSlot(bool)
    def _on_auto_toggled(self, active: bool) -> None:
        self._auto_toggle.setText("Stop Auto Query" if active else "Start Auto Query")
        if active:
            now = time.monotonic()
            for row in self._rows.values():
                row["next_due"] = now + float(row["interval"].value())
            self._timer.start()
        else:
            self._timer.stop()

    def _on_auto_tick(self) -> None:
        now = time.monotonic()
        for command, row in self._rows.items():
            if not row["auto"].isChecked():
                continue
            if now >= row["next_due"]:
                self._send_query(command)
                row["next_due"] = now + float(row["interval"].value())

    def _query_selected_once(self) -> None:
        for command, row in self._rows.items():
            if row["auto"].isChecked():
                self._send_query(command)

    def _send_query(self, command: str) -> None:
        try:
            protocol = getattr(self._worker, "_protocol", None)
            if protocol is None:
                return
            frame = protocol.build_request(command)
            self._worker.send_terminal_command(frame, command)
        except Exception:
            logger.exception("Settings query failed for %s", command)

    def _read_setpoints_once(self) -> None:
        for cmd in ("setpoint_1_read", "setpoint_2_read"):
            if cmd in self._rows:
                self._send_query(cmd)

    def _read_ppg_setpoints_once(self) -> None:
        for sp_idx in self._ppg_setpoint_rows:
            for cmd in (
                f"setpoint_{sp_idx}",
                f"setpoint_{sp_idx}_hysteresis",
                f"setpoint_{sp_idx}_direction",
                f"setpoint_{sp_idx}_enable",
            ):
                if cmd in self._spec.commands:
                    self._send_query(cmd)

    def refresh_setpoints(self) -> None:
        """Pull setpoint values from the gauge and refresh the preview envelope."""
        self._read_setpoints_once()
        self._rebuild_sim_envelope()

    def _apply_setpoints(self) -> None:
        protocol = getattr(self._worker, "_protocol", None)
        if protocol is None:
            return
        for cmd in ("setpoint_1_low", "setpoint_1_high", "setpoint_2_low", "setpoint_2_high"):
            spin = self._setpoint_spins.get(cmd)
            if spin is None:
                continue
            try:
                payload = max(0, min(255, int(round(spin.value()))))
                frame = protocol.build_request(cmd, payload)
                self._worker.send_terminal_command(frame, cmd)
            except Exception:
                logger.exception("Failed to send setpoint command %s", cmd)

    def _apply_ppg_setpoints(self) -> None:
        protocol = getattr(self._worker, "_protocol", None)
        if protocol is None:
            return
        for sp_idx in self._ppg_setpoint_rows:
            cmd_value = f"setpoint_{sp_idx}"
            cmd_hyst = f"setpoint_{sp_idx}_hysteresis"
            cmd_dir = f"setpoint_{sp_idx}_direction"
            cmd_en = f"setpoint_{sp_idx}_enable"

            try:
                value_widget = self._ppg_setpoint_controls.get(cmd_value)
                if cmd_value in self._spec.commands and isinstance(value_widget, QDoubleSpinBox):
                    p_mbar = self._display_to_mbar(float(value_widget.value()))
                    frame = protocol.build_request(cmd_value, f"{p_mbar:.6E}")
                    self._worker.send_terminal_command(frame, cmd_value)

                hyst_widget = self._ppg_setpoint_controls.get(cmd_hyst)
                if cmd_hyst in self._spec.commands and isinstance(hyst_widget, QDoubleSpinBox):
                    p_mbar = self._display_to_mbar(float(hyst_widget.value()))
                    frame = protocol.build_request(cmd_hyst, f"{p_mbar:.6E}")
                    self._worker.send_terminal_command(frame, cmd_hyst)

                dir_widget = self._ppg_setpoint_controls.get(cmd_dir)
                if cmd_dir in self._spec.commands and isinstance(dir_widget, QComboBox):
                    frame = protocol.build_request(cmd_dir, dir_widget.currentText().strip().upper())
                    self._worker.send_terminal_command(frame, cmd_dir)

                en_widget = self._ppg_setpoint_controls.get(cmd_en)
                if cmd_en in self._spec.commands and isinstance(en_widget, QCheckBox):
                    frame = protocol.build_request(cmd_en, "ON" if en_widget.isChecked() else "OFF")
                    self._worker.send_terminal_command(frame, cmd_en)
            except Exception:
                logger.exception("Failed to send PPG setpoint command for SP%d", sp_idx)
        self._refresh_pin_setpoint_plot()

    def _update_ppg_setpoint_control_from_response(self, command: str, parsed) -> bool:
        widget = self._ppg_setpoint_controls.get(command)
        if widget is None:
            return False
        if not parsed.success:
            return True

        if isinstance(widget, QDoubleSpinBox) and parsed.value is not None:
            value = float(parsed.value)
            if parsed.unit in SUPPORTED_UNITS:
                value = float(convert_pressure(value, parsed.unit, self._display_unit))
            elif command.startswith("setpoint_"):
                value = float(convert_pressure(value, "mbar", self._display_unit))
            widget.blockSignals(True)
            widget.setValue(value)
            widget.blockSignals(False)
            return True

        text_value = (parsed.formatted or "").strip().upper()
        if isinstance(widget, QComboBox) and text_value:
            idx = widget.findText(text_value)
            if idx >= 0:
                widget.blockSignals(True)
                widget.setCurrentIndex(idx)
                widget.blockSignals(False)
            return True
        if isinstance(widget, QCheckBox):
            on = text_value in {"ON", "YES", "TRUE", "1"}
            widget.blockSignals(True)
            widget.setChecked(on)
            widget.blockSignals(False)
            return True
        return True

    def on_terminal_response(self, entry) -> None:
        command = entry.command or ""
        protocol = getattr(self._worker, "_protocol", None)
        if command in self._ppg_setpoint_controls and entry.response and protocol is not None:
            try:
                parsed = protocol.parse_response(entry.response, command)
                self._update_ppg_setpoint_control_from_response(command, parsed)
                self._refresh_pin_setpoint_plot()
            except Exception:
                logger.exception("Failed to parse PPG setpoint response for %s", command)

        if command not in self._rows:
            return
        row = self._rows[command]
        text = "(no response)"

        if entry.error:
            text = f"ERR: {entry.error}"
        elif entry.response and protocol is not None:
            try:
                parsed = protocol.parse_response(entry.response, command)
                if parsed.success:
                    if parsed.formatted:
                        text = parsed.formatted
                    elif parsed.value is not None:
                        suffix = f" {parsed.unit}" if parsed.unit else ""
                        text = f"{parsed.value:.6g}{suffix}"
                    else:
                        text = "OK"
                    if command in ("setpoint_1_read", "setpoint_2_read"):
                        sp_num = "1" if command == "setpoint_1_read" else "2"
                        low = parsed.extra.get("low") if parsed.extra else None
                        high = parsed.extra.get("high") if parsed.extra else None
                        if low is not None:
                            self._set_threshold(f"setpoint_{sp_num}_low", float(low), "external")
                        elif parsed.value is not None:
                            self._set_threshold(f"setpoint_{sp_num}_low", float(parsed.value), "external")
                        if high is not None:
                            self._set_threshold(f"setpoint_{sp_num}_high", float(high), "external")
                else:
                    text = parsed.error or "Parse failed"
            except Exception as exc:
                text = f"Decode error: {exc}"

        row["last"].setText(text)
        row["updated"].setText(entry.timestamp.strftime("%H:%M:%S"))

    def on_reading(self, reading: DeviceReading) -> None:
        if reading.command != "pressure" or reading.value is None:
            return
        p_mbar = float(reading.value)
        if reading.unit in SUPPORTED_UNITS:
            p_mbar = float(convert_pressure(reading.value, reading.unit, "mbar"))
        self._last_pressure_mbar = max(p_mbar, 1e-12)
        self._refresh_pin_setpoint_status()
        # Update the live pressure indicator on the CDG setpoint plot and
        # automatically refresh the SP trigger status from the live reading.
        if self._live_pressure_line is not None and self._setpoint_plot is not None:
            p_disp = float(convert_pressure(self._last_pressure_mbar, "mbar", self._display_unit))
            self._live_pressure_line.setValue(p_disp)
            self._live_pressure_line.setVisible(True)
            y_raw = float(self._mbar_to_raw(self._last_pressure_mbar))
            sp1_trig, sp1_region = self._setpoint_triggered_at_raw("1", y_raw)
            sp2_trig, sp2_region = self._setpoint_triggered_at_raw("2", y_raw)
            self._apply_status_ui("Setpoint 1", sp1_trig, "1", sp1_region)
            self._apply_status_ui("Setpoint 2", sp2_trig, "2", sp2_region)


class OPG550SpectrumStudio(QWidget):
    """Advanced OPG550 analysis workspace built around the P3 V02 command set."""

    _CLAUDE_ORANGE = "#D9772F"
    _GASES = ("OH", "H2O", "H2", "N2", "O2", "Ar", "He", "CO", "CO2", "CH4")
    _GAS_COLORS = {
        "OH": "#FF5B5B",
        "H2O": "#43C5FF",
        "H2": "#A18CFF",
        "N2": "#E8D74C",
        "O2": "#90D98E",
        "Ar": "#FFAD5B",
        "He": "#59E0D6",
        "CO": "#C48DFF",
        "CO2": "#C8E07A",
        "CH4": "#F08AC0",
    }

    _ANALYSIS_MODES = (
        "Raw Spectrum",
        "Rate of Rise",
        "Residual Gas Detection",
        "Advanced Analysis",
    )
    _RECORD_COMMANDS = {"spec_record", "ror_record", "rgd_record"}

    _POLL_GROUPS = {
        "Raw Spectrum": (
            "pressure",
            "operating_mode",
            "spectrometer_pixel_count",
            "spec_state",
            "spec_record_count",
            "spec_buffer_size",
            "spec_record",
        ),
        "Rate of Rise": (
            "pressure",
            "operating_mode",
            "ror_state",
            "ror_record_count",
            "ror_buffer_size",
            "ror_record",
        ),
        "Residual Gas Detection": (
            "pressure",
            "operating_mode",
            "rgd_state",
            "rgd_record_count",
            "rgd_buffer_size",
            "rgd_record",
            "analog_output_mode",
            "analog_output_voltage",
        ),
        "Advanced Analysis": (
            "pressure",
            "operating_mode",
            "spec_state",
            "ror_state",
            "rgd_state",
            "spec_record",
            "analog_output_mode",
            "analog_output_voltage",
            "error_status",
        ),
    }

    def __init__(self, spec: DeviceSpec, worker, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._spec = spec
        self._worker = worker
        self._display_unit = get_display_unit()

        self._fw_value = QLabel("-")
        self._sn_value = QLabel("-")
        self._err_value = QLabel("-")
        self._pressure_value = QLabel(f"- {self._display_unit}")
        self._temperature_value = QLabel("- °C")
        self._pressure_quality = QLabel("Vacuum quality: -")
        self._molecule_label = QLabel("Likely optical gas signatures: -")
        self._mode_status = QLabel("Mode status: -")
        self._delta_label = QLabel("Delta: -")
        self._plasma_status = QLabel("Plasma: -")

        self._vacuum_bar = QProgressBar()
        self._temperature_bar = QProgressBar()
        self._spectrum_mode = SpectrumMode.AUTO
        self._analysis_mode = "Raw Spectrum"

        # Time/value buffers are intentionally unbounded so the user keeps the
        # complete session history (matches the explicit request to plot all
        # samples instead of dropping the oldest).
        self._trend_t: deque[float] = deque()
        self._trend_p: deque[float] = deque()
        self._trend_t0: float | None = None
        self._gas_t: deque[float] = deque()
        self._gas_pct: dict[str, deque[float]] = {
            gas: deque() for gas in self._GASES
        }
        self._gas_partial_mbar: dict[str, deque[float]] = {
            gas: deque() for gas in self._GASES
        }
        self._gas_rate_mbar_s: dict[str, deque[float]] = {
            gas: deque() for gas in self._GASES
        }
        self._peer_t0: float | None = None
        self._peer_pressures: dict[str, dict[str, object]] = {}
        self._latest_gas_pct: dict[str, float] = {gas: 0.0 for gas in self._GASES}

        self._trend_plot: pg.PlotWidget | None = None
        self._trend_curve: pg.PlotDataItem | None = None
        self._trend_gas_curves: dict[str, pg.PlotDataItem] = {}
        self._trend_right_view: pg.ViewBox | None = None
        self._trend_pressure_curve: pg.PlotDataItem | None = None
        self._spectrum_plot: pg.PlotWidget | None = None
        self._spectrum_curve: pg.PlotDataItem | None = None
        self._spectrum_right_view: pg.ViewBox | None = None
        self._spectrum_pressure_curve: pg.PlotDataItem | None = None
        self._spectrum_gas_markers: list[pg.InfiniteLine] = []
        self._gas_plot: pg.PlotWidget | None = None
        self._gas_curves: dict[str, pg.PlotDataItem] = {}
        self._gas_right_view: pg.ViewBox | None = None
        self._gas_pressure_curve: pg.PlotDataItem | None = None
        self._advanced_plot: pg.PlotWidget | None = None
        self._advanced_curve_a: pg.PlotDataItem | None = None
        self._advanced_curve_b: pg.PlotDataItem | None = None
        self._advanced_fill: pg.FillBetweenItem | None = None
        self._advanced_right_view: pg.ViewBox | None = None
        self._advanced_gas_curve: pg.PlotDataItem | None = None
        self._advanced_legend: pg.LegendItem | None = None

        self._analysis_combo: QComboBox | None = None
        self._compare_a_label: QLabel | None = None
        self._compare_a_combo: QComboBox | None = None
        self._compare_b_label: QLabel | None = None
        self._compare_b_combo: QComboBox | None = None
        self._correlation_gas_label: QLabel | None = None
        self._correlation_gas_combo: QComboBox | None = None
        self._gas_checks: dict[str, QCheckBox] = {}
        self._telemetry_rows: dict[str, QLabel] = {}
        self._spectrum_user_zoomed: bool = False
        self._spectrum_reset_btn: QPushButton | None = None
        self._spectrum_mode_desc: QLabel | None = None
        self._last_pressure_mbar: float | None = None
        self._last_pressure_t: float | None = None
        # Rolling window of recent mbar readings used for spike rejection.
        self._pressure_mbar_window: deque[float] = deque(maxlen=5)
        self._latest_spectrum_x: np.ndarray | None = None
        self._latest_spectrum_y: np.ndarray | None = None
        self._live_spectrum_x: np.ndarray | None = None
        self._live_spectrum_y: np.ndarray | None = None
        self._opg_export_samples: list[dict[str, object]] = []
        self._chart_boxes: dict[str, QWidget] = {}
        self._gas_box: QWidget | None = None
        self._studio_title: QLabel | None = None
        self._studio_subtitle: QLabel | None = None
        self._section_frames: list[QFrame] = []
        self._metric_title_labels: list[QLabel] = []
        self._metric_value_labels: list[QLabel] = []
        self._studio_value_bar: QLabel | None = None
        self._studio_crosshairs: dict[int, pg.InfiniteLine] = {}  # id(PlotWidget) -> line
        self._studio_proxies: list[pg.SignalProxy] = []
        self._last_spec_request_t: float | None = None
        self._active_opg_algorithm: str | None = None
        self._last_opg_activation_t: float | None = None
        self._opg_record_counts: dict[str, int | None] = {
            "spec_record": None,
            "ror_record": None,
            "rgd_record": None,
        }
        self._live_spec_timer = QTimer(self)
        self._live_spec_timer.setInterval(2000)
        self._live_spec_timer.timeout.connect(self._request_live_spec_if_due)

        # Coalesced chart-refresh scheduler. Multiple calls to
        # _refresh_mode_plots inside a single Qt event-loop tick collapse to
        # one redraw, which keeps the UI responsive when many tracked gases
        # are enabled.
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(120)
        self._refresh_timer.timeout.connect(self._do_refresh_mode_plots)
        self._refresh_pending: bool = False

        # Auto-plasma state. Defaults are loaded from QSettings ("opg/...")
        # and the user can override them per-session via the Spectrum Studio
        # "Plasma Ignition" panel.
        self._auto_plasma_enabled: bool = bool(get_setting("opg/auto_plasma_enabled"))
        self._auto_plasma_min_mbar: float = float(get_setting("opg/min_ignition_pressure_mbar"))
        self._auto_plasma_max_mbar: float = float(get_setting("opg/max_safe_pressure_mbar"))
        self._last_plasma_state: int | None = None  # 0=off, 1=on-not-ignited, 2=ignited
        self._auto_plasma_last_action_t: float = 0.0
        self._initial_plasma_read_done: bool = False
        self._pending_plasma_target: int | None = None
        self._auto_plasma_chk: QCheckBox | None = None
        self._auto_plasma_min_spin: QDoubleSpinBox | None = None
        self._auto_plasma_max_spin: QDoubleSpinBox | None = None

        self._build_ui()
        self._live_spec_timer.start()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)

        self._studio_title = QLabel("Spectrum Studio")
        self._studio_title.setStyleSheet("font-size: 14px; font-weight: 700;")
        self._studio_subtitle = QLabel(
            "OPG550 SPEC, RoR, RGD, analog-output, and pressure-correlation analysis."
        )
        self._studio_subtitle.setStyleSheet("font-size: 11px;")
        root.addWidget(self._studio_title)
        root.addWidget(self._studio_subtitle)

        body = QSplitter(Qt.Orientation.Horizontal)
        body.setChildrenCollapsible(False)
        body.setHandleWidth(6)

        # Left side: dedicated chart workspace.
        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(6)

        self._trend_box = QFrame()
        trend_layout = QVBoxLayout(self._trend_box)
        trend_layout.setContentsMargins(10, 8, 10, 8)
        trend_layout.setSpacing(6)
        trend_title = QLabel("Rate of Rise / OPG550 Pressure")
        trend_title.setStyleSheet("font-weight: 600;")
        trend_hdr = QHBoxLayout()
        trend_hdr.addWidget(trend_title)
        trend_hdr.addStretch()
        trend_export = QPushButton("Export CSV")
        trend_export.setToolTip("Save the trend chart's pressure history to a CSV file")
        trend_export.clicked.connect(self._export_trend_csv)
        trend_hdr.addWidget(trend_export)
        trend_layout.addLayout(trend_hdr)

        self._trend_plot = pg.PlotWidget()
        self._trend_plot.setMinimumHeight(170)
        themed_plot(self._trend_plot)
        self._pad_plot_axes(self._trend_plot)
        self._trend_plot.setLabel("left", "Rate of rise", units=f"{self._display_unit}/s")
        self._trend_plot.setLabel("bottom", "Time", units="s")
        self._trend_plot.showGrid(x=True, y=True, alpha=0.22)
        self._trend_curve = self._trend_plot.plot(
            pen=pg.mkPen("#43C5FF", width=2)
        )
        for gas in self._GASES:
            curve = self._trend_plot.plot(pen=pg.mkPen(self._GAS_COLORS[gas], width=2), name=gas)
            curve.setVisible(False)
            self._trend_gas_curves[gas] = curve
        self._trend_right_view, self._trend_pressure_curve = self._setup_right_axis(
            self._trend_plot,
            f"OPG Pressure ({self._display_unit})",
            "#43C5FF",
        )
        trend_layout.addWidget(self._trend_plot)

        self._spectrum_box = QFrame()
        spectrum_layout = QVBoxLayout(self._spectrum_box)
        spectrum_layout.setContentsMargins(10, 8, 10, 8)
        spectrum_layout.setSpacing(6)

        spectrum_hdr = QHBoxLayout()
        spectrum_title = QLabel("Raw Optical Spectrum")
        spectrum_title.setStyleSheet("font-weight: 600;")
        spectrum_hdr.addWidget(spectrum_title)
        spectrum_hdr.addStretch()
        spectrum_hdr.addWidget(QLabel("Plot options"))
        self._spectrum_mode_combo = QComboBox()
        for mode in SpectrumMode:
            label = "Live Data" if mode is SpectrumMode.AUTO else mode.value
            self._spectrum_mode_combo.addItem(label, mode)
        self._spectrum_mode_combo.currentIndexChanged.connect(self._on_spectrum_mode_changed)
        spectrum_hdr.addWidget(self._spectrum_mode_combo)
        spectrum_export = QPushButton("Export CSV")
        spectrum_export.setToolTip("Save the most recent spectrum (wavelength vs intensity) to a CSV file")
        spectrum_export.clicked.connect(self._export_spectrum_csv)
        spectrum_hdr.addWidget(spectrum_export)
        spectrum_layout.addLayout(spectrum_hdr)

        self._spectrum_mode_desc = QLabel(self._spectrum_mode_description(SpectrumMode.AUTO))
        self._spectrum_mode_desc.setStyleSheet("font-size:10px; font-style:italic;")
        self._spectrum_mode_desc.setWordWrap(True)
        spectrum_layout.addWidget(self._spectrum_mode_desc)

        self._spectrum_plot = pg.PlotWidget()
        self._spectrum_plot.setMinimumHeight(160)
        themed_plot(self._spectrum_plot)
        self._pad_plot_axes(self._spectrum_plot)
        self._spectrum_plot.setLabel("left", "Relative optical intensity")
        self._spectrum_plot.setLabel("bottom", "Wavelength", units="nm")
        self._spectrum_plot.showGrid(x=True, y=True, alpha=0.2)
        self._spectrum_curve = self._spectrum_plot.plot(
            pen=pg.mkPen("#90D98E", width=2),
            fillLevel=0.0,
            brush=pg.mkBrush(144, 217, 142, 90),
        )
        self._spectrum_right_view, self._spectrum_pressure_curve = self._setup_right_axis(
            self._spectrum_plot,
            f"OPG Pressure ({self._display_unit})",
            "#43C5FF",
        )
        self._spectrum_user_zoomed = False
        self._spectrum_plot.getViewBox().sigRangeChangedManually.connect(
            self._on_spectrum_range_manual
        )
        self._spectrum_reset_btn = QPushButton("A")
        self._spectrum_reset_btn.setToolTip("Reset spectrum view to auto-scale")
        self._spectrum_reset_btn.setFixedSize(24, 24)
        self._spectrum_reset_btn.hide()
        self._spectrum_reset_btn.clicked.connect(self._on_spectrum_reset_view)
        spectrum_hdr.addWidget(self._spectrum_reset_btn)
        spectrum_layout.addWidget(self._spectrum_plot)

        self._gas_box = QFrame()
        gas_layout = QVBoxLayout(self._gas_box)
        gas_layout.setContentsMargins(10, 8, 10, 8)
        gas_layout.setSpacing(6)
        gas_title = QLabel("Tracked Gas Analysis")
        gas_title.setStyleSheet("font-weight: 600;")
        gas_hdr = QHBoxLayout()
        gas_hdr.addWidget(gas_title)
        gas_hdr.addStretch()
        gas_export = QPushButton("Export CSV")
        gas_export.setToolTip("Save the tracked-gas partial-pressure history to a CSV file")
        gas_export.clicked.connect(self._export_gas_csv)
        gas_hdr.addWidget(gas_export)
        gas_layout.addLayout(gas_hdr)
        self._gas_plot = pg.PlotWidget()
        self._gas_plot.setMinimumHeight(135)
        themed_plot(self._gas_plot)
        self._pad_plot_axes(self._gas_plot)
        self._gas_plot.setLabel("left", "Partial pressure", units=self._display_unit)
        self._gas_plot.setLabel("bottom", "Time", units="s")
        self._gas_plot.showGrid(x=True, y=True, alpha=0.18)
        for gas in self._GASES:
            curve = self._gas_plot.plot(
                pen=pg.mkPen(self._GAS_COLORS[gas], width=2),
                name=gas,
            )
            self._gas_curves[gas] = curve
        self._gas_right_view, self._gas_pressure_curve = self._setup_right_axis(
            self._gas_plot,
            f"OPG Pressure ({self._display_unit})",
            "#43C5FF",
        )
        gas_layout.addWidget(self._gas_plot)

        self._advanced_box = QFrame()
        advanced_layout = QVBoxLayout(self._advanced_box)
        advanced_layout.setContentsMargins(10, 8, 10, 8)
        advanced_layout.setSpacing(6)
        advanced_title = QLabel("Advanced Gauge Correlation")
        advanced_title.setStyleSheet("font-weight: 600;")
        advanced_hdr = QHBoxLayout()
        advanced_hdr.addWidget(advanced_title)
        advanced_hdr.addStretch()
        advanced_export = QPushButton("Export CSV")
        advanced_export.setToolTip("Save the advanced correlation chart series to a CSV file")
        advanced_export.clicked.connect(self._export_advanced_csv)
        advanced_hdr.addWidget(advanced_export)
        advanced_layout.addLayout(advanced_hdr)
        self._advanced_plot = pg.PlotWidget()
        self._advanced_plot.setMinimumHeight(170)
        themed_plot(self._advanced_plot)
        self._pad_plot_axes(self._advanced_plot)
        self._advanced_plot.setLabel("left", f"Pressure ({self._display_unit})")
        self._advanced_plot.setLabel("bottom", "Time", units="s")
        self._advanced_plot.setLogMode(y=True)
        self._advanced_plot.showGrid(x=True, y=True, alpha=0.18)
        self._advanced_legend = self._advanced_plot.getPlotItem().addLegend(offset=(10, 10))
        self._advanced_curve_a = self._advanced_plot.plot(pen=pg.mkPen("#43C5FF", width=2), name="Source A")
        self._advanced_curve_b = self._advanced_plot.plot(pen=pg.mkPen("#E8D74C", width=2), name="Source B")
        self._advanced_fill = pg.FillBetweenItem(
            self._advanced_curve_a,
            self._advanced_curve_b,
            brush=pg.mkBrush(255, 91, 91, 45),
        )
        self._advanced_plot.addItem(self._advanced_fill)
        self._setup_advanced_right_axis()
        advanced_layout.addWidget(self._advanced_plot)
        self._delta_label.setStyleSheet("font-weight:600;")
        advanced_layout.addWidget(self._delta_label)

        charts_splitter = QSplitter(Qt.Orientation.Vertical)
        charts_splitter.setChildrenCollapsible(False)
        charts_splitter.addWidget(self._advanced_box)
        charts_splitter.addWidget(self._trend_box)
        charts_splitter.addWidget(self._spectrum_box)
        charts_splitter.addWidget(self._gas_box)
        charts_splitter.setStretchFactor(0, 2)
        charts_splitter.setStretchFactor(1, 3)
        charts_splitter.setStretchFactor(2, 3)
        charts_splitter.setStretchFactor(3, 2)
        charts_splitter.setSizes([190, 260, 250, 170])

        self._studio_value_bar = QLabel()
        self._studio_value_bar.setTextFormat(Qt.TextFormat.RichText)
        self._studio_value_bar.setStyleSheet(value_bar_style())
        self._studio_value_bar.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self._studio_value_bar.setFixedHeight(18)
        self._studio_value_bar.hide()
        left_layout.addWidget(charts_splitter, 1)
        left_layout.addWidget(self._studio_value_bar)
        self._chart_boxes = {
            "Rate of Rise": self._trend_box,
            "Raw Spectrum": self._spectrum_box,
            "Residual Gas Detection": self._gas_box,
            "Advanced Analysis": self._advanced_box,
        }
        self._section_frames.extend([self._trend_box, self._spectrum_box, self._gas_box, self._advanced_box])

        # Right side: controls + device info.
        right_scroll = QScrollArea()
        right_scroll.setWidgetResizable(True)
        right_scroll.setFrameShape(QFrame.Shape.NoFrame)

        right_panel = QWidget()
        right = QVBoxLayout(right_panel)
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(8)

        self._mode_box = QFrame()
        mode_layout = QGridLayout(self._mode_box)
        mode_layout.setContentsMargins(8, 8, 8, 8)
        mode_layout.setHorizontalSpacing(6)
        mode_layout.setVerticalSpacing(6)
        mode_layout.addWidget(QLabel("Main plot"), 0, 0)
        self._analysis_combo = QComboBox()
        self._analysis_combo.addItems(self._ANALYSIS_MODES)
        self._analysis_combo.currentTextChanged.connect(self._on_analysis_mode_changed)
        mode_layout.addWidget(self._analysis_combo, 0, 1)
        self._compare_a_label = QLabel("Compare A")
        mode_layout.addWidget(self._compare_a_label, 1, 0)
        self._compare_a_combo = QComboBox()
        self._compare_a_combo.currentIndexChanged.connect(self._refresh_advanced_plot)
        mode_layout.addWidget(self._compare_a_combo, 1, 1)
        self._compare_b_label = QLabel("Compare B")
        mode_layout.addWidget(self._compare_b_label, 2, 0)
        self._compare_b_combo = QComboBox()
        self._compare_b_combo.currentIndexChanged.connect(self._refresh_advanced_plot)
        mode_layout.addWidget(self._compare_b_combo, 2, 1)
        self._correlation_gas_label = QLabel("Right axis gas")
        mode_layout.addWidget(self._correlation_gas_label, 3, 0)
        self._correlation_gas_combo = QComboBox()
        for gas in self._GASES:
            self._correlation_gas_combo.addItem(gas, gas)
        self._correlation_gas_combo.setCurrentText("OH")
        self._correlation_gas_combo.currentIndexChanged.connect(self._refresh_advanced_plot)
        mode_layout.addWidget(self._correlation_gas_combo, 3, 1)
        self._mode_status.setWordWrap(True)
        self._mode_status.setStyleSheet("font-size:11px;")
        mode_layout.addWidget(self._mode_status, 4, 0, 1, 2)
        right.addWidget(self._mode_box)

        gas_select = QGroupBox("Track Gases")
        gas_grid = QGridLayout(gas_select)
        for idx, gas in enumerate(self._GASES):
            check = QCheckBox(gas)
            check.setChecked(False)
            check.toggled.connect(self._refresh_mode_plots)
            check.toggled.connect(lambda _checked: self._sync_chart_visibility())
            self._gas_checks[gas] = check
            gas_grid.addWidget(check, idx // 2, idx % 2)
        right.addWidget(gas_select)

        self._actions_box = QFrame()
        actions_layout = QGridLayout(self._actions_box)
        actions_layout.setContentsMargins(8, 8, 8, 8)
        actions_layout.setHorizontalSpacing(6)
        actions_layout.setVerticalSpacing(6)
        action_defs = [
            ("Poll Mode", "_poll_mode"),
            ("Snapshot All", "_snapshot"),
            ("Export CSV", "_export_opg_csv"),
            ("Read Error", "error_status"),
            ("Read Firmware", "software_version"),
            ("Read Serial", "serial_number"),
            ("Analog Out", "analog_output_voltage"),
        ]
        visible_actions = [
            item for item in action_defs
            if item[1] in {"_snapshot", "_poll_mode", "_export_opg_csv"}
            or item[1] in self._spec.commands
        ]
        for idx, (label, command) in enumerate(visible_actions):
            btn = QPushButton(label)
            btn.setMinimumHeight(28)
            btn.clicked.connect(lambda _=False, c=command: self._on_action(c))
            actions_layout.addWidget(btn, idx // 2, idx % 2)
        right.addWidget(self._actions_box)

        self._plasma_box = QFrame()
        plasma_layout = QVBoxLayout(self._plasma_box)
        plasma_layout.setContentsMargins(8, 8, 8, 8)
        plasma_layout.setSpacing(6)
        plasma_title = QLabel("Plasma Ignition")
        plasma_title.setStyleSheet("font-weight:600;")
        plasma_layout.addWidget(plasma_title)
        self._plasma_status.setStyleSheet("font-size:11px;")
        self._plasma_status.setWordWrap(True)
        plasma_layout.addWidget(self._plasma_status)
        plasma_row = QHBoxLayout()
        plasma_on = QPushButton("On")
        plasma_on.setEnabled("plasma_enable" in self._spec.commands)
        plasma_on.clicked.connect(lambda _=False: self._on_action("_plasma_on"))
        plasma_off = QPushButton("Off")
        plasma_off.setEnabled("plasma_enable" in self._spec.commands)
        plasma_off.clicked.connect(lambda _=False: self._on_action("_plasma_off"))
        plasma_read = QPushButton("Read")
        plasma_read.setEnabled("plasma_state" in self._spec.commands)
        plasma_read.clicked.connect(lambda _=False: self._send_query("plasma_state"))
        plasma_row.addWidget(plasma_on)
        plasma_row.addWidget(plasma_off)
        plasma_row.addWidget(plasma_read)
        plasma_layout.addLayout(plasma_row)

        # Auto-plasma controls. Defaults come from the global Settings dialog
        # (opg/auto_plasma_enabled etc.) but the user can tweak per-session
        # values here; changes are persisted back to QSettings.
        self._auto_plasma_chk = QCheckBox("Auto plasma based on pressure")
        self._auto_plasma_chk.setEnabled(
            "plasma_enable" in self._spec.commands and "plasma_state" in self._spec.commands
        )
        self._auto_plasma_chk.setChecked(self._auto_plasma_enabled)
        self._auto_plasma_chk.toggled.connect(self._on_auto_plasma_toggled)
        plasma_layout.addWidget(self._auto_plasma_chk)

        thresh_row = QGridLayout()
        thresh_row.setHorizontalSpacing(6)
        thresh_row.setVerticalSpacing(4)
        thresh_row.addWidget(QLabel("Min ignite (mbar)"), 0, 0)
        self._auto_plasma_min_spin = QDoubleSpinBox()
        self._auto_plasma_min_spin.setDecimals(2)
        self._auto_plasma_min_spin.setRange(1e-12, 1e3)
        self._auto_plasma_min_spin.setStepType(
            QDoubleSpinBox.StepType.AdaptiveDecimalStepType
        )
        self._auto_plasma_min_spin.setValue(self._auto_plasma_min_mbar)
        self._auto_plasma_min_spin.valueChanged.connect(self._on_auto_plasma_min_changed)
        thresh_row.addWidget(self._auto_plasma_min_spin, 0, 1)
        thresh_row.addWidget(QLabel("Max safe (mbar)"), 1, 0)
        self._auto_plasma_max_spin = QDoubleSpinBox()
        self._auto_plasma_max_spin.setDecimals(2)
        self._auto_plasma_max_spin.setRange(1e-12, 1e3)
        self._auto_plasma_max_spin.setStepType(
            QDoubleSpinBox.StepType.AdaptiveDecimalStepType
        )
        self._auto_plasma_max_spin.setValue(self._auto_plasma_max_mbar)
        self._auto_plasma_max_spin.valueChanged.connect(self._on_auto_plasma_max_changed)
        thresh_row.addWidget(self._auto_plasma_max_spin, 1, 1)
        plasma_layout.addLayout(thresh_row)

        right.insertWidget(0, self._plasma_box)

        telemetry = QGroupBox("Manual Telemetry")
        telemetry_layout = QGridLayout(telemetry)
        telemetry_commands = [
            "operating_mode",
            "bootloader_version",
            "spec_state",
            "spec_record_count",
            "ror_state",
            "ror_record_count",
            "rgd_state",
            "rgd_record_count",
            "analog_output_mode",
            "analog_output_voltage",
            "error_count",
        ]
        row = 0
        for command in telemetry_commands:
            if command not in self._spec.commands:
                continue
            label = QLabel(command.replace("_", " ").title())
            value = QLabel("-")
            value.setStyleSheet("font-family: Consolas, monospace;")
            self._telemetry_rows[command] = value
            telemetry_layout.addWidget(label, row, 0)
            telemetry_layout.addWidget(value, row, 1)
            row += 1
        right.addWidget(telemetry)

        cards = QGridLayout()
        cards.setHorizontalSpacing(10)
        cards.setVerticalSpacing(8)
        cards.addWidget(self._metric_card("Pressure", self._pressure_value), 0, 0)
        cards.addWidget(self._metric_card("Temperature", self._temperature_value), 0, 1)
        cards_frame = QWidget()
        cards_frame.setLayout(cards)
        right.addWidget(cards_frame)

        compact_meta = QGroupBox("Device Details")
        compact_meta.setCheckable(True)
        compact_meta.setChecked(False)
        compact_meta.toggled.connect(lambda checked: self._err_value.setVisible(checked))
        meta_layout = QGridLayout(compact_meta)
        meta_layout.addWidget(QLabel("Firmware"), 0, 0)
        meta_layout.addWidget(self._fw_value, 0, 1)
        meta_layout.addWidget(QLabel("Serial"), 1, 0)
        meta_layout.addWidget(self._sn_value, 1, 1)
        right.addWidget(compact_meta)

        self._status_box = QFrame()
        sv = QVBoxLayout(self._status_box)
        sv.setContentsMargins(10, 8, 10, 8)
        sv.setSpacing(5)
        err_title = QLabel("Error Status")
        err_title.setStyleSheet("font-weight: 600;")
        self._err_value.setStyleSheet("font-family: Consolas, monospace;")
        sv.addWidget(err_title)
        sv.addWidget(self._err_value)
        self._err_value.setVisible(False)
        right.addWidget(self._status_box)

        self._health_box = QFrame()
        hv = QVBoxLayout(self._health_box)
        hv.setContentsMargins(10, 8, 10, 10)
        hv.setSpacing(8)

        vacuum_title = QLabel("Vacuum Regime")
        vacuum_title.setStyleSheet("font-weight: 600;")
        hv.addWidget(vacuum_title)

        self._vacuum_bar.setRange(0, 1000)
        self._vacuum_bar.setValue(0)
        self._vacuum_bar.setFormat("%p%")
        self._vacuum_bar.setStyleSheet(
            "QProgressBar {"
            "  border: 1px solid #3E4C57; border-radius: 5px; background: #10161B;"
            "  text-align: center; color: #DCE9F2;"
            "}"
            "QProgressBar::chunk {"
            "  background: qlineargradient(x1:0, y1:0, x2:1, y2:0,"
            "    stop:0 #2E97D8, stop:0.55 #1FC39A, stop:1 #C7D941);"
            "  border-radius: 4px;"
            "}"
        )
        hv.addWidget(self._vacuum_bar)
        self._pressure_quality.setStyleSheet("font-size: 11px;")
        hv.addWidget(self._pressure_quality)

        temp_title = QLabel("Sensor Thermal Load")
        temp_title.setStyleSheet("font-weight: 600;")
        hv.addWidget(temp_title)
        self._temperature_bar.setRange(0, 1200)
        self._temperature_bar.setValue(0)
        self._temperature_bar.setFormat("%p%")
        self._temperature_bar.setStyleSheet(
            "QProgressBar {"
            "  border: 1px solid #4C3A35; border-radius: 5px; background: #201714;"
            "  text-align: center; color: #F7DDC8;"
            "}"
            "QProgressBar::chunk {"
            "  background: qlineargradient(x1:0, y1:0, x2:1, y2:0,"
            "    stop:0 #E7B56A, stop:0.55 #E98645, stop:1 #D85B4A);"
            "  border-radius: 4px;"
            "}"
        )
        hv.addWidget(self._temperature_bar)

        self._molecule_label.setStyleSheet("font-size:11px;")
        self._molecule_label.setWordWrap(True)
        hv.addWidget(self._molecule_label)

        right.addWidget(self._health_box)
        right.addStretch()
        self._section_frames.extend([self._mode_box, self._actions_box, self._plasma_box, self._status_box, self._health_box])

        right_scroll.setWidget(right_panel)

        body.addWidget(left_panel)
        body.addWidget(right_scroll)
        body.setStretchFactor(0, 3)
        body.setStretchFactor(1, 2)
        body.setSizes([860, 420])

        root.addWidget(body, 1)
        self._refresh_peer_combos()
        self._on_analysis_mode_changed(self._analysis_mode)
        self._setup_studio_crosshairs()
        self.apply_theme()

    @staticmethod
    def _pad_plot_axes(plot: pg.PlotWidget) -> None:
        plot_item = plot.getPlotItem()
        try:
            plot_item.setContentsMargins(10, 8, 18, 12)
        except Exception:
            pass
        for axis_name, size in (("left", 74), ("right", 74), ("bottom", 34)):
            try:
                axis = plot_item.getAxis(axis_name)
                if axis_name == "bottom":
                    axis.setHeight(size)
                else:
                    axis.setWidth(size)
            except Exception:
                pass

    def apply_theme(self) -> None:
        theme = current_theme(self)
        frame_style = (
            f"QFrame {{ background:{theme.panel}; border:1px solid {theme.border}; border-radius:8px; }}"
        )
        for frame in self._section_frames:
            frame.setStyleSheet(frame_style)
        if self._studio_title is not None:
            self._studio_title.setStyleSheet(f"font-size:14px; font-weight:700; color:{theme.text};")
        if self._studio_subtitle is not None:
            self._studio_subtitle.setStyleSheet(f"font-size:11px; color:{theme.muted};")
        if self._spectrum_mode_desc is not None:
            self._spectrum_mode_desc.setStyleSheet(f"color:{theme.muted}; font-size:10px; font-style:italic;")
        if self._studio_value_bar is not None:
            self._studio_value_bar.setStyleSheet(value_bar_style())
        self._mode_status.setStyleSheet(f"color:{theme.muted}; font-size:11px;")
        self._plasma_status.setStyleSheet(f"color:{theme.muted}; font-size:11px;")
        self._pressure_quality.setStyleSheet(f"color:{theme.muted}; font-size:11px;")
        self._molecule_label.setStyleSheet(f"color:{theme.success_fg}; font-size:11px;")
        self._delta_label.setStyleSheet(f"color:{theme.danger_fg}; font-weight:600;")
        self._err_value.setStyleSheet(f"font-family: Consolas, monospace; color:{theme.danger_fg};")
        for value in self._telemetry_rows.values():
            value.setStyleSheet(f"font-family: Consolas, monospace; color:{theme.text};")
        for label in self._metric_title_labels:
            label.setStyleSheet(f"font-size: 11px; color: {theme.muted};")
        for label in self._metric_value_labels:
            label.setStyleSheet(f"font-size: 16px; font-weight: 700; color: {theme.text};")

        if self._spectrum_reset_btn is not None:
            self._spectrum_reset_btn.setStyleSheet(
                f"QPushButton {{ background:{theme.control}; color:{theme.text}; border:1px solid {theme.border};"
                " border-radius:4px; font-weight:bold; font-size:11px; }"
                f"QPushButton:hover {{ background:{theme.control_hover}; }}"
            )

        for plot in (
            self._trend_plot,
            self._spectrum_plot,
            self._gas_plot,
            self._advanced_plot,
        ):
            if plot is not None:
                themed_plot(plot)
                self._pad_plot_axes(plot)

        for gas, check in self._gas_checks.items():
            gas_color = self._GAS_COLORS.get(gas, theme.text)
            check.setStyleSheet(
                f"QCheckBox {{ color:{theme.text}; font-weight:600; spacing:6px; }}"
                f"QCheckBox::indicator {{ width:14px; height:14px; border:2px solid {gas_color};"
                f" border-radius:3px; background:{theme.panel}; }}"
                f"QCheckBox::indicator:checked {{ background:{gas_color}; border-color:{gas_color}; }}"
            )

        self._vacuum_bar.setStyleSheet(
            f"QProgressBar {{ border:1px solid {theme.border}; border-radius:5px; background:{theme.control};"
            f" text-align:center; color:{theme.text}; }}"
            "QProgressBar::chunk {"
            "  background: qlineargradient(x1:0, y1:0, x2:1, y2:0,"
            "    stop:0 #2E97D8, stop:0.55 #1FC39A, stop:1 #C7D941);"
            "  border-radius: 4px;"
            "}"
        )
        self._temperature_bar.setStyleSheet(
            f"QProgressBar {{ border:1px solid {theme.border}; border-radius:5px; background:{theme.control};"
            f" text-align:center; color:{theme.text}; }}"
            "QProgressBar::chunk {"
            "  background: qlineargradient(x1:0, y1:0, x2:1, y2:0,"
            "    stop:0 #E7B56A, stop:0.55 #E98645, stop:1 #D85B4A);"
            "  border-radius: 4px;"
            "}"
        )

    def _setup_advanced_right_axis(self) -> None:
        if self._advanced_plot is None:
            return
        plot_item = self._advanced_plot.getPlotItem()
        plot_item.showAxis("right")
        right_axis = plot_item.getAxis("right")
        right_axis.setLabel("Gas partial pressure", units=self._display_unit, color="#FF5B5B")
        right_axis.setTextPen(pg.mkPen("#FF5B5B"))
        self._advanced_right_view = pg.ViewBox()
        plot_item.scene().addItem(self._advanced_right_view)
        right_axis.linkToView(self._advanced_right_view)
        self._advanced_right_view.setXLink(plot_item)
        self._advanced_gas_curve = pg.PlotDataItem(pen=pg.mkPen("#FF5B5B", width=2))
        self._advanced_right_view.addItem(self._advanced_gas_curve)
        self._advanced_right_view.setYRange(0, 1e-12)

        def update_views() -> None:
            if self._advanced_right_view is not None:
                self._advanced_right_view.setGeometry(plot_item.vb.sceneBoundingRect())
                self._advanced_right_view.linkedViewChanged(plot_item.vb, self._advanced_right_view.XAxis)

        plot_item.vb.sigResized.connect(update_views)
        update_views()

    def _gauge_pressure_min_display(self) -> float:
        """Minimum physical pressure of this gauge in display units (always positive)."""
        return max(convert_pressure(1e-10, "mbar", self._display_unit), 1e-20)

    def _gauge_pressure_max_display(self) -> float:
        """Maximum physical pressure of this gauge in display units."""
        return convert_pressure(1.1e3, "mbar", self._display_unit)

    def _clamp_right_view_to_gauge_range(self, right_view: pg.ViewBox | None) -> None:
        """Set the Y range of a right pressure axis to the gauge's physical range."""
        if right_view is None:
            return
        right_view.setYRange(
            self._gauge_pressure_min_display(),
            self._gauge_pressure_max_display(),
            padding=0,
        )

    def _setup_right_axis(
        self,
        plot: pg.PlotWidget,
        label: str,
        color: str,
    ) -> tuple[pg.ViewBox, pg.PlotDataItem]:
        plot_item = plot.getPlotItem()
        plot_item.showAxis("right")
        right_axis = plot_item.getAxis("right")
        right_axis.setLabel(label, color=color)
        right_axis.setTextPen(pg.mkPen(color))
        right_view = pg.ViewBox()
        plot_item.scene().addItem(right_view)
        right_axis.linkToView(right_view)
        right_view.setXLink(plot_item)
        curve = pg.PlotDataItem(pen=pg.mkPen(color, width=2, style=Qt.PenStyle.DashLine))
        right_view.addItem(curve)

        def update_views() -> None:
            right_view.setGeometry(plot_item.vb.sceneBoundingRect())
            right_view.linkedViewChanged(plot_item.vb, right_view.XAxis)

        plot_item.vb.sigResized.connect(update_views)
        update_views()
        return right_view, curve

    def _setup_studio_crosshairs(self) -> None:
        """Wire a vertical crosshair and SignalProxy to each visible Spectrum Studio plot."""
        for proxy in self._studio_proxies:
            try:
                proxy.disconnect()
            except (AttributeError, RuntimeError):
                pass
        self._studio_proxies.clear()

        for line in self._studio_crosshairs.values():
            try:
                line.getViewBox().removeItem(line)
            except Exception:
                pass
        self._studio_crosshairs.clear()

        studio_plots: list[tuple[pg.PlotWidget, str]] = [
            (self._advanced_plot, "time"),
            (self._trend_plot, "time"),
            (self._spectrum_plot, "wavelength"),
            (self._gas_plot, "time"),
        ]
        for plot, x_kind in studio_plots:
            if plot is None:
                continue
            vline = pg.InfiniteLine(
                angle=90,
                movable=False,
                pen=pg.mkPen(color=(220, 220, 220, 160), width=1),
            )
            vline.setVisible(False)
            plot.addItem(vline, ignoreBounds=True)
            self._studio_crosshairs[id(plot)] = vline

            proxy = pg.SignalProxy(
                plot.scene().sigMouseMoved,
                rateLimit=60,
                slot=lambda ev, p=plot, k=x_kind: self._on_studio_mouse_moved(ev, p, k),
            )
            self._studio_proxies.append(proxy)

    def _on_studio_mouse_moved(self, event: tuple, plot: pg.PlotWidget, x_kind: str) -> None:
        pos = event[0]
        vb = plot.getViewBox()
        if not vb.sceneBoundingRect().contains(pos):
            for line in self._studio_crosshairs.values():
                line.setVisible(False)
            if self._studio_value_bar is not None:
                self._studio_value_bar.hide()
            return

        mp = vb.mapSceneToView(pos)
        x_val = mp.x()

        # Move crosshair only on the active plot
        for pid, line in self._studio_crosshairs.items():
            if pid == id(plot):
                line.setValue(x_val)
                line.setVisible(True)
            else:
                line.setVisible(False)

        self._update_studio_value_bar(plot, x_val, x_kind)

    def _update_studio_value_bar(self, plot: pg.PlotWidget, x: float, x_kind: str) -> None:
        if self._studio_value_bar is None:
            return
        parts: list[str] = []

        if x_kind == "wavelength":
            parts.append(f"<span style='color:#90D98E'><b>X</b></span>: {x:.1f} nm")
        else:
            parts.append(f"<span style='color:#D9D9D9'><b>X</b></span>: {x:.1f} s")

        if x_kind == "wavelength":
            # Spectrum plot: show wavelength + intensity
            if self._latest_spectrum_x is not None and self._latest_spectrum_y is not None:
                idx = int(np.searchsorted(self._latest_spectrum_x, x))
                idx = min(max(idx, 0), len(self._latest_spectrum_x) - 1)
                wl = float(self._latest_spectrum_x[idx])
                intensity = float(self._latest_spectrum_y[idx])
                parts.append(
                    f"<span style='color:#90D98E'><b>Wavelength</b></span>: {wl:.1f} nm"
                    f"  <span style='color:#90D98E'><b>Intensity</b></span>: {intensity:.4f}"
                )
            if self._last_pressure_mbar is not None:
                p_disp = convert_pressure(self._last_pressure_mbar, "mbar", self._display_unit)
                parts.append(
                    f"<span style='color:#43C5FF'><b>OPG Pressure (right axis)</b></span>: {p_disp:.3E} {self._display_unit}"
                )
        else:
            # Time-based plots: interpolate from stored data.
            if plot is self._advanced_plot:
                # Advanced Analysis: show the two peer pressure sources (A and B) and
                # the correlated gas signal on the right axis.
                source_a = self._compare_a_combo.currentData() if self._compare_a_combo is not None else ""
                source_b = self._compare_b_combo.currentData() if self._compare_b_combo is not None else ""
                name_a = self._compare_a_combo.currentText() if self._compare_a_combo is not None else "Source A"
                name_b = self._compare_b_combo.currentText() if self._compare_b_combo is not None else "Source B"
                peer_a = self._peer_pressures.get(str(source_a)) if source_a else None
                peer_b = self._peer_pressures.get(str(source_b)) if source_b else None
                value_a: float | None = None
                value_b: float | None = None
                if peer_a is not None and peer_a["t"]:
                    ta = np.array(peer_a["t"], dtype=float)
                    pa = np.array(peer_a["p"], dtype=float)
                    idx = int(np.searchsorted(ta, x))
                    idx = min(max(idx, 0), len(ta) - 1)
                    value_a = float(pa[idx])
                    parts.append(
                        f"<span style='color:#43C5FF'><b>{name_a}</b></span>: "
                        f"{value_a:.3E} {self._display_unit}"
                    )
                if peer_b is not None and peer_b["t"]:
                    ta = np.array(peer_b["t"], dtype=float)
                    pa = np.array(peer_b["p"], dtype=float)
                    idx = int(np.searchsorted(ta, x))
                    idx = min(max(idx, 0), len(ta) - 1)
                    value_b = float(pa[idx])
                    parts.append(
                        f"<span style='color:#E8D74C'><b>{name_b}</b></span>: "
                        f"{value_b:.3E} {self._display_unit}"
                    )
                if value_a is not None and value_b is not None:
                    delta = value_a - value_b
                    parts.append(
                        f"<span style='color:#FF8C8C'><b>Δ(A−B)</b></span>: {delta:+.3E} {self._display_unit}"
                    )
                # Gas partial pressure on the right axis
                gas = self._correlation_gas_combo.currentData() if self._correlation_gas_combo is not None else None
                if gas and self._gas_t and gas in self._gas_partial_mbar and self._gas_partial_mbar[gas]:
                    gt = np.array(self._gas_t, dtype=float)
                    gd = self._gas_partial_mbar[gas]
                    ga = np.fromiter(gd, dtype=float, count=len(gd))
                    n = min(len(gt), len(ga))
                    if n > 0:
                        idx = int(np.searchsorted(gt[:n], x))
                        idx = min(max(idx, 0), n - 1)
                        val_disp = convert_pressure(float(ga[idx]), "mbar", self._display_unit)
                        parts.append(
                            f"<span style='color:#FF5B5B'><b>{gas} partial (right axis)</b></span>: "
                            f"{val_disp:.3E} {self._display_unit}"
                        )
            else:
                # Trend / gas plots: show OPG pressure and, for the gas plot, each tracked gas.
                if self._trend_t and self._trend_p:
                    ta = np.array(self._trend_t, dtype=float)
                    pa = np.array(self._trend_p, dtype=float)
                    idx = int(np.searchsorted(ta, x))
                    idx = min(max(idx, 0), len(ta) - 1)
                    p_val = float(pa[idx])
                    t_val = float(ta[idx])

                    if plot is self._trend_plot and self._analysis_mode == "Rate of Rise":
                        # Left axis = dP/dt; right dashed curve = absolute OPG pressure.
                        selected = self._selected_gases()
                        if selected:
                            # Per-gas rate traces on the left axis.
                            if self._gas_t:
                                gt = np.array(self._gas_t, dtype=float)
                                for gas in sorted(selected):
                                    rate_data = self._gas_rate_mbar_s.get(gas)
                                    if not rate_data:
                                        continue
                                    ra = np.fromiter(rate_data, dtype=float, count=len(rate_data))
                                    n = min(len(gt), len(ra))
                                    if n == 0:
                                        continue
                                    g_idx = int(np.searchsorted(gt[:n], x))
                                    g_idx = min(max(g_idx, 0), n - 1)
                                    color = self._GAS_COLORS.get(gas, "#FFFFFF")
                                    parts.append(
                                        f"<span style='color:{color}'><b>{gas} RoR</b></span>: "
                                        f"{float(ra[g_idx]):.3E} {self._display_unit}/s"
                                    )
                        else:
                            # Single overall dP/dt on the left axis.
                            if pa.size > 1:
                                dt = np.diff(ta)
                                dp = np.diff(pa)
                                rates = np.concatenate([[0.0], np.divide(dp, np.maximum(dt, 1e-9))])
                            else:
                                rates = np.zeros_like(pa)
                            ror_val = float(rates[idx])
                            parts.append(
                                f"<span style='color:#43C5FF'><b>Rate of Rise</b></span>: "
                                f"{ror_val:.3E} {self._display_unit}/s"
                            )
                        # Right axis = absolute OPG pressure (dashed blue curve).
                        parts.append(
                            f"<span style='color:#43C5FF'><b>OPG Pressure (right axis)</b></span>: "
                            f"{p_val:.3E} {self._display_unit}"
                        )
                    else:
                        # Left axis = pressure (or partial pressures for gas plot).
                        p_label = "OPG Pressure"
                        parts.append(
                            f"<span style='color:#43C5FF'><b>{p_label}</b></span>: "
                            f"{p_val:.3E} {self._display_unit}"
                        )

                # Show individual gas values for the Residual Gas Detection plot.
                # Left axis = partial pressures per gas; right dashed curve = OPG pressure.
                if plot is self._gas_plot and self._gas_t:
                    gt = np.array(self._gas_t, dtype=float)
                    for gas in sorted(self._selected_gases()):
                        gdata = self._gas_partial_mbar[gas]
                        if not gdata:
                            continue
                        ga = np.array(gdata, dtype=float)
                        idx = int(np.searchsorted(gt, x))
                        idx = min(max(idx, 0), len(gt) - 1)
                        val_disp = convert_pressure(float(ga[idx]), "mbar", self._display_unit)
                        color = self._GAS_COLORS.get(gas, "#FFFFFF")
                        parts.append(
                            f"<span style='color:{color}'><b>{gas}</b></span>: {val_disp:.3E} {self._display_unit}"
                        )
                    # Right-axis curve on the gas plot is the OPG absolute pressure (dashed blue).
                    if self._trend_t and self._trend_p:
                        ta2 = np.array(self._trend_t, dtype=float)
                        pa2 = np.array(self._trend_p, dtype=float)
                        g_idx = int(np.searchsorted(ta2, x))
                        g_idx = min(max(g_idx, 0), len(ta2) - 1)
                        parts.append(
                            f"<span style='color:#43C5FF'><b>OPG Pressure (right axis)</b></span>: "
                            f"{float(pa2[g_idx]):.3E} {self._display_unit}"
                        )

        if parts:
            self._studio_value_bar.setText("  |  ".join(parts))
            self._studio_value_bar.show()
        else:
            self._studio_value_bar.hide()

    @staticmethod
    def _spectrum_mode_description(mode: SpectrumMode) -> str:
        return {
            SpectrumMode.AUTO: (
                "Live Data — displays only the current spectrum data returned by the gauge."
            ),
            SpectrumMode.AIR_LEAK: (
                "Air Leak — atmosphere ingress simulation. "
                "N₂ 72%, O₂ 20%, Ar 3%, H₂O 4% baseline; peaks evolve as outgassing grows over time."
            ),
            SpectrumMode.WATER_LEAK: (
                "Water Leak — humid atmosphere ingress. "
                "H₂O/OH dominate; thermal dissociation produces H₂ over time."
            ),
            SpectrumMode.HELIUM_LEAK: (
                "Helium Leak — He tracer simulation. "
                "He dominates (~78%); residual N₂/O₂ diminish as He fills the chamber."
            ),
            SpectrumMode.HYDROCARBON_BACKSTREAM: (
                "Hydrocarbon Backstream — pump oil vapour contamination. "
                "CH₄ and H₂O initially high; CO grows as decomposition proceeds."
            ),
        }.get(mode, mode.value)

    def _on_spectrum_range_manual(self) -> None:
        self._spectrum_user_zoomed = True
        self._spectrum_reset_btn.show()

    def _on_spectrum_reset_view(self) -> None:
        self._spectrum_user_zoomed = False
        self._spectrum_reset_btn.hide()
        if self._spectrum_plot is not None:
            self._spectrum_plot.enableAutoRange()

    def _on_spectrum_mode_changed(self, index: int) -> None:
        mode = self._spectrum_mode_combo.itemData(index)
        if isinstance(mode, SpectrumMode):
            self._spectrum_mode = mode
            self._spectrum_mode_desc.setText(self._spectrum_mode_description(mode))
            self._update_spectrum_plot()

    def _on_analysis_mode_changed(self, mode: str) -> None:
        if mode not in self._ANALYSIS_MODES:
            mode = "Raw Spectrum"
        self._analysis_mode = mode
        detail = {
            "Raw Spectrum": "Polling SPEC state/counts and rendering the visible/near-UV spectrum.",
            "Rate of Rise": "Polling RoR state/counts while plotting dP/dt from live pressure.",
            "Residual Gas Detection": "Polling RGD state/counts and tracking selected gas signatures.",
            "Advanced Analysis": "Compare two pressure sources and correlate their delta with a selected OPG gas signal.",
        }[mode]
        self._mode_status.setText(detail)
        advanced = mode == "Advanced Analysis"
        for widget in (
            self._compare_a_label,
            self._compare_a_combo,
            self._compare_b_label,
            self._compare_b_combo,
            self._correlation_gas_label,
            self._correlation_gas_combo,
        ):
            if widget is not None:
                widget.setVisible(advanced)
        self._sync_chart_visibility()
        self._refresh_mode_plots()
        self._ensure_opg_algorithm_active(force=True)

    def _sync_chart_visibility(self) -> None:
        any_gas_selected = bool(self._selected_gases())
        for name, widget in self._chart_boxes.items():
            if self._analysis_mode == "Residual Gas Detection":
                if name == "Raw Spectrum":
                    widget.setVisible(not any_gas_selected)
                elif name == "Residual Gas Detection":
                    widget.setVisible(any_gas_selected)
                else:
                    widget.setVisible(False)
            elif name == "Residual Gas Detection":
                widget.setVisible(self._analysis_mode == "Advanced Analysis" and any_gas_selected)
            else:
                widget.setVisible(self._analysis_mode == name)

    def _refresh_mode_plots(self) -> None:
        # Coalesce repeated requests inside the same event-loop tick to avoid
        # O(N_gases * 4 charts) redraws per pressure/spectrum sample.
        if self._refresh_pending:
            return
        self._refresh_pending = True
        self._refresh_timer.start()

    def _do_refresh_mode_plots(self) -> None:
        self._refresh_pending = False
        # Only refresh charts that are currently visible. With many tracked
        # gases enabled, redrawing every chart per tick stalls the GUI
        # thread because each call iterates over the full (unbounded)
        # session history. _sync_chart_visibility hides everything except
        # the chart for the active analysis mode (and the gas chart when
        # gases are tracked), so honour that here.
        trend_box = self._chart_boxes.get("Rate of Rise")
        spectrum_box = self._chart_boxes.get("Raw Spectrum")
        gas_box = self._chart_boxes.get("Residual Gas Detection")
        advanced_box = self._chart_boxes.get("Advanced Analysis")
        if trend_box is None or trend_box.isVisible():
            self._refresh_trend()
        if spectrum_box is None or spectrum_box.isVisible():
            self._refresh_spectrum_gas_markers()
        if gas_box is None or gas_box.isVisible():
            self._refresh_gas_plot()
        if advanced_box is None or advanced_box.isVisible():
            self._refresh_advanced_plot()

    def _metric_card(self, title: str, value: QLabel) -> QFrame:
        theme = current_theme(self)
        box = QFrame()
        box.setStyleSheet(
            f"QFrame {{ background: {theme.panel}; border: 1px solid {theme.border}; border-radius: 8px; }}"
        )
        self._section_frames.append(box)
        v = QVBoxLayout(box)
        v.setContentsMargins(10, 8, 10, 8)
        v.setSpacing(2)
        t = QLabel(title)
        t.setStyleSheet(f"font-size: 11px; color: {theme.muted};")
        value.setStyleSheet(f"font-size: 16px; font-weight: 700; color: {theme.text};")
        self._metric_title_labels.append(t)
        self._metric_value_labels.append(value)
        v.addWidget(t)
        v.addWidget(value)
        return box

    def _on_action(self, command: str) -> None:
        if command == "_poll_mode":
            self._ensure_opg_algorithm_active(force=True)
            for cmd in self._POLL_GROUPS.get(self._analysis_mode, ("pressure",)):
                self._send_query(cmd)
            return
        if command == "_snapshot":
            for cmd in self._safe_snapshot_commands():
                self._send_query(cmd)
            return
        if command == "_export_opg_csv":
            self._export_opg_csv()
            return
        if command == "_plasma_on":
            self._send_write("plasma_enable", 1)
            # Track our intent so the response handler can auto-start the
            # spectrum even if the device's reply value is ambiguous.
            self._pending_plasma_target = 1
            return
        if command == "_plasma_off":
            self._send_write("plasma_enable", 0)
            self._pending_plasma_target = 0
            return
        self._send_query(command)

    def _safe_snapshot_commands(self) -> tuple[str, ...]:
        return tuple(
            cmd for cmd in (
                "pressure",
                "product_name",
                "software_version",
                "bootloader_version",
                "serial_number",
                "manufacturer_name",
                "error_status",
                "error_count",
                "plasma_state",
                "spectrometer_pixel_count",
                "spec_record",
                "operating_mode",
                "spec_state",
                "spec_record_count",
                "ror_state",
                "ror_record_count",
                "ror_record",
                "rgd_state",
                "rgd_record_count",
                "rgd_record",
                "analog_output_mode",
                "analog_output_voltage",
            ) if cmd in self._spec.commands
        )

    def _send_query(self, command: str) -> None:
        if command in self._RECORD_COMMANDS:
            self._send_record_query(command)
            return
        self._send_command(command)

    def _active_record_command(self) -> str:
        return {
            "Raw Spectrum": "spec_record",
            "Rate of Rise": "ror_record",
            "Residual Gas Detection": "rgd_record",
            "Advanced Analysis": "spec_record",
        }.get(self._analysis_mode, "spec_record")

    def _ensure_opg_algorithm_active(self, *, force: bool = False) -> None:
        target = {
            "Raw Spectrum": "spec_enable",
            "Rate of Rise": "ror_enable",
            "Residual Gas Detection": "rgd_enable",
            "Advanced Analysis": "spec_enable",
        }.get(self._analysis_mode)
        if target is None or target not in self._spec.commands:
            return
        now = time.monotonic()
        if (
            not force
            and self._active_opg_algorithm == target
            and self._last_opg_activation_t is not None
            and now - self._last_opg_activation_t < 30.0
        ):
            return
        if "all_algorithms_off" in self._spec.commands and self._active_opg_algorithm != target:
            self._send_write("all_algorithms_off", 0)
        self._send_write(target, 1)
        self._active_opg_algorithm = target
        self._last_opg_activation_t = now
        self._last_spec_request_t = None
        for record_command in self._opg_record_counts:
            self._opg_record_counts[record_command] = None
        self._emit_opg_spectrum_diag(
            f"OPG550 activated {target} for {self._analysis_mode}; polling state/count before record reads"
        )

    def _request_live_spec_if_due(self) -> None:
        if self._spectrum_mode is not SpectrumMode.AUTO:
            return
        record_command = self._active_record_command()
        if record_command not in self._spec.commands:
            return
        now = time.monotonic()
        if self._last_spec_request_t is not None and now - self._last_spec_request_t < 2.0:
            return
        self._last_spec_request_t = now
        self._ensure_opg_algorithm_active()
        count_command = {
            "spec_record": "spec_record_count",
            "ror_record": "ror_record_count",
            "rgd_record": "rgd_record_count",
        }.get(record_command)
        state_command = {
            "spec_record": "spec_state",
            "ror_record": "ror_state",
            "rgd_record": "rgd_state",
        }.get(record_command)
        if get_opg_spectrum_verbose_diagnostics() and state_command in self._spec.commands:
            self._send_command(state_command)
        if count_command in self._spec.commands:
            self._send_command(count_command)
        known_count = self._opg_record_counts.get(record_command)
        if known_count is not None and known_count <= 0:
            self._emit_opg_spectrum_diag(
                f"OPG550 {record_command} skipped because record_count=0; waiting for algorithm capture"
            )
            return
        self._send_query(record_command)

    def _send_record_query(self, command: str) -> None:
        if command not in self._spec.commands:
            return
        protocol = getattr(self._worker, "_protocol", None)
        builder = getattr(protocol, "build_read_request", None)
        if not callable(builder):
            self._send_command(command)
            return
        try:
            frame = builder(command)
            self._emit_opg_spectrum_diag(
                self._format_record_tx_diag(command, frame)
            )
            self._worker.send_terminal_command(frame, command)
        except Exception:
            logger.exception("OPG550 panel record query failed for %s", command)
            self._emit_opg_spectrum_diag(f"OPG550 {command} TX failed while building/sending request")

    def _emit_opg_spectrum_diag(self, message: str) -> None:
        if not get_opg_spectrum_verbose_diagnostics():
            return
        signal = getattr(self._worker, "terminal_response", None)
        if signal is None:
            return
        signal.emit(TerminalEntry(
            request=b"",
            response=message.encode("ascii", errors="replace"),
            timestamp=datetime.now(tz=timezone.utc),
            command="__opg_spectrum_diag__",
            diagnostic=True,
        ))

    def _format_record_tx_diag(self, command: str, frame: bytes) -> str:
        request_data = b""
        protocol = getattr(self._worker, "_protocol", None)
        params = getattr(protocol, "_params", {}) if protocol is not None else {}
        command_spec = params.get(command, {}) if isinstance(params, dict) else {}
        raw_request = command_spec.get("request_data") if isinstance(command_spec, dict) else None
        try:
            request_data = bytes(int(value) & 0xFF for value in raw_request or [])
        except (TypeError, ValueError):
            request_data = b""
        return (
            f"OPG550 {command} TX "
            f"request_data={self._hex_bytes(request_data) or '(empty)'} "
            f"frame_len={len(frame)} frame_hex={self._hex_bytes(frame)}"
        )

    def _format_record_rx_diag(self, entry, parsed=None, parse_error: Exception | None = None) -> str:
        command = entry.command or "record"
        parts = [
            f"OPG550 {command} RX",
            f"response_len={len(entry.response or b'')}",
            f"response_hex={self._hex_bytes(entry.response or b'') or '(empty)'}",
        ]
        if entry.error:
            parts.append(f"terminal_error={entry.error}")
            return " ".join(parts)
        if not entry.response:
            parts.append("result=no_response")
            return " ".join(parts)
        if parse_error is not None:
            parts.append(f"parse_exception={parse_error}")
            return " ".join(parts)
        if parsed is None:
            parts.append("parse=not_attempted")
            return " ".join(parts)
        if not parsed.success:
            parts.append(f"parse_fail={parsed.error or 'unknown'}")
            return " ".join(parts)

        pixel_data = parsed.extra.get("pixel_data") if parsed.extra else None
        raw_array = parsed.extra.get("raw_array") if parsed.extra else None
        if pixel_data is not None:
            pixels = list(pixel_data)
            nonzero = sum(1 for value in pixels if int(value) != 0)
            first = pixels[:8]
            last = pixels[-8:] if len(pixels) >= 8 else pixels
            parts.extend([
                "parse=ok",
                f"pixels={len(pixels)}",
                f"raw_values={len(raw_array) if raw_array is not None else 'n/a'}",
                f"nonzero={nonzero}",
                f"min={min(pixels, default=0)}",
                f"max={max(pixels, default=0)}",
                f"first8={first}",
                f"last8={last}",
            ])
            if nonzero == 0:
                parts.append("all_zero=true")
            if parsed.extra:
                if "record_id" in parsed.extra:
                    parts.append(f"record_id={parsed.extra.get('record_id')}")
                if "total_pressure_mbar" in parsed.extra:
                    parts.append(f"pressure_mbar={float(parsed.extra.get('total_pressure_mbar', 0.0)):.6g}")
                if "pressure_rise_mtorr_per_min" in parsed.extra:
                    parts.append(f"pressure_rise_mtorr_min={float(parsed.extra.get('pressure_rise_mtorr_per_min', 0.0)):.6g}")
                if "partial_pressures" in parsed.extra:
                    partials = parsed.extra.get("partial_pressures") or []
                    parts.append(f"partial_pressures={list(partials)[:10]}")
            return " ".join(parts)

        extra_keys = sorted(parsed.extra.keys()) if parsed.extra else []
        parts.extend([
            "parse=ok_no_pixel_data",
            f"formatted={parsed.formatted or '(empty)'}",
            f"value={parsed.value if parsed.value is not None else '(none)'}",
            f"extra_keys={extra_keys}",
        ])
        return " ".join(parts)

    @staticmethod
    def _hex_bytes(data: bytes | bytearray) -> str:
        return bytes(data).hex(" ").upper()

    @staticmethod
    def _pixel_data_has_signal(pixel_data: object) -> bool:
        try:
            return any(float(value) != 0.0 for value in pixel_data)  # type: ignore[union-attr]
        except (TypeError, ValueError):
            return False

    def _send_write(self, command: str, value: object) -> None:
        self._send_command(command, value)

    def _send_command(self, command: str, value: object | None = None) -> None:
        if command not in self._spec.commands:
            return
        try:
            protocol = getattr(self._worker, "_protocol", None)
            if protocol is None:
                return
            frame = protocol.build_request(command, value)
            self._worker.send_terminal_command(frame, command)
        except Exception:
            logger.exception("OPG550 panel query failed for %s", command)

    def on_reading(self, reading: DeviceReading) -> None:
        if reading.command == "pressure":
            self._update_pressure(reading.value, reading.unit, reading.timestamp_mono, reading.timestamp_wall)
        elif reading.command == "temperature":
            self._update_temperature(reading.value)

    def on_terminal_response(self, entry) -> None:
        command = entry.command or ""
        if command not in self._spec.commands:
            return
        if entry.error:
            if command in self._RECORD_COMMANDS:
                self._emit_opg_spectrum_diag(self._format_record_rx_diag(entry))
            if command == "error_status":
                self._err_value.setText(f"ERR: {entry.error}")
            return
        if not entry.response:
            if command in self._RECORD_COMMANDS:
                self._emit_opg_spectrum_diag(self._format_record_rx_diag(entry))
            return
        protocol = getattr(self._worker, "_protocol", None)
        if protocol is None:
            return
        try:
            parsed = protocol.parse_response(entry.response, command)
        except Exception as exc:
            logger.exception("OPG550 response decode failed for %s", command)
            if command in self._RECORD_COMMANDS:
                self._emit_opg_spectrum_diag(self._format_record_rx_diag(entry, parse_error=exc))
                self._last_spec_request_t = None
            return
        if command in self._RECORD_COMMANDS:
            self._emit_opg_spectrum_diag(self._format_record_rx_diag(entry, parsed=parsed))
        if not parsed.success:
            if command == "error_status":
                self._err_value.setText(parsed.error or "Decode failure")
            elif command in self._RECORD_COMMANDS:
                self._last_spec_request_t = None
            return

        if command == "software_version":
            self._fw_value.setText(parsed.formatted or str(parsed.value or "-"))
        elif command == "serial_number":
            self._sn_value.setText(parsed.formatted or str(parsed.value or "-"))
        elif command == "error_status":
            msg = parsed.formatted or "OK"
            self._err_value.setText(msg)
        elif command == "plasma_state":
            self._plasma_status.setText(f"Plasma: {parsed.formatted or str(parsed.value or '-')}")
            try:
                if parsed.value is not None:
                    self._last_plasma_state = int(parsed.value)
            except (TypeError, ValueError):
                pass
        elif command == "plasma_enable":
            self._plasma_status.setText("Plasma command accepted; reading state...")
            self._send_query("plasma_state")
            self._last_spec_request_t = None
            self._request_live_spec_if_due()
            # If the user just turned the plasma ON, automatically activate the
            # configured live-spectrum algorithm so they don't have to press
            # "Start spec" manually.
            try:
                target = self._pending_plasma_target
                self._pending_plasma_target = None
                if target == 1 or (parsed.value is not None and int(parsed.value) >= 1):
                    self._ensure_opg_algorithm_active(force=True)
                    if not self._live_spec_timer.isActive():
                        self._live_spec_timer.start()
            except (TypeError, ValueError):
                pass
        elif command in {"spec_enable", "ror_enable", "rgd_enable"}:
            state_command = {
                "spec_enable": "spec_state",
                "ror_enable": "ror_state",
                "rgd_enable": "rgd_state",
            }.get(command)
            count_command = {
                "spec_enable": "spec_record_count",
                "ror_enable": "ror_record_count",
                "rgd_enable": "rgd_record_count",
            }.get(command)
            if state_command in self._spec.commands:
                self._send_query(state_command)
            if count_command in self._spec.commands:
                self._send_query(count_command)
        elif command == "pressure" and parsed.value is not None:
            self._update_pressure(float(parsed.value), parsed.unit or "mbar", None)
        elif command in {"spec_record_count", "ror_record_count", "rgd_record_count"} and parsed.value is not None:
            record_command = {
                "spec_record_count": "spec_record",
                "ror_record_count": "ror_record",
                "rgd_record_count": "rgd_record",
            }.get(command)
            if record_command is not None:
                self._opg_record_counts[record_command] = int(parsed.value)
        elif parsed.extra.get("pixel_data"):
            pixel_data = parsed.extra["pixel_data"]
            if self._pixel_data_has_signal(pixel_data):
                self._ingest_live_spectrum(pixel_data)
            elif command in self._RECORD_COMMANDS:
                self._emit_opg_spectrum_diag(
                    f"OPG550 {command} pixel payload is all zero; keeping plot unchanged"
                )
        if command == "rgd_record" and parsed.extra.get("partial_pressures"):
            self._apply_rgd_partial_pressures(parsed.extra)
        if command == "ror_record" and parsed.extra.get("pressure_rise_mtorr_per_min") is not None:
            self._mode_status.setText(
                f"RoR active; pressure rise {float(parsed.extra['pressure_rise_mtorr_per_min']):.4g} mTorr/min."
            )

        if command in self._telemetry_rows:
            if parsed.formatted:
                text = parsed.formatted
            elif parsed.value is not None:
                suffix = f" {parsed.unit}" if parsed.unit else ""
                text = f"{parsed.value:.6g}{suffix}"
            else:
                text = "OK"
            self._telemetry_rows[command].setText(text)

    def _apply_rgd_partial_pressures(self, extra: dict[str, object]) -> None:
        partials_obj = extra.get("partial_pressures")
        if not isinstance(partials_obj, (list, tuple)):
            return
        gas_order = ("H2", "He", "N2", "O2", "Ar", "NH", "OH", "CH", "CO", "Fluor")
        partial_map: dict[str, float] = {}
        for gas, value in zip(gas_order, partials_obj):
            try:
                partial_map[gas] = max(float(value), 0.0)
            except (TypeError, ValueError):
                continue
        pressure = extra.get("total_pressure_mbar", self._last_pressure_mbar)
        try:
            pressure_mbar = max(float(pressure), 1e-30)
        except (TypeError, ValueError):
            pressure_mbar = max(float(self._last_pressure_mbar or 0.0), 1e-30)
        self._latest_gas_pct = {gas: 0.0 for gas in self._GASES}
        for gas, partial in partial_map.items():
            mapped = "CH4" if gas == "CH" else gas
            if mapped in self._latest_gas_pct:
                self._latest_gas_pct[mapped] = min(100.0, max(0.0, partial / pressure_mbar * 100.0))
        visible = [
            f"{gas} {convert_pressure(partial, 'mbar', self._display_unit):.2E} {self._display_unit}"
            for gas, partial in partial_map.items()
            if gas in self._latest_gas_pct or gas == "CH"
        ]
        if visible:
            self._molecule_label.setText("RGD partial pressures: " + ", ".join(visible[:5]))
        if self._last_pressure_mbar is None:
            self._last_pressure_mbar = pressure_mbar
        self._append_gas_history()
        self._refresh_mode_plots()

    def set_display_unit(self, unit: str) -> None:
        if unit not in SUPPORTED_UNITS or unit == self._display_unit:
            return
        old = self._display_unit
        self._display_unit = unit
        txt = self._pressure_value.text()
        try:
            value = float(txt.split()[0])
        except (ValueError, IndexError):
            value = None
        if value is not None:
            converted = convert_pressure(value, old, unit)
            self._pressure_value.setText(f"{converted:.4E} {unit}")
        if self._trend_plot is not None:
            self._trend_plot.getPlotItem().getAxis("right").setLabel(f"Pressure ({unit})", color="#43C5FF")
            # Update RoR y-label if currently in Rate of Rise mode
            if self._analysis_mode == "Rate of Rise":
                self._trend_plot.setLabel("left", "Rate of rise", units=f"{unit}/s")
        if self._spectrum_plot is not None:
            self._spectrum_plot.getPlotItem().getAxis("right").setLabel(f"Pressure ({unit})", color="#43C5FF")
        if self._gas_plot is not None:
            self._gas_plot.getPlotItem().getAxis("right").setLabel(f"Pressure ({unit})", color="#43C5FF")
            self._gas_plot.setLabel("left", "Partial pressure", units=unit)
        if self._advanced_plot is not None:
            self._advanced_plot.setLabel("left", f"Pressure ({unit})", units="")
            # Update advanced right axis partial pressure label
            if self._advanced_plot is not None and self._correlation_gas_combo is not None:
                gas = self._correlation_gas_combo.currentData() or "OH"
                self._advanced_plot.getPlotItem().getAxis("right").setLabel(
                    f"{gas} partial pressure", units=unit, color="#FF5B5B"
                )
        if self._trend_p:
            self._trend_p = deque(
                [convert_pressure(v, old, unit) for v in self._trend_p],
            )
            self._refresh_trend()
        for peer in self._peer_pressures.values():
            values: deque = peer["p"]  # type: ignore[assignment]
            peer_unit = str(peer.get("unit", old))
            peer["p"] = deque(
                [convert_pressure(v, peer_unit, unit) for v in values],
            )
            peer["unit"] = unit
        self._refresh_mode_plots()

    def _update_pressure(
        self,
        value: float | None,
        unit: str,
        timestamp_mono: float | None,
        timestamp_wall: datetime | None = None,
    ) -> None:
        if value is None:
            return
        value_display = float(value)
        if unit in SUPPORTED_UNITS:
            value_display = float(convert_pressure(float(value), unit, self._display_unit))
            value_mbar = float(convert_pressure(float(value), unit, "mbar"))
        else:
            value_mbar = float(value)

        # Spike rejection: the OPG550 can momentarily report the raw Pirani
        # saturation value (~1E-4 mbar) during plasma state transitions while
        # the optical measurement is still warming up.  If the new reading is
        # more than 2 orders of magnitude above the rolling median of the last
        # five samples, discard it as a transient artifact.
        if len(self._pressure_mbar_window) >= 3:
            import statistics as _stats
            _median = _stats.median(self._pressure_mbar_window)
            if _median > 1e-12 and value_mbar > _median * 100:
                logger.debug(
                    "OPG550 pressure spike suppressed: %.3E mbar (median %.3E mbar)",
                    value_mbar, _median,
                )
                return
        self._pressure_mbar_window.append(value_mbar)

        self._pressure_value.setText(f"{value_display:.4E} {self._display_unit}")
        quality = self._vacuum_quality(value_mbar)
        self._pressure_quality.setText(f"Vacuum quality: {quality}")
        self._vacuum_bar.setValue(self._vacuum_score(value_mbar))

        if timestamp_mono is None:
            if self._trend_t:
                t_rel = self._trend_t[-1] + 1.0
            else:
                t_rel = 0.0
        else:
            if self._trend_t0 is None:
                self._trend_t0 = timestamp_mono
            t_rel = timestamp_mono - self._trend_t0
        self._trend_t.append(float(t_rel))
        self._trend_p.append(max(value_display, 1e-12))
        self._store_peer_pressure("_opg_self", "This OPG550", t_rel, value_display, self._display_unit)
        # Coalesce all four per-sample plot refreshes through the 120 ms
        # QTimer in _refresh_mode_plots so high-rate pressure streams from
        # peer gauges (e.g. CDG at ~125 Hz) don't pile up redraws on the GUI
        # thread.
        self._refresh_mode_plots()
        self._update_spectrum_plot(
            value_mbar=value_mbar,
            timestamp_mono=timestamp_mono,
            timestamp_wall=timestamp_wall,
        )

    def _ingest_live_spectrum(self, pixel_data: object) -> None:
        try:
            y = np.asarray(list(pixel_data), dtype=float)
        except (TypeError, ValueError):
            return
        if y.size < 2:
            return
        peak = float(np.max(y)) if y.size else 0.0
        if peak > 0:
            y = y / peak
        x = np.linspace(303.05, 876.07, y.size, dtype=float)
        self._live_spectrum_x = x
        self._live_spectrum_y = y
        if self._last_pressure_mbar is not None:
            self._update_spectrum_plot(
                value_mbar=self._last_pressure_mbar,
                timestamp_mono=self._last_pressure_t or time.monotonic(),
            )
            return
        self._plot_live_spectrum_without_pressure(x, y)

    def _plot_live_spectrum_without_pressure(self, x: np.ndarray, y: np.ndarray) -> None:
        if self._spectrum_curve is None:
            return
        self._spectrum_curve.setPen(pg.mkPen("#90D98E", width=2))
        self._spectrum_curve.setBrush(pg.mkBrush(144, 217, 142, 90))
        self._spectrum_curve.setData(x, y)
        if self._spectrum_pressure_curve is not None:
            self._spectrum_pressure_curve.setData([], [])
        self._latest_spectrum_x = np.array(x, dtype=float)
        self._latest_spectrum_y = np.array(y, dtype=float)
        matches = identify_optical_species(
            y.astype(float).tolist(),
            wavelength_min_nm=float(x[0]),
            wavelength_max_nm=float(x[-1]),
            top_k=len(self._GASES),
        )
        if matches:
            total_score = sum(max(m.score, 0.0) for m in matches) or 1.0
            self._latest_gas_pct = {gas: 0.0 for gas in self._GASES}
            for match in matches:
                if match.name in self._latest_gas_pct:
                    self._latest_gas_pct[match.name] = max(0.0, match.score) / total_score * 100.0
            txt = ", ".join(
                f"{m.name} ({self._latest_gas_pct.get(m.name, 0.0):.0f}%)"
                for m in matches[:5]
            )
        else:
            self._latest_gas_pct = {gas: 0.0 for gas in self._GASES}
            txt = "No dominant optical signature"
        self._molecule_label.setText(f"Likely optical gas signatures: {txt}")
        self._refresh_spectrum_gas_markers()
        self._refresh_advanced_plot()

    def on_peer_reading(self, device_id: str, display_name: str, reading: DeviceReading) -> None:
        if reading.value is None or reading.unit not in SUPPORTED_UNITS:
            return
        if self._peer_t0 is None:
            self._peer_t0 = reading.timestamp_mono
        t_rel = reading.timestamp_mono - self._peer_t0
        value = float(convert_pressure(reading.value, reading.unit, self._display_unit))
        source_id, source_name = self._peer_source_label(device_id, display_name, reading.command)
        self._store_peer_pressure(source_id, source_name, t_rel, value, self._display_unit)
        # Coalesce; peer gauges (e.g. CDG) can stream at >100 Hz, and a
        # direct rebuild of the advanced plot per sample makes the UI
        # unresponsive within seconds as the deques grow.
        self._refresh_mode_plots()

    @staticmethod
    def _peer_source_label(device_id: str, display_name: str, command: str) -> tuple[str, str]:
        if not command or command == "pressure":
            return device_id, display_name
        return f"{device_id}\x1f{command}", f"{display_name} - {command_display_name(command)}"

    def _store_peer_pressure(
        self,
        device_id: str,
        display_name: str,
        t_rel: float,
        value: float,
        unit: str,
    ) -> None:
        peer = self._peer_pressures.get(device_id)
        if peer is None:
            # Bound peer history so that high-rate streaming gauges (e.g. a
            # CDG sending ~125 frames/s) don't grow these deques without
            # limit and starve the GUI thread when plots are rebuilt.
            # ~30k samples is roughly 4 minutes at 125 Hz or 8 hours at 1 Hz.
            peer = {
                "name": display_name,
                "unit": unit,
                "t": deque(maxlen=_PEER_PRESSURE_MAXLEN),
                "p": deque(maxlen=_PEER_PRESSURE_MAXLEN),
            }
            self._peer_pressures[device_id] = peer
            self._refresh_peer_combos()
        else:
            peer["name"] = display_name
            peer["unit"] = unit
        peer_t: deque = peer["t"]  # type: ignore[assignment]
        peer_p: deque = peer["p"]  # type: ignore[assignment]
        peer_t.append(float(t_rel))
        peer_p.append(max(float(value), 1e-12))

    def _update_temperature(self, value: float | None) -> None:
        if value is None:
            return
        self._temperature_value.setText(f"{value:.2f} °C")
        self._temperature_bar.setValue(int(max(0.0, min(120.0, value)) * 10.0))

    def _refresh_trend(self) -> None:
        if self._trend_curve is None or not self._trend_t:
            return
        x = np.array(self._trend_t, dtype=float)
        p = np.array(self._trend_p, dtype=float)
        selected = self._selected_gases()

        if self._analysis_mode == "Rate of Rise":
            if selected:
                self._trend_curve.setData([], [])
                self._trend_curve.setVisible(False)
                # Pre-compute the linear scale factor once. Pressure unit
                # conversion is purely multiplicative, so a Python list-comp
                # over the entire history per gas is wasted work that
                # scales with N_selected_gases * N_samples and stalls the
                # GUI thread when many track gases are enabled.
                try:
                    unit_factor = float(convert_pressure(1.0, "mbar", self._display_unit))
                except Exception:
                    unit_factor = 1.0
                gas_t_arr = np.array(self._gas_t, dtype=float)
                for gas, curve in self._trend_gas_curves.items():
                    visible = gas in selected
                    curve.setVisible(visible)
                    if visible:
                        rate_data = np.fromiter(
                            self._gas_rate_mbar_s[gas],
                            dtype=float,
                            count=len(self._gas_rate_mbar_s[gas]),
                        )
                        n = min(len(gas_t_arr), len(rate_data))
                        curve.setData(gas_t_arr[:n], rate_data[:n] * unit_factor)
                    else:
                        curve.setData([], [])
            else:
                rates = np.zeros_like(p)
                if p.size > 1:
                    dt = np.diff(x)
                    dp = np.diff(p)
                    rates[1:] = np.divide(dp, np.maximum(dt, 1e-9))
                self._trend_curve.setVisible(True)
                self._trend_curve.setData(x, rates)
                for curve in self._trend_gas_curves.values():
                    curve.setVisible(False)
                    curve.setData([], [])
            self._trend_plot.setLabel("left", "Rate of rise", units=f"{self._display_unit}/s")
        else:
            self._trend_curve.setVisible(True)
            self._trend_curve.setData(x, p)
            self._trend_plot.setLabel("left", f"Pressure ({self._display_unit})", units="")
            for curve in self._trend_gas_curves.values():
                curve.setVisible(False)
                curve.setData([], [])

        if self._trend_pressure_curve is not None:
            self._trend_pressure_curve.setData(x, p)
            self._clamp_right_view_to_gauge_range(self._trend_right_view)

    def _update_spectrum_plot(
        self,
        *,
        value_mbar: float | None = None,
        timestamp_mono: float | None = None,
        timestamp_wall: datetime | None = None,
    ) -> None:
        if self._spectrum_curve is None:
            return

        p_mbar = self._last_pressure_mbar if value_mbar is None else value_mbar
        if p_mbar is None:
            return

        if timestamp_mono is None:
            timestamp_mono = time.monotonic()

        trend = 0.0
        if self._last_pressure_mbar is not None and self._last_pressure_t is not None:
            dt = max(1e-3, timestamp_mono - self._last_pressure_t)
            trend = (p_mbar - self._last_pressure_mbar) / dt

        elapsed = 0.0
        if self._trend_t0 is not None:
            elapsed = max(0.0, timestamp_mono - self._trend_t0)

        if self._spectrum_mode is SpectrumMode.AUTO:
            self._request_live_spec_if_due()
            if self._live_spectrum_x is None or self._live_spectrum_y is None:
                self._spectrum_curve.setData([], [])
                self._latest_spectrum_x = None
                self._latest_spectrum_y = None
                self._molecule_label.setText("Likely optical gas signatures: waiting for live spectrum data")
                self._last_pressure_mbar = max(p_mbar, 1e-12)
                self._last_pressure_t = float(timestamp_mono)
                self._refresh_advanced_plot()
                return
            x = self._live_spectrum_x
            y = self._live_spectrum_y
            spectrum = y.astype(float).tolist()
            self._spectrum_curve.setPen(pg.mkPen("#90D98E", width=2))
            self._spectrum_curve.setBrush(pg.mkBrush(144, 217, 142, 90))
        else:
            spectrum = simulate_optical_spectrum(
                pressure_mbar=max(p_mbar, 1e-12),
                trend_mbar_per_s=trend,
                elapsed_s=elapsed,
                mode=self._spectrum_mode,
                wavelength_min_nm=303.05,
                wavelength_max_nm=876.07,
                samples=288,
            )
            x = np.linspace(303.05, 876.07, len(spectrum), dtype=float)
            y = np.array(spectrum, dtype=float)
            self._spectrum_curve.setPen(pg.mkPen(self._CLAUDE_ORANGE, width=2))
            self._spectrum_curve.setBrush(pg.mkBrush(217, 119, 47, 80))
        if self._spectrum_user_zoomed:
            self._spectrum_plot.disableAutoRange()
        self._spectrum_curve.setData(x, y)
        if self._spectrum_pressure_curve is not None:
            pressure_display = convert_pressure(max(p_mbar, 1e-12), "mbar", self._display_unit)
            self._spectrum_pressure_curve.setData(x, np.full(len(x), float(pressure_display)))
            self._clamp_right_view_to_gauge_range(self._spectrum_right_view)
        self._latest_spectrum_x = np.array(x, dtype=float)
        self._latest_spectrum_y = np.array(y, dtype=float)

        if p_mbar <= OPG_ANALYSIS_MAX_PRESSURE_MBAR:
            matches = identify_optical_species(
                spectrum,
                wavelength_min_nm=float(x[0]),
                wavelength_max_nm=float(x[-1]),
                top_k=len(self._GASES),
            )
            if matches:
                total_score = sum(max(m.score, 0.0) for m in matches) or 1.0
                self._latest_gas_pct = {
                    gas: 0.0 for gas in self._GASES
                }
                for match in matches:
                    if match.name in self._latest_gas_pct:
                        self._latest_gas_pct[match.name] = max(0.0, match.score) / total_score * 100.0
                txt = ", ".join(
                    f"{m.name} ({self._latest_gas_pct.get(m.name, 0.0):.0f}%)"
                    for m in matches[:5]
                )
            else:
                self._latest_gas_pct = {gas: 0.0 for gas in self._GASES}
                txt = "No dominant optical signature"
            self._molecule_label.setText(f"Likely optical gas signatures: {txt}")
        else:
            self._latest_gas_pct = {gas: 0.0 for gas in self._GASES}
            self._molecule_label.setText(
                "Gas analysis unavailable above "
                f"{OPG_ANALYSIS_MAX_PRESSURE_MBAR:.1E} mbar "
                "(pump down further to enable species identification)"
            )

        self._last_pressure_mbar = max(p_mbar, 1e-12)
        self._last_pressure_t = float(timestamp_mono)
        self._append_gas_history()
        self._capture_opg_export_sample(timestamp_wall)
        self._evaluate_auto_plasma()
        self._refresh_mode_plots()

    def _append_gas_history(self) -> None:
        if not self._trend_t:
            return
        # Don't push an all-zero sample onto the partial-pressure plot. This
        # used to happen on every pressure poll between two genuine spectrum
        # frames (e.g. AUTO-mode requests SPEC every 2 s but pressure polls
        # every 1 s; total pressure briefly above OPG_ANALYSIS_MAX_PRESSURE_MBAR
        # also wipes _latest_gas_pct to zero). The result was the periodic
        # vertical drops to ~0 visible in Tracked Gas Analysis.
        if not any(self._latest_gas_pct.get(gas, 0.0) > 0.0 for gas in self._GASES):
            return
        t_rel = self._trend_t[-1]
        # If we already wrote a sample at this timestamp (same pressure poll
        # firing twice), keep the most recent values rather than appending a
        # duplicate timestamp.
        if self._gas_t and abs(self._gas_t[-1] - float(t_rel)) < 1e-9:
            for gas in self._GASES:
                pct = float(self._latest_gas_pct.get(gas, 0.0))
                partial = max(float(self._last_pressure_mbar or 0.0), 0.0) * pct / 100.0
                if self._gas_pct[gas]:
                    self._gas_pct[gas][-1] = pct
                if self._gas_partial_mbar[gas]:
                    self._gas_partial_mbar[gas][-1] = partial
            return
        self._gas_t.append(float(t_rel))
        for gas in self._GASES:
            pct = float(self._latest_gas_pct.get(gas, 0.0))
            partial = max(float(self._last_pressure_mbar or 0.0), 0.0) * pct / 100.0
            previous_t = self._gas_t[-2] if len(self._gas_t) > 1 else None
            previous_partial = self._gas_partial_mbar[gas][-1] if self._gas_partial_mbar[gas] else None
            rate = 0.0
            if previous_t is not None and previous_partial is not None:
                dt = max(1e-9, float(t_rel) - float(previous_t))
                rate = (partial - float(previous_partial)) / dt
            self._gas_pct[gas].append(pct)
            self._gas_partial_mbar[gas].append(partial)
            self._gas_rate_mbar_s[gas].append(rate)

    def _selected_gases(self) -> set[str]:
        return {gas for gas, check in self._gas_checks.items() if check.isChecked()}

    def _refresh_gas_plot(self) -> None:
        if self._gas_plot is None:
            return
        x = np.array(self._gas_t, dtype=float)
        selected = self._selected_gases()
        # Pre-compute the linear scale factor once per refresh instead of doing
        # a per-sample call to convert_pressure inside a list comprehension.
        # Pressure unit conversion is purely multiplicative, so this is safe.
        try:
            unit_factor = float(convert_pressure(1.0, "mbar", self._display_unit))
        except Exception:
            unit_factor = 1.0
        self._gas_plot.setLabel("left", "Partial pressure", units=self._display_unit)
        for gas, curve in self._gas_curves.items():
            visible = gas in selected
            if visible and self._gas_partial_mbar[gas]:
                raw = np.fromiter(self._gas_partial_mbar[gas], dtype=float, count=len(self._gas_partial_mbar[gas]))
                # Length of raw may differ from x if histories drifted; trim to min.
                n = min(len(x), len(raw))
                curve.setData(x[:n], raw[:n] * unit_factor)
            elif not visible:
                # Skip data update for hidden curves to save time when many
                # gases are tracked but only a few are selected.
                pass
            else:
                curve.setData([], [])
            curve.setVisible(bool(visible))
        if self._gas_pressure_curve is not None:
            self._gas_pressure_curve.setData(
                np.array(self._trend_t, dtype=float),
                np.array(self._trend_p, dtype=float),
            )
            self._clamp_right_view_to_gauge_range(self._gas_right_view)

    def _refresh_spectrum_gas_markers(self) -> None:
        if self._spectrum_plot is None:
            return
        for marker in self._spectrum_gas_markers:
            self._spectrum_plot.removeItem(marker)
        self._spectrum_gas_markers.clear()
        if self._analysis_mode != "Raw Spectrum":
            return
        for gas in sorted(self._selected_gases()):
            color = self._GAS_COLORS.get(gas, "#FFFFFF")
            for wavelength in optical_signature_wavelengths(gas):
                marker = pg.InfiniteLine(
                    pos=wavelength,
                    angle=90,
                    pen=pg.mkPen(color, width=1, style=Qt.PenStyle.DotLine),
                    movable=False,
                )
                marker.setToolTip(f"{gas} {wavelength:g} nm")
                self._spectrum_plot.addItem(marker)
                self._spectrum_gas_markers.append(marker)

    def _capture_opg_export_sample(self, timestamp_wall: datetime | None) -> None:
        if self._latest_spectrum_x is None or self._latest_spectrum_y is None:
            return
        if self._last_pressure_mbar is None:
            return
        if timestamp_wall is None:
            timestamp_wall = datetime.now(tz=timezone.utc)
        elapsed = float(self._trend_t[-1]) if self._trend_t else 0.0
        self._opg_export_samples.append({
            "timestamp": timestamp_wall,
            "time_s": elapsed,
            "pressure_mbar": float(self._last_pressure_mbar),
            "spectrum_x": np.array(self._latest_spectrum_x, dtype=float),
            "spectrum_y": np.array(self._latest_spectrum_y, dtype=float),
            "gas_pct": dict(self._latest_gas_pct),
        })
        # Note: no max-length trim. Per user request, the full session is
        # retained so any export reflects everything captured so far.

    # ------------------------------------------------------------------
    # Per-chart CSV export helpers
    # ------------------------------------------------------------------
    def _ask_csv_path(self, default_name: str, title: str) -> Path | None:
        path, _ = QFileDialog.getSaveFileName(self, title, default_name, "CSV (*.csv)")
        if not path:
            return None
        if not path.lower().endswith(".csv"):
            path += ".csv"
        return Path(path)

    def _export_trend_csv(self) -> None:
        if not self._trend_t:
            QMessageBox.warning(self, "No trend data", "The trend chart has no samples to export yet.")
            return
        path = self._ask_csv_path("OPG550_trend.csv", "Export trend chart CSV")
        if path is None:
            return
        try:
            with path.open("w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(["time_s", f"pressure_{self._display_unit}"])
                for t, p in zip(self._trend_t, self._trend_p):
                    writer.writerow([f"{float(t):.6f}", f"{float(p):.6e}"])
        except Exception as exc:
            logger.exception("Trend CSV export failed")
            QMessageBox.critical(self, "Export failed", str(exc))
            return
        QMessageBox.information(self, "Export complete", f"Saved {len(self._trend_t)} samples to:\n{path}")

    def _export_spectrum_csv(self) -> None:
        if self._latest_spectrum_x is None or self._latest_spectrum_y is None:
            QMessageBox.warning(self, "No spectrum", "No spectrum data is available to export yet.")
            return
        path = self._ask_csv_path("OPG550_spectrum.csv", "Export spectrum CSV")
        if path is None:
            return
        try:
            with path.open("w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(["wavelength_nm", "intensity_counts"])
                for x, y in zip(self._latest_spectrum_x, self._latest_spectrum_y):
                    writer.writerow([f"{float(x):.4f}", f"{float(y):.6e}"])
        except Exception as exc:
            logger.exception("Spectrum CSV export failed")
            QMessageBox.critical(self, "Export failed", str(exc))
            return
        QMessageBox.information(self, "Export complete", f"Saved {len(self._latest_spectrum_x)} points to:\n{path}")

    def _export_gas_csv(self) -> None:
        if not self._gas_t:
            QMessageBox.warning(self, "No tracked-gas data", "No partial-pressure samples are available yet.")
            return
        path = self._ask_csv_path("OPG550_tracked_gases.csv", "Export tracked gases CSV")
        if path is None:
            return
        try:
            with path.open("w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                header = ["time_s"]
                for gas in self._GASES:
                    header.append(f"{gas}_partial_mbar")
                    header.append(f"{gas}_pct")
                writer.writerow(header)
                n = len(self._gas_t)
                for i in range(n):
                    row: list[str] = [f"{float(self._gas_t[i]):.6f}"]
                    for gas in self._GASES:
                        partial = self._gas_partial_mbar[gas][i] if i < len(self._gas_partial_mbar[gas]) else 0.0
                        pct = self._gas_pct[gas][i] if i < len(self._gas_pct[gas]) else 0.0
                        row.append(f"{float(partial):.6e}")
                        row.append(f"{float(pct):.4f}")
                    writer.writerow(row)
        except Exception as exc:
            logger.exception("Gas CSV export failed")
            QMessageBox.critical(self, "Export failed", str(exc))
            return
        QMessageBox.information(self, "Export complete", f"Saved {len(self._gas_t)} samples to:\n{path}")

    def _export_advanced_csv(self) -> None:
        if not self._peer_pressures and not self._trend_t:
            QMessageBox.warning(self, "No advanced data", "The advanced chart has no series to export yet.")
            return
        path = self._ask_csv_path("OPG550_advanced.csv", "Export advanced correlation CSV")
        if path is None:
            return
        try:
            with path.open("w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(["series", "time_s", f"pressure_{self._display_unit}"])
                # OPG own pressure trace (converted to display unit)
                for t, p_disp in zip(self._trend_t, self._trend_p):
                    writer.writerow(["OPG550", f"{float(t):.6f}", f"{float(p_disp):.6e}"])
                for peer in self._peer_pressures.values():
                    name = str(peer.get("name", "peer"))
                    peer_unit = str(peer.get("unit", "mbar"))
                    times = peer["t"]
                    values = peer["p"]
                    for t, p in zip(times, values):
                        # Convert to current display unit for a consistent column.
                        p_disp = convert_pressure(float(p), peer_unit, self._display_unit)
                        writer.writerow([name, f"{float(t):.6f}", f"{float(p_disp):.6e}"])
        except Exception as exc:
            logger.exception("Advanced CSV export failed")
            QMessageBox.critical(self, "Export failed", str(exc))
            return
        QMessageBox.information(self, "Export complete", f"Saved advanced chart data to:\n{path}")

    # ------------------------------------------------------------------
    # Auto-plasma controls
    # ------------------------------------------------------------------
    def _persist_setting(self, key: str, value: object) -> None:
        try:
            QSettings("CustomSerialCommunicator", "CustomSerialCommunicator").setValue(key, value)
        except Exception:
            logger.debug("Failed to persist setting %s", key, exc_info=True)

    def _on_auto_plasma_toggled(self, checked: bool) -> None:
        self._auto_plasma_enabled = bool(checked)
        self._persist_setting("opg/auto_plasma_enabled", self._auto_plasma_enabled)
        # When enabling, evaluate immediately so the user gets a reaction without
        # waiting for the next pressure poll.
        if self._auto_plasma_enabled:
            self._send_query("plasma_state")
            if self._last_pressure_mbar is not None:
                self._evaluate_auto_plasma()

    def _on_auto_plasma_min_changed(self, value: float) -> None:
        self._auto_plasma_min_mbar = float(value)
        self._persist_setting("opg/min_ignition_pressure_mbar", self._auto_plasma_min_mbar)

    def _on_auto_plasma_max_changed(self, value: float) -> None:
        self._auto_plasma_max_mbar = float(value)
        self._persist_setting("opg/max_safe_pressure_mbar", self._auto_plasma_max_mbar)

    def _evaluate_auto_plasma(self) -> None:
        """Decide whether to ignite or extinguish the plasma based on pressure.

        Called from `_update_pressure` after `_last_pressure_mbar` is set.
        Uses a 5 s cooldown between actions to avoid command thrashing if the
        pressure dithers around a threshold.
        """
        if not self._auto_plasma_enabled:
            return
        if "plasma_enable" not in self._spec.commands:
            return
        if self._last_pressure_mbar is None or self._last_plasma_state is None:
            return
        now = time.monotonic()
        if (now - self._auto_plasma_last_action_t) < 5.0:
            return
        p = float(self._last_pressure_mbar)
        # Ignited == 2; "on but not ignited" == 1; off == 0.
        plasma_currently_on = int(self._last_plasma_state) >= 1
        if p > self._auto_plasma_max_mbar and plasma_currently_on:
            self._auto_plasma_last_action_t = now
            self._on_action("_plasma_off")
            self._mode_status.setText(
                f"Auto-plasma: pressure {p:.2e} mbar > max safe "
                f"{self._auto_plasma_max_mbar:.2e} mbar — switching plasma OFF"
            )
        elif p < self._auto_plasma_min_mbar and not plasma_currently_on:
            self._auto_plasma_last_action_t = now
            self._on_action("_plasma_on")
            self._mode_status.setText(
                f"Auto-plasma: pressure {p:.2e} mbar < min ignite "
                f"{self._auto_plasma_min_mbar:.2e} mbar — switching plasma ON"
            )

    # ------------------------------------------------------------------
    def _on_panel_shown(self) -> None:
        """Called when the Spectrum Studio panel becomes the active inner tab.

        Issues a one-time read of plasma_state and pressure so the UI shows
        live values immediately on entry instead of waiting for the next poll.
        """
        try:
            if "pressure" in self._spec.commands:
                self._send_query("pressure")
            if "plasma_state" in self._spec.commands:
                self._send_query("plasma_state")
        except Exception:
            logger.debug("Failed to issue panel-show queries", exc_info=True)
        self._initial_plasma_read_done = True

    def _export_opg_csv(self) -> None:
        if not self._opg_export_samples:
            QMessageBox.warning(self, "No OPG data", "No spectrum samples are available to export yet.")
            return
        export_type = "ror" if self._analysis_mode == "Rate of Rise" else "rgd"
        default_name = "OPG550_RoR.csv" if export_type == "ror" else "OPG550_RGD.csv"
        path, _ = QFileDialog.getSaveFileName(self, "Export OPG CSV", default_name, "CSV (*.csv)")
        if not path:
            return
        if not path.lower().endswith(".csv"):
            path += ".csv"
        try:
            self._write_opg_csv(Path(path), export_type)
        except Exception as exc:
            logger.exception("OPG CSV export failed")
            QMessageBox.critical(self, "Export failed", str(exc))
            return
        QMessageBox.information(self, "Export complete", f"Exported {len(self._opg_export_samples)} OPG samples to:\n{path}")

    def _write_opg_csv(self, path: Path, export_type: str) -> None:
        samples = list(self._opg_export_samples)
        first_ts = samples[0]["timestamp"]
        if not isinstance(first_ts, datetime):
            first_ts = datetime.now(tz=timezone.utc)
        serial = "".join(ch for ch in self._sn_value.text() if ch.isdigit())[-9:].rjust(9, "0")
        bootloader = self._telemetry_rows.get("bootloader_version")
        boot_text = bootloader.text() if bootloader is not None else "03.01.00.0063"
        app_text = self._fw_value.text() if self._fw_value.text() != "-" else "01.00.28.0184"
        if export_type == "ror":
            measurement_type = "RoR Leak Detection Measurement"
            headers, units, block_sizes = self._opg_ror_header(samples[0])
        else:
            measurement_type = "RGD Measurement"
            headers, units, block_sizes = self._opg_rgd_header(samples[0])

        lines = [
            f"Timestamp,{first_ts.strftime('%Y%m%d_%H%M')}",
            f"Measurement Type,{measurement_type}",
            f"serial number,{serial}",
            f"bootloader version,{boot_text}",
            f"application version,{app_text}",
            "",
            ",".join(headers),
            ",".join(units),
        ]
        previous_sample: dict[str, object] | None = None
        for sample in samples:
            values = (
                self._opg_ror_row(sample, previous_sample)
                if export_type == "ror"
                else self._opg_rgd_row(sample)
            )
            lines.append(self._join_opg_blocks(values, block_sizes))
            previous_sample = sample
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _spectrum_values_288(self, sample: dict[str, object], wavelength_min: float, wavelength_max: float) -> tuple[list[float], list[float]]:
        x = np.asarray(sample["spectrum_x"], dtype=float)
        y = np.asarray(sample["spectrum_y"], dtype=float)
        out_x = np.linspace(wavelength_min, wavelength_max, 288, dtype=float)
        out_y = np.interp(out_x, x, y) * 10000.0
        return out_x.tolist(), out_y.tolist()

    def _opg_rgd_header(self, sample: dict[str, object]) -> tuple[list[str], list[str], list[int]]:
        wavelengths, _ = self._spectrum_values_288(sample, 303.05, 876.07)
        species = ["Hydrogen", "Helium", "Nitrogen", "Oxygen", "Argon", "NH", "OH", "CH", "CO", "Fluor"]
        ratios = [
            "391nm N2+ vs  311nm OH", "336nm N2 vs 311nm OH", "391nm N2+ vs 656nm H",
            "336nm N2 vs 656nm H", "391nm N2+ vs 810nm Ar", "777nm O vs 810nm Ar",
            "502nm He vs 336nm N2", "777nm O vs 336nm N2",
        ]
        headers = ["Timestamp", "Time", "TotalPressure", "AnalogOut", "IntegrationTime"]
        headers += [f"{wl:.2f}" for wl in wavelengths]
        headers += species + species + ratios
        units = ["[timestamp]", "[sec]", "[mbar]", "[mV]", "[ms]"]
        units += ["[counts/sec]"] * 288
        units += ["[counts/sec]"] * 10
        units += ["mbar"] + [" mbar"] * 9
        units += ["--"] * 8
        return headers, units, [5, 288, 10, 10, 8]

    def _opg_ror_header(self, sample: dict[str, object]) -> tuple[list[str], list[str], list[int]]:
        wavelengths, _ = self._spectrum_values_288(sample, 305.53, 878.56)
        headers = ["Timestamp", "Time", "TotalPressure", "AnalogOut", "IntegrationTime"]
        headers += [f"{wl:.2f}" for wl in wavelengths]
        headers += ["O2 777nm", "Ar 812nm", "N2 822nm", "N2 870nm", "N2 337nm", "H 656nm", "pressure Rise"]
        units = ["[timestamp]", "[sec]", "[mbar]", "[mV]", "[ms]"]
        units += ["[counts/sec]"] * 288
        units += ["-"] * 6 + ["[mTorr/min]"]
        return headers, units, [5, 288, 6, 1]

    def _opg_rgd_row(self, sample: dict[str, object]) -> list[object]:
        _, spectrum = self._spectrum_values_288(sample, 303.05, 876.07)
        pressure = float(sample["pressure_mbar"])
        gas_pct = sample.get("gas_pct", {})
        if not isinstance(gas_pct, dict):
            gas_pct = {}
        species_pct = [
            float(gas_pct.get("H2", 0.0)), float(gas_pct.get("He", 0.0)), float(gas_pct.get("N2", 0.0)),
            float(gas_pct.get("O2", 0.0)), float(gas_pct.get("Ar", 0.0)), 0.0,
            float(gas_pct.get("OH", 0.0)), float(gas_pct.get("CH4", 0.0)), float(gas_pct.get("CO", 0.0)), 0.0,
        ]
        raw_counts = [pct * 100.0 for pct in species_pct]
        partials = [pressure * pct / 100.0 for pct in species_pct]
        ratios = self._opg_ratio_values(gas_pct)
        return self._opg_common_row(sample) + spectrum + raw_counts + partials + ratios

    def _opg_ror_row(self, sample: dict[str, object], previous_sample: dict[str, object] | None) -> list[object]:
        wavelengths, spectrum = self._spectrum_values_288(sample, 305.53, 878.56)
        lines = [777.0, 812.0, 822.0, 870.0, 337.0, 656.0]
        intensities = [self._interp_line(wavelengths, spectrum, wl) for wl in lines]
        pressure_rise = self._pressure_rise_mtorr_per_min(sample, previous_sample)
        return self._opg_common_row(sample) + spectrum + intensities + [pressure_rise]

    @staticmethod
    def _opg_common_row(sample: dict[str, object]) -> list[object]:
        ts = sample["timestamp"]
        if not isinstance(ts, datetime):
            ts = datetime.now(tz=timezone.utc)
        return [
            ts.strftime("%Y-%m-%d %H:%M:%S.%f"),
            float(sample["time_s"]),
            float(sample["pressure_mbar"]),
            0,
            0.0,
        ]

    @staticmethod
    def _join_opg_blocks(values: list[object], block_sizes: list[int]) -> str:
        fields = [str(value) for value in values]
        blocks: list[str] = []
        pos = 0
        for size in block_sizes:
            block = fields[pos:pos + size]
            blocks.append(", ".join(block))
            pos += size
        return ",".join(blocks)

    @staticmethod
    def _interp_line(wavelengths: list[float], spectrum: list[float], wavelength: float) -> float:
        return float(np.interp([wavelength], np.asarray(wavelengths), np.asarray(spectrum))[0])

    @staticmethod
    def _safe_ratio(num: float, den: float) -> float:
        return float(num / den) if den > 1e-12 else 0.0

    def _opg_ratio_values(self, gas_pct: dict[object, object]) -> list[float]:
        n2 = float(gas_pct.get("N2", 0.0))
        oh = float(gas_pct.get("OH", 0.0))
        h2 = float(gas_pct.get("H2", 0.0))
        ar = float(gas_pct.get("Ar", 0.0))
        o2 = float(gas_pct.get("O2", 0.0))
        he = float(gas_pct.get("He", 0.0))
        return [
            self._safe_ratio(n2, oh), self._safe_ratio(n2, oh), self._safe_ratio(n2, h2),
            self._safe_ratio(n2, h2), self._safe_ratio(n2, ar), self._safe_ratio(o2, ar),
            self._safe_ratio(he, n2), self._safe_ratio(o2, n2),
        ]

    def _pressure_rise_mtorr_per_min(
        self,
        sample: dict[str, object],
        previous_sample: dict[str, object] | None,
    ) -> float:
        if previous_sample is None:
            return 0.0
        dt = float(sample["time_s"]) - float(previous_sample["time_s"])
        if dt <= 0:
            return 0.0
        dp_mbar = float(sample["pressure_mbar"]) - float(previous_sample["pressure_mbar"])
        return dp_mbar * 750.062 * 60.0 / dt

    def _refresh_peer_combos(self) -> None:
        combos = [self._compare_a_combo, self._compare_b_combo]
        if any(combo is None for combo in combos):
            return
        items = [(device_id, str(peer.get("name", device_id))) for device_id, peer in self._peer_pressures.items()]
        items.sort(key=lambda item: item[1])
        for combo in combos:
            if combo is None:
                continue
            current = combo.currentData()
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("-", "")
            for device_id, name in items:
                combo.addItem(name, device_id)
            if current:
                idx = combo.findData(current)
                if idx >= 0:
                    combo.setCurrentIndex(idx)
            combo.blockSignals(False)
        if self._compare_a_combo is not None and self._compare_a_combo.currentIndex() <= 0 and items:
            self._compare_a_combo.setCurrentIndex(1)
        if self._compare_b_combo is not None and self._compare_b_combo.currentIndex() <= 0 and len(items) > 1:
            self._compare_b_combo.setCurrentIndex(2)

    def _refresh_advanced_plot(self) -> None:
        if self._advanced_plot is None:
            return
        source_a = self._compare_a_combo.currentData() if self._compare_a_combo is not None else ""
        source_b = self._compare_b_combo.currentData() if self._compare_b_combo is not None else ""
        peer_a = self._peer_pressures.get(str(source_a)) if source_a else None
        peer_b = self._peer_pressures.get(str(source_b)) if source_b else None

        # Resolve human-readable names for A/B and keep the legend in sync.
        name_a = (
            str(peer_a.get("name", source_a)) if peer_a else
            (self._compare_a_combo.currentText() if self._compare_a_combo is not None else "Source A")
        )
        name_b = (
            str(peer_b.get("name", source_b)) if peer_b else
            (self._compare_b_combo.currentText() if self._compare_b_combo is not None else "Source B")
        )
        if self._advanced_legend is not None and self._advanced_curve_a is not None and self._advanced_curve_b is not None:
            self._advanced_legend.removeItem(self._advanced_curve_a)
            self._advanced_legend.removeItem(self._advanced_curve_b)
            self._advanced_legend.addItem(self._advanced_curve_a, name_a)
            self._advanced_legend.addItem(self._advanced_curve_b, name_b)

        if peer_a is not None and self._advanced_curve_a is not None:
            self._advanced_curve_a.setData(
                np.array(peer_a["t"], dtype=float),
                np.array(peer_a["p"], dtype=float),
            )
        elif self._advanced_curve_a is not None:
            self._advanced_curve_a.setData([], [])

        if peer_b is not None and self._advanced_curve_b is not None:
            self._advanced_curve_b.setData(
                np.array(peer_b["t"], dtype=float),
                np.array(peer_b["p"], dtype=float),
            )
        elif self._advanced_curve_b is not None:
            self._advanced_curve_b.setData([], [])

        if peer_a is not None and peer_b is not None:
            latest_a = float(peer_a["p"][-1]) if peer_a["p"] else math.nan
            latest_b = float(peer_b["p"][-1]) if peer_b["p"] else math.nan
            if math.isfinite(latest_a) and math.isfinite(latest_b):
                delta = latest_a - latest_b
                self._delta_label.setText(
                    f"<span style='color:#43C5FF'><b>{name_a}</b></span>"
                    f" \u2212 <span style='color:#E8D74C'><b>{name_b}</b></span>:  "
                    f"\u0394 = {delta:+.4E} {self._display_unit}  |  "
                    f"ratio = {latest_a / max(latest_b, 1e-30):.4g}"
                )
            else:
                self._delta_label.setText("Delta: -")
        else:
            self._delta_label.setText("Delta: select two pressure sources")

        gas = self._correlation_gas_combo.currentData() if self._correlation_gas_combo is not None else "OH"
        # Pre-compute linear unit factor; pressure conversion is purely
        # multiplicative, so vectorized numpy ops avoid an O(N) Python
        # list-comp per refresh.
        try:
            unit_factor = float(convert_pressure(1.0, "mbar", self._display_unit))
        except Exception:
            unit_factor = 1.0
        gas_values: np.ndarray | None = None
        if self._advanced_gas_curve is not None and gas in self._gas_partial_mbar:
            partial = self._gas_partial_mbar[str(gas)]
            gas_values = np.fromiter(partial, dtype=float, count=len(partial)) * unit_factor
            gas_t_arr = np.array(self._gas_t, dtype=float)
            n = min(len(gas_t_arr), len(gas_values))
            self._advanced_gas_curve.setData(gas_t_arr[:n], gas_values[:n])
            if self._advanced_plot is not None:
                axis = self._advanced_plot.getPlotItem().getAxis("right")
                axis.setLabel(
                    f"{gas} partial pressure (linear scale, right axis)",
                    units=self._display_unit,
                    color="#FF5B5B",
                )

        if self._advanced_right_view is not None:
            if gas_values is not None and gas_values.size:
                ymax = float(gas_values.max())
            else:
                ymax = 1e-12
            self._advanced_right_view.setYRange(0, max(ymax * 1.2, 1e-12))

    @staticmethod
    def _vacuum_score(pressure_mbar: float) -> int:
        p = max(pressure_mbar, 1e-12)
        # Map 1e+3..1e-9 mbar to 0..1000 in log space.
        score = (3.0 - math.log10(p)) / 12.0
        return int(max(0.0, min(1.0, score)) * 1000)

    @staticmethod
    def _vacuum_quality(pressure_mbar: float) -> str:
        p = max(pressure_mbar, 0.0)
        if p >= 1e0:
            return "Rough"
        if p >= 1e-2:
            return "Medium"
        if p >= 1e-4:
            return "Fine"
        if p >= 1e-6:
            return "High"
        return "Ultra-high"


# ---------------------------------------------------------------------------
# GaugeTab
# ---------------------------------------------------------------------------

class GaugeTab(QWidget):
    """One gauge's live view, command displays, settings, and terminal tabs."""

    #: Emitted when the user picks a new colour via the header swatch.
    #: Payload: (device_id, hex_color)
    color_changed = pyqtSignal(str, str)
    poll_commands_changed = pyqtSignal(str, object)

    def __init__(
        self,
        device_id: str,
        spec: DeviceSpec,
        worker,
        parent: QWidget | None = None,
        *,
        is_simulated: bool = False,
        display_name: str | None = None,
        color: str = "#4C9BE8",
    ) -> None:
        super().__init__(parent)
        self.device_id = device_id
        self._spec = spec
        self.worker = worker
        self.is_simulated = bool(is_simulated)
        self.display_name = display_name or device_id
        self._display_unit = get_display_unit()
        self._gauge_color: str = color

        self._all_readings: list[DeviceReading] = []
        self._opg_panel: OPG550SpectrumStudio | None = None
        self._command_panel: CommandDisplayPanel | None = None

        self._build_ui()
        # Push current display-unit into the plot so its axes match the table.
        self._plot_panel.set_display_unit(self._display_unit)
        self.apply_theme()

        # Wire terminal/settings signal fan-out
        worker.terminal_response.connect(self._on_terminal_response)
        # Refresh when the user changes display units in Settings.
        display_signals.units_changed.connect(self._on_units_changed)

    # ------------------------------------------------------------------
    # Build UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(4)

        # Simulation banner (visible only for simulated gauges)
        if self.is_simulated:
            banner = QLabel(
                f"⊕  SIMULATED — {self.display_name}"
            )
            banner.setTextFormat(Qt.TextFormat.PlainText)
            banner.setAlignment(Qt.AlignmentFlag.AlignCenter)
            banner.setStyleSheet(
                "background:#009CDE; color:white; font-weight:bold;"
                "padding:4px; border-radius:3px;"
            )
            root.addWidget(banner)

        # Header
        header = QHBoxLayout()
        title_model = self.display_name if self.is_simulated else self._spec.model
        self._title_label = QLabel(
            f"<b>{title_model}</b>&nbsp;&nbsp;"
            f"<span style='color:#888'>{self.device_id}</span>"
        )
        self._title_label.setTextFormat(Qt.TextFormat.RichText)
        header.addWidget(self._title_label)

        self._status_label = QLabel("Connecting…")
        self._status_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        header.addStretch()
        header.addWidget(self._status_label)
        root.addLayout(header)

        # Main tab widget
        self._tabs = QTabWidget()
        self._tabs.currentChanged.connect(self._on_inner_tab_changed)
        root.addWidget(self._tabs)

        # ── Live View tab ──
        live = QWidget()
        live_layout = QVBoxLayout(live)
        live_layout.setContentsMargins(0, 4, 0, 0)

        live_actions = QHBoxLayout()
        live_actions.setContentsMargins(2, 0, 2, 0)
        live_actions.setSpacing(6)
        live_actions.addWidget(QLabel("Live Data"))
        live_actions.addStretch()
        self._poll_commands_btn = QPushButton("Commands")
        self._poll_commands_btn.setToolTip("Change the commands polled in the background for this tab")
        self._poll_commands_btn.setFixedSize(92, 24)
        self._poll_commands_btn.setObjectName("CompactActionButton")
        self._poll_commands_btn.clicked.connect(self._edit_poll_commands)
        live_actions.addWidget(self._poll_commands_btn)
        self._export_plot_btn = QPushButton("Export Plot")
        self._export_plot_btn.setToolTip("Export readings shown by this gauge tab")
        self._export_plot_btn.setFixedSize(92, 24)
        self._export_plot_btn.setObjectName("CompactActionButton")
        self._export_plot_btn.clicked.connect(self._export_live_plot)
        live_actions.addWidget(self._export_plot_btn)
        live_layout.addLayout(live_actions)

        splitter = QSplitter(Qt.Orientation.Vertical)

        self._plot_panel = PlotPanel(spec=self._spec, trace_base_color=self._gauge_color)
        splitter.addWidget(self._plot_panel)

        # Readings table
        self._table = QTableWidget(0, 4)
        self._table.setHorizontalHeaderLabels(["Command", "Value", "Unit", "Time"])
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.setMinimumHeight(120)
        self._table.setMaximumHeight(220)
        splitter.addWidget(self._table)

        splitter.setSizes([420, 140])
        self._live_splitter = splitter
        live_layout.addWidget(splitter)
        self._tabs.addTab(live, "Live View")

        # ── Command Displays tab ──
        self._command_panel = CommandDisplayPanel(
            spec=self._spec,
            initial_commands=list(getattr(self.worker, "_commands", [])),
            trace_color=self._gauge_color,
        )
        self._tabs.addTab(self._command_panel, "Command Displays")

        # ── Terminal tab ──
        self._terminal = TerminalWidget(
            spec=self._spec, worker=self.worker, gauge_color=self._gauge_color
        )
        self._tabs.addTab(self._terminal, "Terminal")

        # ── Settings tab ──
        self._settings = GaugeSettingsPanel(spec=self._spec, worker=self.worker)
        self._settings.set_display_unit(self._display_unit)
        self._tabs.addTab(self._settings, "Settings")

        if self._spec.model.upper() == "OPG550":
            self._opg_panel = OPG550SpectrumStudio(spec=self._spec, worker=self.worker)
            self._opg_panel.set_display_unit(self._display_unit)
            self._tabs.addTab(self._opg_panel, "Spectrum Studio")

    def apply_theme(self) -> None:
        theme = current_theme(self)
        title_model = self.display_name if self.is_simulated else self._spec.model
        self._title_label.setText(
            f"<b>{title_model}</b>&nbsp;&nbsp;"
            f"<span style='color:{theme.muted}'>{self.device_id}</span>"
        )
        compact_action_style = (
            f"QPushButton#CompactActionButton {{ background:{theme.control}; color:{theme.text};"
            f" border:1px solid {theme.border}; border-radius:5px; padding:2px 8px; font-weight:600; }}"
            f"QPushButton#CompactActionButton:hover {{ background:{theme.control_hover}; }}"
        )
        self._poll_commands_btn.setStyleSheet(compact_action_style)
        self._export_plot_btn.setStyleSheet(compact_action_style)
        for panel in (self._plot_panel, self._command_panel, self._terminal, self._settings, self._opg_panel):
            hook = getattr(panel, "apply_theme", None)
            if callable(hook):
                hook()
        for plot in self.findChildren(pg.PlotWidget):
            themed_plot(plot)

    def _on_inner_tab_changed(self, index: int) -> None:
        tab_text = self._tabs.tabText(index)
        if tab_text == "Settings":
            self._settings.refresh_setpoints()
        elif tab_text == "Spectrum Studio" and self._opg_panel is not None:
            # Fire a one-shot read of pressure + plasma_state so the user sees
            # live values immediately on entering the tab.
            self._opg_panel._on_panel_shown()

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        table_h = max(120, min(300, int(self.height() * 0.28)))
        self._table.setMaximumHeight(table_h)

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

    @pyqtSlot(object)
    def on_reading(self, reading: DeviceReading) -> None:
        self._all_readings.append(reading)
        if reading.value is not None:
            value = self._to_display(reading.value, reading.unit)
            self._plot_panel.feed(reading.command, reading.timestamp_mono, value)
        if self._command_panel is not None:
            self._command_panel.on_reading(reading)
        self._settings.on_reading(reading)
        if self._opg_panel is not None:
            self._opg_panel.on_reading(reading)
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

    @pyqtSlot(object)
    def _on_terminal_response(self, entry) -> None:
        self._terminal.on_terminal_response(entry)
        self._settings.on_terminal_response(entry)
        if self._command_panel is not None:
            parsed = None
            protocol = getattr(self.worker, "_protocol", None)
            command = entry.command or ""
            if command and entry.response and protocol is not None:
                try:
                    parsed = protocol.parse_response(entry.response, command)
                except Exception:
                    logger.exception("Failed to parse terminal response for command panel")
            self._command_panel.on_terminal_response(entry, parsed)
        if self._opg_panel is not None:
            self._opg_panel.on_terminal_response(entry)

    def _edit_poll_commands(self) -> None:
        current = self.current_poll_commands()
        dlg = PollCommandsDialog(self._spec, current, self)
        if not dlg.exec():
            return
        commands = dlg.selected_commands()
        self.apply_poll_commands(commands)

    def current_poll_commands(self) -> list[str]:
        return list(getattr(self.worker, "_commands", []))

    def apply_poll_commands(self, commands: list[str]) -> None:
        if not commands:
            return
        setter = getattr(self.worker, "set_commands", None)
        if callable(setter):
            setter(commands)
        else:
            self.worker._commands = list(commands)
        if self._command_panel is not None:
            self._command_panel.set_polled_commands(commands)
        self.poll_commands_changed.emit(self.device_id, commands)

    def edit_poll_commands(self) -> None:
        self._edit_poll_commands()

    def _export_live_plot(self) -> None:
        if not self._all_readings:
            return
        commands = set(getattr(self.worker, "_commands", [])) or None
        dlg = ExportDialog(
            self._all_readings,
            self,
            title=f"Export {self.display_name} Plot",
            preselected_devices={self.device_id},
            preselected_commands=commands,
        )
        dlg.exec()

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
        if reading.value is None:
            val_str = "—"
            unit_str = reading.unit
        else:
            disp_value = self._to_display(reading.value, reading.unit)
            val_str = f"{disp_value:.4g}"
            unit_str = self._unit_for_display(reading.unit)
        ts_str = reading.timestamp_wall.strftime("%H:%M:%S")
        for col, text in enumerate(
            [reading.command, val_str, unit_str, ts_str]
        ):
            item = QTableWidgetItem(text)
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._table.setItem(row, col, item)

    def _to_display(self, value: float, native_unit: str) -> float:
        """Convert a pressure ``value`` from ``native_unit`` to the current
        display unit.  Non-pressure units pass through unchanged."""
        if native_unit in SUPPORTED_UNITS and self._display_unit in SUPPORTED_UNITS:
            return convert_pressure(value, native_unit, self._display_unit)
        return value

    def _unit_for_display(self, native_unit: str) -> str:
        """Return the unit string to show — display unit for pressures,
        native unit otherwise."""
        if native_unit in SUPPORTED_UNITS:
            return self._display_unit
        return native_unit

    @pyqtSlot(str)
    def _on_units_changed(self, new_unit: str) -> None:
        """Respond to a unit change emitted by the Settings dialog."""
        if new_unit not in SUPPORTED_UNITS or new_unit == self._display_unit:
            return
        self._display_unit = new_unit
        self._plot_panel.set_display_unit(new_unit)
        self._settings.set_display_unit(new_unit)
        if self._opg_panel is not None:
            self._opg_panel.set_display_unit(new_unit)
        # Rebuild table from raw readings in the new unit.
        self._table.setRowCount(0)
        for reading in self._all_readings:
            self._update_table(reading)

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # Gauge colour
    # ------------------------------------------------------------------

    def set_gauge_color(self, color: str) -> None:
        """Update the gauge's assigned colour.

        Does *not* emit ``color_changed`` — call this when propagating an
        externally-initiated change to avoid feedback loops.
        """
        self._gauge_color = color
        self._plot_panel.set_trace_base_color(color)
        self._terminal.set_rx_color(color)
        if self._command_panel is not None:
            self._command_panel.set_trace_color(color)

    # ------------------------------------------------------------------
    # Data access
    # ------------------------------------------------------------------

    def get_readings(self) -> list[DeviceReading]:
        return list(self._all_readings)

    def get_session_config(self) -> dict:
        if self.is_simulated:
            raise RuntimeError(
                "get_session_config() is for real gauges; "
                "use get_simulated_config() for simulated tabs."
            )
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

    def get_simulated_config(self):
        """Return the :class:`SimulatedGaugeConfig` for a simulated tab.

        Raises
        ------
        RuntimeError
            If called on a real-gauge tab.
        """
        if not self.is_simulated:
            raise RuntimeError("Tab is not a simulated gauge.")
        return self.worker.config
