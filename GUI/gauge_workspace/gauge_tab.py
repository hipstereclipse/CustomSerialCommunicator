"""
GaugeTab — one tab in the main window's QTabWidget, per connected gauge.

Contains two sub-tabs:
  "Live View"  — pyqtgraph multi-plot panel with interactive crosshair +
                 per-trace toggle buttons
  "Terminal"   — interactive serial terminal with format selector
"""

from __future__ import annotations

import logging
import math
import time
from collections import deque

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFrame, QGridLayout,
    QGroupBox, QHBoxLayout, QHeaderView, QLabel, QPushButton, QScrollArea, QSizePolicy,
    QProgressBar, QSlider, QSplitter, QTabWidget, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from serial_comm.models import DeviceError, DeviceReading, DeviceSpec
from serial_comm.opg_spectrum import (
    OPG_ANALYSIS_MAX_PRESSURE_MBAR,
    SpectrumMode,
    identify_optical_species,
    simulate_optical_spectrum,
)
from serial_comm.units import SUPPORTED_UNITS, convert_pressure
from GUI.gauge_workspace.terminal_widget import TerminalWidget
from GUI.settings_dialog import display_signals, get_display_unit

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
        self._value_bar.setStyleSheet(
            "color:#CCCCCC; font-size:11px; padding:1px 6px;"
            "background:#2A2A2A; border-top:1px solid #3A3A3A;"
        )
        self._value_bar.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self._value_bar.setFixedHeight(18)
        self._value_bar.hide()
        root.addWidget(self._value_bar)

        # ── Plot widget ────────────────────────────────────────────────
        self._glw = pg.GraphicsLayoutWidget()
        self._glw.setBackground("#1E1E1E")
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
        fixed_top.setStyleSheet(
            "QWidget { background: #1A1A1A; border-bottom: 1px solid #333; }"
        )
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
        self._setpoint_plot.setBackground("#1E1E1E")
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
        self._pin_setpoint_plot.setBackground("#181818")
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


