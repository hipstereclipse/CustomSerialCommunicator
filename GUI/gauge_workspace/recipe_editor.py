"""
RecipeEditorWidget — table-based editor for a :class:`SimulationPattern.CUSTOM`
recipe.

Shared by :class:`AddSimulatedGaugeDialog` (where the user picks an initial
recipe when adding a simulated gauge) and :class:`SimulationControlTab`
(where the live engine recipe can be edited).  ``steps_changed`` is emitted
after every user edit so callers can push updates to the engine immediately.
"""

from __future__ import annotations

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox, QDoubleSpinBox, QHBoxLayout, QHeaderView,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from serial_comm.simulation_models import RecipeStep


_INTERP_CHOICES = ("linear", "exponential", "flat")
_COL_NAME, _COL_DUR, _COL_START, _COL_END, _COL_INTERP = range(5)


_RECIPE_PRESETS: dict[str, list[RecipeStep]] = {
    "Semiconductor Loadlock": [
        RecipeStep("Loadlock Vent", 25.0, 8e-6, 950.0, "linear"),
        RecipeStep("Rough Pump", 95.0, 950.0, 2.5e-1, "exponential"),
        RecipeStep("Turbo Crossover", 140.0, 2.5e-1, 9e-6, "exponential"),
        RecipeStep("Transfer Hold", 90.0, 9e-6, 1.4e-5, "linear"),
        RecipeStep("Door Crack Spike", 8.0, 1.4e-5, 4e-4, "linear"),
        RecipeStep("Recover", 60.0, 4e-4, 1.2e-5, "exponential"),
    ],
    "PVD / Sputter Process": [
        RecipeStep("Chamber Pumpdown", 180.0, 1013.0, 7e-6, "exponential"),
        RecipeStep("Argon Backfill", 20.0, 7e-6, 4e-3, "linear"),
        RecipeStep("Throttle Stabilize", 80.0, 4e-3, 3.2e-3, "linear"),
        RecipeStep("Sputter Drift", 300.0, 3.2e-3, 3.8e-3, "linear"),
        RecipeStep("Gas Shutoff", 15.0, 3.8e-3, 2e-4, "linear"),
        RecipeStep("Base Recovery", 140.0, 2e-4, 1.2e-5, "exponential"),
    ],
    "RAC / Leak Detection": [
        RecipeStep("Rough Evacuation", 120.0, 1013.0, 1.5, "exponential"),
        RecipeStep("Deep Pull", 140.0, 1.5, 2e-2, "exponential"),
        RecipeStep("Isolation Hold", 220.0, 2e-2, 2.6e-2, "linear"),
        RecipeStep("Helium Spray Event", 18.0, 2.6e-2, 7.2e-2, "linear"),
        RecipeStep("Post-spray Decay", 75.0, 7.2e-2, 2.4e-2, "exponential"),
    ],
    "General Vacuum Commissioning": [
        RecipeStep("Initial Pumpdown", 150.0, 1013.0, 8e-3, "exponential"),
        RecipeStep("Overnight Hold", 420.0, 8e-3, 1.6e-2, "linear"),
        RecipeStep("Leak Tightening", 180.0, 1.6e-2, 1.2e-3, "exponential"),
        RecipeStep("Fine Pump", 220.0, 1.2e-3, 2e-5, "exponential"),
        RecipeStep("Outgassing Bump", 90.0, 2e-5, 7e-5, "linear"),
        RecipeStep("Conditioned Base", 240.0, 7e-5, 1.6e-5, "exponential"),
    ],
}

_OPERATION_SNIPPETS: dict[str, RecipeStep] = {
    "Roughing Pump Stage": RecipeStep("Roughing Pump", 120.0, 1013.0, 4e-1, "exponential"),
    "Turbo Pump Stage": RecipeStep("Turbo Pump", 180.0, 4e-1, 8e-6, "exponential"),
    "Semiconductor Cleaning Hold": RecipeStep("Cleaning Hold", 180.0, 2e-2, 1.8e-2, "linear"),
    "PVD Argon Backfill": RecipeStep("Argon Backfill", 18.0, 8e-6, 4.5e-3, "linear"),
    "PVD Process Hold": RecipeStep("Deposition Hold", 240.0, 3.4e-3, 3.8e-3, "linear"),
    "RAC Isolation Hold": RecipeStep("Isolation Hold", 180.0, 2e-2, 2.6e-2, "linear"),
    "RAC Leak Spike": RecipeStep("Leak Spike", 15.0, 2.6e-2, 7.5e-2, "linear"),
    "Recovery Stage": RecipeStep("Recovery", 120.0, 5e-4, 1.5e-5, "exponential"),
}


