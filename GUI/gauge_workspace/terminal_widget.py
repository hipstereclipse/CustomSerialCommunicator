"""
TerminalWidget — interactive serial terminal embedded in a GaugeTab.

Lets the user send quick commands (from the device spec) or raw custom
frames to a connected gauge and view responses in multiple formats.

Design notes
------------
- All GUI updates happen via Qt signals from the worker thread; never touch
  the worker's transport directly.
- Rendering is defensive: bytes are rendered with ``_ascii_escape`` (lossless
  ``\\xNN`` escapes) so the display never contains Unicode replacement
  characters that look like a crash.
- Every slot is wrapped in try/except so a bad frame can never crash the
  GUI thread.
"""

from __future__ import annotations

import html
import logging
import math
from datetime import datetime
from typing import TYPE_CHECKING

from PyQt6.QtCore import Qt, pyqtSlot
from PyQt6.QtGui import QFont, QFontMetrics
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QGridLayout, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QScrollArea, QTextEdit, QVBoxLayout, QWidget,
)

from serial_comm.models import DeviceSpec, TerminalEntry

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

_FORMATS = ["Decoded", "Translated", "ASCII", "Hex", "Binary"]

# Colour palette (matches gauge_tab trace colours)
_COL_TX  = "#4C9BE8"   # blue  — sent frames
_COL_RX  = "#4CE87A"   # green — received frames
_COL_ERR = "#E84C4C"   # red   — errors

# Protocols whose raw frames are binary — Hex is the only sensible default.
_BINARY_PROTOCOLS = {"pfeiffer_binary", "cdg_serial"}


def _ascii_escape(data: bytes) -> str:
    """Lossless, printable representation of *data*.

    Printable ASCII (0x20..0x7E) stays literal. Everything else — control
    codes, 8-bit bytes — is shown as a ``\\xNN`` escape so the user can
    always see what was on the wire without Unicode replacement boxes.
    """
    out: list[str] = []
    for b in data:
        if 0x20 <= b <= 0x7E:
            out.append(chr(b))
        elif b == 0x0D:
            out.append("\\r")
        elif b == 0x0A:
            out.append("\\n")
        elif b == 0x09:
            out.append("\\t")
        else:
            out.append(f"\\x{b:02X}")
    return "".join(out)


