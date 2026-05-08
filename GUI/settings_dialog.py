"""
SettingsDialog — application-wide settings editor.

Sections
--------
Logging
    • Log level (root logger)
    • Maximum number of messages buffered in terminal widgets
    • Optionally log to a rolling file

Acquisition
    • Default poll interval for new gauges

Serial
    • Default connection timeout

Diagnostics
    • Verbose OPG550 spectrum trace in terminal widgets
"""

from __future__ import annotations

import logging
import os

from PyQt6.QtCore import QObject, QSettings, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QGroupBox, QHBoxLayout, QLabel, QCheckBox, QComboBox,
    QDoubleSpinBox, QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

from serial_comm.units import SUPPORTED_UNITS

logger = logging.getLogger(__name__)

# ── Defaults (also used as fallback when QSettings has no value) ─────────────
DEFAULTS: dict[str, object] = {
    "logging/level": "DEBUG",
    "logging/max_terminal_messages": 500,
    "logging/log_to_file": False,
    "logging/log_file_path": "",
    "acquisition/default_poll_interval": 0.1,
    "serial/default_timeout": 2.0,
    "display/pressure_unit": "mbar",
    "diagnostics/opg_spectrum_verbose": False,
    # OPG550 plasma auto-control thresholds (in mbar). Defaults are conservative
    # placeholders; the user is expected to tune them in the Settings dialog or
    # the Spectrum Studio panel.
    "opg/auto_plasma_enabled": False,
    "opg/min_ignition_pressure_mbar": 1.0e-6,
    "opg/max_safe_pressure_mbar": 1.0e-2,
}

_LOG_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class ScientificPressureSpinBox(QDoubleSpinBox):
    """Pressure spinbox that renders values in scientific notation."""

    def textFromValue(self, value: float) -> str:  # type: ignore[override]
        return f"{float(value):.2e}"

    def valueFromText(self, text: str) -> float:  # type: ignore[override]
        clean = text.strip()
        if clean.endswith("mbar"):
            clean = clean[:-4].strip()
        try:
            return float(clean)
        except ValueError:
            return super().valueFromText(text)


# ── Public helpers used by other modules ─────────────────────────────────────

def get_setting(key: str) -> object:
    """Return a setting value, using the default if not yet persisted."""
    s = QSettings()
    if key == "acquisition/default_poll_interval":
        migrated = s.value("acquisition/default_poll_interval_migrated_v2", False)
        migrated_flag = (
            migrated.lower() in ("true", "1", "yes")
            if isinstance(migrated, str)
            else bool(migrated)
        )
        if not migrated_flag:
            try:
                raw_existing = s.value(key, None)
                existing = float(raw_existing) if raw_existing is not None else None
            except (TypeError, ValueError):
                existing = None
            # Upgrade legacy default from older builds.
            if existing is None or abs(existing - 1.0) < 1e-12:
                s.setValue(key, float(DEFAULTS["acquisition/default_poll_interval"]))
            s.setValue("acquisition/default_poll_interval_migrated_v2", True)

    default = DEFAULTS.get(key)
    raw = s.value(key, default)
    # QSettings may return strings for bools/ints; coerce to the default type.
    if default is not None:
        try:
            if isinstance(default, bool):
                if isinstance(raw, str):
                    return raw.lower() in ("true", "1", "yes")
                return bool(raw)
            if isinstance(default, int):
                return int(raw)
            if isinstance(default, float):
                return float(raw)
        except (ValueError, TypeError):
            return default
    return raw


def get_display_unit() -> str:
    """Return the user-selected display unit for pressure values.

    Always one of :data:`serial_comm.units.SUPPORTED_UNITS`.  Falls back to
    the default ("mbar") if the stored value is unrecognised (e.g. someone
    edited QSettings by hand).
    """
    raw = get_setting("display/pressure_unit")
    unit = str(raw).strip() if raw is not None else "mbar"
    if unit not in SUPPORTED_UNITS:
        return str(DEFAULTS["display/pressure_unit"])
    return unit


def get_opg_spectrum_verbose_diagnostics() -> bool:
    """Return whether OPG550 spectrum trace entries should be shown."""
    return bool(get_setting("diagnostics/opg_spectrum_verbose"))


