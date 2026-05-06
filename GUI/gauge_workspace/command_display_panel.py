from __future__ import annotations

import csv
import time
from collections import deque
from datetime import datetime, timezone

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QFileDialog, QHBoxLayout, QLabel, QFrame, QPushButton,
    QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

from serial_comm.models import DeviceReading, DeviceSpec, GaugeReading, TerminalEntry
from GUI.theme import current_theme, panel_frame_style, themed_plot

_NUMERIC_TYPES = {
    "uint8",
    "enum_uint8",
    "bool_uint8",
    "uint16_be",
    "uint32_be",
    "int32_be",
    "float32_be",
    "u_integer",
    "u_real",
    "u_expo",
}
_SPECTRUM_TYPES = {"uint16_be_array"}


class CommandDisplayPanel(QWidget):
    """Dedicated displays for non-pressure polled commands."""

    def __init__(
        self,
        spec: DeviceSpec,
        parent: QWidget | None = None,
        *,
        initial_commands: list[str] | None = None,
        trace_color: str = "#4C9BE8",
    ) -> None:
        super().__init__(parent)
        self._spec = spec
        self._trace_color = trace_color
        self._commands: list[str] = []

        self._numeric_histories: dict[str, tuple[deque[float], deque[float], str]] = {}
        self._latest_text: dict[str, tuple[str, str]] = {}
        self._latest_status: dict[str, tuple[str, str]] = {}
        self._spectrum_data: dict[str, tuple[np.ndarray, np.ndarray, str]] = {}
        # Session time reference (set on first data point)
        self._t0_mono: float | None = None
        self._t0_wall: datetime | None = None

        self._cards: dict[str, QWidget] = {}
        self._value_labels: dict[str, QLabel] = {}
        self._detail_labels: dict[str, QLabel] = {}
        self._curves: dict[str, pg.PlotDataItem] = {}
        self._spectrum_curves: dict[str, pg.PlotDataItem] = {}
        self._plot_widgets: dict[str, pg.PlotWidget] = {}

        self._build_ui()
        self.set_polled_commands(initial_commands or [])
        self.apply_theme()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(4)

        header = QLabel("Command Displays")
        self._header = header
        root.addWidget(header)

        self._empty_label = QLabel(
            "Enable non-pressure commands to add dedicated displays here.\n"
            "Numeric commands get their own mini-trend; metadata commands become status cards."
        )
        self._empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_label.setStyleSheet(f"color:{current_theme(self).muted}; padding:18px;")
        root.addWidget(self._empty_label)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        root.addWidget(scroll, 1)

        self._inner = QWidget()
        self._layout = QVBoxLayout(self._inner)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(8)
        self._layout.addStretch()
        scroll.setWidget(self._inner)

    def set_trace_color(self, color: str) -> None:
        self._trace_color = color
        for curve in self._curves.values():
            curve.setPen(pg.mkPen(color, width=2))

    def apply_theme(self) -> None:
        theme = current_theme(self)
        self._header.setStyleSheet(f"font-size:13px; font-weight:600; color:{theme.text};")
        self._empty_label.setStyleSheet(f"color:{theme.muted}; padding:18px;")
        for card in self._cards.values():
            card.setStyleSheet(panel_frame_style())
        for label in self._value_labels.values():
            label.setStyleSheet(f"font-size:16px; font-weight:700; color:{theme.text};")
        for label in self._detail_labels.values():
            label.setStyleSheet(f"font-size:11px; color:{theme.muted};")
        for plot in self._plot_widgets.values():
            themed_plot(plot)

    def set_polled_commands(self, commands: list[str]) -> None:
        filtered = [
            command for command in commands
            if command in self._spec.commands and command != "pressure"
        ]
        self._commands = filtered
        self._rebuild_cards()

    def on_reading(self, reading: DeviceReading) -> None:
        command = reading.command
        if command not in self._commands or command == "pressure":
            return
        if self._card_kind(command) == "spectrum":
            return  # spectrum data arrives via on_terminal_response

        unit = reading.unit or self._spec.commands.get(command, None).unit if command in self._spec.commands else ""
        history = self._numeric_histories.get(command)
        if history is None:
            history = (
                deque(),  # unbounded — keep all data from session start
                deque(),
                unit or "",
            )
            self._numeric_histories[command] = history

        times, values, existing_unit = history
        t_mono = float(reading.timestamp_mono) if reading.timestamp_mono is not None else time.monotonic()
        if self._t0_mono is None:
            self._t0_mono = t_mono
            self._t0_wall = datetime.now(tz=timezone.utc)
        t_rel = t_mono - self._t0_mono
        times.append(t_rel)
        values.append(float(reading.value))
        if unit and unit != existing_unit:
            self._numeric_histories[command] = (times, values, unit)

        if command not in self._cards:
            self._rebuild_cards()
        self._refresh_numeric_card(command)

    def on_terminal_response(
        self,
        entry: TerminalEntry,
        parsed: GaugeReading | None,
    ) -> None:
        command = entry.command or ""
        if command not in self._commands or command == "pressure":
            return

        updated = entry.timestamp.strftime("%H:%M:%S") if entry.timestamp else "-"

        if entry.error:
            self._latest_text[command] = (f"ERR: {entry.error}", updated)
            if command not in self._cards:
                self._rebuild_cards()
            self._refresh_text_card(command)
            return

        if parsed is None:
            return

        pixel_data = parsed.extra.get("pixel_data") if parsed.extra else None
        if pixel_data:
            y = np.array(pixel_data, dtype=float)
            peak = float(np.max(y)) if len(y) else 1.0
            if peak <= 0:
                peak = 1.0
            y = y / peak
            x = np.linspace(380.0, 780.0, len(y), dtype=float)
            detail = parsed.formatted or f"{len(pixel_data)} pixels"
            self._spectrum_data[command] = (x, y, detail)
            if command not in self._cards:
                self._rebuild_cards()
            self._refresh_spectrum_card(command)
            return

        display_text = self._format_parsed_value(parsed)
        if self._is_textual_command(command, parsed):
            self._latest_text[command] = (display_text, updated)
            if command not in self._cards:
                self._rebuild_cards()
            self._refresh_text_card(command)
            return

        self._latest_status[command] = (display_text, updated)
        if command not in self._cards:
            self._rebuild_cards()
        self._refresh_numeric_card(command)

    def _rebuild_cards(self) -> None:
        while self._layout.count():
            item = self._layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)

        self._cards.clear()
        self._value_labels.clear()
        self._detail_labels.clear()
        self._curves.clear()
        self._spectrum_curves.clear()
        self._plot_widgets.clear()

        if not self._commands:
            self._empty_label.show()
            self._layout.addStretch()
            return

        self._empty_label.hide()
        for command in self._commands:
            kind = self._card_kind(command)
            if kind == "spectrum":
                widget = self._build_spectrum_card(command)
            elif kind == "text":
                widget = self._build_text_card(command)
            else:
                widget = self._build_numeric_card(command)
            self._cards[command] = widget
            self._layout.addWidget(widget)
            if kind == "spectrum":
                self._refresh_spectrum_card(command)
            elif kind == "text":
                self._refresh_text_card(command)
            else:
                self._refresh_numeric_card(command)

        self._layout.addStretch()

    def _card_kind(self, command: str) -> str:
        spec = self._spec.commands.get(command)
        data_type = (spec.data_type or "") if spec is not None else ""
        if command in self._spectrum_data or data_type in _SPECTRUM_TYPES or "spectrum" in command:
            return "spectrum"
        if command in self._numeric_histories:
            return "numeric"
        if data_type in _NUMERIC_TYPES:
            return "numeric"
        return "text"

    def _build_card_shell(self, command: str) -> tuple[QFrame, QVBoxLayout]:
        box = QFrame()
        box.setFrameShape(QFrame.Shape.StyledPanel)
        box.setStyleSheet(panel_frame_style())
        layout = QVBoxLayout(box)
        layout.setContentsMargins(10, 8, 10, 10)
        layout.setSpacing(6)

        title = QLabel(command.replace("_", " ").title())
        title.setStyleSheet(f"font-weight:600; color:{current_theme(self).text};")
        layout.addWidget(title)

        spec = self._spec.commands.get(command)
        if spec is not None and spec.description:
            subtitle = QLabel(spec.description)
            subtitle.setWordWrap(True)
            subtitle.setStyleSheet(f"font-size:10px; color:{current_theme(self).muted};")
            layout.addWidget(subtitle)
        return box, layout

    def _build_numeric_card(self, command: str) -> QWidget:
        box, layout = self._build_card_shell(command)
        row = QHBoxLayout()
        value_label = QLabel("Awaiting data…")
        value_label.setStyleSheet("font-size:16px; font-weight:700; color:#F6FBFF;")
        row.addWidget(value_label, 1)
        export_btn = QPushButton("Export CSV")
        export_btn.setFixedHeight(24)
        export_btn.setStyleSheet(
            "QPushButton{background:#1C2B38;border:1px solid #3A5068;"
            "border-radius:4px;color:#A8C8E8;font-size:11px;padding:0 10px;}"
            "QPushButton:hover{background:#243647;}"
        )
        export_btn.clicked.connect(lambda _=False, c=command: self._export_numeric_csv(c))
        row.addWidget(export_btn)
        layout.addLayout(row)
        detail_label = QLabel("-")
        detail_label.setStyleSheet("font-size:11px; color:#86A0B5;")
        layout.addWidget(detail_label)

        plot = pg.PlotWidget()
        plot.setMinimumHeight(150)
        themed_plot(plot)
        plot.showGrid(x=True, y=True, alpha=0.2)
        plot.setLabel("bottom", "Time", units="s")
        plot.setMouseEnabled(x=True, y=True)
        curve = plot.plot(pen=pg.mkPen(self._trace_color, width=2))
        layout.addWidget(plot)

        reset_btn = QPushButton("Reset Zoom")
        reset_btn.setFixedHeight(24)
        reset_btn.setStyleSheet(
            "QPushButton{background:#1A2430;border:1px solid #2E4055;"
            "border-radius:4px;color:#7A9AB5;font-size:11px;padding:0 8px;}"
            "QPushButton:hover{background:#233040;}"
        )
        reset_btn.clicked.connect(plot.autoRange)
        layout.addWidget(reset_btn)

        self._value_labels[command] = value_label
        self._detail_labels[command] = detail_label
        self._curves[command] = curve
        self._plot_widgets[command] = plot
        return box

    def _build_text_card(self, command: str) -> QWidget:
        box, layout = self._build_card_shell(command)
        value_label = QLabel("Awaiting data…")
        value_label.setWordWrap(True)
        value_label.setTextFormat(Qt.TextFormat.PlainText)
        value_label.setStyleSheet("font-size:15px; font-weight:600; color:#F4FAFF;")
        layout.addWidget(value_label)
        detail_label = QLabel("-")
        detail_label.setStyleSheet("font-size:11px; color:#86A0B5;")
        layout.addWidget(detail_label)
        self._value_labels[command] = value_label
        self._detail_labels[command] = detail_label
        return box

    def _build_spectrum_card(self, command: str) -> QWidget:
        box, layout = self._build_card_shell(command)
        row = QHBoxLayout()
        row.addStretch(1)
        export_btn = QPushButton("Export CSV")
        export_btn.setFixedHeight(24)
        export_btn.setStyleSheet(
            "QPushButton{background:#1C2B38;border:1px solid #3A5068;"
            "border-radius:4px;color:#A8C8E8;font-size:11px;padding:0 10px;}"
            "QPushButton:hover{background:#243647;}"
        )
        export_btn.clicked.connect(lambda _=False, c=command: self._export_spectrum_csv(c))
        row.addWidget(export_btn)
        layout.addLayout(row)

        detail_label = QLabel("Awaiting spectrum data…")
        detail_label.setWordWrap(True)
        detail_label.setStyleSheet("font-size:11px; color:#86A0B5;")
        layout.addWidget(detail_label)

        plot = pg.PlotWidget()
        plot.setMinimumHeight(190)
        themed_plot(plot)
        plot.setLabel("left", "Relative intensity")
        plot.setLabel("bottom", "Wavelength", units="nm")
        plot.showGrid(x=True, y=True, alpha=0.2)
        plot.setMouseEnabled(x=True, y=True)
        curve = plot.plot(
            pen=pg.mkPen("#90D98E", width=2),
            fillLevel=0.0,
            brush=pg.mkBrush(144, 217, 142, 85),
        )
        layout.addWidget(plot)

        reset_btn = QPushButton("Reset Zoom")
        reset_btn.setFixedHeight(24)
        reset_btn.setStyleSheet(
            "QPushButton{background:#1A2430;border:1px solid #2E4055;"
            "border-radius:4px;color:#7A9AB5;font-size:11px;padding:0 8px;}"
            "QPushButton:hover{background:#233040;}"
        )
        reset_btn.clicked.connect(plot.autoRange)
        layout.addWidget(reset_btn)

        self._detail_labels[command] = detail_label
        self._spectrum_curves[command] = curve
        self._plot_widgets[command] = plot
        return box

    def _refresh_numeric_card(self, command: str) -> None:
        label = self._value_labels.get(command)
        detail = self._detail_labels.get(command)
        curve = self._curves.get(command)
        if label is None or detail is None or curve is None:
            return

        history = self._numeric_histories.get(command)
        if history is None:
            status_text, updated = self._latest_status.get(command, ("Awaiting data…", "-"))
            label.setText(status_text)
            detail.setText(f"Updated {updated}")
            return

        times, values, unit = history
        arr_y = np.array(values, dtype=float)
        arr_x = np.array(times, dtype=float)
        curve.setData(arr_x, arr_y)

        latest = float(arr_y[-1])
        status_text, updated = self._latest_status.get(command, ("", "-"))
        suffix = f" {unit}" if unit else ""
        label.setText(f"{latest:.4g}{suffix}")
        if status_text:
            detail.setText(f"{status_text}  |  Updated {updated}")
        else:
            detail.setText(f"Updated {updated}")

    def _export_numeric_csv(self, command: str) -> None:
        history = self._numeric_histories.get(command)
        if history is None:
            return
        times, values, unit = history
        if not times:
            return
        default = f"{command}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        path, _ = QFileDialog.getSaveFileName(
            self,
            f"Export {command}",
            default,
            "CSV files (*.csv);;All files (*)",
        )
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["time_s", "value", "unit"])
            for t, v in zip(times, values):
                writer.writerow([f"{float(t):.6f}", f"{float(v):.10g}", unit or ""])

    def _export_spectrum_csv(self, command: str) -> None:
        data = self._spectrum_data.get(command)
        if data is None:
            return
        x, y, _ = data
        if len(x) == 0:
            return
        default = f"{command}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        path, _ = QFileDialog.getSaveFileName(
            self,
            f"Export {command}",
            default,
            "CSV files (*.csv);;All files (*)",
        )
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["wavelength_nm", "relative_intensity"])
            for wl, inten in zip(x, y):
                writer.writerow([f"{float(wl):.6f}", f"{float(inten):.10g}"])

    def _refresh_text_card(self, command: str) -> None:
        label = self._value_labels.get(command)
        detail = self._detail_labels.get(command)
        if label is None or detail is None:
            return
        value, updated = self._latest_text.get(command, ("Awaiting data…", "-"))
        label.setText(value)
        detail.setText(f"Updated {updated}")

    def _refresh_spectrum_card(self, command: str) -> None:
        detail = self._detail_labels.get(command)
        curve = self._spectrum_curves.get(command)
        if detail is None or curve is None:
            return
        data = self._spectrum_data.get(command)
        if data is None:
            detail.setText("Awaiting spectrum data…")
            return
        x, y, text = data
        curve.setData(x, y)
        detail.setText(text)

    def _is_textual_command(self, command: str, parsed: GaugeReading) -> bool:
        spec = self._spec.commands.get(command)
        data_type = (spec.data_type or "") if spec is not None else ""
        if data_type == "string":
            return True
        if parsed.value is None:
            return True
        return False

    @staticmethod
    def _format_parsed_value(parsed: GaugeReading) -> str:
        if parsed.formatted:
            return parsed.formatted
        if parsed.value is not None and parsed.unit:
            return f"{parsed.value:.4g} {parsed.unit}"
        if parsed.value is not None:
            return f"{parsed.value:.4g}"
        if parsed.extra:
            return ", ".join(f"{key}={value}" for key, value in parsed.extra.items())
        return "No data"