class OPG550ControlPanel(QWidget):
    """Dedicated OPG550 controls and visual telemetry.

    This panel is only attached to OPG550 tabs and surfaces high-value
    metadata/status commands with one-click actions, plus compact visual
    indicators tailored to optical Pirani behavior.
    """

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
        self._molecule_label = QLabel("Likely molecules: -")

        self._vacuum_bar = QProgressBar()
        self._temperature_bar = QProgressBar()
        self._spectrum_mode = SpectrumMode.AUTO

        self._trend_t: deque[float] = deque(maxlen=360)
        self._trend_p: deque[float] = deque(maxlen=360)
        self._trend_t0: float | None = None
        self._trend_plot: pg.PlotWidget | None = None
        self._trend_curve: pg.PlotDataItem | None = None
        self._spectrum_plot: pg.PlotWidget | None = None
        self._spectrum_curve: pg.PlotDataItem | None = None
        self._spectrum_user_zoomed: bool = False
        self._spectrum_reset_btn: QPushButton | None = None
        self._spectrum_mode_desc: QLabel | None = None
        self._last_pressure_mbar: float | None = None
        self._last_pressure_t: float | None = None

        self._build_ui()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)

        title = QLabel("OPG550 Optical Pirani Studio")
        title.setStyleSheet("font-size: 14px; font-weight: 700; color: #E6F4FF;")
        subtitle = QLabel(
            "Fast access to identity, diagnostics, thermal state, and vacuum regime."
        )
        subtitle.setStyleSheet("font-size: 11px; color: #8FA9BA;")
        root.addWidget(title)
        root.addWidget(subtitle)

        body = QSplitter(Qt.Orientation.Horizontal)
        body.setChildrenCollapsible(False)
        body.setHandleWidth(6)

        # Left side: dedicated chart workspace.
        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(6)

        trend_box = QFrame()
        trend_box.setStyleSheet(
            "QFrame { background: #141414; border: 1px solid #2F3C47; border-radius: 8px; }"
        )
        trend_layout = QVBoxLayout(trend_box)
        trend_layout.setContentsMargins(10, 8, 10, 8)
        trend_layout.setSpacing(6)
        trend_title = QLabel("Pressure Trend")
        trend_title.setStyleSheet("font-weight: 600; color: #D8E2EA;")
        trend_layout.addWidget(trend_title)

        self._trend_plot = pg.PlotWidget()
        self._trend_plot.setMinimumHeight(170)
        self._trend_plot.setBackground("#121619")
        # Avoid SI prefix scaling by building our own label
        self._trend_plot.setLabel("left", f"Pressure ({self._display_unit})", units="")
        self._trend_plot.setLabel("bottom", "Time", units="s")
        self._trend_plot.setLogMode(y=True)
        self._trend_plot.showGrid(x=True, y=True, alpha=0.22)
        self._trend_curve = self._trend_plot.plot(
            pen=pg.mkPen("#43C5FF", width=2)
        )
        trend_layout.addWidget(self._trend_plot)

        spectrum_box = QFrame()
        spectrum_box.setStyleSheet(
            "QFrame { background: #121816; border: 1px solid #2F3C47; border-radius: 8px; }"
        )
        spectrum_layout = QVBoxLayout(spectrum_box)
        spectrum_layout.setContentsMargins(10, 8, 10, 8)
        spectrum_layout.setSpacing(6)

        spectrum_hdr = QHBoxLayout()
        spectrum_title = QLabel("Virtual Spectrum")
        spectrum_title.setStyleSheet("font-weight: 600; color: #D8E2EA;")
        spectrum_hdr.addWidget(spectrum_title)
        spectrum_hdr.addStretch()
        spectrum_hdr.addWidget(QLabel("Mode"))
        self._spectrum_mode_combo = QComboBox()
        for mode in SpectrumMode:
            self._spectrum_mode_combo.addItem(mode.value, mode)
        self._spectrum_mode_combo.currentIndexChanged.connect(self._on_spectrum_mode_changed)
        spectrum_hdr.addWidget(self._spectrum_mode_combo)
        spectrum_layout.addLayout(spectrum_hdr)

        self._spectrum_mode_desc = QLabel(self._spectrum_mode_description(SpectrumMode.AUTO))
        self._spectrum_mode_desc.setStyleSheet("color:#7FAABB; font-size:10px; font-style:italic;")
        self._spectrum_mode_desc.setWordWrap(True)
        spectrum_layout.addWidget(self._spectrum_mode_desc)

        self._spectrum_plot = pg.PlotWidget()
        self._spectrum_plot.setMinimumHeight(160)
        self._spectrum_plot.setBackground("#101316")
        self._spectrum_plot.setLabel("left", "Relative optical intensity")
        self._spectrum_plot.setLabel("bottom", "Wavelength", units="nm")
        self._spectrum_plot.showGrid(x=True, y=True, alpha=0.2)
        self._spectrum_curve = self._spectrum_plot.plot(
            pen=pg.mkPen("#90D98E", width=2),
            fillLevel=0.0,
            brush=pg.mkBrush(144, 217, 142, 90),
        )
        self._spectrum_user_zoomed = False
        self._spectrum_plot.getViewBox().sigRangeChangedManually.connect(
            self._on_spectrum_range_manual
        )
        self._spectrum_reset_btn = QPushButton("A")
        self._spectrum_reset_btn.setToolTip("Reset spectrum view to auto-scale")
        self._spectrum_reset_btn.setFixedSize(24, 24)
        self._spectrum_reset_btn.setStyleSheet(
            "QPushButton { background: #2A3A2A; color: #90D98E; border: 1px solid #3E6E4D;"
            " border-radius: 4px; font-weight: bold; font-size: 11px; }"
            "QPushButton:hover { background: #3A5A3A; }"
        )
        self._spectrum_reset_btn.hide()
        self._spectrum_reset_btn.clicked.connect(self._on_spectrum_reset_view)
        spectrum_hdr.addWidget(self._spectrum_reset_btn)
        spectrum_layout.addWidget(self._spectrum_plot)

        charts_splitter = QSplitter(Qt.Orientation.Vertical)
        charts_splitter.setChildrenCollapsible(False)
        charts_splitter.addWidget(trend_box)
        charts_splitter.addWidget(spectrum_box)
        charts_splitter.setStretchFactor(0, 3)
        charts_splitter.setStretchFactor(1, 2)
        charts_splitter.setSizes([320, 220])
        left_layout.addWidget(charts_splitter, 1)

        # Right side: controls + device info.
        right_scroll = QScrollArea()
        right_scroll.setWidgetResizable(True)
        right_scroll.setFrameShape(QFrame.Shape.NoFrame)

        right_panel = QWidget()
        right = QVBoxLayout(right_panel)
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(8)

        actions = QFrame()
        actions.setStyleSheet(
            "QFrame { background: #1B232A; border: 1px solid #2E3F4D; border-radius: 8px; }"
        )
        actions_layout = QGridLayout(actions)
        actions_layout.setContentsMargins(8, 8, 8, 8)
        actions_layout.setHorizontalSpacing(6)
        actions_layout.setVerticalSpacing(6)
        for idx, (label, command) in enumerate((
            ("Snapshot All", "_snapshot"),
            ("Read Error", "error_status"),
            ("Read Firmware", "software_version"),
            ("Read Serial", "serial_number"),
            ("Read Temperature", "temperature"),
        )):
            btn = QPushButton(label)
            btn.setMinimumHeight(28)
            btn.clicked.connect(lambda _=False, c=command: self._on_action(c))
            actions_layout.addWidget(btn, idx // 2, idx % 2)
        right.addWidget(actions)

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

        status_box = QFrame()
        status_box.setStyleSheet(
            "QFrame { background: #1B232A; border: 1px solid #2E3F4D; border-radius: 8px; }"
        )
        sv = QVBoxLayout(status_box)
        sv.setContentsMargins(10, 8, 10, 8)
        sv.setSpacing(5)
        err_title = QLabel("Error Status")
        err_title.setStyleSheet("font-weight: 600; color: #D8E2EA;")
        self._err_value.setStyleSheet("font-family: Consolas, monospace; color: #F9C6C6;")
        sv.addWidget(err_title)
        sv.addWidget(self._err_value)
        self._err_value.setVisible(False)
        right.addWidget(status_box)

        health = QFrame()
        health.setStyleSheet(
            "QFrame { background: #1A1A1A; border: 1px solid #333; border-radius: 8px; }"
        )
        hv = QVBoxLayout(health)
        hv.setContentsMargins(10, 8, 10, 10)
        hv.setSpacing(8)

        vacuum_title = QLabel("Vacuum Regime")
        vacuum_title.setStyleSheet("font-weight: 600; color: #D8E2EA;")
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
        self._pressure_quality.setStyleSheet("color: #AFC7D6; font-size: 11px;")
        hv.addWidget(self._pressure_quality)

        temp_title = QLabel("Sensor Thermal Load")
        temp_title.setStyleSheet("font-weight: 600; color: #D8E2EA;")
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

        self._molecule_label.setStyleSheet("color:#CFE6D5; font-size:11px;")
        self._molecule_label.setWordWrap(True)
        hv.addWidget(self._molecule_label)

        right.addWidget(health)
        right.addStretch()

        right_scroll.setWidget(right_panel)

        body.addWidget(left_panel)
        body.addWidget(right_scroll)
        body.setStretchFactor(0, 3)
        body.setStretchFactor(1, 2)
        body.setSizes([860, 420])

        root.addWidget(body, 1)

    @staticmethod
    def _spectrum_mode_description(mode: SpectrumMode) -> str:
        return {
            SpectrumMode.AUTO: (
                "Auto — composition derived from pressure level and trend. "
                "N₂/O₂ dominate at higher pressures; H₂/H₂O/CO shift in at deep vacuum."
            ),
            SpectrumMode.AIR_LEAK: (
                "Air Leak — atmosphere ingress simulation. "
                "N₂ 72%, O₂ 20%, Ar 3%, H₂O 4% baseline; peaks evolve as outgassing grows over time."
            ),
            SpectrumMode.WATER_LEAK: (
                "Water Leak — humid atmosphere ingress. "
                "H₂O dominates; thermal dissociation produces H₂ over time."
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

    def _metric_card(self, title: str, value: QLabel) -> QFrame:
        box = QFrame()
        box.setStyleSheet(
            "QFrame { background: #1F2730; border: 1px solid #334452; border-radius: 8px; }"
        )
        v = QVBoxLayout(box)
        v.setContentsMargins(10, 8, 10, 8)
        v.setSpacing(2)
        t = QLabel(title)
        t.setStyleSheet("font-size: 11px; color: #8FA9BA;")
        value.setStyleSheet("font-size: 16px; font-weight: 700; color: #F3FAFF;")
        v.addWidget(t)
        v.addWidget(value)
        return box

    def _on_action(self, command: str) -> None:
        if command == "_snapshot":
            for cmd in (
                "pressure",
                "temperature",
                "software_version",
                "serial_number",
                "error_status",
            ):
                self._send_query(cmd)
            return
        self._send_query(command)

    def _send_query(self, command: str) -> None:
        if command not in self._spec.commands:
            return
        try:
            protocol = getattr(self._worker, "_protocol", None)
            if protocol is None:
                return
            frame = protocol.build_request(command)
            self._worker.send_terminal_command(frame, command)
        except Exception:
            logger.exception("OPG550 panel query failed for %s", command)

    def on_reading(self, reading: DeviceReading) -> None:
        if reading.command == "pressure":
            self._update_pressure(reading.value, reading.unit, reading.timestamp_mono)
        elif reading.command == "temperature":
            self._update_temperature(reading.value)

    def on_terminal_response(self, entry) -> None:
        command = entry.command or ""
        if command not in self._spec.commands:
            return
        if entry.error:
            if command == "error_status":
                self._err_value.setText(f"ERR: {entry.error}")
            return
        if not entry.response:
            return
        protocol = getattr(self._worker, "_protocol", None)
        if protocol is None:
            return
        try:
            parsed = protocol.parse_response(entry.response, command)
        except Exception:
            logger.exception("OPG550 response decode failed for %s", command)
            return
        if not parsed.success:
            if command == "error_status":
                self._err_value.setText(parsed.error or "Decode failure")
            return

        if command == "software_version":
            self._fw_value.setText(parsed.formatted or str(parsed.value or "-"))
        elif command == "serial_number":
            self._sn_value.setText(parsed.formatted or str(parsed.value or "-"))
        elif command == "error_status":
            msg = parsed.formatted or "OK"
            self._err_value.setText(msg)
        elif command == "temperature" and parsed.value is not None:
            self._update_temperature(float(parsed.value))
        elif command == "pressure" and parsed.value is not None:
            self._update_pressure(float(parsed.value), parsed.unit or "mbar", None)

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
            # Avoid SI prefix scaling by building our own label
            self._trend_plot.setLabel("left", f"Pressure ({unit})", units="")
        if self._trend_p:
            self._trend_p = deque(
                [convert_pressure(v, old, unit) for v in self._trend_p],
                maxlen=self._trend_p.maxlen,
            )
            self._refresh_trend()

    def _update_pressure(
        self,
        value: float | None,
        unit: str,
        timestamp_mono: float | None,
    ) -> None:
        if value is None:
            return
        value_display = float(value)
        if unit in SUPPORTED_UNITS:
            value_display = float(convert_pressure(float(value), unit, self._display_unit))
            value_mbar = float(convert_pressure(float(value), unit, "mbar"))
        else:
            value_mbar = float(value)

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
        self._refresh_trend()
        self._update_spectrum_plot(value_mbar=value_mbar, timestamp_mono=timestamp_mono)

    def _update_temperature(self, value: float | None) -> None:
        if value is None:
            return
        self._temperature_value.setText(f"{value:.2f} °C")
        self._temperature_bar.setValue(int(max(0.0, min(120.0, value)) * 10.0))

    def _refresh_trend(self) -> None:
        if self._trend_curve is None or not self._trend_t:
            return
        self._trend_curve.setData(
            np.array(self._trend_t, dtype=float),
            np.array(self._trend_p, dtype=float),
        )

    def _update_spectrum_plot(
        self,
        *,
        value_mbar: float | None = None,
        timestamp_mono: float | None = None,
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

        spectrum = simulate_optical_spectrum(
            pressure_mbar=max(p_mbar, 1e-12),
            trend_mbar_per_s=trend,
            elapsed_s=elapsed,
            mode=self._spectrum_mode,
            wavelength_min_nm=380.0,
            wavelength_max_nm=780.0,
            samples=401,
        )
        x = np.linspace(380.0, 780.0, len(spectrum), dtype=float)
        y = np.array(spectrum, dtype=float)
        if self._spectrum_user_zoomed:
            self._spectrum_plot.disableAutoRange()
        self._spectrum_curve.setData(x, y)

        if p_mbar <= OPG_ANALYSIS_MAX_PRESSURE_MBAR:
            matches = identify_optical_species(
                spectrum,
                wavelength_min_nm=380.0,
                wavelength_max_nm=780.0,
                top_k=4,
            )
            if matches:
                txt = ", ".join(f"{m.name} ({m.score * 100:.0f}%)" for m in matches)
            else:
                txt = "No dominant optical signature"
            self._molecule_label.setText(f"Likely optical gas signatures: {txt}")
        else:
            self._molecule_label.setText(
                "Gas analysis unavailable above "
                f"{OPG_ANALYSIS_MAX_PRESSURE_MBAR:.1E} mbar "
                "(pump down further to enable species identification)"
            )

        self._last_pressure_mbar = max(p_mbar, 1e-12)
        self._last_pressure_t = float(timestamp_mono)

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
    """One gauge's live view + terminal tab."""

    #: Emitted when the user picks a new colour via the header swatch.
    #: Payload: (device_id, hex_color)
    color_changed = pyqtSignal(str, str)

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
        self._opg_panel: OPG550ControlPanel | None = None

        self._build_ui()
        # Push current display-unit into the plot so its axes match the table.
        self._plot_panel.set_display_unit(self._display_unit)

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

        splitter.setSizes([400, 130])
        self._live_splitter = splitter
        live_layout.addWidget(splitter)
        self._tabs.addTab(live, "Live View")

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
            self._opg_panel = OPG550ControlPanel(spec=self._spec, worker=self.worker)
            self._opg_panel.set_display_unit(self._display_unit)
            self._tabs.addTab(self._opg_panel, "OPG550 Studio")

    def _on_inner_tab_changed(self, index: int) -> None:
        if self._tabs.tabText(index) == "Settings":
            self._settings.refresh_setpoints()

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
        if self._opg_panel is not None:
            self._opg_panel.on_terminal_response(entry)

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
