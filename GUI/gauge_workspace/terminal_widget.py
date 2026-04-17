"""
TerminalWidget — interactive serial terminal embedded in a GaugeTab.

Lets the user send quick commands (from the device spec) or raw custom
frames to a connected gauge and view responses in multiple formats.
"""

from __future__ import annotations

import html
import logging
from typing import TYPE_CHECKING

from PyQt6.QtCore import Qt, pyqtSlot
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QScrollArea, QTextEdit, QVBoxLayout, QWidget,
)

from serial_comm.models import DeviceSpec, TerminalEntry

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

_FORMATS = ["Decoded", "ASCII", "Hex", "Binary"]

# Colour palette (matches gauge_tab trace colours)
_COL_TX  = "#4C9BE8"   # blue  — sent frames
_COL_RX  = "#4CE87A"   # green — received frames
_COL_ERR = "#E84C4C"   # red   — errors
_COL_TS  = "#888888"   # grey  — timestamp


class TerminalWidget(QWidget):
    """
    Interactive terminal for a connected gauge.

    Parameters
    ----------
    spec:
        DeviceSpec for the connected gauge.
    worker:
        The live GaugeWorker (used to call ``send_terminal_command``).
    """

    def __init__(self, spec: DeviceSpec, worker, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._spec = spec
        self._worker = worker
        self._build_ui()

    # ------------------------------------------------------------------
    # Build UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(4)

        # --- Format bar ---
        fmt_bar = QHBoxLayout()
        fmt_bar.addWidget(QLabel("Output format:"))
        self._fmt_combo = QComboBox()
        self._fmt_combo.addItems(_FORMATS)
        self._fmt_combo.setFixedWidth(110)
        fmt_bar.addWidget(self._fmt_combo)

        self._raw_check = QCheckBox("Show raw hex alongside")
        fmt_bar.addWidget(self._raw_check)
        fmt_bar.addStretch()

        clear_btn = QPushButton("Clear")
        clear_btn.setFixedWidth(60)
        clear_btn.clicked.connect(self._output.clear if hasattr(self, "_output") else lambda: None)
        fmt_bar.addWidget(clear_btn)
        root.addLayout(fmt_bar)

        # --- Quick-command buttons ---
        quick_grp = QGroupBox("Quick Commands")
        quick_grp.setMaximumHeight(64)
        quick_inner = QWidget()
        quick_layout = QHBoxLayout(quick_inner)
        quick_layout.setContentsMargins(2, 2, 2, 2)
        quick_layout.setSpacing(4)

        for cmd_name, cmd_spec in self._spec.commands.items():
            if cmd_spec.read:
                label = cmd_name.replace("_", " ").title()
                btn = QPushButton(label)
                btn.setFixedHeight(26)
                btn.setToolTip(cmd_spec.description or cmd_name)
                btn.clicked.connect(
                    lambda _checked=False, c=cmd_name: self._send_quick(c)
                )
                quick_layout.addWidget(btn)

        quick_layout.addStretch()

        scroll = QScrollArea()
        scroll.setWidget(quick_inner)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)

        quick_grp_layout = QVBoxLayout(quick_grp)
        quick_grp_layout.setContentsMargins(4, 2, 4, 2)
        quick_grp_layout.addWidget(scroll)
        root.addWidget(quick_grp)

        # --- Output display ---
        self._output = QTextEdit()
        self._output.setReadOnly(True)
        self._output.setFont(QFont("Courier New", 9))
        self._output.document().setMaximumBlockCount(4000)
        self._output.setPlaceholderText(
            "Terminal output appears here.\n"
            "Use the quick-command buttons above or type a raw frame below."
        )
        root.addWidget(self._output)

        # Wire clear button now that _output exists
        clear_btn.clicked.disconnect()
        clear_btn.clicked.connect(self._output.clear)

        # --- Custom input row ---
        input_row = QHBoxLayout()

        self._input = QLineEdit()
        self._input.setPlaceholderText(
            r'Raw frame, e.g. @254PR3?\ or 001 00 309 02 =?{checksum}\r'
        )
        self._input.returnPressed.connect(self._send_custom)
        input_row.addWidget(self._input)

        send_btn = QPushButton("Send")
        send_btn.setFixedWidth(60)
        send_btn.clicked.connect(self._send_custom)
        input_row.addWidget(send_btn)

        root.addLayout(input_row)

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

    def _send_quick(self, command: str) -> None:
        try:
            frame = self._worker._protocol.build_request(command)
            self._worker.send_terminal_command(frame, command)
            self._append(
                f"TX ({command}): {self._fmt(frame, 'ASCII')}",
                _COL_TX,
            )
        except Exception as exc:
            self._append(f"ERR building '{command}': {exc}", _COL_ERR)

    @pyqtSlot()
    def _send_custom(self) -> None:
        text = self._input.text().strip()
        if not text:
            return
        # Let the user type \\ or \\r as escape sequences
        text = text.replace("\\r", "\r").replace("\\n", "\n").replace("\\\\", "\\")
        frame = text.encode("latin-1", errors="replace")
        self._worker.send_terminal_command(frame, "")
        self._append(f"TX: {self._fmt(frame, 'ASCII')}", _COL_TX)
        self._input.clear()

    @pyqtSlot(object)
    def on_terminal_response(self, entry: TerminalEntry) -> None:
        fmt = self._fmt_combo.currentText()
        show_raw = self._raw_check.isChecked()
        ts = entry.timestamp.strftime("%H:%M:%S")

        # TX echo for quick commands (custom TX already shown in _send_custom)
        if entry.command:
            self._append(
                f"[{ts}] TX ({entry.command}): {self._fmt(entry.request, 'ASCII')}",
                _COL_TX,
            )

        if entry.error:
            self._append(f"[{ts}] ERR: {entry.error}", _COL_ERR)
            return

        # Format the response
        if fmt == "Decoded" and entry.command:
            try:
                result = self._worker._protocol.parse_response(
                    entry.response, entry.command
                )
                if result.success:
                    rx_text = result.formatted or str(result.value)
                else:
                    rx_text = f"[FAIL] {result.error}"
            except Exception as exc:
                rx_text = f"[PARSE ERR] {exc}"
        else:
            display_fmt = fmt if fmt != "Decoded" else "ASCII"
            rx_text = self._fmt(entry.response, display_fmt)

        line = f"[{ts}] RX: {rx_text}"
        if show_raw and entry.response:
            line += f"  ‹{entry.response.hex(' ').upper()}›"
        self._append(line, _COL_RX)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _append(self, text: str, colour: str = "") -> None:
        escaped = html.escape(text)
        if colour:
            html_frag = f'<span style="color:{colour}">{escaped}</span>'
        else:
            html_frag = escaped
        self._output.append(html_frag)
        sb = self._output.verticalScrollBar()
        sb.setValue(sb.maximum())

    @staticmethod
    def _fmt(data: bytes, mode: str) -> str:
        if mode == "Hex":
            return data.hex(" ").upper() if data else "(empty)"
        if mode == "Binary":
            return " ".join(f"{b:08b}" for b in data) if data else "(empty)"
        # ASCII (default / Decoded fallback)
        return data.decode("ascii", errors="replace") if data else "(empty)"