class _DisplaySignals(QObject):
    """Tiny signals broker so open tabs can refresh when units change."""

    units_changed = pyqtSignal(str)  # payload: new unit string


#: Singleton instance — connect to ``display_signals.units_changed`` to be
#: notified when the user changes display units in the Settings dialog.
display_signals = _DisplaySignals()


def apply_log_level() -> None:
    """Apply the persisted log level to the root logger."""
    level_name = str(get_setting("logging/level"))
    level = getattr(logging, level_name, logging.DEBUG)
    logging.getLogger().setLevel(level)
    logger.debug("Log level set to %s", level_name)


def apply_log_file() -> None:
    """Add or remove a rotating file handler based on persisted settings."""
    root = logging.getLogger()

    # Remove any existing RotatingFileHandler we previously added
    for h in list(root.handlers):
        if getattr(h, "_csc_managed", False):
            root.removeHandler(h)
            h.close()

    if get_setting("logging/log_to_file"):
        path = str(get_setting("logging/log_file_path")).strip()
        if not path:
            return
        try:
            from logging.handlers import RotatingFileHandler
            fh = RotatingFileHandler(
                path,
                maxBytes=5 * 1024 * 1024,   # 5 MB
                backupCount=3,
                encoding="utf-8",
            )
            fh._csc_managed = True          # type: ignore[attr-defined]
            fh.setFormatter(logging.Formatter(
                "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
            ))
            root.addHandler(fh)
            logger.info("Logging to file: %s", path)
        except Exception as exc:
            logger.error("Cannot open log file %s: %s", path, exc)


# ── Dialog ───────────────────────────────────────────────────────────────────