class RecipeEditorWidget(QWidget):
    """Editable step table for a CUSTOM simulation pattern."""

    steps_changed = pyqtSignal()  # fires after every meaningful edit

    def __init__(
        self,
        steps: list[RecipeStep] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._suppress_signal = False
        self._build_ui()
        self.set_steps(steps or [])

    # ------------------------------------------------------------------
    # Build UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        preset_row = QHBoxLayout()
        self._preset_combo = QComboBox(self)
        self._preset_combo.addItem("Custom")
        self._preset_combo.addItems(_RECIPE_PRESETS.keys())
        self._preset_apply_btn = QPushButton("Load Preset")
        self._preset_apply_btn.clicked.connect(self._on_apply_preset)
        self._snippet_combo = QComboBox(self)
        self._snippet_combo.addItems(_OPERATION_SNIPPETS.keys())
        self._snippet_btn = QPushButton("Insert Operation")
        self._snippet_btn.clicked.connect(self._on_insert_snippet)
        preset_row.addWidget(self._preset_combo)
        preset_row.addWidget(self._preset_apply_btn)
        preset_row.addSpacing(8)
        preset_row.addWidget(self._snippet_combo)
        preset_row.addWidget(self._snippet_btn)
        preset_row.addStretch()
        root.addLayout(preset_row)

        self._table = QTableWidget(0, 5, self)
        self._table.setHorizontalHeaderLabels(
            ["Name", "Duration (s)", "Start (mbar)", "End (mbar)", "Interpolation"]
        )
        self._table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self._table.itemChanged.connect(self._on_item_changed)
        root.addWidget(self._table)

        btns = QHBoxLayout()
        self._btn_add = QPushButton("+ Add step")
        self._btn_add.clicked.connect(self._on_add)
        self._btn_remove = QPushButton("− Remove")
        self._btn_remove.clicked.connect(self._on_remove)
        self._btn_up = QPushButton("▲ Up")
        self._btn_up.clicked.connect(lambda: self._move_selected(-1))
        self._btn_down = QPushButton("▼ Down")
        self._btn_down.clicked.connect(lambda: self._move_selected(+1))
        for b in (self._btn_add, self._btn_remove, self._btn_up, self._btn_down):
            btns.addWidget(b)
        btns.addStretch()
        root.addLayout(btns)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_steps(self) -> list[RecipeStep]:
        """Extract the current table contents as a list of :class:`RecipeStep`."""
        steps: list[RecipeStep] = []
        for row in range(self._table.rowCount()):
            name_item = self._table.item(row, _COL_NAME)
            dur_w = self._table.cellWidget(row, _COL_DUR)
            start_w = self._table.cellWidget(row, _COL_START)
            end_w = self._table.cellWidget(row, _COL_END)
            interp_w = self._table.cellWidget(row, _COL_INTERP)
            if dur_w is None or start_w is None or end_w is None or interp_w is None:
                continue
            steps.append(RecipeStep(
                name=name_item.text() if name_item else f"Step {row + 1}",
                duration_s=float(dur_w.value()),
                start_pressure_mbar=float(start_w.value()),
                end_pressure_mbar=float(end_w.value()),
                interpolation=interp_w.currentText(),
            ))
        return steps

    def set_steps(self, steps: list[RecipeStep]) -> None:
        """Replace the table contents with ``steps``."""
        self._suppress_signal = True
        try:
            self._table.setRowCount(0)
            for step in steps:
                self._append_row(step)
        finally:
            self._suppress_signal = False

    # ------------------------------------------------------------------
    # Row management
    # ------------------------------------------------------------------

    def _append_row(self, step: RecipeStep) -> None:
        row = self._table.rowCount()
        self._table.insertRow(row)

        name_item = QTableWidgetItem(step.name)
        self._table.setItem(row, _COL_NAME, name_item)

        dur = QDoubleSpinBox()
        dur.setRange(0.0, 3600.0)
        dur.setDecimals(2)
        dur.setValue(step.duration_s)
        dur.valueChanged.connect(self._emit_changed)
        self._table.setCellWidget(row, _COL_DUR, dur)

        start = _make_pressure_spin(step.start_pressure_mbar)
        start.valueChanged.connect(self._emit_changed)
        self._table.setCellWidget(row, _COL_START, start)

        end = _make_pressure_spin(step.end_pressure_mbar)
        end.valueChanged.connect(self._emit_changed)
        self._table.setCellWidget(row, _COL_END, end)

        combo = QComboBox()
        combo.addItems(_INTERP_CHOICES)
        combo.setCurrentText(step.interpolation)
        combo.currentTextChanged.connect(self._emit_changed)
        self._table.setCellWidget(row, _COL_INTERP, combo)

    def _on_add(self) -> None:
        row = self._table.rowCount()
        self._append_row(
            RecipeStep(
                name=f"Step {row + 1}",
                duration_s=10.0,
                start_pressure_mbar=1.0,
                end_pressure_mbar=1.0,
                interpolation="linear",
            )
        )
        self._emit_changed()

    def _on_remove(self) -> None:
        row = self._table.currentRow()
        if row < 0:
            return
        self._table.removeRow(row)
        self._emit_changed()

    def _move_selected(self, delta: int) -> None:
        row = self._table.currentRow()
        target = row + delta
        if row < 0 or not (0 <= target < self._table.rowCount()):
            return
        # Simplest reliable swap: capture both as RecipeStep, rebuild table.
        steps = self.get_steps()
        steps[row], steps[target] = steps[target], steps[row]
        self.set_steps(steps)
        self._table.selectRow(target)
        self._emit_changed()

    # ------------------------------------------------------------------
    # Change wiring
    # ------------------------------------------------------------------

    def _on_item_changed(self, _item: QTableWidgetItem) -> None:
        self._emit_changed()

    def _emit_changed(self, *_args) -> None:
        if not self._suppress_signal:
            self.steps_changed.emit()

    def _on_apply_preset(self) -> None:
        name = self._preset_combo.currentText()
        if name == "Custom":
            return
        preset = _RECIPE_PRESETS.get(name)
        if not preset:
            return
        self.set_steps([RecipeStep(**step.__dict__) for step in preset])
        self._emit_changed()

    def _on_insert_snippet(self) -> None:
        name = self._snippet_combo.currentText()
        snippet = _OPERATION_SNIPPETS.get(name)
        if snippet is None:
            return
        self._append_row(RecipeStep(**snippet.__dict__))
        self._table.selectRow(self._table.rowCount() - 1)
        self._emit_changed()


def _make_pressure_spin(value: float) -> QDoubleSpinBox:
    """Scientific-range pressure spinbox (extreme vacuum to atmosphere)."""
    sp = QDoubleSpinBox()
    sp.setRange(1e-12, 1.0e6)
    sp.setDecimals(6)
    sp.setValue(value)
    return sp
