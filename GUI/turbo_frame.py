#!/usr/bin/env python3
"""
turbo_frame.py

A comprehensive TurboFrame for controlling a Turbo Pump (e.g., "TC600") with:
 - Connection UI (port selection, refresh, connect/disconnect)
 - A specialized settings frame (toggle open/close)
 - Manual & Quick Commands (always logs to console)
 - Cyc reading toggles for multiple statuses (speed, motor temp, current, electronics temp, bearing temp, error code, warning code, operating hours)
 - A "Show Debug" toggle that sets the main logger to DEBUG or INFO vs. CRITICAL, controlling debug logs (sending/received commands, etc.).
 - A "Show Cyc Logs" toggle that hides/shows cyc reading lines only in the console.
 - Thorough, active-voice comments in each section.
"""

import tkinter as tk
from tkinter import ttk, messagebox
from typing import Optional
import threading
import serial.tools.list_ports
import queue
import logging

# Imports your communicator
from serial_communication.communicator.gauge_communicator import GaugeCommunicator
from serial_communication.models import GaugeCommand, GaugeResponse

# The specialized settings frame
from .turbo_serial_settings_frame import TurboSerialSettingsFrame


class TurboFrame(ttk.Frame):
    """
    A robust GUI for controlling a Turbo Pump ("TC600") that includes:
     - Connection controls (port, gauge type, etc.)
     - Quick/manual commands
     - A comprehensive status section for speed, motor temp, load/current, electronics temp, bearing temp, error code, warning code, operating hours
     - Cyc reading toggles for each status
     - "Show Debug" toggle for turning debug-level logs on/off system-wide
     - "Show Cyc Logs" toggle for hiding or showing cyc reading lines in the console
    """

    def __init__(self, parent, main_app):
        """
        Initializes the TurboFrame with references to the parent and the main_app.
        main_app: The main application instance, so we can call main_app.log_message(...) and adjust log levels.
        """
        super().__init__(parent, relief=tk.RAISED, borderwidth=1)

        # References
        self.parent = parent
        self.main_app = main_app

        # Tracks connection & communicator
        self.connected = False
        self.turbo_communicator: Optional[GaugeCommunicator] = None

        # Connection variables
        self.selected_port = tk.StringVar(value="")
        self.selected_turbo = tk.StringVar(value="TC600")
        self.status_text = tk.StringVar(value="Disconnected")

        # Specialized settings frame reference
        self.settings_frame: Optional[TurboSerialSettingsFrame] = None

        # cyc reading
        self.cycle_var = tk.BooleanVar(value=False)         # Master on/off for cyc reading
        self.update_interval = tk.StringVar(value="1000")    # Interval in ms
        self.stop_thread = False
        self.status_thread: Optional[threading.Thread] = None

        # Toggles for cyc logs vs debug logs:
        self.show_debug_var = tk.BooleanVar(value=True)      # "Show Debug" => toggles main logger level
        self.cyc_log_var = tk.BooleanVar(value=True)         # "Show Cyc Logs" => toggles cyc reading lines only

        # Status data variables & cyc toggles
        self.speed_var = tk.StringVar(value="---")
        self.speed_cyc = tk.BooleanVar(value=True)

        self.load_var = tk.StringVar(value="---")  # Current / Load
        self.load_cyc = tk.BooleanVar(value=True)

        self.temp_var = tk.StringVar(value="---")    # Motor temp
        self.temp_cyc = tk.BooleanVar(value=True)

        self.temp_electr_var = tk.StringVar(value="---")
        self.temp_electr_cyc = tk.BooleanVar(value=True)

        self.temp_bearing_var = tk.StringVar(value="---")
        self.temp_bearing_cyc = tk.BooleanVar(value=True)

        self.error_code_var = tk.StringVar(value="---")
        self.error_code_cyc = tk.BooleanVar(value=True)

        self.warning_code_var = tk.StringVar(value="---")
        self.warning_code_cyc = tk.BooleanVar(value=True)

        self.hours_var = tk.StringVar(value="---")
        self.hours_cyc = tk.BooleanVar(value=True)

        # Manual & Quick commands
        self.manual_command_var = tk.StringVar()
        self.quick_cmd_var = tk.StringVar()
        self.cmd_type_var = tk.StringVar(value="?")  # "?" => read, "!" => set
        self.param_var = tk.StringVar()
        self.desc_var = tk.StringVar(self, value="")

        # Build all widgets
        self._create_widgets()

        # Possibly finalize geometry
        self._finalize_geometry()

    def _create_widgets(self):
        """
        Creates the layout in these main sections:
         1) Connection frame
         2) Commands frame (manual/quick)
         3) Status frame (speed, motor temp, etc.)
         4) Cyc frame (updates + toggles for cyc logs & debug logs)
        """
        # ========== Connection Frame ==========
        self.conn_frame = ttk.LabelFrame(self, text="Turbo Connection")
        self.conn_frame.pack(fill=tk.X, padx=5, pady=5)

        row1 = ttk.Frame(self.conn_frame)
        row1.pack(fill=tk.X, padx=5, pady=2)

        ttk.Label(row1, text="Port:").pack(side=tk.LEFT, padx=2)
        self.port_menu = ttk.OptionMenu(row1, self.selected_port, "")
        self.port_menu.pack(side=tk.LEFT, padx=2)

        refresh_btn = ttk.Button(row1, text="Refresh", command=self._refresh_ports)
        refresh_btn.pack(side=tk.LEFT, padx=5)

        ttk.Label(row1, text="Turbo:").pack(side=tk.LEFT, padx=5)
        turbo_list = ["TC600"]  # TC1200/TC700 not yet supported; add to GAUGE_PARAMETERS before enabling
        self.turbo_combo = ttk.Combobox(
            row1,
            textvariable=self.selected_turbo,
            values=turbo_list,
            state="readonly",
            width=10
        )
        self.turbo_combo.pack(side=tk.LEFT, padx=5)
        if turbo_list:
            self.selected_turbo.set(turbo_list[0])

        settings_btn = ttk.Button(row1, text="Settings", command=self._toggle_settings_frame)
        settings_btn.pack(side=tk.LEFT, padx=5)

        row2 = ttk.Frame(self.conn_frame)
        row2.pack(fill=tk.X, padx=5, pady=2)

        ttk.Label(row2, textvariable=self.status_text).pack(side=tk.LEFT, padx=5)
        self.connect_btn = ttk.Button(row2, text="Connect", command=self._toggle_connection)
        self.connect_btn.pack(side=tk.LEFT, padx=5)

        # A container for specialized settings frame if toggled
        self.settings_container = ttk.Frame(self.conn_frame)
        self.settings_container.pack(fill=tk.X, padx=5, pady=5)

        # ========== Commands Frame ==========
        cmd_frame = ttk.LabelFrame(self, text="Turbo Commands")
        cmd_frame.pack(fill=tk.X, padx=5, pady=5)

        # Manual command subframe
        manual_frame = ttk.LabelFrame(cmd_frame, text="Manual Command")
        manual_frame.pack(fill=tk.X, padx=5, pady=5)

        ttk.Label(manual_frame, text="Command:").pack(side=tk.LEFT, padx=5)
        manual_entry = ttk.Entry(manual_frame, textvariable=self.manual_command_var, width=40)
        manual_entry.pack(side=tk.LEFT, padx=5, fill=tk.X, expand=True)

        manual_send_btn = ttk.Button(manual_frame, text="Send", command=self._send_manual_command)
        manual_send_btn.pack(side=tk.RIGHT, padx=5)

        # Quick commands subframe
        quick_frame = ttk.LabelFrame(cmd_frame, text="Quick Commands")
        quick_frame.pack(fill=tk.X, padx=5, pady=5)

        ttk.Label(quick_frame, text="Command:").pack(side=tk.LEFT, padx=5)
        self.quick_cmd_combo = ttk.Combobox(
            quick_frame,
            textvariable=self.quick_cmd_var,
            values=[
                "start_pump - Start turbo",
                "stop_pump - Stop turbo",
                "vent - Vent turbo",
                "get_speed - Read speed",
                "get_temp_motor - Read motor temperature",
                "get_current - Read current",
                "get_error - Read error code"
            ],
            state="readonly",
            width=30
        )
        self.quick_cmd_combo.pack(side=tk.LEFT, padx=5, fill=tk.X, expand=True)

        radio_frame = ttk.Frame(quick_frame)
        radio_frame.pack(side=tk.LEFT, padx=2)
        self.query_radio = ttk.Radiobutton(radio_frame, text="Query (?)", variable=self.cmd_type_var, value="?")
        self.set_radio   = ttk.Radiobutton(radio_frame, text="Set (!)",   variable=self.cmd_type_var, value="!")
        self.query_radio.pack(side=tk.LEFT, padx=2)
        self.set_radio.pack(side=tk.LEFT, padx=2)

        self.cmd_type_var.trace("w", self._update_param_state)

        param_frame = ttk.Frame(cmd_frame)
        param_frame.pack(fill=tk.X, padx=5, pady=5)

        ttk.Label(param_frame, text="Parameter:").pack(side=tk.LEFT, padx=5)
        self.param_entry = ttk.Entry(param_frame, textvariable=self.param_var, width=20)
        self.param_entry.pack(side=tk.LEFT, padx=5)

        quick_send_btn = ttk.Button(param_frame, text="Send", command=self._send_quick_command)
        quick_send_btn.pack(side=tk.RIGHT, padx=5)

        self.desc_label = ttk.Label(cmd_frame, textvariable=self.desc_var, wraplength=400)
        self.desc_label.pack(fill=tk.X, padx=5, pady=5)

        # ========== Status Frame ==========
        status_frame = ttk.LabelFrame(self, text="Turbo Status")
        status_frame.pack(fill=tk.X, padx=5, pady=5)

        # Reuse a helper to build rows
        self._build_status_row(status_frame, "Speed (rpm):",           self.speed_var,       self.speed_cyc,         self._retrieve_speed)
        self._build_status_row(status_frame, "Current (A):",           self.load_var,        self.load_cyc,          self._retrieve_load)
        self._build_status_row(status_frame, "Motor Temp (C):",        self.temp_var,        self.temp_cyc,          self._retrieve_temp)
        self._build_status_row(status_frame, "Electronics Temp (C):",  self.temp_electr_var, self.temp_electr_cyc,   self._retrieve_temp_electronics)
        self._build_status_row(status_frame, "Bearing Temp (C):",      self.temp_bearing_var,self.temp_bearing_cyc,  self._retrieve_temp_bearing)
        self._build_status_row(status_frame, "Warning Code:",          self.warning_code_var,self.warning_code_cyc,  self._retrieve_warning_code)
        self._build_status_row(status_frame, "Error Code:",            self.error_code_var,  self.error_code_cyc,    self._retrieve_error_code)
        self._build_status_row(status_frame, "Operating Hours:",       self.hours_var,       self.hours_cyc,         self._retrieve_hours)

        # ========== Cyc Frame ==========
        cyc_frame = ttk.LabelFrame(self, text="Cyclical Status Update")
        cyc_frame.pack(fill=tk.X, padx=5, pady=5)

        cyc_row1 = ttk.Frame(cyc_frame)
        cyc_row1.pack(fill=tk.X, padx=5, pady=2)

        cyc_chk = ttk.Checkbutton(
            cyc_row1,
            text="Enable Cyclical Updates",
            variable=self.cycle_var,
            command=self._toggle_cycle
        )
        cyc_chk.pack(side=tk.LEFT, padx=5)

        # A checkbutton to control cyc logs specifically
        cyc_log_chk = ttk.Checkbutton(
            cyc_row1,
            text="Show Cyc Logs",
            variable=self.cyc_log_var,
            command=self._toggle_cyc_logs
        )
        cyc_log_chk.pack(side=tk.LEFT, padx=10)

        # Another checkbutton to control debug logs system-wide
        debug_chk = ttk.Checkbutton(
            cyc_row1,
            text="Show Debug",
            variable=self.show_debug_var,
            command=self._toggle_debug
        )
        debug_chk.pack(side=tk.LEFT, padx=10)

        cyc_row2 = ttk.Frame(cyc_frame)
        cyc_row2.pack(fill=tk.X, padx=5, pady=2)

        ttk.Label(cyc_row2, text="Update Interval (ms):").pack(side=tk.LEFT, padx=5)
        ttk.Entry(cyc_row2, textvariable=self.update_interval, width=6).pack(side=tk.LEFT, padx=5)

        apply_btn = ttk.Button(cyc_row2, text="Apply", command=self._apply_interval)
        apply_btn.pack(side=tk.LEFT, padx=5)

        self.pack(fill=tk.BOTH, expand=True)
        self._refresh_ports()
        self._update_param_state()

    def _build_status_row(self, parent, label_text, var, cyc_var, retrieve_callback):
        """
        Builds a row with a cyc toggle, a label, a variable label, and a 'Retrieve' button.
        cyc_var toggles cyc reading for that parameter.
        retrieve_callback is the function that fetches that parameter from the pump.
        """
        row = ttk.Frame(parent)
        row.pack(fill=tk.X, padx=5, pady=3)

        cyc_cb = ttk.Checkbutton(row, variable=cyc_var)
        cyc_cb.pack(side=tk.LEFT, padx=5)

        lbl = ttk.Label(row, text=label_text)
        lbl.pack(side=tk.LEFT, padx=5)

        val_lbl = ttk.Label(row, textvariable=var)
        val_lbl.pack(side=tk.LEFT, padx=5)

        rtv_btn = ttk.Button(row, text="Retrieve", command=retrieve_callback)
        rtv_btn.pack(side=tk.RIGHT, padx=5)

    # ========== Toggling param entry for query vs. set ==========
    def _update_param_state(self, *args):
        """
        Disables param_entry if cmd_type_var is '?', enables if '!'.
        """
        if self.cmd_type_var.get() == "!":
            self.param_entry.config(state="normal")
        else:
            self.param_entry.config(state="disabled")

    # ========== Toggling specialized settings frame ==========
    def _toggle_settings_frame(self):
        """
        Toggles the specialized TurboSerialSettingsFrame open/closed,
        resizing the connection frame accordingly.
        """
        if self.settings_frame:
            self.settings_frame.pack_forget()
            self.settings_frame.destroy()
            self.settings_frame = None

            self.conn_frame.config(width=200, height=80)
            self.conn_frame.pack_propagate(False)
            self._finalize_geometry()
        else:
            self.settings_frame = TurboSerialSettingsFrame(
                parent=self.settings_container,
                apply_callback=self._on_turbo_settings_apply
            )
            self.settings_frame.pack(fill=tk.X, padx=5, pady=5)

            self.conn_frame.config(width=400, height=150)
            self.conn_frame.pack_propagate(False)
            self._finalize_geometry()

    def _on_turbo_settings_apply(self, settings: dict):
        """
        Called when user hits 'Apply' in TurboSerialSettingsFrame.
        Applies new serial settings if connected, or logs that they apply on connect.
        """
        if self.turbo_communicator and self.turbo_communicator.ser and self.turbo_communicator.ser.is_open:
            try:
                self.turbo_communicator.ser.baudrate  = settings["baudrate"]
                self.turbo_communicator.ser.bytesize  = settings["bytesize"]
                self.turbo_communicator.ser.parity    = settings["parity"]
                self.turbo_communicator.ser.stopbits  = settings["stopbits"]
                self.main_app.log_message(f"Turbo serial settings updated: {settings}")
            except Exception as e:
                self.main_app.log_message(f"Failed to update Turbo serial settings: {str(e)}")
        else:
            self.main_app.log_message("Turbo not connected. Settings will apply after connect.")

    # ========== Refresh Ports logic ==========

    def _refresh_ports(self):
        """
        Gathers available COM ports and populates the OptionMenu.
        """
        ports = [p.device for p in serial.tools.list_ports.comports()]
        menu = self.port_menu["menu"]
        menu.delete(0, "end")
        for port in ports:
            menu.add_command(label=port, command=lambda p=port: self.selected_port.set(p))
        if ports:
            self.selected_port.set(ports[0])
        else:
            self.selected_port.set("")

    # ========== Connect/Disconnect logic ==========

    def _toggle_connection(self):
        """
        Toggles between connected and disconnected states.
        """
        if not self.connected:
            self._connect_turbo()
        else:
            self._disconnect_turbo()

    def _connect_turbo(self):
        """
        Creates a GaugeCommunicator, attempts to connect, logs result,
        updates self.connected and status_text accordingly.
        """
        port = self.selected_port.get()
        if not port:
            messagebox.showerror("Turbo Error", "No port selected for Turbo.")
            return
        try:
            self.turbo_communicator = GaugeCommunicator(
                port=port,
                gauge_type=self.selected_turbo.get(),
                logger=self.main_app  # We'll pass main_app as the logger
            )
            if self.turbo_communicator.connect():
                self.connected = True
                self.status_text.set("Connected")
                self.connect_btn.config(text="Disconnect")
                self.main_app.log_message("Turbo: Connection established.")
            else:
                self.main_app.log_message("Turbo: Failed to connect.")
                self.turbo_communicator = None
        except Exception as e:
            self.main_app.log_message(f"Turbo Connect error: {str(e)}")
            self.turbo_communicator = None

    def _disconnect_turbo(self):
        """
        Disconnects if present, updates status & logs,
        stops cyc reading if needed.
        """
        if self.turbo_communicator:
            try:
                self.turbo_communicator.disconnect()
            except Exception as e:
                self.main_app.log_message(f"Turbo Disconnect error: {str(e)}")

        self.turbo_communicator = None
        self.connected = False
        self.status_text.set("Disconnected")
        self.connect_btn.config(text="Connect")

        if self.cycle_var.get():
            self.cycle_var.set(False)
            self._toggle_cycle()

        self.main_app.log_message("Turbo: Disconnected.")

    def _check_connected(self) -> bool:
        """
        Returns True if connected, logs a note if not.
        """
        if not self.connected or not self.turbo_communicator:
            self.main_app.log_message("Turbo is not connected.")
            return False
        return True

    # ========== Manual + Quick Commands ==========

    def _send_manual_command(self):
        """
        Sends a manual command if connected, logs the result.
        Always logs (since manual commands are not cyc).
        """
        if not self._check_connected():
            return
        cmd_str = self.manual_command_var.get().strip()
        if not cmd_str:
            self.main_app.log_message("No manual command entered.")
            return
        try:
            cmd = GaugeCommand(name=cmd_str, command_type="!")
            resp = self.turbo_communicator.send_command(cmd)
            self._log_cmd_result(cmd_str, resp)
        except Exception as e:
            self.main_app.log_message(f"Manual command error: {str(e)}")

    def _send_quick_command(self):
        """
        Sends the selected quick command from 'quick_cmd_combo' to the communicator.
        Always logs results. This is separate from cyc logs.
        """
        if not self._check_connected():
            return
        quick_val = self.quick_cmd_var.get()
        if not quick_val:
            self.main_app.log_message("No quick command selected.")
            return

        cmd_name = quick_val.split(" - ")[0]
        command_type = self.cmd_type_var.get()
        param_value = self.param_var.get().strip()

        try:
            if command_type == "!":
                command = GaugeCommand(
                    name=cmd_name,
                    command_type="!",
                    parameters={"value": param_value} if param_value else None
                )
            else:
                command = GaugeCommand(name=cmd_name, command_type="?")

            response = self.turbo_communicator.send_command(command)
            self._log_cmd_result(cmd_name, response)
        except Exception as e:
            self.main_app.log_message(f"Quick command error: {str(e)}", level="ERROR")

    def _log_cmd_result(self, cmd_name: str, resp: GaugeResponse):
        """
        Logs the result of sending a manual/quick command.
        This is not controlled by cyc_log_var, so it always shows in console.
        """
        if resp.success:
            self.main_app.log_message(f"Turbo {cmd_name} => {resp.formatted_data}")
        else:
            self.main_app.log_message(f"Turbo {cmd_name} failed => {resp.error_message}", level="ERROR")

    # ========== Toggling cyc reading & cyc logs ==========

    def _toggle_cycle(self):
        """
        Toggles cyc reading. If on => start thread, if off => stop it.
        """
        if self.cycle_var.get():
            if not self._check_connected():
                self.cycle_var.set(False)
                return
            self._start_cycle_thread()
        else:
            self._stop_cycle_thread()

    def _toggle_cyc_logs(self):
        """
        Called when user toggles "Show Cyc Logs."
        If off => cyc retrieval methods skip self.main_app.log_message(...).
        If on => cyc retrieval methods log again.
        """
        if self.cyc_log_var.get():
            self.main_app.log_message("Cyc logging is now ON.")
        else:
            self.main_app.log_message("Cyc logging is now OFF.")

    def _toggle_debug(self):
        """
        Called when user toggles "Show Debug."
        If unchecked => sets main_app.logger to CRITICAL so debug logs vanish.
        If checked => sets to DEBUG so they appear.
        """
        if self.show_debug_var.get():
            # Turn debug on
            self.main_app.logger.setLevel(logging.DEBUG)
            self.main_app.log_message("Show Debug: ON")
        else:
            # Turn debug off => CRITICAL means only critical errors appear
            self.main_app.logger.setLevel(logging.CRITICAL)
            self.main_app.log_message("Show Debug: OFF")

    def _apply_interval(self):
        """
        Applies new cyc reading interval if cyc is on.
        """
        if self.cycle_var.get():
            self._stop_cycle_thread()
            self._start_cycle_thread()

    def _start_cycle_thread(self):
        """
        Launches a background thread for cyc reading.
        """
        self.stop_thread = False
        self.status_thread = threading.Thread(target=self._cycle_loop, daemon=True)
        self.status_thread.start()

    def _stop_cycle_thread(self):
        """
        Signals cyc read thread to stop, then joins it briefly.
        """
        self.stop_thread = True
        if self.status_thread and self.status_thread.is_alive():
            self.status_thread.join(timeout=1.0)
        self.status_thread = None

    def _cycle_loop(self):
        """
        Worker function that periodically calls retrieve methods if toggles are on,
        respecting the update_interval from self.update_interval.
        """
        import time
        while not self.stop_thread and self.connected and self.turbo_communicator:
            try:
                interval = int(self.update_interval.get())
            except ValueError:
                interval = 1000

            # Call retrieve methods that are toggled ON
            if self.speed_cyc.get():
                self._retrieve_speed()
            if self.temp_cyc.get():
                self._retrieve_temp()
            if self.load_cyc.get():
                self._retrieve_load()
            if self.temp_electr_cyc.get():
                self._retrieve_temp_electronics()
            if self.temp_bearing_cyc.get():
                self._retrieve_temp_bearing()
            if self.error_code_cyc.get():
                self._retrieve_error_code()
            if self.warning_code_cyc.get():
                self._retrieve_warning_code()
            if self.hours_cyc.get():
                self._retrieve_hours()

            time.sleep(interval / 1000.0)

    # ========== CYC RETRIEVE METHODS (wrapped by cyc_log_var) ==========

    def _retrieve_speed(self):
        """Reads speed if connected, updates speed_var, logs only if cyc_log_var is True."""
        if not self._check_connected():
            return
        try:
            cmd = GaugeCommand(name="get_speed", command_type="?")
            resp = self.turbo_communicator.send_command(cmd)

            if resp.success:
                self.speed_var.set(resp.formatted_data)
                if self.cyc_log_var.get():
                    self.main_app.log_message(f"Turbo get_speed => {resp.formatted_data}")
            else:
                if self.cyc_log_var.get():
                    self.main_app.log_message(f"Turbo get_speed => {resp.error_message}", level="ERROR")
        except Exception as e:
            if self.cyc_log_var.get():
                self.main_app.log_message(f"Turbo get_speed exception => {str(e)}", level="ERROR")

    def _retrieve_temp(self):
        """Reads motor temp if connected, updates temp_var, logs only if cyc_log_var is True."""
        if not self._check_connected():
            return
        try:
            cmd = GaugeCommand(name="get_temp_motor", command_type="?")
            resp = self.turbo_communicator.send_command(cmd)
            if resp.success:
                self.temp_var.set(resp.formatted_data)
                if self.cyc_log_var.get():
                    self.main_app.log_message(f"Turbo get_temp_motor => {resp.formatted_data}")
            else:
                if self.cyc_log_var.get():
                    self.main_app.log_message(f"Turbo get_temp_motor => {resp.error_message}", level="ERROR")
        except Exception as e:
            if self.cyc_log_var.get():
                self.main_app.log_message(f"Turbo get_temp_motor exception => {str(e)}", level="ERROR")

    def _retrieve_load(self):
        """Reads load/current if connected, updates load_var, logs only if cyc_log_var is True."""
        if not self._check_connected():
            return
        try:
            cmd = GaugeCommand(name="get_current", command_type="?")
            resp = self.turbo_communicator.send_command(cmd)
            if resp.success:
                self.load_var.set(resp.formatted_data)
                if self.cyc_log_var.get():
                    self.main_app.log_message(f"Turbo get_current => {resp.formatted_data}")
            else:
                if self.cyc_log_var.get():
                    self.main_app.log_message(f"Turbo get_current => {resp.error_message}", level="ERROR")
        except Exception as e:
            if self.cyc_log_var.get():
                self.main_app.log_message(f"Turbo get_current exception => {str(e)}", level="ERROR")

    def _retrieve_temp_electronics(self):
        """Reads electronics temp if connected, updates temp_electr_var, logs only if cyc_log_var is True."""
        if not self._check_connected():
            return
        try:
            cmd = GaugeCommand(name="get_temp_electronic", command_type="?")
            resp = self.turbo_communicator.send_command(cmd)
            if resp.success:
                self.temp_electr_var.set(resp.formatted_data)
                if self.cyc_log_var.get():
                    self.main_app.log_message(f"Turbo get_temp_electronic => {resp.formatted_data}")
            else:
                if self.cyc_log_var.get():
                    self.main_app.log_message(f"Turbo get_temp_electronic => {resp.error_message}", level="ERROR")
        except Exception as e:
            if self.cyc_log_var.get():
                self.main_app.log_message(f"Turbo get_temp_electronic exception => {str(e)}", level="ERROR")

    def _retrieve_temp_bearing(self):
        """Reads bearing temp if connected, updates temp_bearing_var, logs only if cyc_log_var is True."""
        if not self._check_connected():
            return
        try:
            cmd = GaugeCommand(name="get_temp_bearing", command_type="?")
            resp = self.turbo_communicator.send_command(cmd)
            if resp.success:
                self.temp_bearing_var.set(resp.formatted_data)
                if self.cyc_log_var.get():
                    self.main_app.log_message(f"Turbo get_temp_bearing => {resp.formatted_data}")
            else:
                if self.cyc_log_var.get():
                    self.main_app.log_message(f"Turbo get_temp_bearing => {resp.error_message}", level="ERROR")
        except Exception as e:
            if self.cyc_log_var.get():
                self.main_app.log_message(f"Turbo get_temp_bearing exception => {str(e)}", level="ERROR")

    def _retrieve_error_code(self):
        """Reads error code if connected, updates error_code_var, logs only if cyc_log_var is True."""
        if not self._check_connected():
            return
        try:
            cmd = GaugeCommand(name="get_error", command_type="?")
            resp = self.turbo_communicator.send_command(cmd)
            if resp.success:
                self.error_code_var.set(resp.formatted_data)
                if self.cyc_log_var.get():
                    self.main_app.log_message(f"Turbo get_error => {resp.formatted_data}")
            else:
                if self.cyc_log_var.get():
                    self.main_app.log_message(f"Turbo get_error => {resp.error_message}", level="ERROR")
        except Exception as e:
            if self.cyc_log_var.get():
                self.main_app.log_message(f"Turbo get_error exception => {str(e)}", level="ERROR")

    def _retrieve_warning_code(self):
        """Reads warning code if connected, updates warning_code_var, logs only if cyc_log_var is True."""
        if not self._check_connected():
            return
        try:
            cmd = GaugeCommand(name="get_warning", command_type="?")
            resp = self.turbo_communicator.send_command(cmd)
            if resp.success:
                self.warning_code_var.set(resp.formatted_data)
                if self.cyc_log_var.get():
                    self.main_app.log_message(f"Turbo get_warning => {resp.formatted_data}")
            else:
                if self.cyc_log_var.get():
                    self.main_app.log_message(f"Turbo get_warning => {resp.error_message}", level="ERROR")
        except Exception as e:
            if self.cyc_log_var.get():
                self.main_app.log_message(f"Turbo get_warning exception => {str(e)}", level="ERROR")

    def _retrieve_hours(self):
        """Reads operating hours if connected, updates hours_var, logs only if cyc_log_var is True."""
        if not self._check_connected():
            return
        try:
            cmd = GaugeCommand(name="operating_hours", command_type="?")
            resp = self.turbo_communicator.send_command(cmd)
            if resp.success:
                self.hours_var.set(resp.formatted_data)
                if self.cyc_log_var.get():
                    self.main_app.log_message(f"Turbo operating_hours => {resp.formatted_data}")
            else:
                if self.cyc_log_var.get():
                    self.main_app.log_message(f"Turbo operating_hours => {resp.error_message}", level="ERROR")
        except Exception as e:
            if self.cyc_log_var.get():
                self.main_app.log_message(f"Turbo operating_hours exception => {str(e)}", level="ERROR")

    # ========== Final geometry logic ==========

    def _finalize_geometry(self):
        """
        Called after building or toggling the settings frame.
        If parent is a Toplevel, recalc minimal size and position near main window.
        """
        if isinstance(self.parent, tk.Toplevel):
            self.parent.geometry("")
            self.parent.update_idletasks()

            w = self.parent.winfo_width()
            h = self.parent.winfo_height()

            main_x = self.main_app.root.winfo_x()
            main_y = self.main_app.root.winfo_y()
            main_w = self.main_app.root.winfo_width()
            main_h = self.main_app.root.winfo_height()

            offset_x = main_x + main_w + 30
            offset_y = main_y + (main_h - h)//2

            self.parent.geometry(f"{w}x{h}+{offset_x}+{offset_y}")
