"""
SimulationControlTab — global cockpit for the shared :class:`SimulationEngine`.

One instance is created the first time a simulated gauge is added and sits in
the main right-side ``QTabWidget``.  It is responsible for:

* Showing the current "real" engine pressure, pattern, elapsed time, and
  (for CUSTOM) the progress through the current recipe step.
* Letting the user switch pattern on the fly, restart, pause/resume.
* Listing every active simulated gauge with a per-row Remove button.
* Editing the live CUSTOM recipe.
* Selecting the ambient gas species that drives Pirani gas-correction for
  every registered simulated gauge simultaneously.

The tab does not own any workers; it communicates with :class:`MainWindow`
via the :data:`remove_requested` signal and drives the engine directly.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox, QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
    QProgressBar, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
    QWidget,
)

from serial_comm.simulation_engine import SimulationEngine, get_engine
from serial_comm.simulation_models import (
    GasType,
    HumidityLevel,
    SimulatedGaugeConfig,
    SimulationPattern,
)
from serial_comm.units import convert_pressure, format_pressure

from GUI.gauge_workspace.recipe_editor import RecipeEditorWidget


INFICON_BLUE = "#009CDE"
INFICON_BLUE_HOVER = "#33B5E5"

# Human-friendly labels for the gas combo.
_GAS_LABELS: list[tuple[str, GasType]] = [
    ("Air / N₂", GasType.N2),
    ("Argon", GasType.AR),
    ("Helium", GasType.HE),
    ("CO₂", GasType.CO2),
]

_HUMIDITY_LABELS: list[tuple[str, HumidityLevel]] = [
    ("Low (dry / controlled)", HumidityLevel.LOW),
    ("Medium (typical lab)", HumidityLevel.MEDIUM),
    ("High (humid / exposed)", HumidityLevel.HIGH),
]


class SimulationControlTab(QWidget):
    """Global control widget for the shared simulation engine."""

    #: Emitted when the user clicks "Remove" on a row in the active-gauges
    #: table.  Payload is the :class:`SimulatedGaugeConfig.sim_id`.
    remove_requested = pyqtSignal(str)

    def __init__(
        self,
        engine: SimulationEngine | None = None,
        display_unit_getter=None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._engine = engine if engine is not None else get_engine()
        # The getter is injected so tests don't need QSettings and the
        # Settings module can keep ownership of the canonical key.
        self._get_display_unit = display_unit_getter or (lambda: "mbar")

        self._build_ui()
        self._wire()

        # Refresh timer — the engine advances continuously; we repaint at 2 Hz.
        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()

        self.refresh()

    # ------------------------------------------------------------------
    # Build UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)

        # ── Status ─────────────────────────────────────────────────────
        status_grp = QGroupBox("Simulation Status")
        status_form = QFormLayout(status_grp)
        self._pressure_label = QLabel("—")
        self._pressure_label.setTextFormat(Qt.TextFormat.RichText)
        status_form.addRow("Real pressure:", self._pressure_label)

        self._pattern_label = QLabel("—")
        status_form.addRow("Pattern:", self._pattern_label)

        self._elapsed_label = QLabel("0.0 s")
        status_form.addRow("Elapsed:", self._elapsed_label)

        self._step_progress = QProgressBar()
        self._step_progress.setRange(0, 1000)
        self._step_progress.setValue(0)
        self._step_progress.setFormat("%p %")
        status_form.addRow("Step progress:", self._step_progress)

        root.addWidget(status_grp)

        # ── Pattern controls ───────────────────────────────────────────
        ctrl_grp = QGroupBox("Pattern Controls")
        ctrl_layout = QHBoxLayout(ctrl_grp)
        ctrl_layout.addWidget(QLabel("Pattern:"))
        self._pattern_combo = QComboBox()
        self._pattern_combo.addItems([p.value for p in SimulationPattern])
        ctrl_layout.addWidget(self._pattern_combo)

        self._restart_btn = QPushButton("Restart")
        self._pause_btn = QPushButton("Pause")
        for b in (self._restart_btn, self._pause_btn):
            b.setStyleSheet(_button_style())
            ctrl_layout.addWidget(b)
        ctrl_layout.addStretch()

        ctrl_layout.addWidget(QLabel("Gas:"))
        self._gas_combo = QComboBox()
        for label, _ in _GAS_LABELS:
            self._gas_combo.addItem(label)
        ctrl_layout.addWidget(self._gas_combo)

        ctrl_layout.addWidget(QLabel("Humidity:"))
        self._humidity_combo = QComboBox()
        for label, _ in _HUMIDITY_LABELS:
            self._humidity_combo.addItem(label)
        # Default to MEDIUM
        self._humidity_combo.setCurrentIndex(1)
        self._humidity_combo.setToolTip(
            "Humidity increases adsorbed water vapour outgassing, slowing pumpdowns"
        )
        ctrl_layout.addWidget(self._humidity_combo)

        root.addWidget(ctrl_grp)

        # ── Active gauges table ────────────────────────────────────────
        self._gauges_grp = QGroupBox("Active Simulated Gauges")
        grp_layout = QVBoxLayout(self._gauges_grp)
        self._table = QTableWidget(0, 5, self)
        self._table.setHorizontalHeaderLabels(
            ["Name", "Model", "Pattern", "Status", ""]
        )
        self._table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self._table.horizontalHeader().setSectionResizeMode(
            4, QHeaderView.ResizeMode.ResizeToContents
        )
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        grp_layout.addWidget(self._table)
        root.addWidget(self._gauges_grp)

        # ── Live recipe editor (collapsible) ───────────────────────────
        self._recipe_grp = QGroupBox("Recipe Editor (CUSTOM)")
        self._recipe_grp.setCheckable(True)
        self._recipe_grp.setChecked(True)
        recipe_layout = QVBoxLayout(self._recipe_grp)
        self._recipe_editor = RecipeEditorWidget()
        recipe_layout.addWidget(self._recipe_editor)
        self._recipe_grp.toggled.connect(
            lambda checked: self._recipe_editor.setVisible(checked)
        )
        root.addWidget(self._recipe_grp)

        root.addStretch()

    # ------------------------------------------------------------------
    # Wire signals
    # ------------------------------------------------------------------

    def _wire(self) -> None:
        self._pattern_combo.currentTextChanged.connect(self._on_pattern_changed)
        self._restart_btn.clicked.connect(self._on_restart)
        self._pause_btn.clicked.connect(self._on_pause_toggle)
        self._gas_combo.currentIndexChanged.connect(self._on_gas_changed)
        self._humidity_combo.currentIndexChanged.connect(self._on_humidity_changed)
        self._recipe_editor.steps_changed.connect(self._on_recipe_changed)

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

    def _on_pattern_changed(self, text: str) -> None:
        pattern = SimulationPattern(text)
        if pattern is SimulationPattern.CUSTOM:
            self._engine.set_pattern(
                pattern,
                recipe_steps=self._recipe_editor.get_steps(),
                reset_clock=True,
            )
        else:
            self._engine.set_pattern(pattern, reset_clock=True)
        self.refresh()

    def _on_restart(self) -> None:
        self._engine.restart()
        self.refresh()

    def _on_pause_toggle(self) -> None:
        if self._engine.is_paused():
            self._engine.resume()
        else:
            self._engine.pause()
        self.refresh()

    def _on_gas_changed(self, index: int) -> None:
        if 0 <= index < len(_GAS_LABELS):
            self._engine.set_gas(_GAS_LABELS[index][1])

    def _on_humidity_changed(self, index: int) -> None:
        if 0 <= index < len(_HUMIDITY_LABELS):
            self._engine.set_humidity(_HUMIDITY_LABELS[index][1])

    def _on_recipe_changed(self) -> None:
        # Only push changes to the engine if CUSTOM is the active pattern.
        if self._engine.current_pattern() is SimulationPattern.CUSTOM:
            self._engine.set_recipe_steps(self._recipe_editor.get_steps())

    # ------------------------------------------------------------------
    # Periodic refresh
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        """Repaint status widgets + gauge table from engine state.

        Called from a 500 ms timer and whenever an action mutates the engine.
        """
        snap = self._engine.snapshot()
        display_unit = self._get_display_unit() or "mbar"

        # Pressure display (respect user unit preference)
        p_disp = convert_pressure(snap["pressure_mbar"], "mbar", display_unit)
        self._pressure_label.setText(
            f"<span style='color:{INFICON_BLUE}; font-weight:bold'>"
            f"{format_pressure(p_disp, display_unit)}</span>"
        )

        pat: SimulationPattern = snap["pattern"]
        self._pattern_label.setText(pat.value)
        self._elapsed_label.setText(f"{snap['elapsed_s']:.1f} s")

        # Sync pattern combo without firing slots.
        if self._pattern_combo.currentText() != pat.value:
            self._pattern_combo.blockSignals(True)
            self._pattern_combo.setCurrentText(pat.value)
            self._pattern_combo.blockSignals(False)

        # Step progress (only meaningful for CUSTOM).
        if pat is SimulationPattern.CUSTOM:
            self._step_progress.setEnabled(True)
            self._step_progress.setValue(int(snap["current_step_fraction"] * 1000))
            if snap["current_step_index"] >= 0 and snap["recipe_steps"]:
                step = snap["recipe_steps"][snap["current_step_index"]]
                self._step_progress.setFormat(
                    f"{step.name} — %p %"
                )
        else:
            self._step_progress.setEnabled(False)
            self._step_progress.setValue(0)
            self._step_progress.setFormat("n/a")

        # Pause button label
        self._pause_btn.setText("Resume" if snap["paused"] else "Pause")

        # Gas combo sync
        current_gas: GasType = snap["gas"]
        for idx, (_, gas) in enumerate(_GAS_LABELS):
            if gas is current_gas:
                if self._gas_combo.currentIndex() != idx:
                    self._gas_combo.blockSignals(True)
                    self._gas_combo.setCurrentIndex(idx)
                    self._gas_combo.blockSignals(False)
                break

        # Humidity combo sync
        current_humidity = snap.get("humidity")
        if current_humidity is not None:
            for idx, (_, hum) in enumerate(_HUMIDITY_LABELS):
                if hum is current_humidity:
                    if self._humidity_combo.currentIndex() != idx:
                        self._humidity_combo.blockSignals(True)
                        self._humidity_combo.setCurrentIndex(idx)
                        self._humidity_combo.blockSignals(False)
                    break

        # Recipe editor visibility + contents
        show_recipe = pat is SimulationPattern.CUSTOM
        self._recipe_grp.setVisible(show_recipe)
        if show_recipe:
            # Only rewrite the table when it looks stale — otherwise we'd
            # clobber the user's in-progress edits every 500 ms.
            current_steps = self._recipe_editor.get_steps()
            engine_steps = snap["recipe_steps"]
            if _steps_diverge(current_steps, engine_steps):
                self._recipe_editor.blockSignals(True)
                try:
                    self._recipe_editor.set_steps(engine_steps)
                finally:
                    self._recipe_editor.blockSignals(False)

        # Active gauges table
        self._rebuild_gauges_table(self._engine.registered())

    def _rebuild_gauges_table(self, gauges: list[SimulatedGaugeConfig]) -> None:
        self._table.setRowCount(len(gauges))
        for row, cfg in enumerate(gauges):
            name_item = QTableWidgetItem(cfg.display_name)
            name_item.setData(Qt.ItemDataRole.UserRole, cfg.sim_id)
            self._table.setItem(row, 0, name_item)
            self._table.setItem(row, 1, QTableWidgetItem(cfg.model))
            self._table.setItem(row, 2, QTableWidgetItem(cfg.pattern.value))
            self._table.setItem(row, 3, QTableWidgetItem("● running"))

            btn = QPushButton("Remove")
            btn.clicked.connect(
                lambda _checked=False, sid=cfg.sim_id: self.remove_requested.emit(sid)
            )
            self._table.setCellWidget(row, 4, btn)


def _button_style() -> str:
    return (
        f"QPushButton {{ background:{INFICON_BLUE}; color:white; "
        f"border-radius:5px; padding:4px 12px; font-weight:bold; }}"
        f"QPushButton:hover {{ background:{INFICON_BLUE_HOVER}; }}"
    )


def _steps_diverge(a, b) -> bool:
    """Return True if the two step lists have structurally different content."""
    if len(a) != len(b):
        return True
    for sa, sb in zip(a, b):
        if (
            sa.name != sb.name
            or abs(sa.duration_s - sb.duration_s) > 1e-9
            or abs(sa.start_pressure_mbar - sb.start_pressure_mbar) > 1e-12
            or abs(sa.end_pressure_mbar - sb.end_pressure_mbar) > 1e-12
            or sa.interpolation != sb.interpolation
        ):
            return True
    return False
