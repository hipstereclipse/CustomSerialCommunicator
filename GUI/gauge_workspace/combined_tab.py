"""
CombinedTab — combined pressure dashboard for multiple gauges.

Default behavior remains a single combined overlay plot, but users can switch
between multiple simultaneous chart layouts and optionally synchronize crosshair
hover across all charts.
"""

from __future__ import annotations

import logging
import math
from collections import deque

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QDoubleValidator
from PyQt6.QtWidgets import (
    QButtonGroup,
    QColorDialog,
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from serial_comm.units import SUPPORTED_UNITS, convert_pressure
from serial_comm.command_utils import command_display_name
from GUI.theme import current_theme, line_edit_style, style_plot_item, themed_graphics_layout, value_bar_style

logger = logging.getLogger(__name__)

_MAX_POINTS = 200_000
_AXIS_REFRESH_MS = 120
_CONTROL_PANEL_WIDTH = 270
_CONTROL_PANEL_MIN_WIDTH = 245
_CONTROL_PANEL_MAX_WIDTH = 310

COLOR_PALETTE: list[str] = [
    "#4C9BE8",
    "#E8954C",
    "#4CE87A",
    "#E84C6F",
    "#9B4CE8",
    "#E8D74C",
    "#4CE8D7",
    "#E84CCA",
    "#A8E84C",
    "#E8744C",
    "#4C74E8",
    "#E8C14C",
]


class CombinedTab(QWidget):
    color_changed = pyqtSignal(str, str)
    gauge_toggled = pyqtSignal(str, bool)
    export_requested = pyqtSignal(object)
    poll_commands_requested = pyqtSignal(object)

    def __init__(
        self,
        title: str = "Main",
        is_simulation: bool = False,
        display_unit: str = "mbar",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._title = title
        self._is_simulation = is_simulation
        self._display_unit = display_unit

        self._devices: dict[str, dict] = {}
        self._device_series: dict[str, set[str]] = {}
        self._active_commands: dict[str, set[str]] = {}
        self._gauges: dict[str, dict] = {}
        self._visible: dict[str, bool] = {}
        self._t0: float | None = None
        self._latest_t: float = 0.0

        self._y_mode: str = "auto_center"
        self._x_mode: str = "all"
        self._x_window_s: float = 5 * 60.0

        self._chart_layout: str = "overlay"  # overlay | stacked | grid
        self._sync_hover: bool = True
        self._comparison_enabled: bool = False
        self._plot_paused: bool = False
        self._pending_x_refresh: bool = False
        self._pending_y_refresh: bool = False

        self._axis_refresh_timer = QTimer(self)
        self._axis_refresh_timer.setSingleShot(True)
        self._axis_refresh_timer.setInterval(_AXIS_REFRESH_MS)
        self._axis_refresh_timer.timeout.connect(self._apply_deferred_axis_refresh)

        self._plots: list[pg.PlotItem] = []
        self._plot_to_devices: dict[pg.PlotItem, list[str]] = {}
        self._plot_for_device: dict[str, pg.PlotItem] = {}
        self._crosshair_lines: dict[pg.PlotItem, pg.InfiniteLine] = {}
        self._legend = None
        self._proxy: pg.SignalProxy | None = None

        self._build_ui()

    # ------------------------------------------------------------------
    # Build UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(4)

        desc = "simulated" if self._is_simulation else "real-time"
        self._header_desc = desc
        header_row = QHBoxLayout()
        header_row.setSpacing(8)

        hdr = QLabel(
            f"<b>{self._title}</b>"
            f"<span style='color:#888; font-size:11px'>"
            f"  -  combined {desc} pressure view</span>"
        )
        hdr.setTextFormat(Qt.TextFormat.RichText)
        hdr.setStyleSheet("font-size:13px; padding:4px 4px 0px 4px;")
        self._header_label = hdr
        header_row.addWidget(hdr, 1)

        self._poll_commands_btn = QPushButton("Poll Commands")
        self._poll_commands_btn.setToolTip("Choose which commands are polled for the gauges shown here")
        self._poll_commands_btn.setStyleSheet(
            "QPushButton { background:#1F4C72; color:#F4FBFF; border:1px solid #4DB2FF; "
            "border-radius:6px; padding:6px 12px; font-weight:600; }"
            "QPushButton:hover { background:#285C88; }"
        )
        self._poll_commands_btn.clicked.connect(self._on_poll_commands_clicked)
        header_row.addWidget(self._poll_commands_btn, 0)
        root.addLayout(header_row)

        middle = QSplitter(Qt.Orientation.Horizontal)
        middle.setChildrenCollapsible(False)

        plot_widget = QWidget()
        plot_col = QVBoxLayout()
        plot_col.setContentsMargins(0, 0, 0, 0)
        plot_col.setSpacing(2)
        plot_widget.setLayout(plot_col)
        plot_widget.setMinimumWidth(360)

        self._glw = pg.GraphicsLayoutWidget()
        themed_graphics_layout(self._glw)
        self._glw.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self._glw.setMinimumSize(320, 260)
        plot_col.addWidget(self._glw, 1)

        self._value_bar = QLabel()
        self._value_bar.setTextFormat(Qt.TextFormat.RichText)
        self._value_bar.setStyleSheet(value_bar_style())
        self._value_bar.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        self._value_bar.setFixedHeight(18)
        self._value_bar.hide()
        plot_col.addWidget(self._value_bar)

        middle.addWidget(plot_widget)
        self._controls_panel = self._build_controls_panel()
        controls_scroll = QScrollArea()
        controls_scroll.setWidgetResizable(True)
        controls_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        controls_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        controls_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        controls_scroll.setWidget(self._controls_panel)
        controls_scroll.setMinimumWidth(_CONTROL_PANEL_MIN_WIDTH + 8)
        controls_scroll.setMaximumWidth(_CONTROL_PANEL_MAX_WIDTH + 8)
        controls_scroll.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self._controls_panel.setMinimumWidth(_CONTROL_PANEL_MIN_WIDTH)
        self._controls_panel.setMaximumWidth(_CONTROL_PANEL_MAX_WIDTH)
        self._controls_scroll = controls_scroll
        middle.addWidget(self._controls_scroll)
        middle.setStretchFactor(0, 1)
        middle.setStretchFactor(1, 0)
        middle.setSizes([760, _CONTROL_PANEL_WIDTH + 8])
        root.addWidget(middle, 1)

        self._gauge_row_inner = QWidget()
        self._gauge_row_layout = QHBoxLayout(self._gauge_row_inner)
        self._gauge_row_layout.setContentsMargins(2, 2, 2, 2)
        self._gauge_row_layout.setSpacing(6)
        self._gauge_row_layout.addStretch()

        gauge_scroll = QScrollArea()
        gauge_scroll.setWidget(self._gauge_row_inner)
        gauge_scroll.setWidgetResizable(True)
        gauge_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        gauge_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        gauge_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        gauge_scroll.setFixedHeight(46)
        self._gauge_scroll = gauge_scroll
        root.addWidget(gauge_scroll)

        kind = "simulated" if self._is_simulation else "real"
        self._empty_label = QLabel(
            f"No {kind} gauges connected.\n"
            f"Use '{'⊕ Simulate' if self._is_simulation else '+ Gauge'}'"
            f" to add a gauge.",
            alignment=Qt.AlignmentFlag.AlignCenter,
        )
        self._empty_label.setStyleSheet("color:#555; font-size:13px; padding:16px;")
        root.addWidget(self._empty_label)

        self._rebuild_plot_layout()
        self.apply_theme()

    def _build_controls_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("CombinedPlotControls")
        panel.setFrameShape(QFrame.Shape.StyledPanel)
        panel.setMinimumWidth(_CONTROL_PANEL_MIN_WIDTH)
        panel.setMaximumWidth(_CONTROL_PANEL_MAX_WIDTH)
        panel.setStyleSheet(
            "QFrame#CombinedPlotControls {"
            "  background: #252525;"
            "  border: 1px solid #3A3A3A;"
            "  border-radius: 8px;"
            "}"
            "QLabel { color: #DDDDDD; }"
            "QLabel[section='true'] {"
            "  color: #FFFFFF; font-weight: 600; font-size: 12px;"
            "  padding: 2px 0 0 0;"
            "}"
            "QRadioButton { color: #DDDDDD; font-size: 11px; }"
            "QPushButton#ClearBtn {"
            "  background: #3A1F1F; color: #FFBABA;"
            "  border: 1px solid #5A2A2A; border-radius: 4px;"
            "  padding: 6px 8px; font-weight: 600;"
            "}"
            "QPushButton#ClearBtn:hover {"
            "  background: #4A2626; border-color: #7A3333;"
            "}"
            "QPushButton[layout='true'] {"
            "  border: 1px solid #435260; border-radius: 12px;"
            "  padding: 2px 10px; color: #BDD2E5; background: #1E2730;"
            "}"
            "QPushButton[layout='true'][checked='true'] {"
            "  border: 1px solid #4DB2FF; color: #F4FBFF; background: #1F4C72;"
            "}"
            "QPushButton#SyncBtn {"
            "  border: 1px solid #4A4A4A; border-radius: 12px;"
            "  padding: 4px 10px; color: #E6E6E6; background: #2A2A2A;"
            "}"
            "QPushButton#SyncBtn[checked='true'] {"
            "  border-color: #5BC38F; background: #18422D; color: #E9FFF2;"
            "}"
        )

        v = QVBoxLayout(panel)
        v.setContentsMargins(10, 10, 10, 10)
        v.setSpacing(8)

        title = QLabel("Plot Controls")
        title.setStyleSheet("color:#FFFFFF; font-weight:600; font-size:13px;")
        self._controls_title = title
        v.addWidget(title)

        plot_btn_row = QHBoxLayout()
        plot_btn_row.setSpacing(6)

        self._clear_btn = QPushButton("Clear Plot")
        self._clear_btn.setObjectName("ClearBtn")
        self._clear_btn.clicked.connect(self.clear_data)
        plot_btn_row.addWidget(self._clear_btn, 1)

        self._export_btn = QPushButton("Export")
        self._export_btn.setObjectName("SyncBtn")
        self._export_btn.setToolTip("Export data from the currently visible plot series")
        self._export_btn.clicked.connect(self._on_export_clicked)
        plot_btn_row.addWidget(self._export_btn, 1)

        self._pause_plot_btn = QPushButton("Pause Plot")
        self._pause_plot_btn.setObjectName("SyncBtn")
        self._pause_plot_btn.clicked.connect(self._on_pause_plot_clicked)

        v.addLayout(plot_btn_row)
        v.addWidget(self._pause_plot_btn)

        v.addWidget(_hline())

        mode_lbl = QLabel("Chart Layout")
        mode_lbl.setProperty("section", True)
        v.addWidget(mode_lbl)

        mode_row1 = QHBoxLayout()
        mode_row1.setSpacing(5)
        self._layout_overlay_btn = self._make_layout_btn("Overlay", "overlay")
        self._layout_stacked_btn = self._make_layout_btn("Stacked", "stacked")
        self._layout_grid_btn = self._make_layout_btn("Grid", "grid")
        mode_row1.addWidget(self._layout_overlay_btn)
        mode_row1.addWidget(self._layout_stacked_btn)
        mode_row1.addWidget(self._layout_grid_btn)
        v.addLayout(mode_row1)

        self._sync_btn = QPushButton("Synchronize Charts")
        self._sync_btn.setObjectName("SyncBtn")
        self._sync_btn.setCheckable(True)
        self._sync_btn.setChecked(True)
        self._sync_btn.toggled.connect(self._on_sync_toggled)
        v.addWidget(self._sync_btn)

        v.addWidget(_hline())

        cmp_lbl = QLabel("Analysis")
        cmp_lbl.setProperty("section", True)
        v.addWidget(cmp_lbl)

        self._compare_enable_btn = QPushButton("Compare Two Gauges")
        self._compare_enable_btn.setCheckable(True)
        self._compare_enable_btn.setObjectName("SyncBtn")
        self._compare_enable_btn.toggled.connect(self._on_compare_toggled)
        v.addWidget(self._compare_enable_btn)

        self._compare_a = QComboBox()
        self._compare_b = QComboBox()
        self._compare_a.currentIndexChanged.connect(self._refresh_value_bar_for_compare)
        self._compare_b.currentIndexChanged.connect(self._refresh_value_bar_for_compare)
        v.addWidget(QLabel("Gauge A"))
        v.addWidget(self._compare_a)
        v.addWidget(QLabel("Gauge B"))
        v.addWidget(self._compare_b)

        self._compare_help = QLabel("Shows ΔP, Δ%, and A/B at cursor position")
        self._compare_help.setStyleSheet("color:#9FA9B2; font-size:10px;")
        v.addWidget(self._compare_help)
        self._set_compare_controls_enabled(False)

        v.addWidget(_hline())

        y_lbl = QLabel("Pressure (Y)")
        y_lbl.setProperty("section", True)
        v.addWidget(y_lbl)

        self._y_group = QButtonGroup(self)
        self._rb_y_auto = QRadioButton("Auto-center on readings")
        self._rb_y_custom = QRadioButton("Custom range")
        self._rb_y_full = QRadioButton("Gauge full-scale")
        for idx, rb in enumerate((self._rb_y_auto, self._rb_y_custom, self._rb_y_full)):
            self._y_group.addButton(rb, idx)
            v.addWidget(rb)
        self._rb_y_auto.setChecked(True)
        self._y_group.idToggled.connect(self._on_y_mode_toggled)

        self._y_custom_box = QWidget()
        yform = QHBoxLayout(self._y_custom_box)
        yform.setContentsMargins(18, 2, 0, 2)
        yform.setSpacing(4)
        self._y_min_edit = _make_sci_edit("1e-8")
        self._y_max_edit = _make_sci_edit("1e3")
        self._y_min_edit.editingFinished.connect(self._refresh_y_axis)
        self._y_max_edit.editingFinished.connect(self._refresh_y_axis)
        yform.addWidget(QLabel("min"))
        yform.addWidget(self._y_min_edit, 1)
        yform.addWidget(QLabel("max"))
        yform.addWidget(self._y_max_edit, 1)
        self._y_custom_box.setVisible(False)
        v.addWidget(self._y_custom_box)

        v.addWidget(_hline())

        x_lbl = QLabel("Time (X)")
        x_lbl.setProperty("section", True)
        v.addWidget(x_lbl)

        self._x_group = QButtonGroup(self)
        self._rb_x_all = QRadioButton("Full history (dynamic)")
        self._rb_x_window = QRadioButton("Rolling window")
        for idx, rb in enumerate((self._rb_x_all, self._rb_x_window)):
            self._x_group.addButton(rb, idx)
            v.addWidget(rb)
        self._rb_x_all.setChecked(True)
        self._x_group.idToggled.connect(self._on_x_mode_toggled)

        self._x_window_box = QWidget()
        xform = QHBoxLayout(self._x_window_box)
        xform.setContentsMargins(18, 2, 0, 2)
        xform.setSpacing(4)
        self._x_window_spin = QDoubleSpinBox()
        self._x_window_spin.setRange(0.25, 720.0)
        self._x_window_spin.setDecimals(2)
        self._x_window_spin.setValue(self._x_window_s / 60.0)
        self._x_window_spin.setSuffix(" min")
        self._x_window_spin.valueChanged.connect(self._on_x_window_changed)
        xform.addWidget(QLabel("last"))
        xform.addWidget(self._x_window_spin, 1)
        self._x_window_box.setVisible(False)
        v.addWidget(self._x_window_box)

        v.addStretch()
        self._set_layout_button_state()
        return panel

    def apply_theme(self) -> None:
        theme = current_theme(self)
        self._header_label.setText(
            f"<b>{self._title}</b>"
            f"<span style='color:{theme.muted}; font-size:11px'>"
            f"  -  combined {self._header_desc} pressure view</span>"
        )
        themed_graphics_layout(self._glw)
        self._value_bar.setStyleSheet(value_bar_style())
        self._empty_label.setStyleSheet(f"color:{theme.muted}; font-size:13px; padding:16px;")
        self._controls_panel.setStyleSheet(_controls_panel_style(theme))
        self._controls_title.setStyleSheet(f"color:{theme.text}; font-weight:600; font-size:13px;")
        self._compare_help.setStyleSheet(f"color:{theme.muted}; font-size:10px;")
        self._y_min_edit.setStyleSheet(line_edit_style())
        self._y_max_edit.setStyleSheet(line_edit_style())
        for plot in self._plots:
            style_plot_item(plot)

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._gauge_scroll.setFixedHeight(46 if self.height() > 520 else 56)

    def _make_layout_btn(self, text: str, mode: str) -> QPushButton:
        btn = QPushButton(text)
        btn.setProperty("layout", True)
        btn.setCheckable(True)
        btn.clicked.connect(lambda _=False, m=mode: self._set_chart_layout(m))
        return btn

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add_gauge(
        self,
        device_id: str,
        display_name: str,
        color: str,
        full_scale_mbar: float | None = None,
    ) -> None:
        if device_id in self._devices:
            return
        fs = float(full_scale_mbar) if full_scale_mbar else None
        self._devices[device_id] = {
            "name": display_name,
            "color": color,
            "full_scale_mbar": fs,
        }
        self._device_series.setdefault(device_id, set())
        self._active_commands.setdefault(device_id, {"pressure"})
        self._visible.setdefault(device_id, True)
        self._empty_label.hide()

    def remove_gauge(self, device_id: str) -> None:
        series_ids = list(self._device_series.pop(device_id, set()))
        if device_id not in self._devices and not series_ids:
            return
        self._devices.pop(device_id, None)
        self._active_commands.pop(device_id, None)
        self._visible.pop(device_id, None)
        for series_id in series_ids:
            series = self._gauges.pop(series_id, None)
            if series is None:
                continue
            self._visible.pop(series_id, None)
            series["toggle_btn"] = None
            series["color_btn"] = None
        self._rebuild_plot_layout()
        self._rebuild_chip_row()
        self._refresh_comparison_sources()
        if not self._devices:
            self._empty_label.show()
        self._refresh_y_axis()

    def _series_id(self, device_id: str, command: str) -> str:
        return f"{device_id}\x1f{command}"

    def _ensure_series(self, device_id: str, command: str) -> str | None:
        device = self._devices.get(device_id)
        if device is None:
            return None
        command = command or "pressure"
        series_id = self._series_id(device_id, command)
        if series_id in self._gauges:
            return series_id
        series_ids = self._device_series.setdefault(device_id, set())
        series_index = len(series_ids)
        label = device["name"] if command == "pressure" else f"{device['name']} - {command_display_name(command)}"
        color = str(device["color"]) if series_index == 0 else _series_color(str(device["color"]), series_index)
        self._gauges[series_id] = {
            "device_id": device_id,
            "command": command,
            "name": device["name"],
            "label": label,
            "color": color,
            "time_buf": deque(maxlen=_MAX_POINTS),
            "val_buf": deque(maxlen=_MAX_POINTS),
            "curve": None,
            "color_btn": None,
            "toggle_btn": None,
            "full_scale_mbar": device.get("full_scale_mbar"),
        }
        series_ids.add(series_id)
        self._visible.setdefault(series_id, self._visible.get(device_id, True))
        self._rebuild_plot_layout()
        self._rebuild_chip_row()
        self._refresh_comparison_sources()
        self._refresh_y_axis()
        return series_id

    def feed(self, device_id: str, t_mono: float, value: float, command: str = "pressure") -> None:
        series_id = self._ensure_series(device_id, command)
        if series_id is None:
            return
        g = self._gauges.get(series_id)
        if g is None:
            return
        if self._t0 is None:
            self._t0 = t_mono
        t_rel = t_mono - self._t0
        if t_rel > self._latest_t:
            self._latest_t = t_rel

        tb: deque = g["time_buf"]
        vb: deque = g["val_buf"]
        tb.append(t_rel)
        vb.append(value)

        if self._plot_paused:
            return

        curve = g.get("curve")
        if curve is not None:
            t_arr = np.array(tb, dtype=float)
            v_arr = np.where(np.array(vb, dtype=float) > 0, np.array(vb, dtype=float), 1e-12)
            curve.setData(t_arr, v_arr)
            curve.setVisible(self._visible.get(series_id, True))

        self._request_axis_refresh(x=True, y=self._y_mode == "auto_center")

    def set_active_commands(self, device_id: str, commands: list[str]) -> None:
        self._active_commands[device_id] = set(commands or ["pressure"])

    def _request_axis_refresh(self, *, x: bool = False, y: bool = False) -> None:
        self._pending_x_refresh = self._pending_x_refresh or x
        self._pending_y_refresh = self._pending_y_refresh or y
        if not self._axis_refresh_timer.isActive():
            self._axis_refresh_timer.start()

    def _apply_deferred_axis_refresh(self) -> None:
        refresh_x = self._pending_x_refresh
        refresh_y = self._pending_y_refresh
        self._pending_x_refresh = False
        self._pending_y_refresh = False
        if refresh_x:
            self._refresh_x_axis()
        if refresh_y:
            self._refresh_y_axis()

    def _refresh_all_curves(self) -> None:
        for series_id, g in self._gauges.items():
            curve = g.get("curve")
            if curve is None:
                continue
            tb: deque = g["time_buf"]
            vb: deque = g["val_buf"]
            t_arr = np.array(tb, dtype=float)
            v_arr = np.where(np.array(vb, dtype=float) > 0, np.array(vb, dtype=float), 1e-12)
            curve.setData(t_arr, v_arr)
            curve.setVisible(self._visible.get(series_id, True))

    def _on_pause_plot_clicked(self) -> None:
        self._plot_paused = not self._plot_paused
        if self._plot_paused:
            self._pause_plot_btn.setText("Resume Plot")
            return

        self._pause_plot_btn.setText("Pause Plot")
        self._refresh_all_curves()
        self._refresh_x_axis()
        if self._y_mode == "auto_center":
            self._refresh_y_axis()

    def _on_export_clicked(self) -> None:
        visible_ids = {
            g["device_id"] for series_id, g in self._gauges.items()
            if self._visible.get(series_id, True)
        }
        self.export_requested.emit(visible_ids or set(self._devices.keys()))

    def _on_poll_commands_clicked(self) -> None:
        visible_ids = {
            g["device_id"] for series_id, g in self._gauges.items()
            if self._visible.get(series_id, True)
        }
        self.poll_commands_requested.emit(visible_ids or set(self._devices.keys()))

    def clear_data(self) -> None:
        removed = False
        for series_id, g in list(self._gauges.items()):
            active = self._active_commands.get(g["device_id"])
            if active is not None and g.get("command", "pressure") not in active:
                self._remove_series(series_id)
                removed = True
                continue
            g["time_buf"].clear()
            g["val_buf"].clear()
            curve = g.get("curve")
            if curve is not None:
                curve.setData([], [])
        if removed:
            self._rebuild_plot_layout()
            self._rebuild_chip_row()
            self._refresh_comparison_sources()
        self._t0 = None
        self._latest_t = 0.0
        self._value_bar.hide()
        self._refresh_x_axis()
        self._refresh_y_axis()

    def _remove_series(self, series_id: str) -> None:
        g = self._gauges.pop(series_id, None)
        if g is None:
            return
        self._visible.pop(series_id, None)
        device_id = str(g.get("device_id", ""))
        self._device_series.get(device_id, set()).discard(series_id)
        curve = g.get("curve")
        if curve is not None:
            try:
                curve.clear()
            except Exception:
                logger.debug("Could not clear removed series %s", series_id, exc_info=True)

    def set_gauge_color(self, device_id: str, color: str) -> None:
        device = self._devices.get(device_id)
        if device is None:
            return
        device["color"] = color
        for index, series_id in enumerate(sorted(self._device_series.get(device_id, set()))):
            g = self._gauges.get(series_id)
            if g is None:
                continue
            series_color = color if index == 0 else _series_color(color, index)
            g["color"] = series_color
            curve = g.get("curve")
            if curve is not None:
                curve.setPen(pg.mkPen(series_color, width=2))
        self._rebuild_chip_row()

    def set_display_unit(self, unit: str) -> None:
        if unit not in SUPPORTED_UNITS or unit == self._display_unit:
            return
        old_unit = self._display_unit
        self._display_unit = unit

        for g in self._gauges.values():
            vb: deque = g["val_buf"]
            converted = [convert_pressure(v, old_unit, unit) for v in vb]
            vb.clear()
            vb.extend(converted)
        for edit in (self._y_min_edit, self._y_max_edit):
            val = _parse_sci(edit.text())
            if val is not None and val > 0:
                edit.setText(_format_sci(convert_pressure(val, old_unit, unit)))
        self._rebuild_plot_layout()
        self._refresh_y_axis()

    def set_visible_filter(self, device_ids: set[str] | None) -> None:
        for series_id, g in self._gauges.items():
            device_id = g.get("device_id", series_id)
            should_show = (device_ids is None) or (device_id in device_ids)
            self._visible[series_id] = should_show
            curve = g.get("curve")
            if curve is not None:
                curve.setVisible(should_show)
            toggle_btn = g.get("toggle_btn")
            if toggle_btn is not None:
                toggle_btn.blockSignals(True)
                toggle_btn.setChecked(should_show)
                toggle_btn.blockSignals(False)
        self._refresh_y_axis()

    # ------------------------------------------------------------------
    # Internal — plot layout
    # ------------------------------------------------------------------

    def _set_chart_layout(self, mode: str) -> None:
        if mode == self._chart_layout:
            return
        self._chart_layout = mode
        self._set_layout_button_state()
        self._rebuild_plot_layout()
        self._refresh_x_axis()
        self._refresh_y_axis()

    def _set_layout_button_state(self) -> None:
        for mode, btn in (
            ("overlay", self._layout_overlay_btn),
            ("stacked", self._layout_stacked_btn),
            ("grid", self._layout_grid_btn),
        ):
            checked = mode == self._chart_layout
            btn.setChecked(checked)
            btn.setProperty("checked", checked)
            btn.style().unpolish(btn)
            btn.style().polish(btn)

    def _rebuild_plot_layout(self) -> None:
        self._glw.clear()
        self._plots.clear()
        self._plot_to_devices.clear()
        self._plot_for_device.clear()
        self._crosshair_lines.clear()
        self._legend = None

        series_ids = list(self._gauges.keys())
        if not series_ids:
            plot = self._glw.addPlot(row=0, col=0)
            self._setup_plot_item(plot)
            self._plots = [plot]
            self._plot_to_devices[plot] = []
            self._setup_crosshair_proxy()
            return

        if self._chart_layout == "overlay":
            plot = self._glw.addPlot(row=0, col=0)
            self._setup_plot_item(plot)
            self._legend = plot.addLegend()
            self._legend.setOffset((10, 10))
            self._plots = [plot]
            self._plot_to_devices[plot] = list(series_ids)
            for did in series_ids:
                g = self._gauges[did]
                curve = plot.plot(pen=pg.mkPen(g["color"], width=2), name=g.get("label", g["name"]))
                curve.setVisible(self._visible.get(did, True))
                g["curve"] = curve
                self._plot_for_device[did] = plot
        elif self._chart_layout == "stacked":
            for idx, did in enumerate(series_ids):
                plot = self._glw.addPlot(row=idx, col=0)
                self._setup_plot_item(plot, title=self._gauges[did].get("label", self._gauges[did]["name"]))
                if idx < len(series_ids) - 1:
                    plot.getAxis("bottom").setStyle(showValues=False)
                curve = plot.plot(pen=pg.mkPen(self._gauges[did]["color"], width=2))
                curve.setVisible(self._visible.get(did, True))
                self._gauges[did]["curve"] = curve
                self._plots.append(plot)
                self._plot_to_devices[plot] = [did]
                self._plot_for_device[did] = plot
        else:  # grid
            for idx, did in enumerate(series_ids):
                row = idx // 2
                col = idx % 2
                plot = self._glw.addPlot(row=row, col=col)
                self._setup_plot_item(plot, title=self._gauges[did].get("label", self._gauges[did]["name"]))
                curve = plot.plot(pen=pg.mkPen(self._gauges[did]["color"], width=2))
                curve.setVisible(self._visible.get(did, True))
                self._gauges[did]["curve"] = curve
                self._plots.append(plot)
                self._plot_to_devices[plot] = [did]
                self._plot_for_device[did] = plot

        self._setup_crosshair_proxy()
        self._replay_all()

    def _setup_plot_item(self, plot: pg.PlotItem, *, title: str = "") -> None:
        plot.showGrid(x=True, y=True, alpha=0.3)
        plot.setLabel("bottom", "Time", units="s")
        # Avoid SI prefix scaling by building our own label
        plot.setLabel("left", f"Pressure ({self._display_unit})", units="")
        plot.setLogMode(x=False, y=True)
        if title:
            plot.setTitle(title, color=current_theme(self).muted, size="10pt")
        style_plot_item(plot)

    # ------------------------------------------------------------------
    # Internal — chips
    # ------------------------------------------------------------------

    def _rebuild_chip_row(self) -> None:
        for series_id, g in self._gauges.items():
            toggle_widget = g["toggle_btn"]
            if toggle_widget is not None:
                self._visible[series_id] = toggle_widget.isChecked()
            g["toggle_btn"] = None
            g["color_btn"] = None

        while self._gauge_row_layout.count():
            item = self._gauge_row_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)

        for series_id, g in self._gauges.items():
            color = g["color"]
            color_btn = QPushButton()
            color_btn.setFixedSize(18, 26)
            color_btn.setToolTip(f"Change colour for {g.get('label', g['name'])}")
            color_btn.setStyleSheet(
                f"QPushButton {{ background: {color}; border: none; border-radius: 4px 0px 0px 4px; }}"
                "QPushButton:hover { border: 1px solid rgba(255,255,255,180); }"
            )
            color_btn.clicked.connect(lambda _=False, sid=series_id: self._pick_color(sid))

            prev_checked = self._visible.get(series_id, True)
            toggle_btn = QPushButton(f"● {g.get('label', g['name'])}")
            toggle_btn.setCheckable(True)
            toggle_btn.setChecked(prev_checked)
            toggle_btn.setFixedHeight(26)
            toggle_btn.setStyleSheet(_chip_toggle_style(color))
            toggle_btn.toggled.connect(lambda vis, sid=series_id: self._toggle_gauge(sid, vis))

            g["color_btn"] = color_btn
            g["toggle_btn"] = toggle_btn
            curve = g.get("curve")
            if curve is not None:
                curve.setVisible(prev_checked)

            chip = QWidget()
            chip_layout = QHBoxLayout(chip)
            chip_layout.setContentsMargins(0, 0, 0, 0)
            chip_layout.setSpacing(0)
            chip_layout.addWidget(color_btn)
            chip_layout.addWidget(toggle_btn)
            self._gauge_row_layout.addWidget(chip)

        self._gauge_row_layout.addStretch()

    def _pick_color(self, series_id: str) -> None:
        g = self._gauges.get(series_id)
        if g is None:
            return
        new_color = QColorDialog.getColor(QColor(g["color"]), self, f"Choose colour for {g.get('label', g['name'])}")
        if not new_color.isValid():
            return
        hex_color = new_color.name()
        device_id = g.get("device_id", series_id)
        self.set_gauge_color(device_id, hex_color)
        self.color_changed.emit(device_id, hex_color)

    def _toggle_gauge(self, series_id: str, visible: bool) -> None:
        self._visible[series_id] = visible
        g = self._gauges.get(series_id)
        if g is None:
            return
        curve = g.get("curve")
        if curve is not None:
            curve.setVisible(visible)
        device_id = g.get("device_id", series_id)
        self.gauge_toggled.emit(device_id, visible)
        self._refresh_y_axis()

    # ------------------------------------------------------------------
    # Internal — axis controls
    # ------------------------------------------------------------------

    def _on_y_mode_toggled(self, btn_id: int, checked: bool) -> None:
        if not checked:
            return
        self._y_mode = {0: "auto_center", 1: "custom", 2: "full_scale"}[btn_id]
        self._y_custom_box.setVisible(self._y_mode == "custom")
        self._refresh_y_axis()

    def _on_x_mode_toggled(self, btn_id: int, checked: bool) -> None:
        if not checked:
            return
        self._x_mode = {0: "all", 1: "window"}[btn_id]
        self._x_window_box.setVisible(self._x_mode == "window")
        self._refresh_x_axis()
        if self._y_mode == "auto_center":
            self._refresh_y_axis()

    def _on_x_window_changed(self, minutes: float) -> None:
        self._x_window_s = max(float(minutes) * 60.0, 1.0)
        if self._x_mode == "window":
            self._refresh_x_axis()
            if self._y_mode == "auto_center":
                self._refresh_y_axis()

    def _refresh_x_axis(self) -> None:
        x_lo, x_hi = self._current_x_range()
        if x_hi <= x_lo:
            return
        for plot in self._plots:
            plot.setXRange(x_lo, x_hi, padding=0.0)

    def _current_x_range(self) -> tuple[float, float]:
        latest = self._latest_t
        if latest <= 0.0:
            return (0.0, 1.0)
        lo = max(0.0, latest - self._x_window_s) if self._x_mode == "window" else 0.0
        hi = latest + max((latest - lo) * 0.01, 0.5)
        return (lo, hi)

    def _refresh_y_axis(self) -> None:
        if self._y_mode == "custom":
            lo = _parse_sci(self._y_min_edit.text())
            hi = _parse_sci(self._y_max_edit.text())
            if lo is None or hi is None or lo <= 0 or hi <= 0 or hi <= lo:
                return
            for plot in self._plots:
                plot.setYRange(math.log10(lo), math.log10(hi), padding=0.0)
            return

        if self._y_mode == "full_scale":
            lo_disp, hi_disp = self._full_scale_range_display()
            if lo_disp <= 0 or hi_disp <= lo_disp:
                return
            for plot in self._plots:
                plot.setYRange(math.log10(lo_disp), math.log10(hi_disp), padding=0.03)
            return

        lo, hi = self._visible_data_extent()
        if lo is None or hi is None:
            return
        lo = max(lo, 1e-12)
        if hi <= lo:
            hi = lo * 10.0
        log_lo = math.log10(lo)
        log_hi = math.log10(hi)
        pad = max((log_hi - log_lo) * 0.12, 0.15)
        for plot in self._plots:
            plot.setYRange(log_lo - pad, log_hi + pad, padding=0.0)

    def _visible_data_extent(self) -> tuple[float | None, float | None]:
        x_lo, x_hi = self._current_x_range()
        lo_overall: float | None = None
        hi_overall: float | None = None
        for device_id, g in self._gauges.items():
            if not self._visible.get(device_id, True):
                continue
            tb: deque = g["time_buf"]
            vb: deque = g["val_buf"]
            if not tb:
                continue
            ta = np.asarray(tb, dtype=float)
            va = np.asarray(vb, dtype=float)
            mask = (ta >= x_lo) & (ta <= x_hi) & (va > 0)
            va = va[mask]
            if va.size == 0:
                continue
            lo = float(np.min(va))
            hi = float(np.max(va))
            lo_overall = lo if lo_overall is None else min(lo_overall, lo)
            hi_overall = hi if hi_overall is None else max(hi_overall, hi)
        return (lo_overall, hi_overall)

    def _full_scale_range_display(self) -> tuple[float, float]:
        max_mbar = 0.0
        for g in self._devices.values():
            fs = g.get("full_scale_mbar")
            if fs and fs > max_mbar:
                max_mbar = float(fs)
        if max_mbar <= 0:
            max_mbar = 1100.0
        min_mbar = 1e-10
        lo = float(convert_pressure(min_mbar, "mbar", self._display_unit))
        hi = float(convert_pressure(max_mbar, "mbar", self._display_unit))
        return (lo, hi)

    # ------------------------------------------------------------------
    # Internal — crosshair
    # ------------------------------------------------------------------

    def _on_sync_toggled(self, enabled: bool) -> None:
        self._sync_hover = bool(enabled)
        self._sync_btn.setProperty("checked", self._sync_hover)
        self._sync_btn.style().unpolish(self._sync_btn)
        self._sync_btn.style().polish(self._sync_btn)

    def _set_compare_controls_enabled(self, enabled: bool) -> None:
        self._compare_a.setEnabled(enabled)
        self._compare_b.setEnabled(enabled)
        self._compare_help.setEnabled(enabled)

    def _on_compare_toggled(self, enabled: bool) -> None:
        self._comparison_enabled = bool(enabled)
        self._set_compare_controls_enabled(enabled)
        self._refresh_value_bar_for_compare()

    def _refresh_comparison_sources(self) -> None:
        current_a = self._compare_a.currentData()
        current_b = self._compare_b.currentData()
        self._compare_a.blockSignals(True)
        self._compare_b.blockSignals(True)
        self._compare_a.clear()
        self._compare_b.clear()
        for did, gauge in self._gauges.items():
            label = gauge.get("label", gauge["name"])
            self._compare_a.addItem(label, did)
            self._compare_b.addItem(label, did)
        if self._compare_a.count() >= 1:
            idx_a = self._compare_a.findData(current_a)
            self._compare_a.setCurrentIndex(max(idx_a, 0))
        if self._compare_b.count() >= 1:
            idx_b = self._compare_b.findData(current_b)
            if idx_b < 0:
                idx_b = 1 if self._compare_b.count() > 1 else 0
            self._compare_b.setCurrentIndex(idx_b)
        self._compare_a.blockSignals(False)
        self._compare_b.blockSignals(False)

    def _refresh_value_bar_for_compare(self) -> None:
        text = self._value_bar.text()
        if text:
            self._value_bar.setText(text)

    def _setup_crosshair_proxy(self) -> None:
        for plot in self._plots:
            line = pg.InfiniteLine(
                angle=90,
                movable=False,
                pen=pg.mkPen(color=(220, 220, 220, 160), width=1),
            )
            line.setVisible(False)
            plot.addItem(line, ignoreBounds=True)
            self._crosshair_lines[plot] = line

        if self._proxy is not None:
            try:
                self._proxy.disconnect()
            except (RuntimeError, AttributeError):
                pass
            self._proxy = None

        scene = self._glw.scene()
        if scene is not None:
            self._proxy = pg.SignalProxy(scene.sigMouseMoved, rateLimit=60, slot=self._on_mouse_moved)

    def _on_mouse_moved(self, event: tuple) -> None:
        pos = event[0]
        hovered_plot: pg.PlotItem | None = None
        x = 0.0
        for plot in self._plots:
            if plot.vb.sceneBoundingRect().contains(pos):
                mp = plot.vb.mapSceneToView(pos)
                x = float(mp.x())
                hovered_plot = plot
                break

        if hovered_plot is None:
            for line in self._crosshair_lines.values():
                line.setVisible(False)
            self._value_bar.hide()
            return

        if self._sync_hover:
            for line in self._crosshair_lines.values():
                line.setValue(x)
                line.setVisible(True)
            self._update_value_bar(x)
            return

        for plot, line in self._crosshair_lines.items():
            if plot is hovered_plot:
                line.setValue(x)
                line.setVisible(True)
            else:
                line.setVisible(False)
        self._update_value_bar(x, device_filter=self._plot_to_devices.get(hovered_plot, []))

    def _update_value_bar(self, x: float, device_filter: list[str] | None = None) -> None:
        if device_filter is None:
            series_ids = [did for did in self._gauges if self._visible.get(did, True)]
        else:
            series_ids = [did for did in device_filter if self._visible.get(did, True)]

        parts: list[str] = []
        for did in series_ids:
            g = self._gauges.get(did)
            if g is None:
                continue
            v = _value_at_x(g["time_buf"], g["val_buf"], x)
            if v is None:
                continue
            parts.append(
                f"<span style='color:{g['color']}'><b>{g.get('label', g['name'])}</b></span>: "
                f"{v:.3E} {self._display_unit}"
            )

        if self._comparison_enabled and self._compare_a.count() > 0 and self._compare_b.count() > 0:
            did_a = self._compare_a.currentData()
            did_b = self._compare_b.currentData()
            if isinstance(did_a, str) and isinstance(did_b, str) and did_a != did_b:
                ga = self._gauges.get(did_a)
                gb = self._gauges.get(did_b)
                if ga is not None and gb is not None:
                    va = _value_at_x(ga["time_buf"], ga["val_buf"], x)
                    vb = _value_at_x(gb["time_buf"], gb["val_buf"], x)
                    if va is not None and vb is not None and vb != 0:
                        delta = va - vb
                        ratio = va / vb
                        pct = (delta / abs(vb)) * 100.0
                        name_a = ga.get("label", ga["name"])
                        name_b = gb.get("label", gb["name"])
                        parts.append(
                            f"<span style='color:#E8D74C'><b>\u0394 ({name_a} \u2212 {name_b})</b></span>: "
                            f"{delta:.3E} {self._display_unit},  \u0394% = {pct:+.2f}%,  "
                            f"{name_a}/{name_b} = {ratio:.4g}"
                        )

        if parts:
            self._value_bar.setText("  |  ".join(parts))
            self._value_bar.show()
        else:
            self._value_bar.hide()

    def _replay_all(self) -> None:
        for did, g in self._gauges.items():
            curve = g.get("curve")
            tb: deque = g["time_buf"]
            vb: deque = g["val_buf"]
            if curve is None:
                continue
            if not tb:
                curve.setData([], [])
                continue
            t_arr = np.array(tb, dtype=float)
            v_arr = np.where(np.array(vb, dtype=float) > 0, np.array(vb, dtype=float), 1e-12)
            curve.setData(t_arr, v_arr)
            curve.setVisible(self._visible.get(did, True))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _chip_toggle_style(color: str) -> str:
    r = int(color[1:3], 16)
    g = int(color[3:5], 16)
    b = int(color[5:7], 16)
    return (
        f"QPushButton {{"
        f"  background-color: rgba({r},{g},{b},60);"
        f"  border: 1px solid {color};"
        f"  border-left: none;"
        f"  border-radius: 0px 10px 10px 0px;"
        f"  color: white;"
        f"  padding: 0px 10px;"
        f"  font-size: 11px;"
        f"  font-weight: bold;"
        f"}}"
        f"QPushButton:checked {{"
        f"  background-color: rgba({r},{g},{b},90);"
        f"}}"
        f"QPushButton:!checked {{"
        f"  background-color: rgba(50,50,50,180);"
        f"  border-color: #555555;"
        f"  border-left: none;"
        f"  color: #777777;"
        f"}}"
        f"QPushButton:hover {{ border-width: 2px; border-left: none; }}"
    )


def _series_color(base_color: str, index: int) -> str:
    color = QColor(base_color)
    if not color.isValid():
        return COLOR_PALETTE[index % len(COLOR_PALETTE)]
    hue, sat, val, alpha = color.getHsv()
    hue = (hue + index * 34) % 360 if hue >= 0 else (index * 34) % 360
    sat = min(255, max(90, sat + 20))
    val = min(255, max(130, val + (18 if index % 2 else -10)))
    color.setHsv(hue, sat, val, alpha)
    return color.name()


def _value_at_x(time_buf: deque, val_buf: deque, x: float) -> float | None:
    if not time_buf:
        return None
    ta = np.array(time_buf, dtype=float)
    idx = int(np.searchsorted(ta, x))
    idx = min(max(idx, 0), len(ta) - 1)
    return float(np.array(val_buf, dtype=float)[idx])


def _hline() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    theme = current_theme()
    line.setStyleSheet(f"color: {theme.border}; background: {theme.border}; max-height: 1px;")
    return line


def _make_sci_edit(initial: str) -> QLineEdit:
    edit = QLineEdit(initial)
    edit.setFixedHeight(22)
    edit.setStyleSheet(line_edit_style())
    v = QDoubleValidator(1e-20, 1e20, 12, edit)
    v.setNotation(QDoubleValidator.Notation.ScientificNotation)
    edit.setValidator(v)
    return edit


def _controls_panel_style(theme) -> str:
    return (
        "QFrame#CombinedPlotControls {"
        f"background: {theme.panel}; border: 1px solid {theme.border}; border-radius: 8px;"
        "}"
        f"QLabel {{ color: {theme.text}; }}"
        f"QLabel[section='true'] {{ color: {theme.text}; font-weight: 600; font-size: 12px; padding: 2px 0 0 0; }}"
        f"QRadioButton {{ color: {theme.text}; font-size: 11px; }}"
        f"QPushButton#ClearBtn {{ background: {theme.danger_bg}; color: {theme.danger_fg}; border: 1px solid {theme.border}; border-radius: 4px; padding: 6px 8px; font-weight: 600; }}"
        f"QPushButton#ClearBtn:hover {{ background: {theme.control_hover}; }}"
        f"QPushButton[layout='true'] {{ border: 1px solid {theme.border}; border-radius: 12px; padding: 2px 10px; color: {theme.text}; background: {theme.control}; }}"
        "QPushButton[layout='true'][checked='true'] { border: 1px solid #4DB2FF; color: #FFFFFF; background: #1F6FA5; }"
        f"QPushButton#SyncBtn {{ border: 1px solid {theme.border}; border-radius: 12px; padding: 4px 10px; color: {theme.text}; background: {theme.control}; }}"
        f"QPushButton#SyncBtn[checked='true'] {{ border-color: #5BC38F; background: {theme.success_bg}; color: {theme.success_fg}; }}"
    )


def _parse_sci(text: str) -> float | None:
    text = text.strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _format_sci(value: float) -> str:
    if value == 0:
        return "0"
    if 1e-3 <= abs(value) < 1e4:
        return f"{value:g}"
    return f"{value:.3e}"
"""
CombinedTab — shows multiple gauges' pressure readings on a single shared plot.

Used for:
  • "Main" tab (real connected gauges)
  • "Combined Simulation" tab (simulated gauges)

Each gauge gets a user-assignable color displayed via a two-part chip:
  • Left part: small colored square that opens QColorDialog when clicked
  • Right part: toggle button showing the gauge name

Clicking the color swatch emits ``color_changed(device_id, hex_color)`` so that
MainWindow can propagate the change to the individual GaugeTab and the tab bar.
Calling ``set_gauge_color(device_id, hex_color)`` updates the swatch and plot
curve without re-emitting the signal (avoiding feedback loops).

The right side of the plot exposes interactive axis controls:
  • Y axis mode: auto-center on min/max of visible readings (default),
    custom user-supplied min/max, or union of attached gauges' full-scale
    ranges.
  • X axis mode: entire recording (default, dynamic), or a rolling window
    of the last N minutes.
  • Clear button wipes all buffered readings.
"""

