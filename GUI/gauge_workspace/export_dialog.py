"""
ExportDialog — export recorded gauge readings.

Formats: CSV, Parquet, HDF5.
Unit selection: persisted via QSettings (default: mbar).
"""

from __future__ import annotations

import logging
from pathlib import Path

from PyQt6.QtCore import QDateTime, QSettings, Qt
from PyQt6.QtWidgets import (
    QDialog, QDialogButtonBox, QVBoxLayout, QFormLayout,
    QComboBox, QLabel, QFileDialog, QPushButton, QHBoxLayout,
    QProgressBar, QMessageBox, QListWidget, QListWidgetItem,
    QGroupBox, QCheckBox, QDateTimeEdit,
)

from serial_comm.models import DeviceReading
from serial_comm.units import SUPPORTED_UNITS, convert_pressure
from GUI.theme import current_theme, list_style

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

    def __init__(
        self,
        readings: list[DeviceReading],
        parent=None,
        *,
        title: str = "Export Data",
        preselected_devices: set[str] | None = None,
        preselected_commands: set[str] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumSize(560, 620)
        self._readings = readings
        self._preselected_devices = preselected_devices
        self._preselected_commands = preselected_commands
        self._settings = QSettings()
        self._build_ui()
        self.apply_theme()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        info = QLabel(f"<b>{len(self._readings)}</b> readings from "
                  f"<b>{len({r.device_id for r in self._readings})}</b> device(s)")
        self._info = info
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

        self._csv_shape = QComboBox()
        self._csv_shape.addItems(["Wide by command", "Long table"])
        self._csv_shape.setToolTip(
            "Wide by command: each series gets its own pair of (timestamp, value) "
            "columns side-by-side.\nLong table: a single row per reading."
        )
        form.addRow("CSV layout:", self._csv_shape)

        layout.addLayout(form)

        filters = QGroupBox("Export Scope")
        self._filters_group = filters
        filters_layout = QVBoxLayout(filters)

        self._device_list = QListWidget()
        self._device_list.setMaximumHeight(120)
        for device_id in sorted({r.device_id for r in self._readings}):
            item = QListWidgetItem(device_id)
            item.setData(Qt.ItemDataRole.UserRole, device_id)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            selected = self._preselected_devices is None or device_id in self._preselected_devices
            item.setCheckState(Qt.CheckState.Checked if selected else Qt.CheckState.Unchecked)
            self._device_list.addItem(item)
        filters_layout.addWidget(QLabel("Gauges"))
        filters_layout.addWidget(self._device_list)

        self._command_list = QListWidget()
        self._command_list.setMaximumHeight(140)
        for command in sorted({r.command for r in self._readings}):
            item = QListWidgetItem(command.replace("_", " "))
            item.setData(Qt.ItemDataRole.UserRole, command)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            selected = self._preselected_commands is None or command in self._preselected_commands
            item.setCheckState(Qt.CheckState.Checked if selected else Qt.CheckState.Unchecked)
            self._command_list.addItem(item)
        filters_layout.addWidget(QLabel("Commands"))
        filters_layout.addWidget(self._command_list)

        times = [r.timestamp_wall for r in self._readings if r.timestamp_wall]
        self._range_check = QCheckBox("Limit by timestamp range")
        filters_layout.addWidget(self._range_check)
        range_row = QHBoxLayout()
        self._start_dt = QDateTimeEdit()
        self._end_dt = QDateTimeEdit()
        for editor in (self._start_dt, self._end_dt):
            editor.setCalendarPopup(True)
            editor.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
            editor.setEnabled(False)
        self._range_check.toggled.connect(self._start_dt.setEnabled)
        self._range_check.toggled.connect(self._end_dt.setEnabled)
        if times:
            self._start_dt.setDateTime(QDateTime.fromSecsSinceEpoch(int(times[0].timestamp()), Qt.TimeSpec.UTC))
            self._end_dt.setDateTime(QDateTime.fromSecsSinceEpoch(int(times[-1].timestamp()), Qt.TimeSpec.UTC))
        range_row.addWidget(QLabel("From"))
        range_row.addWidget(self._start_dt, 1)
        range_row.addWidget(QLabel("To"))
        range_row.addWidget(self._end_dt, 1)
        filters_layout.addLayout(range_row)
        layout.addWidget(filters)

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

    def apply_theme(self) -> None:
        theme = current_theme(self)
        self._info.setStyleSheet(f"font-size:13px; color:{theme.text};")
        self._filters_group.setStyleSheet(
            f"QGroupBox {{ font-weight:600; color:{theme.text}; }}" + list_style()
        )

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

            filtered = self._filtered_readings()
            if not filtered:
                QMessageBox.warning(self, "No matching data", "No readings match the selected export scope.")
                self._progress.hide()
                return

            if "CSV" in fmt:
                self._export_csv(filtered, factor, unit)
            elif "Parquet" in fmt:
                self._export_parquet(filtered, factor, unit)
            else:
                self._export_hdf5(filtered, factor, unit)

            self._progress.setRange(0, 1)
            self._progress.setValue(1)
            QMessageBox.information(self, "Export complete",
                                    f"Exported {len(filtered)} readings to:\n{self._export_path}")
            self.accept()
        except Exception as exc:
            logger.error("Export failed: %s", exc)
            QMessageBox.critical(self, "Export failed", str(exc))
            self._progress.hide()

    def _filtered_readings(self) -> list[DeviceReading]:
        devices = self._checked_values(self._device_list)
        commands = self._checked_values(self._command_list)
        start_ts = self._start_dt.dateTime().toSecsSinceEpoch() if self._range_check.isChecked() else None
        end_ts = self._end_dt.dateTime().toSecsSinceEpoch() if self._range_check.isChecked() else None
        out: list[DeviceReading] = []
        for reading in self._readings:
            if reading.device_id not in devices or reading.command not in commands:
                continue
            reading_ts = int(reading.timestamp_wall.timestamp())
            if start_ts is not None and reading_ts < start_ts:
                continue
            if end_ts is not None and reading_ts > end_ts:
                continue
            out.append(reading)
        return out

    @staticmethod
    def _checked_values(widget: QListWidget) -> set[str]:
        values: set[str] = set()
        for row in range(widget.count()):
            item = widget.item(row)
            if item.checkState() == Qt.CheckState.Checked:
                values.add(item.data(Qt.ItemDataRole.UserRole))
        return values

    def _convert_value(self, reading: DeviceReading, factor: float, unit: str) -> tuple[float | str, str]:
        if reading.value is None:
            return "", reading.unit
        if reading.unit in SUPPORTED_UNITS and unit in SUPPORTED_UNITS:
            return convert_pressure(reading.value, reading.unit, unit), unit
        return reading.value, reading.unit

    def _export_csv(self, readings: list[DeviceReading], factor: float, unit: str) -> None:
        import csv
        with open(self._export_path, "w", newline="", encoding="utf-8") as fh:
            if self._csv_shape.currentText().startswith("Wide"):
                self._export_csv_wide(fh, readings, factor, unit)
                return
            writer = csv.writer(fh)
            writer.writerow(["device_id", "timestamp_utc", "command", "value", "unit", "raw_hex"])
            for r in readings:
                val, out_unit = self._convert_value(r, factor, unit)
                writer.writerow([r.device_id, r.timestamp_wall.isoformat(), r.command, val, out_unit, r.raw.hex()])

    def _export_csv_wide(self, fh, readings: list[DeviceReading], factor: float, unit: str) -> None:
        """Write a CSV with each series as its own pair of adjacent columns.

        Layout::

            <series1>_timestamp_utc, <series1>_<unit>, <series2>_timestamp_utc, <series2>_<unit>, ...

        Each series fills its two columns top-to-bottom independently, so
        timestamps from different series are NOT forced into a single shared
        timeline.  Rows past a given series' length are left blank.  This is
        the layout users typically want for plotting/analysis tools where
        each curve has its own (t, y) pairs.
        """
        import csv
        from collections import defaultdict

        series: dict[str, list[DeviceReading]] = defaultdict(list)
        for r in readings:
            series[f"{r.device_id}:{r.command}"].append(r)
        keys = sorted(series.keys())
        for k in keys:
            series[k].sort(key=lambda r: r.timestamp_wall)

        writer = csv.writer(fh)
        header: list[str] = []
        for k in keys:
            header.extend([f"{k}_timestamp_utc", f"{k}_{unit}"])
        writer.writerow(header)

        max_len = max((len(series[k]) for k in keys), default=0)
        for i in range(max_len):
            row: list[object] = []
            for k in keys:
                bucket = series[k]
                if i < len(bucket):
                    r = bucket[i]
                    val, _ = self._convert_value(r, factor, unit)
                    row.extend([r.timestamp_wall.isoformat(), val])
                else:
                    row.extend(["", ""])
            writer.writerow(row)

    def _export_parquet(self, readings: list[DeviceReading], factor: float, unit: str) -> None:
        import pandas as pd  # type: ignore[import]
        rows = [
            dict(device_id=r.device_id, timestamp_utc=r.timestamp_wall, command=r.command,
                 value=self._convert_value(r, factor, unit)[0], unit=self._convert_value(r, factor, unit)[1])
            for r in readings
        ]
        df = pd.DataFrame(rows)
        df.to_parquet(self._export_path, index=False)

    def _export_hdf5(self, readings: list[DeviceReading], factor: float, unit: str) -> None:
        import re

        import h5py  # type: ignore[import]
        import numpy as np

        by_device: dict[str, list] = {}
        for r in readings:
            by_device.setdefault(r.device_id, []).append(r)

        with h5py.File(self._export_path, "w") as fh:
            fh.attrs["unit"] = unit
            for dev_id, readings in by_device.items():
                safe_name = re.sub(r"[^a-zA-Z0-9_-]", "_", dev_id)
                grp = fh.create_group(safe_name)
                grp.create_dataset(
                    "timestamp",
                    data=np.array([r.timestamp_wall.timestamp() for r in readings]),
                )
                grp.create_dataset(
                    "value",
                    data=np.array([
                        self._convert_value(r, factor, unit)[0]
                        if r.value is not None else float("nan")
                        for r in readings
                    ]),
                )
                grp.attrs["unit"] = unit
