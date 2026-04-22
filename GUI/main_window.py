"""
MainWindow — top-level QMainWindow for Serial Communicator v2.

Layout:
  ┌─────────────────────────────────────────────────────────┐
  │  Menu bar                                               │
  │  Tool bar                                               │
  ├─────────────────────────────────────────────────────────┤
  │  Left panel (device list)  │  Right panel (tab per gauge)│
  │  ─────────────────────────────────────────────────────  │
  │  • [+] gauge  [+] turbo    │  [Gauge A tab][Gauge B tab]│
  │  • PPG550 COM3             │  plot + readings table     │
  │  • BCG450 COM5             │                            │
  ├─────────────────────────────────────────────────────────┤
  │  Status bar                                             │
  └─────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import logging

from PyQt6.QtCore import Qt, QSettings, pyqtSlot
from PyQt6.QtGui import QAction, QColor, QKeySequence
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QSplitter, QVBoxLayout,
    QHBoxLayout, QPushButton, QListWidget, QListWidgetItem,
    QTabWidget, QTabBar, QLabel, QStatusBar, QToolBar, QMessageBox,
    QColorDialog,
    QFileDialog, QSizePolicy,
)

from serial_comm.device_registry import DeviceRegistry
from serial_comm.models import DeviceReading, DeviceError
from serial_comm.simulation_engine import get_engine
from serial_comm.simulation_models import (
    SimulatedGaugeConfig,
    SimulationPattern,
)
from serial_comm.units import SUPPORTED_UNITS, convert_pressure

from GUI.gauge_workspace.add_gauge_dialog import AddGaugeDialog
from GUI.gauge_workspace.add_simulated_gauge_dialog import (
    AddSimulatedGaugeDialog, INFICON_BLUE, INFICON_BLUE_HOVER,
)
from GUI.gauge_workspace.combined_tab import CombinedTab, COLOR_PALETTE
from GUI.gauge_workspace.gauge_tab import GaugeTab
from GUI.gauge_workspace.simulation_tab import SimulationControlTab
from GUI.gauge_workspace.export_dialog import ExportDialog
from GUI.turbo_workspace.turbo_window import TurboWindow
from GUI.settings_dialog import (
    SettingsDialog, apply_log_level, apply_log_file, get_display_unit,
    display_signals,
)

# Hard cap on concurrently-active simulated gauges — see spec §1.
_MAX_SIMULATED_GAUGES: int = 12
_SIM_SETTINGS_TITLE: str = "⊕ Simulation"

logger = logging.getLogger(__name__)


def _spec_full_scale_mbar(spec) -> float | None:
    """Extract the declared full-scale pressure (mbar) from a DeviceSpec, if any.

    Reads ``_raw_extra.full_scale_mbar`` — currently set by CDG specs. Returns
    ``None`` for gauges without a declared factory range so the combined plot
    can decide on a sensible fallback.
    """
    raw = getattr(spec, "__dict__", {}).get("_raw_extra", {}) or {}
    fs = raw.get("full_scale_mbar")
    try:
        return float(fs) if fs else None
    except (TypeError, ValueError):
        return None


class MainWindow(QMainWindow):
    """Main application window."""

    def __init__(self) -> None:
        super().__init__()
        self._registry = DeviceRegistry()
        self._gauge_tabs: dict[str, GaugeTab] = {}   # device_id → GaugeTab
        self._sim_ids: set[str] = set()              # subset of _gauge_tabs that are simulated
        self._simulation_tab: SimulationControlTab | None = None
        self._turbo_window: TurboWindow | None = None
        self._settings = QSettings()

        # Colour management
        self._gauge_colors: dict[str, str] = {}  # device_id → hex colour
        self._list_entry_widgets: dict[str, _GaugeListEntryWidget] = {}
        self._palette_idx: int = 0

        # Combined-view tabs (references kept for ordering logic)
        self._main_tab: CombinedTab | None = None
        self._combined_sim_tab: CombinedTab | None = None

        self.setWindowTitle("Serial Communicator")
        self.resize(1280, 800)

        self._build_menu()
        self._build_toolbar()
        self._build_central()
        self._build_status_bar()
        self._restore_geometry()

    # ------------------------------------------------------------------
    # Build UI
    # ------------------------------------------------------------------

    def _build_menu(self) -> None:
        mb = self.menuBar()

        # File menu
        file_menu = mb.addMenu("&File")

        save_session = QAction("&Save Session…", self)
        save_session.setShortcut(QKeySequence.StandardKey.Save)
        save_session.triggered.connect(self._on_save_session)
        file_menu.addAction(save_session)

        load_session = QAction("&Load Session…", self)
        load_session.setShortcut(QKeySequence.StandardKey.Open)
        load_session.triggered.connect(self._on_load_session)
        file_menu.addAction(load_session)

        file_menu.addSeparator()

        export_act = QAction("&Export Data…", self)
        export_act.setShortcut(QKeySequence("Ctrl+E"))
        export_act.triggered.connect(self._on_export)
        file_menu.addAction(export_act)

        file_menu.addSeparator()

        quit_act = QAction("&Quit", self)
        quit_act.setShortcut(QKeySequence.StandardKey.Quit)
        quit_act.triggered.connect(self.close)
        file_menu.addAction(quit_act)

        # Devices menu
        devices_menu = mb.addMenu("&Devices")

        add_gauge = QAction("&Add Gauge…", self)
        add_gauge.setShortcut(QKeySequence("Ctrl+G"))
        add_gauge.triggered.connect(self._on_add_gauge)
        devices_menu.addAction(add_gauge)

        add_turbo = QAction("&Open Turbo Controller…", self)
        add_turbo.setShortcut(QKeySequence("Ctrl+T"))
        add_turbo.triggered.connect(self._on_open_turbo)
        devices_menu.addAction(add_turbo)

        # Settings menu
        settings_menu = mb.addMenu("&Settings")
        prefs_act = QAction("&Preferences…", self)
        prefs_act.setShortcut(QKeySequence("Ctrl+,"))
        prefs_act.triggered.connect(self._on_open_settings)
        settings_menu.addAction(prefs_act)

        # Help menu
        help_menu = mb.addMenu("&Help")
        about_act = QAction("&About", self)
        about_act.triggered.connect(self._on_about)
        help_menu.addAction(about_act)

    def _build_toolbar(self) -> None:
        tb = QToolBar("Main", self)
        tb.setObjectName("MainToolBar")
        tb.setMovable(False)
        self.addToolBar(tb)

        add_gauge_btn = QAction("+ Gauge", self)
        add_gauge_btn.setToolTip("Connect a new gauge")
        add_gauge_btn.triggered.connect(self._on_add_gauge)
        tb.addAction(add_gauge_btn)

        turbo_btn = QAction("Turbo", self)
        turbo_btn.setToolTip("Open Pfeiffer TC600 turbo controller window")
        turbo_btn.triggered.connect(self._on_open_turbo)
        tb.addAction(turbo_btn)

        tb.addSeparator()

        export_btn = QAction("Export…", self)
        export_btn.setToolTip("Export recorded data")
        export_btn.triggered.connect(self._on_export)
        tb.addAction(export_btn)

    def _build_central(self) -> None:
        splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.setCentralWidget(splitter)

        # --- Left panel: device list ---
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(4, 4, 4, 4)

        btn_row = QHBoxLayout()
        add_gauge_btn = QPushButton("+ Gauge")
        add_gauge_btn.clicked.connect(self._on_add_gauge)
        btn_row.addWidget(add_gauge_btn)

        turbo_btn = QPushButton("Turbo")
        turbo_btn.clicked.connect(self._on_open_turbo)
        btn_row.addWidget(turbo_btn)
        left_layout.addLayout(btn_row)

        # INFICON-blue "Simulate Gauge" button — visually distinct from the
        # real-gauge add button, capped at _MAX_SIMULATED_GAUGES.
        self._add_sim_btn = QPushButton("⊕ Simulate Gauge")
        self._add_sim_btn.setStyleSheet(
            f"QPushButton {{ background:{INFICON_BLUE}; color:white; "
            f"border-radius:6px; padding:6px 12px; font-weight:bold; }}"
            f"QPushButton:hover {{ background:{INFICON_BLUE_HOVER}; }}"
            f"QPushButton:disabled {{ background:#555; color:#CCC; }}"
        )
        self._add_sim_btn.clicked.connect(self._on_add_simulated_gauge)
        left_layout.addWidget(self._add_sim_btn)

        self._device_list = QListWidget()
        self._device_list.setSelectionMode(
            QListWidget.SelectionMode.ExtendedSelection
        )
        self._device_list.itemSelectionChanged.connect(self._on_selection_changed)
        left_layout.addWidget(self._device_list)

        remove_btn = QPushButton("Disconnect Selected")
        remove_btn.clicked.connect(self._on_disconnect_selected)
        left_layout.addWidget(remove_btn)

        left.setMinimumWidth(180)
        left.setMaximumWidth(300)
        splitter.addWidget(left)

        # --- Right panel: tab widget (always visible) ---
        self._tab_widget = QTabWidget()
        self._tab_widget.setTabsClosable(True)
        self._tab_widget.tabCloseRequested.connect(self._on_tab_close_requested)
        splitter.addWidget(self._tab_widget)

        # ── Main combined tab — always index 0 ──────────────────────
        self._main_tab = CombinedTab(
            title="Main",
            is_simulation=False,
            display_unit=get_display_unit(),
            parent=self,
        )
        self._main_tab.color_changed.connect(self._on_combined_color_changed)
        self._main_tab.gauge_toggled.connect(self._on_combined_gauge_toggled)
        display_signals.units_changed.connect(self._main_tab.set_display_unit)
        self._tab_widget.addTab(self._main_tab, "Main")
        self._hide_tab_close_button(0)

        splitter.setSizes([200, 1080])
        self._splitter = splitter

    def _build_status_bar(self) -> None:
        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)
        self._status_label = QLabel("Ready")
        self._status_bar.addPermanentWidget(self._status_label)

    # ------------------------------------------------------------------
    # Gauge management
    # ------------------------------------------------------------------

    @pyqtSlot()
    def _on_add_gauge(self) -> None:
        try:
            dlg = AddGaugeDialog(self._registry, self)
            if not dlg.exec():
                return
            cfgs = dlg.result_configs()
        except Exception as exc:
            logger.exception("Add-gauge dialog crashed")
            QMessageBox.critical(
                self, "Add Gauge failed",
                f"Could not build gauge configuration:\n\n{exc}",
            )
            return

        if not cfgs:
            QMessageBox.warning(
                self, "Incomplete configuration",
                "No valid gauge configuration was produced. "
                "Please check that a model and port are selected.",
            )
            return

        failures: list[str] = []
        for cfg in cfgs:
            try:
                self._connect_gauge(cfg)
            except Exception as exc:
                logger.exception("Failed to connect gauge")
                model = getattr(cfg.get("spec"), "model", "Unknown")
                port = cfg.get("port", "?")
                failures.append(f"{model} on {port}: {exc}")

        if failures:
            QMessageBox.critical(
                self,
                "Connection failed",
                "Some gauges could not be connected:\n\n" + "\n".join(failures),
            )

    def _connect_gauge(self, cfg: dict) -> None:
        from serial_comm.acquisition import GaugeWorker
        from serial_comm.transport import RS485Config, TransportConfig

        spec = cfg["spec"]
        protocol = cfg["protocol"]
        baud = cfg.get("baud_override", spec.default_baud)
        rs485 = RS485Config() if cfg.get("rs485_enabled") else None
        transport_cfg = TransportConfig(
            port=cfg["port"],
            baud=baud,
            parity=spec.parity,
            data_bits=spec.data_bits,
            stop_bits=spec.stop_bits,
            timeout=2.0,
            rs485=rs485,
        )
        device_id = f"{cfg['port']}:{protocol.address}"

        if device_id in self._gauge_tabs:
            QMessageBox.information(
                self, "Already connected",
                f"{device_id} is already connected.",
            )
            return

        worker = GaugeWorker(
            spec=spec,
            protocol=protocol,
            transport_config=transport_cfg,
            commands=cfg.get("commands", ["pressure"]),
            poll_interval=cfg.get("poll_interval", 1.0),
            device_id=device_id,
        )

        # Assign colour before creating the tab
        color = self._next_gauge_color()
        self._gauge_colors[device_id] = color

        tab = GaugeTab(
            device_id=device_id,
            spec=spec,
            worker=worker,
            parent=self,
            color=color,
        )
        worker.reading_ready.connect(tab.on_reading)
        worker.error_occurred.connect(tab.on_error)
        worker.connected.connect(lambda: self._on_gauge_connected(device_id))
        worker.disconnected.connect(lambda: self._on_gauge_disconnected(device_id))

        # Register in Main combined tab
        display_name = f"{spec.model} {cfg['port']}"
        self._main_tab.add_gauge(
            device_id, display_name, color,
            full_scale_mbar=_spec_full_scale_mbar(spec),
        )
        # Forward pressure readings to the combined view
        worker.reading_ready.connect(
            lambda reading, did=device_id: self._feed_main_tab(did, reading)
        )

        # Insert at the correct position: after Main tab + existing real gauge tabs
        real_count = self._real_gauge_tab_count()   # count BEFORE adding this one
        insert_idx = 1 + real_count

        self._gauge_tabs[device_id] = tab
        inserted_idx = self._tab_widget.insertTab(insert_idx, tab, display_name)
        self._tab_widget.tabBar().setTabTextColor(inserted_idx, QColor(color))
        self._tab_widget.setCurrentIndex(inserted_idx)

        self._add_device_list_entry(
            device_id=device_id,
            title=spec.model,
            subtitle=cfg["port"],
            color=color,
            simulated=False,
        )

        worker.start()
        self._set_status(f"Connecting to {device_id}…")

    def _on_gauge_connected(self, device_id: str) -> None:
        self._set_status(f"{device_id} connected")
        logger.info("Connected: %s", device_id)

    def _on_gauge_disconnected(self, device_id: str) -> None:
        self._set_status(f"{device_id} disconnected")
        logger.info("Disconnected: %s", device_id)

    @pyqtSlot()
    def _on_disconnect_selected(self) -> None:
        row = self._device_list.currentRow()
        if row < 0:
            return
        item = self._device_list.item(row)
        device_id = item.data(Qt.ItemDataRole.UserRole)
        self._disconnect_gauge(device_id)

    # ------------------------------------------------------------------
    # Simulated-gauge management
    # ------------------------------------------------------------------

    @pyqtSlot()
    def _on_add_simulated_gauge(self) -> None:
        if len(self._sim_ids) >= _MAX_SIMULATED_GAUGES:
            QMessageBox.information(
                self, "Simulation limit reached",
                f"Maximum {_MAX_SIMULATED_GAUGES} simulated gauges reached.",
            )
            return
        try:
            dlg = AddSimulatedGaugeDialog(self._registry, self)
            if not dlg.exec():
                return
            config = dlg.result_config()
        except Exception as exc:
            logger.exception("AddSimulatedGaugeDialog crashed")
            QMessageBox.critical(
                self, "Add Simulated Gauge failed",
                f"Could not configure simulated gauge:\n\n{exc}",
            )
            return
        if config is None:
            return
        try:
            self._connect_simulated_gauge(config)
        except Exception as exc:
            logger.exception("Failed to start simulated gauge")
            QMessageBox.critical(
                self, "Simulation failed",
                f"Could not start simulated gauge:\n\n{exc}",
            )

    def _connect_simulated_gauge(self, config: SimulatedGaugeConfig) -> None:
        """Start a :class:`SimulatedGaugeWorker`, create its tab, wire signals."""
        from serial_comm.simulated_worker import SimulatedGaugeWorker

        spec = self._registry.get_spec(config.model)
        engine = get_engine()

        # First simulated gauge seeds the engine's initial pattern + params.
        if engine.count() == 0:
            engine.set_pattern(
                config.pattern,
                base_pressure_mbar=config.base_pressure_mbar,
                leak_rate_mbar_l_s=config.leak_rate_mbar_l_s,
                recipe_steps=config.recipe_steps
                if config.pattern is SimulationPattern.CUSTOM else None,
                reset_clock=True,
            )
            engine.set_humidity(config.humidity_level)
            engine.set_gas(config.gas_type)

        engine.register(config)
        worker = SimulatedGaugeWorker(spec=spec, config=config)

        # Assign colour before creating the tab
        color = self._next_gauge_color()
        self._gauge_colors[config.sim_id] = color

        tab = GaugeTab(
            device_id=config.sim_id,
            spec=spec,
            worker=worker,
            parent=self,
            is_simulated=True,
            display_name=config.display_name,
            color=color,
        )
        worker.reading_ready.connect(tab.on_reading)
        worker.error_occurred.connect(tab.on_error)

        # 1. Ensure Combined Simulation tab exists (inserts before sim gauge tabs)
        self._ensure_combined_sim_tab()

        # 2. Compute insertion index for the sim gauge tab
        #    (after Combined Sim tab + all currently-registered sim gauge tabs)
        combined_idx = self._tab_widget.indexOf(self._combined_sim_tab)
        sim_insert_idx = combined_idx + 1 + len(self._sim_ids)  # BEFORE adding to _sim_ids

        # 3. Register in tracking dicts and Combined Simulation tab
        self._gauge_tabs[config.sim_id] = tab
        self._sim_ids.add(config.sim_id)
        # Simulated gauges can supply an explicit CDG full-scale; fall back to the
        # spec default otherwise.
        sim_full_scale = (
            float(config.cdg_full_scale_mbar)
            if config.cdg_full_scale_mbar is not None
            else _spec_full_scale_mbar(spec)
        )
        self._combined_sim_tab.add_gauge(
            config.sim_id, config.display_name, color,
            full_scale_mbar=sim_full_scale,
        )
        worker.reading_ready.connect(
            lambda reading, did=config.sim_id: self._feed_combined_sim_tab(did, reading)
        )

        # 4. Insert sim gauge tab
        inserted_idx = self._tab_widget.insertTab(
            sim_insert_idx, tab, f"⊕ {config.display_name}"
        )
        self._tab_widget.tabBar().setTabTextColor(inserted_idx, QColor(color))

        # If this is the second or more simulated gauge, jump to the Combined Sim tab.
        if len(self._sim_ids) >= 2 and self._combined_sim_tab is not None:
            self._tab_widget.setCurrentWidget(self._combined_sim_tab)
        else:
            self._tab_widget.setCurrentIndex(inserted_idx)

        self._add_device_list_entry(
            device_id=config.sim_id,
            title=f"⊕ {config.display_name}",
            subtitle=f"{config.model} (SIM)",
            color=color,
            simulated=True,
        )

        # 5. Settings tab always at the very end
        self._ensure_simulation_settings_tab()
        self._refresh_sim_button_state()

        worker.start()
        self._set_status(f"Simulated {config.display_name} started")

    @pyqtSlot(str)
    def _on_remove_simulated_gauge(self, sim_id: str) -> None:
        self._disconnect_gauge(sim_id)

    def _ensure_combined_sim_tab(self) -> None:
        """Insert the Combined Simulation tab if not yet present."""
        if self._combined_sim_tab is not None:
            return
        tab = CombinedTab(
            title="Combined Simulation",
            is_simulation=True,
            display_unit=get_display_unit(),
            parent=self,
        )
        tab.color_changed.connect(self._on_combined_color_changed)
        tab.gauge_toggled.connect(self._on_combined_gauge_toggled)
        display_signals.units_changed.connect(tab.set_display_unit)
        # Position: after Main tab + all currently-connected real gauge tabs
        real_count = self._real_gauge_tab_count()
        insert_idx = 1 + real_count
        idx = self._tab_widget.insertTab(insert_idx, tab, "⊕ Combined Sim")
        self._tab_widget.tabBar().setTabTextColor(idx, QColor(INFICON_BLUE))
        self._hide_tab_close_button(idx)
        self._combined_sim_tab = tab

    def _remove_combined_sim_tab(self) -> None:
        """Remove the Combined Simulation tab (called when all sim gauges removed)."""
        if self._combined_sim_tab is None:
            return
        tab = self._combined_sim_tab
        self._combined_sim_tab = None
        for i in range(self._tab_widget.count()):
            if self._tab_widget.widget(i) is tab:
                self._tab_widget.removeTab(i)
                break
        tab.deleteLater()

    def _ensure_simulation_settings_tab(self) -> None:
        """Create and append the SimulationControlTab if it isn't already."""
        if self._simulation_tab is not None:
            return
        tab = SimulationControlTab(
            engine=get_engine(),
            display_unit_getter=get_display_unit,
            parent=self,
        )
        tab.remove_requested.connect(self._on_remove_simulated_gauge)
        idx = self._tab_widget.addTab(tab, _SIM_SETTINGS_TITLE)
        self._tab_widget.tabBar().setTabTextColor(idx, QColor(INFICON_BLUE))
        self._hide_tab_close_button(idx)
        self._simulation_tab = tab

    def _remove_simulation_settings_tab(self) -> None:
        """Drop the SimulationControlTab when no simulated gauges remain."""
        if self._simulation_tab is None:
            return
        tab = self._simulation_tab
        self._simulation_tab = None
        for i in range(self._tab_widget.count()):
            if self._tab_widget.widget(i) is tab:
                self._tab_widget.removeTab(i)
                break
        tab.deleteLater()

    def _refresh_sim_button_state(self) -> None:
        at_cap = len(self._sim_ids) >= _MAX_SIMULATED_GAUGES
        self._add_sim_btn.setEnabled(not at_cap)
        self._add_sim_btn.setToolTip(
            f"Maximum {_MAX_SIMULATED_GAUGES} simulated gauges reached" if at_cap
            else "Add a simulated gauge (no real serial port opened)"
        )

    def _on_tab_close_requested(self, index: int) -> None:
        tab = self._tab_widget.widget(index)
        if isinstance(tab, GaugeTab):
            self._disconnect_gauge(tab.device_id)

    def _disconnect_gauge(self, device_id: str) -> None:
        tab = self._gauge_tabs.pop(device_id, None)
        if tab is None:
            return

        tab.worker.stop()
        tab.worker.wait(3000)

        # Remove from tab widget
        for i in range(self._tab_widget.count()):
            if self._tab_widget.widget(i) is tab:
                self._tab_widget.removeTab(i)
                break

        # Remove from device list
        for i in range(self._device_list.count()):
            if self._device_list.item(i).data(Qt.ItemDataRole.UserRole) == device_id:
                self._device_list.takeItem(i)
                break
        self._list_entry_widgets.pop(device_id, None)

        was_simulated = device_id in self._sim_ids
        if was_simulated:
            if self._combined_sim_tab is not None:
                self._combined_sim_tab.remove_gauge(device_id)
            self._sim_ids.discard(device_id)
            get_engine().unregister(device_id)
            if not self._sim_ids:
                self._remove_combined_sim_tab()
                self._remove_simulation_settings_tab()
            self._refresh_sim_button_state()
        else:
            if self._main_tab is not None:
                self._main_tab.remove_gauge(device_id)

        self._gauge_colors.pop(device_id, None)
        tab.deleteLater()
        self._set_status(f"Disconnected {device_id}")

    # ------------------------------------------------------------------
    # Turbo window
    # ------------------------------------------------------------------

    @pyqtSlot()
    def _on_open_turbo(self) -> None:
        if self._turbo_window is None or not self._turbo_window.isVisible():
            self._turbo_window = TurboWindow(parent=self)
        self._turbo_window.show()
        self._turbo_window.raise_()
        self._turbo_window.activateWindow()

    # ------------------------------------------------------------------
    # Session / export
    # ------------------------------------------------------------------

    @pyqtSlot()
    def _on_save_session(self) -> None:
        from serial_comm.session import save_session, SessionError

        path, _ = QFileDialog.getSaveFileName(
            self, "Save Session", "", "Session config (*.scj);;Session with data (*.scd)"
        )
        if not path:
            return

        include_data = path.endswith(".scd")
        gauge_configs = [
            tab.get_session_config() for tab in self._gauge_tabs.values()
            if not tab.is_simulated
        ]
        simulated_configs = [
            tab.get_simulated_config() for tab in self._gauge_tabs.values()
            if tab.is_simulated
        ]
        readings = []
        if include_data:
            for tab in self._gauge_tabs.values():
                readings.extend(tab.get_readings())

        try:
            save_session(
                path,
                gauge_configs,
                readings=readings if include_data else None,
                simulated_configs=simulated_configs or None,
            )
            self._set_status(f"Session saved: {path}")
        except SessionError as exc:
            QMessageBox.critical(self, "Save failed", str(exc))

    @pyqtSlot()
    def _on_load_session(self) -> None:
        from serial_comm.session import load_session, SessionError

        path, _ = QFileDialog.getOpenFileName(
            self, "Load Session", "", "Session files (*.scj *.scd)"
        )
        if not path:
            return

        try:
            session = load_session(path)
        except SessionError as exc:
            QMessageBox.critical(self, "Load failed", str(exc))
            return

        gauges = session.get("gauges", [])
        sim_configs: list[SimulatedGaugeConfig] = session.get("simulated_gauges", [])
        if not gauges and not sim_configs:
            QMessageBox.information(self, "Empty session", "No gauges found in session file.")
            return

        real_names = ", ".join(f"{g['model']} ({g['port']})" for g in gauges)
        sim_names = ", ".join(f"⊕ {c.display_name}" for c in sim_configs)
        summary_parts = [p for p in (real_names, sim_names) if p]
        summary = "\n".join(summary_parts)

        reply = QMessageBox.question(
            self,
            "Restore connections?",
            f"Reconnect {len(gauges)} gauge(s) and {len(sim_configs)} simulated gauge(s)?\n\n{summary}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        for gauge_cfg in gauges:
            self._reconnect_from_session(gauge_cfg)

        for sim_cfg in sim_configs:
            try:
                self._connect_simulated_gauge(sim_cfg)
            except Exception as exc:
                logger.exception("Failed to restore simulated gauge %s", sim_cfg.sim_id)
                QMessageBox.warning(
                    self, "Simulation restore failed",
                    f"Could not restore {sim_cfg.display_name}: {exc}",
                )

        self._set_status(f"Session loaded: {path}")

    def _reconnect_from_session(self, gauge_cfg: dict) -> None:
        try:
            spec = self._registry.get_spec(gauge_cfg["model"])
            protocol = self._registry.make_protocol(spec, address=gauge_cfg["address"])
        except Exception as exc:
            QMessageBox.warning(
                self, "Restore failed",
                f"Cannot restore {gauge_cfg['model']} on {gauge_cfg['port']}: {exc}",
            )
            return

        self._connect_gauge({
            "spec": spec,
            "protocol": protocol,
            "port": gauge_cfg["port"],
            "commands": gauge_cfg["commands"],
            "poll_interval": gauge_cfg["poll_interval"],
            "baud_override": gauge_cfg.get("baud_override", spec.default_baud),
            "rs485_enabled": gauge_cfg.get("rs485_enabled", False),
        })

    @pyqtSlot()
    def _on_export(self) -> None:
        if not self._gauge_tabs:
            QMessageBox.information(self, "No data", "No gauges are connected.")
            return
        # Gather all recorded data from all tabs
        all_readings: list[DeviceReading] = []
        for tab in self._gauge_tabs.values():
            all_readings.extend(tab.get_readings())

        if not all_readings:
            QMessageBox.information(self, "No data", "No readings recorded yet.")
            return

        dlg = ExportDialog(all_readings, self)
        dlg.exec()

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------

    @pyqtSlot()
    def _on_open_settings(self) -> None:
        dlg = SettingsDialog(self)
        dlg.exec()

    # ------------------------------------------------------------------
    # About
    # ------------------------------------------------------------------

    @pyqtSlot()
    def _on_about(self) -> None:
        QMessageBox.about(
            self,
            "About Serial Communicator",
            "<b>Serial Communicator v2.0</b><br>"
            "INFICON vacuum gauge and Pfeiffer turbo-pump serial communication.<br><br>"
            "Gauge protocols: PPG ASCII, Pfeiffer ASCII, Pfeiffer Binary, CDG Serial<br>"
            "Turbo support: Pfeiffer TC600",
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _next_gauge_color(self) -> str:
        color = COLOR_PALETTE[self._palette_idx % len(COLOR_PALETTE)]
        self._palette_idx += 1
        return color

    def _real_gauge_tab_count(self) -> int:
        """Number of currently-open real (non-simulated) gauge tabs."""
        return sum(1 for did in self._gauge_tabs if did not in self._sim_ids)

    def _hide_tab_close_button(self, idx: int) -> None:
        """Remove the close button from a tab that should not be user-closeable."""
        self._tab_widget.tabBar().setTabButton(
            idx, QTabBar.ButtonPosition.RightSide, None
        )

    def _feed_main_tab(self, device_id: str, reading: DeviceReading) -> None:
        """Forward a pressure reading to the Main combined tab."""
        if reading.value is None:
            return
        if reading.unit not in SUPPORTED_UNITS:
            return
        display_unit = get_display_unit()
        value = convert_pressure(reading.value, reading.unit, display_unit)
        self._main_tab.feed(device_id, reading.timestamp_mono, value)

    def _feed_combined_sim_tab(
        self, device_id: str, reading: DeviceReading
    ) -> None:
        """Forward a pressure reading to the Combined Simulation tab."""
        if reading.value is None or self._combined_sim_tab is None:
            return
        if reading.unit not in SUPPORTED_UNITS:
            return
        display_unit = get_display_unit()
        value = convert_pressure(reading.value, reading.unit, display_unit)
        self._combined_sim_tab.feed(device_id, reading.timestamp_mono, value)

    # ------------------------------------------------------------------
    # Colour propagation
    # ------------------------------------------------------------------

    @pyqtSlot(str, str)
    def _on_combined_color_changed(self, device_id: str, color: str) -> None:
        """A colour swatch was clicked inside a combined tab → propagate."""
        self._gauge_colors[device_id] = color
        # Update the individual gauge tab's swatch
        tab = self._gauge_tabs.get(device_id)
        if tab is not None:
            tab.set_gauge_color(color)
        # Update the tab-bar label colour
        self._set_tab_label_color(tab, color)
        self._set_list_entry_color(device_id, color)

    @pyqtSlot(str, str)
    def _on_gauge_color_changed(self, device_id: str, color: str) -> None:
        """Colour changed for a gauge (list-panel dot or GaugeTab swatch) → propagate everywhere."""
        self._gauge_colors[device_id] = color
        # Update the matching combined tab
        if device_id in self._sim_ids:
            if self._combined_sim_tab is not None:
                self._combined_sim_tab.set_gauge_color(device_id, color)
        else:
            if self._main_tab is not None:
                self._main_tab.set_gauge_color(device_id, color)
        # Update the tab-bar label colour
        tab = self._gauge_tabs.get(device_id)
        # Update the individual GaugeTab's plot traces and terminal highlight
        if tab is not None:
            tab.set_gauge_color(color)
        self._set_tab_label_color(tab, color)
        self._set_list_entry_color(device_id, color)

    def _set_tab_label_color(self, tab: GaugeTab | None, color: str) -> None:
        if tab is None:
            return
        for i in range(self._tab_widget.count()):
            if self._tab_widget.widget(i) is tab:
                self._tab_widget.tabBar().setTabTextColor(i, QColor(color))
                break

    def _set_status(self, message: str) -> None:
        self._status_label.setText(message)

    def _add_device_list_entry(
        self,
        *,
        device_id: str,
        title: str,
        subtitle: str,
        color: str,
        simulated: bool,
    ) -> None:
        item = QListWidgetItem()
        item.setData(Qt.ItemDataRole.UserRole, device_id)
        widget = _GaugeListEntryWidget(
            title=title,
            subtitle=subtitle,
            color=color,
            simulated=simulated,
            on_color_changed=lambda hex_color, did=device_id: self._on_gauge_color_changed(did, hex_color),
            parent=self._device_list,
        )
        item.setSizeHint(widget.sizeHint())
        self._device_list.addItem(item)
        self._device_list.setItemWidget(item, widget)
        self._list_entry_widgets[device_id] = widget

    def _set_list_entry_color(self, device_id: str, color: str) -> None:
        widget = self._list_entry_widgets.get(device_id)
        if widget is not None:
            widget.set_color(color)

    @pyqtSlot()
    def _on_selection_changed(self) -> None:
        """Left-panel multi-selection changed — update active tab and combined filter."""
        selected_items = self._device_list.selectedItems()
        selected_ids = [
            item.data(Qt.ItemDataRole.UserRole) for item in selected_items
        ]

        if not selected_ids:
            # No selection — clear all filters so combined shows everything.
            if self._main_tab:
                self._main_tab.set_visible_filter(None)
            if self._combined_sim_tab:
                self._combined_sim_tab.set_visible_filter(None)
            return

        if len(selected_ids) == 1:
            # Single selection — navigate to that gauge’s individual tab.
            tab = self._gauge_tabs.get(selected_ids[0])
            if tab:
                self._tab_widget.setCurrentWidget(tab)
            # Still filter combined views to show only this gauge.

        else:
            # Multiple selection — navigate to the appropriate combined tab.
            # Determine if selection is all sim, all real, or mixed
            sim_ids_selected = [i for i in selected_ids if i in self._sim_ids]
            real_ids_selected = [i for i in selected_ids if i not in self._sim_ids]
            if sim_ids_selected and self._combined_sim_tab:
                self._tab_widget.setCurrentWidget(self._combined_sim_tab)
            elif real_ids_selected and self._main_tab:
                self._tab_widget.setCurrentWidget(self._main_tab)

        # Apply visibility filter to combined tabs.
        sim_filter = set(i for i in selected_ids if i in self._sim_ids) or None
        real_filter = set(i for i in selected_ids if i not in self._sim_ids) or None
        # Only filter if some devices of that type are selected.
        if self._main_tab:
            if real_filter:
                self._main_tab.set_visible_filter(real_filter)
            else:
                self._main_tab.set_visible_filter(None)
        if self._combined_sim_tab:
            if sim_filter:
                self._combined_sim_tab.set_visible_filter(sim_filter)
            else:
                self._combined_sim_tab.set_visible_filter(None)

    @pyqtSlot(str, bool)
    def _on_combined_gauge_toggled(self, device_id: str, visible: bool) -> None:
        """Toggle button in a combined tab was clicked — sync the left-panel selection."""
        # Find the QListWidgetItem for this device_id.
        target_item: QListWidgetItem | None = None
        for i in range(self._device_list.count()):
            item = self._device_list.item(i)
            if item.data(Qt.ItemDataRole.UserRole) == device_id:
                target_item = item
                break
        if target_item is None:
            return

        # Block list signals to avoid re-triggering _on_selection_changed.
        self._device_list.blockSignals(True)
        if visible:
            target_item.setSelected(True)
        else:
            target_item.setSelected(False)
        self._device_list.blockSignals(False)

    # ------------------------------------------------------------------
    # Geometry persistence
    # ------------------------------------------------------------------

    def _restore_geometry(self) -> None:
        geom = self._settings.value("mainwindow/geometry")
        if geom:
            self.restoreGeometry(geom)
        state = self._settings.value("mainwindow/state")
        if state:
            self.restoreState(state)

    def closeEvent(self, event) -> None:
        self._settings.setValue("mainwindow/geometry", self.saveGeometry())
        self._settings.setValue("mainwindow/state", self.saveState())

        # Stop all workers cleanly
        for tab in self._gauge_tabs.values():
            tab.worker.stop()
        for device_id, tab in self._gauge_tabs.items():
            if not tab.worker.wait(3000):
                logger.warning("Worker %s did not stop within timeout", device_id)

        if self._turbo_window:
            self._turbo_window.close()

        super().closeEvent(event)


def _style_sim_list_item(item: QListWidgetItem) -> None:
    """Apply INFICON-blue background + white text to a left-panel list entry."""
    from PyQt6.QtGui import QBrush, QColor
    item.setBackground(QBrush(QColor(INFICON_BLUE)))
    item.setForeground(QBrush(QColor("white")))
    item.setToolTip("Simulated gauge — no real serial port is open.")


class _GaugeListEntryWidget(QWidget):
    """Left-panel list entry with a colour-dot picker to the left of the name."""

    _DOT_SIZE: int = 18

    def __init__(
        self,
        *,
        title: str,
        subtitle: str,
        color: str,
        simulated: bool,
        on_color_changed,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._title = QLabel(title)
        self._subtitle = QLabel(subtitle)
        self._dot = QPushButton("")
        self._simulated = simulated
        self._on_color_changed = on_color_changed
        self._color = color
        self.setObjectName("gaugeEntry")

        self._title.setStyleSheet("font-weight: 600;")
        self._subtitle.setStyleSheet("color: #A9A9A9; font-size: 11px;")

        self._dot.setObjectName("gaugeColorDot")
        self._dot.setFixedSize(self._DOT_SIZE, self._DOT_SIZE)
        self._dot.setCursor(Qt.CursorShape.PointingHandCursor)
        self._dot.setToolTip("Click to change gauge colour")
        self._dot.clicked.connect(self._pick_color)

        info_col = QVBoxLayout()
        info_col.setContentsMargins(0, 0, 0, 0)
        info_col.setSpacing(1)
        info_col.addWidget(self._title)
        info_col.addWidget(self._subtitle)

        row = QHBoxLayout(self)
        row.setContentsMargins(8, 6, 8, 6)
        row.setSpacing(8)
        row.addWidget(self._dot, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addLayout(info_col, 1)

        self.set_color(color)

    def set_color(self, color: str) -> None:
        self._color = color
        bg = "rgba(0,156,222,26)" if self._simulated else "rgba(60,60,60,120)"
        radius = self._DOT_SIZE // 2
        self.setStyleSheet(
            "QWidget#gaugeEntry {"
            f"background: {bg};"
            f"border: 2px solid {color};"
            "border-radius: 7px;"
            "}"
            "QWidget#gaugeEntry QPushButton#gaugeColorDot {"
            f"background: {color};"
            "border: 2px solid rgba(255,255,255,55);"
            f"border-radius: {radius}px;"
            "padding: 0;"
            "min-width: 0;"
            "}"
            "QWidget#gaugeEntry QPushButton#gaugeColorDot:hover {"
            "border: 2px solid white;"
            "}"
        )

    def _pick_color(self) -> None:
        new_color = QColorDialog.getColor(QColor(self._color), self, "Choose gauge color")
        if not new_color.isValid():
            return
        color = new_color.name()
        self.set_color(color)
        self._on_color_changed(color)
