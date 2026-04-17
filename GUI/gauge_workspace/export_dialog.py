"""
ExportDialog — export recorded gauge readings.

Formats: CSV, Parquet, HDF5.
Unit selection: persisted via QSettings (default: mbar).
"""

from __future__ import annotations

import logging
from pathlib import Path

from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import (
    QDialog, QDialogButtonBox, QVBoxLayout, QFormLayout,
    QComboBox, QLabel, QFileDialog, QPushButton, QHBoxLayout,
    QProgressBar, QMessageBox,
)

from serial_comm.models import DeviceReading

logger = logging.getLogger(__name__)

_UNIT_OPTIONS = ["mbar", "Torr", "Pa", "psi"]
_FORMAT_OPTIONS = ["CSV (.csv)", "Parquet (.parquet)", "HDF5 (.h5)"]

_UNIT_CONVERSION: dict[str, float] = {
    "mbar": 1.0,
    "Torr": 0.750062,
    "Pa":   100.0,
    "psi":  0.014504,
}


class ExportDialog(QDialog):
    """Modal dialog for exporting recorded gauge data."""

    def __init__(self, readings: list[DeviceReading], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Export Data")
        self.setMinimumWidth(400)
        self._readings = readings
        self._settings = QSettings()
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        info = QLabel(f"<b>{len(self._readings)}</b> readings from "
                      f"<b>{len({r.device_id for r in self._readings})}</b> device(s)")
        layout.addWidget(info)

        form = QFormLayout()

        # Format selector
        self._fmt_combo = QComboBox()
        for f in _FORMAT_OPTIONS:
            self._fmt_combo.addItem(f)
        form.addRow("Format:", self._fmt_combo)

        # Unit selector (persisted)
        self._unit_combo = QComboBox()
        for u in _UNIT_OPTIONS:
            self._unit_combo.addItem(u)
        saved_unit = self._settings.value("export/unit", "mbar")
        idx = self._unit_combo.findText(saved_unit)
        if idx >= 0:
            self._unit_combo.setCurrentIndex(idx)
        form.addRow("Pressure unit:", self._unit_combo)

        layout.addLayout(form)

        # File path row
        path_row = QHBoxLayout()
        self._path_label = QLabel("(no file selected)")
        path_row.addWidget(self._path_label)
        browse_btn = QPushButton("Browse…")
        browse_btn.clicked.connect(self._on_browse)
        path_row.addWidget(browse_btn)
        layout.addLayout(path_row)

        self._progress = QProgressBar()
        self._progress.hide()
        layout.addWidget(self._progress)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_export)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._export_path: str | None = None

    def _on_browse(self) -> None:
        fmt = self._fmt_combo.currentText()
        if "CSV" in fmt:
            filter_ = "CSV (*.csv)"
            suffix = ".csv"
        elif "Parquet" in fmt:
            filter_ = "Parquet (*.parquet)"
            suffix = ".parquet"
        else:
            filter_ = "HDF5 (*.h5)"
            suffix = ".h5"

        path, _ = QFileDialog.getSaveFileName(self, "Export to", "", filter_)
        if path:
            if not path.endswith(suffix):
                path += suffix
            self._export_path = path
            self._path_label.setText(path)

    def _on_export(self) -> None:
        if not self._export_path:
            QMessageBox.warning(self, "No file", "Please select an output file.")
            return

        unit = self._unit_combo.currentText()
        self._settings.setValue("export/unit", unit)
        factor = _UNIT_CONVERSION.get(unit, 1.0)

        fmt = self._fmt_combo.currentText()

        try:
            self._progress.show()
            self._progress.setRange(0, 0)

            if "CSV" in fmt:
                self._export_csv(factor, unit)
            elif "Parquet" in fmt:
                self._export_parquet(factor, unit)
            else:
                self._export_hdf5(factor, unit)

            self._progress.setRange(0, 1)
            self._progress.setValue(1)
            QMessageBox.information(self, "Export complete",
                                    f"Exported {len(self._readings)} readings to:\n{self._export_path}")
            self.accept()
        except Exception as exc:
            logger.error("Export failed: %s", exc)
            QMessageBox.critical(self, "Export failed", str(exc))
            self._progress.hide()

    def _export_csv(self, factor: float, unit: str) -> None:
        import csv
        with open(self._export_path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["device_id", "timestamp_utc", "command", "value", "unit"])
            for r in self._readings:
                val = r.value * factor if r.value is not None else ""
                writer.writerow([
                    r.device_id,
                    r.timestamp_wall.isoformat(),
                    r.command,
                    val,
                    unit,
                ])

    def _export_parquet(self, factor: float, unit: str) -> None:
        import pandas as pd  # type: ignore[import]
        rows = [
            {
                "device_id": r.device_id,
                "timestamp_utc": r.timestamp_wall,
                "command": r.command,
                "value": r.value * factor if r.value is not None else None,
                "unit": unit,
            }
            for r in self._readings
        ]
        df = pd.DataFrame(rows)
        df.to_parquet(self._export_path, index=False)

    def _export_hdf5(self, factor: float, unit: str) -> None:
        import h5py  # type: ignore[import]
        import numpy as np

        by_device: dict[str, list] = {}
        for r in self._readings:
            by_device.setdefault(r.device_id, []).append(r)

        with h5py.File(self._export_path, "w") as fh:
            fh.attrs["unit"] = unit
            for dev_id, readings in by_device.items():
                grp = fh.create_group(dev_id.replace("/", "_"))
                grp.create_dataset(
                    "timestamp",
                    data=np.array([r.timestamp_wall.timestamp() for r in readings]),
                )
                grp.create_dataset(
                    "value",
                    data=np.array([
                        r.value * factor if r.value is not None else float("nan")
                        for r in readings
                    ]),
                )
                grp.attrs["unit"] = unit
