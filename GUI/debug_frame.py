"""
debug_frame.py
Provides debugging controls:
 - "Try All Baud Rates"
 - "Send ENQ"
 - "Show Settings"
 - "Show Debug" checkbox to hide or show debug lines in the OutputFrame

We preserve all existing methods, calling parent.set_show_debug(...) for toggling.
"""

import ast
import tkinter as tk
from tkinter import ttk
from typing import Callable


class DebugFrame(ttk.LabelFrame):
    """
    Frame for debugging features.
    Lets the user try baud rates, send ENQ, show settings, and toggle debug visibility.
    """

    def __init__(
        self,
        parent,
        baud_callback: Callable,
        enq_callback: Callable,
        settings_callback: Callable,
        output_format: tk.StringVar
    ):
        """
        parent: The main GaugeApplication instance
        baud_callback: Function to attempt known baud rates
        enq_callback: Function to send ENQ
        settings_callback: Function to show port settings
        output_format: The user's chosen output format (ASCII, Hex, etc.)
        """
        super().__init__(parent, text="Debug")
        self.output_format = output_format
        self.parent = parent

        # Creates a sub-frame for debug controls
        controls_frame = ttk.Frame(self)
        controls_frame.pack(fill=tk.X, padx=5, pady=5)

        # Adds a "Try All Baud Rates" button
        self.baud_button = ttk.Button(
            controls_frame,
            text="Try All Baud Rates",
            command=lambda: self._wrap_debug_callback(baud_callback)
        )
        self.baud_button.pack(side=tk.LEFT, padx=5)

        # Adds a "Send ENQ" button
        self.enq_button = ttk.Button(
            controls_frame,
            text="Send ENQ",
            command=lambda: self._wrap_debug_callback(enq_callback)
        )
        self.enq_button.pack(side=tk.LEFT, padx=5)

        # Adds a "Show Settings" button
        self.settings_button = ttk.Button(
            controls_frame,
            text="Show Settings",
            command=settings_callback
        )
        self.settings_button.pack(side=tk.LEFT, padx=5)

        # Checkbox to toggle debug logs
        self.show_debug_var = tk.BooleanVar(value=True)
        self.debug_checkbox = ttk.Checkbutton(
            controls_frame,
            text="Show Debug",
            variable=self.show_debug_var,
            command=self._on_debug_toggle
        )
        self.debug_checkbox.pack(side=tk.LEFT, padx=5)

        # Some controls remain active even if gauge is disconnected
        self.non_connection_buttons = [self.settings_button]

    def _wrap_debug_callback(self, callback: Callable):
        """
        Calls the debug function, tries to reformat lines in the output if needed.
        """
        try:
            result = callback()
            if hasattr(self.parent, 'output_frame'):
                debug_text = self.parent.output_frame.output_text.get("1.0", tk.END)
                formatted = self._format_debug_messages(debug_text)
                if formatted != debug_text:
                    self.parent.output_frame.clear()
                    self.parent.output_frame.append_log(formatted)
            return result
        except Exception as e:
            if hasattr(self.parent, 'output_frame'):
                self.parent.output_frame.append_log(f"Debug error: {str(e)}")
            return None

    def _format_debug_messages(self, text: str) -> str:
        """
        Replaces lines containing "command:" or "response:" with a user-chosen format (Hex, ASCII, etc.).
        """
        lines = []
        for line in text.split('\n'):
            if "command:" in line.lower() or "response:" in line.lower():
                lines.append(self._format_protocol_message(line))
            else:
                lines.append(line)
        return '\n'.join(lines)

    def _format_protocol_message(self, message: str) -> str:
        """
        Attempts to interpret raw bytes in the chosen format.
        """
        try:
            prefix, data = message.split(':', 1)
            data = data.strip()
            if data.startswith("b'") or data.startswith('b\"'):
                data_bytes = ast.literal_eval(data)
            elif all(c in '0123456789ABCDEFabcdef ' for c in data):
                data_bytes = bytes.fromhex(data.replace(' ', ''))
            else:
                return message

            fmt = self.output_format.get()
            if fmt == "Hex":
                formatted_data = ' '.join(f'{b:02X}' for b in data_bytes)
            elif fmt == "ASCII":
                formatted_data = data_bytes.decode('ascii', errors='replace')
            elif fmt == "Decimal":
                formatted_data = ' '.join(str(b) for b in data_bytes)
            elif fmt == "Binary":
                formatted_data = ' '.join(f'{b:08b}' for b in data_bytes)
            elif fmt == "UTF-8":
                formatted_data = data_bytes.decode('utf-8', errors='replace')
            else:
                formatted_data = str(data_bytes)

            return f"{prefix}: {formatted_data}"
        except Exception:
            return message

    def _on_debug_toggle(self):
        """
        Called when user toggles "Show Debug" in the checkbox.
        Invokes the parent method to manage visibility of debug messages.
        """
        if hasattr(self.parent, 'set_show_debug'):
            # Notify parent about the toggle state
            self.parent.set_show_debug(self.show_debug_var.get())

    def set_enabled(self, enabled: bool):
        """
        Enables or disables debug features based on connection state,
        except for those in self.non_connection_buttons.
        """
        state = "normal" if enabled else "disabled"
        for widget in self.winfo_children():
            for child in widget.winfo_children():
                if child not in self.non_connection_buttons:
                    child.config(state=state)
