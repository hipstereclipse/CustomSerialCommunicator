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
"""

from __future__ import annotations

import logging
from collections import deque

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QColorDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from serial_comm.units import SUPPORTED_UNITS, convert_pressure

logger = logging.getLogger(__name__)

_PLOT_HISTORY_S = 120.0
_MAX_POINTS = 2000

# Auto-assignment palette — 12 visually distinct colours.
COLOR_PALETTE: list[str] = [
    "#4C9BE8",  # blue
    "#E8954C",  # orange
    "#4CE87A",  # green
    "#E84C6F",  # red
    "#9B4CE8",  # purple
    "#E8D74C",  # yellow
    "#4CE8D7",  # cyan
    "#E84CCA",  # pink
    "#A8E84C",  # lime
    "#E8744C",  # salmon
    "#4C74E8",  # indigo
    "#E8C14C",  # gold
]


class CombinedTab(QWidget):
    """
    Combined multi-gauge pressure plot with per-gauge colour pickers.

    Signals
    -------
    color_changed(device_id: str, hex_color: str)
        Emitted when the user interactively picks a new colour for a gauge
        in *this* tab.  MainWindow connects this to propagate the change to
        the matching GaugeTab header swatch and the tab-bar label colour.
    """

    color_changed = pyqtSignal(str, str)
    #: Emitted when a gauge chip toggle is clicked by the user.
    #: Payload: (device_id, is_now_visible)
    gauge_toggled = pyqtSignal(str, bool)

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

        # device_id → {name, color, time_buf, val_buf, curve, color_btn, toggle_btn}
        self._gauges: dict[str, dict] = {}
        # Separate plain-bool visibility state — never holds Qt widget references.
        # feed() reads this instead of calling isChecked() on potentially-deleted widgets.
        self._visible: dict[str, bool] = {}
        self._t0: float | None = None

        # Crosshair
        self._crosshair_line: pg.InfiniteLine | None = None
        self._proxy: pg.SignalProxy | None = None

        self._build_ui()

    # ------------------------------------------------------------------
    # Build UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(4)

        # Header label
        desc = "simulated" if self._is_simulation else "real-time"
        hdr = QLabel(
            f"<b>{self._title}</b>"
            f"<span style='color:#888; font-size:11px'>"
            f"  —  combined {desc} pressure view</span>"
        )
        hdr.setTextFormat(Qt.TextFormat.RichText)
        hdr.setStyleSheet("font-size:13px; padding:4px 4px 0px 4px;")
        root.addWidget(hdr)

        # ── Plot ─────────────────────────────────────────────────────
        self._glw = pg.GraphicsLayoutWidget()
        self._glw.setBackground("#1E1E1E")
        self._glw.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        root.addWidget(self._glw)

        self._plot = self._glw.addPlot(row=0, col=0)
        self._plot.showGrid(x=True, y=True, alpha=0.3)
        self._plot.setLabel("bottom", "Time", units="s")
        self._plot.setLabel("left", "Pressure", units=self._display_unit)
        self._plot.setLogMode(x=False, y=True)
        self._legend = self._plot.addLegend()
        self._legend.setOffset((10, 10))

        # ── Crosshair value bar ───────────────────────────────────────
        self._value_bar = QLabel()
        self._value_bar.setTextFormat(Qt.TextFormat.RichText)
        self._value_bar.setStyleSheet(
            "color:#CCCCCC; font-size:11px; padding:1px 6px;"
            "background:#2A2A2A; border-top:1px solid #3A3A3A;"
        )
        self._value_bar.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        self._value_bar.setFixedHeight(18)
        self._value_bar.hide()
        root.addWidget(self._value_bar)

        # ── Per-gauge color/toggle chip row ───────────────────────────
        self._gauge_row_inner = QWidget()
        self._gauge_row_layout = QHBoxLayout(self._gauge_row_inner)
        self._gauge_row_layout.setContentsMargins(2, 2, 2, 2)
        self._gauge_row_layout.setSpacing(6)
        self._gauge_row_layout.addStretch()

        gauge_scroll = QScrollArea()
        gauge_scroll.setWidget(self._gauge_row_inner)
        gauge_scroll.setWidgetResizable(True)
        gauge_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        gauge_scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        gauge_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        gauge_scroll.setFixedHeight(46)
        root.addWidget(gauge_scroll)

        # ── Empty-state overlay ───────────────────────────────────────
        kind = "simulated" if self._is_simulation else "real"
        self._empty_label = QLabel(
            f"No {kind} gauges connected.\n"
            f"Use '{'⊕ Simulate' if self._is_simulation else '+ Gauge'}'"
            f" to add a gauge.",
            alignment=Qt.AlignmentFlag.AlignCenter,
        )
        self._empty_label.setStyleSheet(
            "color:#555; font-size:13px; padding:16px;"
        )
        root.addWidget(self._empty_label)

        self._setup_crosshair()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add_gauge(
        self, device_id: str, display_name: str, color: str
    ) -> None:
        """Register a gauge on this combined plot."""
        if device_id in self._gauges:
            return

        curve = self._plot.plot(
            pen=pg.mkPen(color, width=2),
            name=display_name,
        )
        self._gauges[device_id] = {
            "name": display_name,
            "color": color,
            "time_buf": deque(maxlen=_MAX_POINTS),
            "val_buf": deque(maxlen=_MAX_POINTS),
            "curve": curve,
            "color_btn": None,
            "toggle_btn": None,
        }
        self._visible.setdefault(device_id, True)
        self._rebuild_chip_row()
        self._empty_label.hide()

    def remove_gauge(self, device_id: str) -> None:
        """Unregister a gauge and remove its curve from the plot."""
        g = self._gauges.pop(device_id, None)
        if g is None:
            return
        self._visible.pop(device_id, None)
        self._plot.removeItem(g["curve"])
        # Remove from legend
        self._legend.removeItem(g["name"])
        # Null widget refs before rebuilding so teardown can't see stale pointers
        g["toggle_btn"] = None
        g["color_btn"] = None
        self._rebuild_chip_row()
        if not self._gauges:
            self._empty_label.show()

    def feed(self, device_id: str, t_mono: float, value: float) -> None:
        """Push a new pressure reading (already in display unit)."""
        g = self._gauges.get(device_id)
        if g is None:
            return
        if self._t0 is None:
            self._t0 = t_mono
        t_rel = t_mono - self._t0

        tb: deque = g["time_buf"]
        vb: deque = g["val_buf"]
        tb.append(t_rel)
        vb.append(value)

        cutoff = t_rel - _PLOT_HISTORY_S
        while tb and tb[0] < cutoff:
            tb.popleft()
            vb.popleft()

        # Use the plain bool dict — never touch the widget reference from a worker thread
        if not self._visible.get(device_id, True):
            return

        t_arr = np.array(tb, dtype=float)
        v_arr = np.array(vb, dtype=float)
        v_arr = np.where(v_arr > 0, v_arr, 1e-12)
        g["curve"].setData(t_arr, v_arr)

    def set_gauge_color(self, device_id: str, color: str) -> None:
        """Update a gauge's colour externally without emitting color_changed."""
        g = self._gauges.get(device_id)
        if g is None:
            return
        g["color"] = color
        g["curve"].setPen(pg.mkPen(color, width=2))
        self._rebuild_chip_row()

    def set_display_unit(self, unit: str) -> None:
        """Rescale buffered pressure values and update axis label."""
        if unit not in SUPPORTED_UNITS or unit == self._display_unit:
            return
        old_unit = self._display_unit
        self._display_unit = unit
        self._plot.setLabel("left", "Pressure", units=unit)

        for g in self._gauges.values():
            vb: deque = g["val_buf"]
            rescaled = [convert_pressure(v, old_unit, unit) for v in vb]
            vb.clear()
            vb.extend(rescaled)
        self._replay_all()

    # ------------------------------------------------------------------
    # Internal — chip row
    # ------------------------------------------------------------------

    def _rebuild_chip_row(self) -> None:
        """Remove and recreate every per-gauge chip widget."""
        # Step 1: save visibility states and null out widget refs BEFORE teardown.
        # This prevents stale C++ pointer access when Qt deletes child widgets
        # as the chip containers lose all Python references after setParent(None).
        for device_id, g in self._gauges.items():
            toggle_widget = g["toggle_btn"]
            if toggle_widget is not None:
                self._visible[device_id] = toggle_widget.isChecked()
            g["toggle_btn"] = None
            g["color_btn"] = None

        # Step 2: now safe to clear the layout — chip containers can be GC'd freely.
        while self._gauge_row_layout.count():
            item = self._gauge_row_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)  # type: ignore[arg-type]

        for device_id, g in self._gauges.items():
            color = g["color"]

            # ── Color swatch (left side of chip) ─────────────────────
            color_btn = QPushButton()
            color_btn.setFixedSize(18, 26)
            color_btn.setToolTip(f"Change colour for {g['name']}")
            color_btn.setStyleSheet(
                f"QPushButton {{"
                f"  background: {color};"
                f"  border: none;"
                f"  border-radius: 4px 0px 0px 4px;"
                f"}}"
                f"QPushButton:hover {{"
                f"  border: 1px solid rgba(255,255,255,180);"
                f"}}"
            )
            color_btn.clicked.connect(
                lambda _=False, did=device_id: self._pick_color(did)
            )

            # ── Visibility toggle (right side of chip) ────────────────
            # Read from the plain bool dict — the old widget may already be gone.
            prev_checked = self._visible.get(device_id, True)

            toggle_btn = QPushButton(f"● {g['name']}")
            toggle_btn.setCheckable(True)
            toggle_btn.setChecked(prev_checked)
            toggle_btn.setFixedHeight(26)
            toggle_btn.setToolTip(f"Show / hide {g['name']}")
            toggle_btn.setStyleSheet(_chip_toggle_style(color))
            toggle_btn.toggled.connect(
                lambda vis, did=device_id: self._toggle_gauge(did, vis)
            )

            g["color_btn"] = color_btn
            g["toggle_btn"] = toggle_btn

            # Apply current visibility to the curve
            g["curve"].setVisible(prev_checked)

            # ── Wrap in a horizontal pair ─────────────────────────────
            chip = QWidget()
            chip_layout = QHBoxLayout(chip)
            chip_layout.setContentsMargins(0, 0, 0, 0)
            chip_layout.setSpacing(0)
            chip_layout.addWidget(color_btn)
            chip_layout.addWidget(toggle_btn)
            self._gauge_row_layout.addWidget(chip)

        self._gauge_row_layout.addStretch()

    # ------------------------------------------------------------------
    # Internal — interactions
    # ------------------------------------------------------------------

    def _pick_color(self, device_id: str) -> None:
        g = self._gauges.get(device_id)
        if g is None:
            return
        initial = QColor(g["color"])
        new_color = QColorDialog.getColor(
            initial,
            self,
            f"Choose colour for {g['name']}",
        )
        if not new_color.isValid():
            return
        hex_color = new_color.name()  # "#rrggbb"
        # Update visuals without re-entering the signal chain
        self.set_gauge_color(device_id, hex_color)
        # Notify MainWindow so it can propagate to the individual GaugeTab
        self.color_changed.emit(device_id, hex_color)

    def _toggle_gauge(self, device_id: str, visible: bool) -> None:
        self._visible[device_id] = visible
        g = self._gauges.get(device_id)
        if g is None:
            return
        g["curve"].setVisible(visible)
        self.gauge_toggled.emit(device_id, visible)

    def set_visible_filter(self, device_ids: set[str] | None) -> None:
        """Show only gauges in *device_ids*; pass None to show all.

        This drives visibility silently (no ``gauge_toggled`` signal) so that
        MainWindow can update the plot when the left-panel selection changes
        without creating a feedback loop.
        """
        for device_id, g in self._gauges.items():
            should_show = (device_ids is None) or (device_id in device_ids)
            self._visible[device_id] = should_show
            g["curve"].setVisible(should_show)
            toggle_btn = g.get("toggle_btn")
            if toggle_btn is not None:
                toggle_btn.blockSignals(True)
                toggle_btn.setChecked(should_show)
                toggle_btn.blockSignals(False)

    # ------------------------------------------------------------------
    # Internal — crosshair
    # ------------------------------------------------------------------

    def _setup_crosshair(self) -> None:
        vline = pg.InfiniteLine(
            angle=90,
            movable=False,
            pen=pg.mkPen(color=(220, 220, 220, 160), width=1),
        )
        vline.setVisible(False)
        self._plot.addItem(vline, ignoreBounds=True)
        self._crosshair_line = vline

        scene = self._glw.scene()
        if scene is not None:
            self._proxy = pg.SignalProxy(
                scene.sigMouseMoved,
                rateLimit=60,
                slot=self._on_mouse_moved,
            )

    def _on_mouse_moved(self, event: tuple) -> None:
        pos = event[0]
        if self._plot.vb.sceneBoundingRect().contains(pos):
            mp = self._plot.vb.mapSceneToView(pos)
            x = mp.x()
            if self._crosshair_line:
                self._crosshair_line.setValue(x)
                self._crosshair_line.setVisible(True)
            self._update_value_bar(x)
        else:
            if self._crosshair_line:
                self._crosshair_line.setVisible(False)
            self._value_bar.hide()

    def _update_value_bar(self, x: float) -> None:
        parts: list[str] = []
        for device_id, g in self._gauges.items():
            if not self._visible.get(device_id, True):
                continue
            v = _value_at_x(g["time_buf"], g["val_buf"], x)
            if v is None:
                continue
            colour = g["color"]
            parts.append(
                f"<span style='color:{colour}'>"
                f"<b>{g['name']}</b></span>: {v:.3E} {self._display_unit}"
            )
        if parts:
            self._value_bar.setText("  |  ".join(parts))
            self._value_bar.show()
        else:
            self._value_bar.hide()

    # ------------------------------------------------------------------
    # Internal — replay
    # ------------------------------------------------------------------

    def _replay_all(self) -> None:
        for g in self._gauges.values():
            tb: deque = g["time_buf"]
            vb: deque = g["val_buf"]
            if not tb:
                continue
            t_arr = np.array(tb, dtype=float)
            v_arr = np.array(vb, dtype=float)
            v_arr = np.where(v_arr > 0, v_arr, 1e-12)
            g["curve"].setData(t_arr, v_arr)


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


def _value_at_x(
    time_buf: deque, val_buf: deque, x: float
) -> float | None:
    if not time_buf:
        return None
    ta = np.array(time_buf, dtype=float)
    idx = int(np.searchsorted(ta, x))
    idx = min(max(idx, 0), len(ta) - 1)
    return float(np.array(val_buf, dtype=float)[idx])
