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
    "acquisition/default_poll_interval": 1.0,
    "serial/default_timeout": 2.0,
    "display/pressure_unit": "mbar",
}

_LOG_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


# ── Public helpers used by other modules ─────────────────────────────────────

def get_setting(key: str) -> object:
    """Return a setting value, using the default if not yet persisted."""
    s = QSettings()
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
        self._poll_spin.setRange(0.1, 60.0)
        self._poll_spin.setSingleStep(0.5)
        self._poll_spin.setDecimals(1)
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

    def _save_values(self) -> None:
        self._settings.setValue("logging/level", self._level_combo.currentText())
        self._settings.setValue("logging/max_terminal_messages", self._max_msg_spin.value())
        self._settings.setValue("logging/log_to_file", self._log_to_file_chk.isChecked())
        self._settings.setValue("logging/log_file_path", self._log_file_label.text())
        self._settings.setValue("acquisition/default_poll_interval", self._poll_spin.value())
        self._settings.setValue("serial/default_timeout", self._timeout_spin.value())
        self._settings.setValue("display/pressure_unit", self._unit_combo.currentText())

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