class SettingsDialog(QDialog):
    """Modal settings editor. Call ``exec()`` to show it."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(420)
        self._settings = QSettings()
        self._build_ui()
        self._load_values()

    # ------------------------------------------------------------------
    # Build UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(10)

        root.addWidget(self._build_logging_group())
        root.addWidget(self._build_acquisition_group())
        root.addWidget(self._build_serial_group())
        root.addWidget(self._build_display_group())
        root.addWidget(self._build_opg_group())
        root.addWidget(self._build_diagnostics_group())

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Cancel |
            QDialogButtonBox.StandardButton.RestoreDefaults,
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        buttons.button(
            QDialogButtonBox.StandardButton.RestoreDefaults
        ).clicked.connect(self._on_restore_defaults)
        root.addWidget(buttons)

    def _build_logging_group(self) -> QGroupBox:
        grp = QGroupBox("Logging")
        form = QFormLayout(grp)

        # Log level
        self._level_combo = QComboBox()
        self._level_combo.addItems(_LOG_LEVELS)
        form.addRow("Log level:", self._level_combo)

        # Max terminal messages
        self._max_msg_spin = QSpinBox()
        self._max_msg_spin.setRange(50, 10_000)
        self._max_msg_spin.setSingleStep(50)
        self._max_msg_spin.setSuffix(" messages")
        self._max_msg_spin.setToolTip(
            "Maximum number of log messages kept in each terminal widget.\n"
            "Older messages are discarded when this limit is reached."
        )
        form.addRow("Terminal buffer:", self._max_msg_spin)

        # Log to file
        self._log_to_file_chk = QCheckBox("Enable")
        self._log_to_file_chk.toggled.connect(self._on_log_to_file_toggled)
        form.addRow("Log to file:", self._log_to_file_chk)

        file_row = QHBoxLayout()
        self._log_file_label = QLabel()
        self._log_file_label.setWordWrap(True)
        self._log_file_label.setMinimumWidth(200)
        file_row.addWidget(self._log_file_label, 1)
        self._browse_btn = QPushButton("Browse…")
        self._browse_btn.clicked.connect(self._on_browse_log_file)
        file_row.addWidget(self._browse_btn)
        form.addRow("Log file path:", file_row)

        return grp

    def _build_acquisition_group(self) -> QGroupBox:
        grp = QGroupBox("Acquisition")
        form = QFormLayout(grp)

        self._poll_spin = QDoubleSpinBox()
        self._poll_spin.setRange(0.01, 600.0)
        self._poll_spin.setSingleStep(0.01)
        self._poll_spin.setDecimals(2)
        self._poll_spin.setSuffix(" s")
        self._poll_spin.setToolTip("Default polling interval applied when adding a new gauge.")
        form.addRow("Default poll interval:", self._poll_spin)

        return grp

    def _build_serial_group(self) -> QGroupBox:
        grp = QGroupBox("Serial")
        form = QFormLayout(grp)

        self._timeout_spin = QDoubleSpinBox()
        self._timeout_spin.setRange(0.5, 30.0)
        self._timeout_spin.setSingleStep(0.5)
        self._timeout_spin.setDecimals(1)
        self._timeout_spin.setSuffix(" s")
        self._timeout_spin.setToolTip("Read/write timeout for serial port connections.")
        form.addRow("Connection timeout:", self._timeout_spin)

        return grp

    def _build_display_group(self) -> QGroupBox:
        grp = QGroupBox("Display Units")
        form = QFormLayout(grp)

        self._unit_combo = QComboBox()
        self._unit_combo.addItems(list(SUPPORTED_UNITS))
        self._unit_combo.setToolTip(
            "Pressure display unit used for both real and simulated gauges. "
            "Internal storage is always in mbar — this only affects display."
        )
        form.addRow("Pressure unit:", self._unit_combo)

        return grp

    def _build_opg_group(self) -> QGroupBox:
        grp = QGroupBox("OPG550 Plasma")
        form = QFormLayout(grp)

        self._opg_auto_plasma_chk = QCheckBox("Auto ignite/extinguish plasma based on pressure")
        self._opg_auto_plasma_chk.setToolTip(
            "When enabled, Spectrum Studio will automatically turn the plasma ON when\n"
            "pressure falls below the safe range and OFF when it rises above the max."
        )
        form.addRow("Auto plasma (default):", self._opg_auto_plasma_chk)

        self._opg_min_p_spin = ScientificPressureSpinBox()
        self._opg_min_p_spin.setRange(1e-12, 1e3)
        self._opg_min_p_spin.setSingleStep(1.0)
        self._opg_min_p_spin.setSuffix(" mbar")
        self._opg_min_p_spin.setToolTip(
            "Minimum total pressure required to allow plasma ignition (mbar).\n"
            "Below this pressure auto-plasma keeps the plasma OFF."
        )
        # Tiny step values won't fit a normal QDoubleSpinBox; allow scientific text.
        self._opg_min_p_spin.setStepType(QDoubleSpinBox.StepType.AdaptiveDecimalStepType)
        form.addRow("Min ignition pressure:", self._opg_min_p_spin)

        self._opg_max_p_spin = ScientificPressureSpinBox()
        self._opg_max_p_spin.setRange(1e-12, 1e3)
        self._opg_max_p_spin.setSingleStep(1.0)
        self._opg_max_p_spin.setSuffix(" mbar")
        self._opg_max_p_spin.setToolTip(
            "Maximum total pressure for safe plasma operation (mbar).\n"
            "Above this pressure auto-plasma forces the plasma OFF."
        )
        self._opg_max_p_spin.setStepType(QDoubleSpinBox.StepType.AdaptiveDecimalStepType)
        form.addRow("Max safe pressure:", self._opg_max_p_spin)

        return grp

    def _build_diagnostics_group(self) -> QGroupBox:
        grp = QGroupBox("Diagnostics")
        form = QFormLayout(grp)

        self._opg_spectrum_diag_chk = QCheckBox("Enable verbose OPG550 spectrum trace")
        self._opg_spectrum_diag_chk.setToolTip(
            "Show OPG550 SPEC request/response payload summaries and decoded pixel statistics "
            "in the gauge terminal while Spectrum Studio is polling live spectrum data."
        )
        form.addRow("OPG550 spectrum:", self._opg_spectrum_diag_chk)

        return grp

    # ------------------------------------------------------------------
    # Populate / persist
    # ------------------------------------------------------------------

    def _load_values(self) -> None:
        level = str(get_setting("logging/level"))
        idx = self._level_combo.findText(level)
        self._level_combo.setCurrentIndex(max(idx, 0))

        self._max_msg_spin.setValue(int(get_setting("logging/max_terminal_messages")))

        log_to_file = bool(get_setting("logging/log_to_file"))
        self._log_to_file_chk.setChecked(log_to_file)
        self._log_file_label.setText(str(get_setting("logging/log_file_path")))
        self._on_log_to_file_toggled(log_to_file)

        self._poll_spin.setValue(float(get_setting("acquisition/default_poll_interval")))
        self._timeout_spin.setValue(float(get_setting("serial/default_timeout")))

        unit = get_display_unit()
        idx = self._unit_combo.findText(unit)
        self._unit_combo.setCurrentIndex(max(idx, 0))

        self._opg_spectrum_diag_chk.setChecked(
            bool(get_setting("diagnostics/opg_spectrum_verbose"))
        )
        self._opg_auto_plasma_chk.setChecked(bool(get_setting("opg/auto_plasma_enabled")))
        self._opg_min_p_spin.setValue(float(get_setting("opg/min_ignition_pressure_mbar")))
        self._opg_max_p_spin.setValue(float(get_setting("opg/max_safe_pressure_mbar")))

    def _save_values(self) -> None:
        self._settings.setValue("logging/level", self._level_combo.currentText())
        self._settings.setValue("logging/max_terminal_messages", self._max_msg_spin.value())
        self._settings.setValue("logging/log_to_file", self._log_to_file_chk.isChecked())
        self._settings.setValue("logging/log_file_path", self._log_file_label.text())
        self._settings.setValue("acquisition/default_poll_interval", self._poll_spin.value())
        self._settings.setValue("serial/default_timeout", self._timeout_spin.value())
        self._settings.setValue("display/pressure_unit", self._unit_combo.currentText())
        self._settings.setValue(
            "diagnostics/opg_spectrum_verbose",
            self._opg_spectrum_diag_chk.isChecked(),
        )
        self._settings.setValue("opg/auto_plasma_enabled", self._opg_auto_plasma_chk.isChecked())
        self._settings.setValue(
            "opg/min_ignition_pressure_mbar", float(self._opg_min_p_spin.value())
        )
        self._settings.setValue(
            "opg/max_safe_pressure_mbar", float(self._opg_max_p_spin.value())
        )

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

    def _on_accept(self) -> None:
        prev_unit = get_display_unit()
        self._save_values()
        apply_log_level()
        apply_log_file()
        new_unit = self._unit_combo.currentText()
        if new_unit != prev_unit:
            display_signals.units_changed.emit(new_unit)
        self.accept()

    def _on_restore_defaults(self) -> None:
        level = str(DEFAULTS["logging/level"])
        self._level_combo.setCurrentIndex(self._level_combo.findText(level))
        self._max_msg_spin.setValue(int(DEFAULTS["logging/max_terminal_messages"]))  # type: ignore[arg-type]
        self._log_to_file_chk.setChecked(bool(DEFAULTS["logging/log_to_file"]))
        self._log_file_label.setText(str(DEFAULTS["logging/log_file_path"]))
        self._poll_spin.setValue(float(DEFAULTS["acquisition/default_poll_interval"]))  # type: ignore[arg-type]
        self._timeout_spin.setValue(float(DEFAULTS["serial/default_timeout"]))  # type: ignore[arg-type]
        idx = self._unit_combo.findText(str(DEFAULTS["display/pressure_unit"]))
        self._unit_combo.setCurrentIndex(max(idx, 0))
        self._opg_spectrum_diag_chk.setChecked(
            bool(DEFAULTS["diagnostics/opg_spectrum_verbose"])
        )

    def _on_log_to_file_toggled(self, checked: bool) -> None:
        self._log_file_label.setEnabled(checked)
        self._browse_btn.setEnabled(checked)

    def _on_browse_log_file(self) -> None:
        current = self._log_file_label.text() or os.path.expanduser("~")
        path, _ = QFileDialog.getSaveFileName(
            self, "Choose log file", current, "Log files (*.log *.txt);;All files (*)"
        )
        if path:
            self._log_file_label.setText(path)