class TerminalWidget(QWidget):
    """Interactive terminal for a connected gauge."""

    def __init__(
        self,
        spec: DeviceSpec,
        worker,
        parent: QWidget | None = None,
        *,
        gauge_color: str | None = None,
    ) -> None:
        super().__init__(parent)
        self._spec = spec
        self._worker = worker
        # RX colour: use the gauge's assigned colour when provided, default green.
        self._col_rx: str = gauge_color if gauge_color else _COL_RX
        self._build_ui()

        # Default to Hex output for binary protocols.
        if getattr(spec, "protocol", "") in _BINARY_PROTOCOLS:
            idx = self._fmt_combo.findText("Hex")
            if idx >= 0:
                self._fmt_combo.setCurrentIndex(idx)

    def set_rx_color(self, color: str) -> None:
        """Update the colour used for RX lines (call when gauge colour changes)."""
        self._col_rx = color if color else _COL_RX

    # ------------------------------------------------------------------
    # Build UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(4)

        # Output display (created first so the Clear button can bind it).
        self._output = QTextEdit()
        self._output.setReadOnly(True)
        self._output.setFont(QFont("Courier New", 9))
        self._output.document().setMaximumBlockCount(4000)
        self._output.setPlaceholderText(
            "Terminal output appears here.\n"
            "Use the quick-command buttons above or type a raw frame below."
        )

        # --- Format bar ---
        fmt_bar = QHBoxLayout()
        fmt_bar.addWidget(QLabel("Output format:"))
        self._fmt_combo = QComboBox()
        self._fmt_combo.addItems(_FORMATS)
        self._fmt_combo.setFixedWidth(110)
        fmt_bar.addWidget(self._fmt_combo)

        self._raw_check = QCheckBox("Show raw hex alongside")
        self._raw_check.setToolTip(
            "Append the raw hex bytes to each TX/RX line for easy debugging."
        )
        fmt_bar.addWidget(self._raw_check)

        self._pause_btn = QPushButton("Pause Polling")
        self._pause_btn.setCheckable(True)
        self._pause_btn.setToolTip("Pause automatic polling for this gauge tab")
        self._pause_btn.toggled.connect(self._on_pause_toggled)
        fmt_bar.addWidget(self._pause_btn)
        fmt_bar.addStretch()

        clear_btn = QPushButton("Clear")
        clear_btn.setFixedWidth(60)
        clear_btn.clicked.connect(self._output.clear)
        fmt_bar.addWidget(clear_btn)
        root.addLayout(fmt_bar)

        # --- Quick-command buttons ---
        quick_grp = QGroupBox("Quick Commands")
        quick_grp.setMaximumHeight(150)
        self._quick_group = quick_grp
        quick_inner = QWidget()
        quick_layout = QGridLayout(quick_inner)
        quick_layout.setContentsMargins(2, 2, 2, 2)
        quick_layout.setHorizontalSpacing(4)
        quick_layout.setVerticalSpacing(4)
        self._quick_inner = quick_inner
        self._quick_layout = quick_layout
        self._quick_buttons: list[tuple[str, QPushButton]] = []
        self._quick_hint: QLabel | None = None

        commands = getattr(self._spec, "commands", {}) or {}
        read_cmds = [(n, c) for n, c in commands.items() if getattr(c, "read", False)]
        if not read_cmds:
            self._quick_hint = QLabel("(no read commands defined for this device)")
            quick_layout.addWidget(self._quick_hint, 0, 0)
        else:
            for cmd_name, cmd_spec in read_cmds:
                label = cmd_name.replace("_", " ").title()
                btn = QPushButton(label)
                btn.setMinimumHeight(30)
                btn.setMinimumWidth(150)
                btn.setToolTip(getattr(cmd_spec, "description", "") or cmd_name)
                btn.clicked.connect(
                    lambda _checked=False, c=cmd_name: self._send_quick(c)
                )
                self._quick_buttons.append((cmd_name, btn))

        self._relayout_quick_buttons()

        scroll = QScrollArea()
        scroll.setWidget(quick_inner)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._quick_scroll = scroll

        quick_grp_layout = QVBoxLayout(quick_grp)
        quick_grp_layout.setContentsMargins(4, 2, 4, 2)
        quick_grp_layout.addWidget(scroll)
        root.addWidget(quick_grp)

        root.addWidget(self._output)

        # --- Custom input row ---
        input_row = QHBoxLayout()

        self._input = QLineEdit()
        self._input.setPlaceholderText(
            r"Raw frame — ASCII with \r \n \xNN escapes, or prefix with 'hex:' for raw hex"
        )
        self._input.returnPressed.connect(self._send_custom)
        input_row.addWidget(self._input)

        send_btn = QPushButton("Send")
        send_btn.setFixedWidth(60)
        send_btn.clicked.connect(self._send_custom)
        input_row.addWidget(send_btn)

        root.addLayout(input_row)

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        self._relayout_quick_buttons()

    def _relayout_quick_buttons(self) -> None:
        layout = getattr(self, "_quick_layout", None)
        if layout is None:
            return

        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(self._quick_inner)

        if self._quick_hint is not None:
            layout.addWidget(self._quick_hint, 0, 0)
            return

        if not self._quick_buttons:
            return

        viewport = getattr(self, "_quick_scroll", None)
        avail_w = viewport.viewport().width() if viewport is not None else self.width()
        metrics = QFontMetrics(self.font())
        widest = max(
            metrics.horizontalAdvance(btn.text()) for _cmd, btn in self._quick_buttons
        )
        cell_w = max(150, widest + 28)
        row_h = max(30, metrics.height() + 12)
        cols = max(1, int((max(avail_w, cell_w) + 4) / (cell_w + 4)))

        for _cmd, btn in self._quick_buttons:
            btn.setMinimumWidth(cell_w)
            btn.setMinimumHeight(row_h)

        for idx, (_cmd, btn) in enumerate(self._quick_buttons):
            row = idx // cols
            col = idx % cols
            layout.addWidget(btn, row, col)

        rows = int(math.ceil(len(self._quick_buttons) / cols))
        visible_rows = min(max(rows, 1), 3)
        group_h = 34 + (visible_rows * (row_h + 4))
        if hasattr(self, "_quick_group"):
            self._quick_group.setMaximumHeight(group_h)

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------

    def _send_quick(self, command: str) -> None:
        try:
            protocol = getattr(self._worker, "_protocol", None)
            if protocol is None:
                self._append("ERR: worker has no protocol attached", _COL_ERR)
                return
            frame = protocol.build_request(command)
            self._worker.send_terminal_command(frame, command)
            self._append_tx(frame, command)
        except Exception as exc:
            logger.exception("Terminal: failed to send quick command %r", command)
            self._append(f"ERR building '{command}': {exc}", _COL_ERR)

    @pyqtSlot()
    def _send_custom(self) -> None:
        text = self._input.text().strip()
        if not text:
            return
        try:
            frame = self._parse_custom_input(text)
            if not frame:
                self._append("ERR: empty frame after parsing", _COL_ERR)
                return
            self._worker.send_terminal_command(frame, "")
            self._append_tx(frame, "")
            self._input.clear()
        except Exception as exc:
            logger.exception("Terminal: failed to send custom frame")
            self._append(f"ERR sending custom frame: {exc}", _COL_ERR)

    @staticmethod
    def _parse_custom_input(text: str) -> bytes:
        """Parse the custom-frame text field into raw bytes.

        Supported forms:
          * ``hex: 01 02 0A`` — space/comma-separated hex bytes.
          * ASCII with Python-style escapes: ``\\r``, ``\\n``, ``\\t``,
            ``\\xNN``, ``\\\\``.
        """
        low = text.lower().lstrip()
        if low.startswith("hex:"):
            payload = text.split(":", 1)[1]
            cleaned = payload.replace("0x", "").replace(",", " ")
            parts = cleaned.split()
            return bytes(int(p, 16) for p in parts if p)

        out = bytearray()
        i = 0
        n = len(text)
        while i < n:
            ch = text[i]
            if ch == "\\" and i + 1 < n:
                nxt = text[i + 1]
                if nxt == "r":
                    out.append(0x0D); i += 2; continue
                if nxt == "n":
                    out.append(0x0A); i += 2; continue
                if nxt == "t":
                    out.append(0x09); i += 2; continue
                if nxt == "\\":
                    out.append(0x5C); i += 2; continue
                if nxt == "x" and i + 3 < n:
                    try:
                        out.append(int(text[i + 2:i + 4], 16))
                        i += 4
                        continue
                    except ValueError:
                        pass
            out.extend(ch.encode("latin-1", errors="replace"))
            i += 1
        return bytes(out)

    # ------------------------------------------------------------------
    # Receive
    # ------------------------------------------------------------------

    @pyqtSlot(object)
    def on_terminal_response(self, entry: TerminalEntry) -> None:
        try:
            self._render_response(entry)
        except Exception as exc:
            logger.exception("Terminal: failed to render response")
            try:
                self._append(f"ERR rendering response: {exc}", _COL_ERR)
            except Exception:
                pass

    def _render_response(self, entry: TerminalEntry) -> None:
        ts = entry.timestamp.strftime("%H:%M:%S") if entry.timestamp else ""

        if entry.error:
            self._append(f"[{ts}] ERR: {entry.error}", _COL_ERR)
            return

        # For auto-poll entries, render the TX frame inline before the RX.
        if getattr(entry, "auto_poll", False) and entry.request:
            self._append_tx(entry.request, entry.command, ts=ts)

        if not entry.response:
            self._append(f"[{ts}] RX: (no response)", self._col_rx)
            return

        fmt = self._fmt_combo.currentText()
        rx_text = self._format_response(entry.response, entry.command, fmt)
        line = f"[{ts}] RX: {rx_text}"
        if self._raw_check.isChecked() and fmt not in {"Hex", "Binary", "ASCII"}:
            line = f"[{ts}] RX: {self._fmt_hex(entry.response)}  ->  {rx_text}"
        elif self._raw_check.isChecked():
            line += f"  «{entry.response.hex(' ').upper()}»"
        self._append(line, self._col_rx)

    def _format_response(self, data: bytes, command: str, fmt: str) -> str:
        if fmt == "Hex":
            return self._fmt_hex(data)
        if fmt == "Binary":
            return self._fmt_binary(data)
        if fmt == "ASCII":
            return _ascii_escape(data)
        if fmt == "Translated":
            return self._translate_response(data, command)

        # "Decoded" — try the protocol parser; fall back sensibly.
        if command:
            try:
                protocol = getattr(self._worker, "_protocol", None)
                if protocol is not None:
                    result = protocol.parse_response(data, command)
                    if result.success:
                        unit = f" {result.unit}" if result.unit else ""
                        if result.formatted:
                            return f"{result.formatted}{unit}"
                        if result.value is not None:
                            return f"{result.value}{unit}"
                        return "(decoded, no value)"
                    return f"[PARSE FAIL] {result.error}"
            except Exception as exc:
                return f"[DECODE ERR] {exc}"

        # No command context (custom frame) — best effort fallback.
        if getattr(self._spec, "protocol", "") in _BINARY_PROTOCOLS:
            return self._fmt_hex(data)
        return _ascii_escape(data)

    def _translate_response(self, data: bytes, command: str) -> str:
        """Translate a response into plain-language text when possible."""
        protocol = getattr(self._worker, "_protocol", None)
        if protocol is not None and command:
            try:
                result = protocol.parse_response(data, command)
                if result.success:
                    label = command.replace("_", " ").title()
                    if result.value is not None and result.unit:
                        return f"{label}: {result.value:.6g} {result.unit}"
                    if result.formatted:
                        return f"{label}: {result.formatted}"
                    if result.extra:
                        details = ", ".join(f"{k}={v}" for k, v in result.extra.items())
                        return f"{label}: OK ({details})"
                    return f"{label}: OK"
                if result.error:
                    return f"{command}: device reported {result.error}"
            except Exception:
                pass

        txt = _ascii_escape(data).strip()
        if txt:
            return f"ASCII payload: {txt}"
        return f"{len(data)} byte binary payload"

    # ------------------------------------------------------------------
    # Rendering helpers
    # ------------------------------------------------------------------

    def _append_tx(self, frame: bytes, command: str, *, ts: str | None = None) -> None:
        if ts is None:
            ts = datetime.now().strftime("%H:%M:%S")
        fmt = self._fmt_combo.currentText()
        if fmt == "Hex":
            body = self._fmt_hex(frame)
        elif fmt == "Binary":
            body = self._fmt_binary(frame)
        elif fmt == "Translated":
            body = f"Sent {len(frame)} bytes"
        else:
            body = _ascii_escape(frame)

        tag = f" ({command})" if command else ""
        line = f"[{ts}] TX{tag}: {body}"
        if self._raw_check.isChecked():
            line += f"  «{frame.hex(' ').upper()}»"
        self._append(line, _COL_TX)

    def _append(self, text: str, colour: str = "") -> None:
        try:
            escaped = html.escape(text)
            if colour:
                frag = f'<span style="color:{colour}">{escaped}</span>'
            else:
                frag = escaped
            self._output.append(frag)
            sb = self._output.verticalScrollBar()
            sb.setValue(sb.maximum())
        except Exception:
            logger.exception("Terminal: failed to append text")

    @staticmethod
    def _fmt_hex(data: bytes) -> str:
        return data.hex(" ").upper() if data else "(empty)"

    @staticmethod
    def _fmt_binary(data: bytes) -> str:
        return " ".join(f"{b:08b}" for b in data) if data else "(empty)"

    @pyqtSlot(bool)
    def _on_pause_toggled(self, paused: bool) -> None:
        self._pause_btn.setText("Resume Polling" if paused else "Pause Polling")
        setter = getattr(self._worker, "set_polling_enabled", None)
        if callable(setter):
            setter(not paused)
