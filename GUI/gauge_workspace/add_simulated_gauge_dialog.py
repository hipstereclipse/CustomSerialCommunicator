"""
AddSimulatedGaugeDialog — modal dialog used to configure a new simulated gauge.

The dialog is the only place a :class:`SimulatedGaugeConfig` is produced in
the UI.  On accept, :meth:`result_config` returns a fully validated config;
the caller (``MainWindow._on_add_simulated_gauge``) then hands it to a
:class:`SimulatedGaugeWorker`.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout,
    QGroupBox, QLabel, QLineEdit, QVBoxLayout, QWidget,
)

from serial_comm.device_registry import DeviceRegistry
from serial_comm.simulation_models import (
    CDGFullScaleOption,
    SimulatedGaugeConfig,
    SimulationPattern,
    cdg_full_scale_options,
)

from GUI.gauge_workspace.recipe_editor import RecipeEditorWidget


# INFICON brand colours used consistently for every simulated-gauge UI touch.
INFICON_BLUE = "#009CDE"
INFICON_BLUE_HOVER = "#33B5E5"


class AddSimulatedGaugeDialog(QDialog):
    """Configure a new simulated gauge.

    Parameters
    ----------
    registry:
        Shared :class:`DeviceRegistry` — used to populate the model combo.
    parent:
        Parent widget.
    """

    def __init__(
        self,
        registry: DeviceRegistry,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add Simulated Gauge")
        self.setMinimumWidth(540)
        self._registry = registry
        self._config: SimulatedGaugeConfig | None = None

        self._build_ui()
        self._populate_models()
        self._on_model_changed(self._model_combo.currentText())
        self._on_pattern_changed(self._pattern_combo.currentText())
        self._autofill_name()

    # ------------------------------------------------------------------
    # Build UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(10)

        header = QLabel(
            "<b style='color:%s'>⊕ Simulated gauge</b> — "
            "no real serial port is opened." % INFICON_BLUE
        )
        header.setTextFormat(Qt.TextFormat.RichText)
        root.addWidget(header)

        # ── General ────────────────────────────────────────────────────
        general = QGroupBox("General")
        form = QFormLayout(general)

        self._model_combo = QComboBox()
        self._model_combo.currentTextChanged.connect(self._on_model_changed)
        self._model_combo.currentTextChanged.connect(self._autofill_name)
        form.addRow("Model:", self._model_combo)

        self._cdg_fs_combo = QComboBox()
        self._cdg_fs_label = QLabel("CDG full scale:")
        # Populated per-model in _on_model_changed; seeded here as a placeholder.
        self._cdg_fs_combo.addItem("—", 1.333)
        form.addRow(self._cdg_fs_label, self._cdg_fs_combo)

        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText("e.g. SIM – PPG550")
        form.addRow("Display name:", self._name_edit)

        self._poll_spin = QDoubleSpinBox()
        self._poll_spin.setRange(0.1, 10.0)
        self._poll_spin.setDecimals(2)
        self._poll_spin.setValue(1.0)
        self._poll_spin.setSuffix(" s")
        form.addRow("Poll interval:", self._poll_spin)
        root.addWidget(general)

        # ── Pattern ────────────────────────────────────────────────────
        pattern_grp = QGroupBox("Simulation Pattern")
        p_layout = QVBoxLayout(pattern_grp)
        combo_form = QFormLayout()
        self._pattern_combo = QComboBox()
        self._pattern_combo.addItems([p.value for p in SimulationPattern])
        self._pattern_combo.currentTextChanged.connect(self._on_pattern_changed)
        combo_form.addRow("Pattern:", self._pattern_combo)
        p_layout.addLayout(combo_form)

        # Parameter widgets — each is shown/hidden per pattern.
        self._params_form = QFormLayout()
        self._base_spin = QDoubleSpinBox()
        self._base_spin.setRange(1e-12, 1.1e3)
        self._base_spin.setDecimals(9)
        self._base_spin.setValue(1e-6)
        self._base_spin.setSuffix(" mbar")
        self._base_row_label = QLabel("Base pressure:")
        self._params_form.addRow(self._base_row_label, self._base_spin)

        self._leak_spin = QDoubleSpinBox()
        self._leak_spin.setRange(0.0, 1.0e3)
        self._leak_spin.setDecimals(6)
        self._leak_spin.setValue(0.0)
        self._leak_spin.setSuffix(" mbar·L/s")
        self._leak_row_label = QLabel("Leak rate:")
        self._params_form.addRow(self._leak_row_label, self._leak_spin)
        p_layout.addLayout(self._params_form)

        self._recipe_editor = RecipeEditorWidget()
        p_layout.addWidget(self._recipe_editor)
        root.addWidget(pattern_grp)

        # ── Buttons ────────────────────────────────────────────────────
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        ok_btn = buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok_btn.setText("⊕ Add Simulated")
        ok_btn.setStyleSheet(
            f"QPushButton {{ background:{INFICON_BLUE}; color:white; "
            f"border-radius:6px; padding:5px 14px; font-weight:bold; }}"
            f"QPushButton:hover {{ background:{INFICON_BLUE_HOVER}; }}"
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    # ------------------------------------------------------------------
    # Population helpers
    # ------------------------------------------------------------------

    def _populate_models(self) -> None:
        models = self._registry.all_models()
        self._model_combo.addItems(models)

    def _autofill_name(self) -> None:
        current = self._name_edit.text().strip()
        model = self._model_combo.currentText()
        auto = f"SIM – {model}"
        # Only overwrite if the field still holds the previous auto-value
        # (so the user's manual edits survive model-combo changes).
        if not current or current.startswith("SIM – "):
            self._name_edit.setText(auto)

    def _on_model_changed(self, model: str) -> None:
        is_cdg = model.upper().startswith("CDG")
        self._cdg_fs_label.setVisible(is_cdg)
        self._cdg_fs_combo.setVisible(is_cdg)
        if is_cdg:
            # Repopulate with per-model labeled options (both Torr and mbar heads).
            options: tuple[CDGFullScaleOption, ...] = cdg_full_scale_options(model)
            self._cdg_fs_combo.blockSignals(True)
            self._cdg_fs_combo.clear()
            for opt in options:
                self._cdg_fs_combo.addItem(opt.label, opt.mbar)
            # Default to index 2 (first 1-unit mbar head — a common choice).
            default_idx = min(2, len(options) - 1)
            self._cdg_fs_combo.setCurrentIndex(default_idx)
            self._cdg_fs_combo.blockSignals(False)

    # ------------------------------------------------------------------
    # Pattern-specific UI visibility
    # ------------------------------------------------------------------

    def _on_pattern_changed(self, text: str) -> None:
        pattern = SimulationPattern(text)
        show_base = pattern in (
            SimulationPattern.PUMPDOWN,
            SimulationPattern.LEAK,
            SimulationPattern.ARGON_ENVIRONMENT,
        )
        show_leak = pattern is SimulationPattern.LEAK
        show_recipe = pattern is SimulationPattern.CUSTOM

        self._base_row_label.setVisible(show_base)
        self._base_spin.setVisible(show_base)
        self._leak_row_label.setVisible(show_leak)
        self._leak_spin.setVisible(show_leak)
        self._recipe_editor.setVisible(show_recipe)

    # ------------------------------------------------------------------
    # Accept / result
    # ------------------------------------------------------------------

    def _on_accept(self) -> None:
        model = self._model_combo.currentText().strip()
        name = self._name_edit.text().strip() or f"SIM – {model}"
        pattern = SimulationPattern(self._pattern_combo.currentText())
        recipe = self._recipe_editor.get_steps()
        # CUSTOM must have at least one step; others don't need one.
        if pattern is SimulationPattern.CUSTOM and not recipe:
            # Fall back to a single 60-s flat step so the engine still works.
            from serial_comm.simulation_models import RecipeStep
            recipe = [RecipeStep("Default", 60.0, 1e-3, 1e-3, "flat")]

        self._config = SimulatedGaugeConfig(
            model=model,
            display_name=name,
            pattern=pattern,
            recipe_steps=recipe,
            base_pressure_mbar=float(self._base_spin.value()),
            leak_rate_mbar_l_s=float(self._leak_spin.value()),
            poll_interval_s=float(self._poll_spin.value()),
            cdg_full_scale_mbar=(
                float(self._cdg_fs_combo.currentData())
                if model.upper().startswith("CDG")
                else None
            ),
        )
        self.accept()

    def result_config(self) -> SimulatedGaugeConfig | None:
        """Return the config the user accepted, or ``None`` if cancelled."""
        return self._config
