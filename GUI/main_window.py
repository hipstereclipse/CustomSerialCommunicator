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
from PyQt6.QtGui import QAction, QKeySequence
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QSplitter, QVBoxLayout,
    QHBoxLayout, QPushButton, QListWidget, QListWidgetItem,
    QTabWidget, QLabel, QStatusBar, QToolBar, QMessageBox,
    QFileDialog, QSizePolicy,
)

from serial_comm.device_registry import DeviceRegistry
from serial_comm.models import DeviceReading, DeviceError

from GUI.gauge_workspace.add_gauge_dialog import AddGaugeDialog
from GUI.gauge_workspace.gauge_tab import GaugeTab
from GUI.gauge_workspace.export_dialog import ExportDialog
from GUI.turbo_workspace.turbo_window import TurboWindow

logger = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    """Main application window."""

    def __init__(self) -> None:
        super().__init__()
        self._registry = DeviceRegistry()
        self._gauge_tabs: dict[str, GaugeTab] = {}   # device_id → GaugeTab
        self._turbo_window: TurboWindow | None = None
        self._settings = QSettings()

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

        # Help menu
        help_menu = mb.addMenu("&Help")
        about_act = QAction("&About", self)
        about_act.triggered.connect(self._on_about)
        help_menu.addAction(about_act)

    def _build_toolbar(self) -> None:
        tb = QToolBar("Main", self)
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

        self._device_list = QListWidget()
        self._device_list.currentRowChanged.connect(self._on_device_selected)
        left_layout.addWidget(self._device_list)

        remove_btn = QPushButton("Disconnect Selected")
        remove_btn.clicked.connect(self._on_disconnect_selected)
        left_layout.addWidget(remove_btn)

        left.setMinimumWidth(180)
        left.setMaximumWidth(300)
        splitter.addWidget(left)

        # --- Right panel: tabs ---
        self._tab_widget = QTabWidget()
        self._tab_widget.setTabsClosable(True)
        self._tab_widget.tabCloseRequested.connect(self._on_tab_close_requested)

        # Placeholder shown when no gauges are connected
        self._placeholder = QLabel(
            "No gauges connected.\n\nUse '+ Gauge' to add a device.",
            alignment=Qt.AlignmentFlag.AlignCenter,
        )
        self._placeholder.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        splitter.addWidget(self._placeholder)

        self._right_widget = self._placeholder
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
        dlg = AddGaugeDialog(self._registry, self)
        if dlg.exec():
            cfg = dlg.result_config()
            if cfg is None:
                return
            self._connect_gauge(cfg)

    def _connect_gauge(self, cfg: dict) -> None:
        from serial_comm.acquisition import GaugeWorker
        from serial_comm.transport import TransportConfig

        spec = cfg["spec"]
        protocol = cfg["protocol"]
        transport_cfg = TransportConfig(
            port=cfg["port"],
            baud=spec.default_baud,
            parity=spec.parity,
            data_bits=spec.data_bits,
            stop_bits=spec.stop_bits,
            timeout=2.0,
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

        tab = GaugeTab(device_id=device_id, spec=spec, worker=worker, parent=self)
        worker.reading_ready.connect(tab.on_reading)
        worker.error_occurred.connect(tab.on_error)
        worker.connected.connect(lambda: self._on_gauge_connected(device_id))
        worker.disconnected.connect(lambda: self._on_gauge_disconnected(device_id))

        self._gauge_tabs[device_id] = tab
        self._show_tab_widget()

        idx = self._tab_widget.addTab(tab, f"{spec.model} {cfg['port']}")
        self._tab_widget.setCurrentIndex(idx)

        item = QListWidgetItem(f"{spec.model}\n{cfg['port']}")
        item.setData(Qt.ItemDataRole.UserRole, device_id)
        self._device_list.addItem(item)

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

        # Remove from list
        for i in range(self._device_list.count()):
            if self._device_list.item(i).data(Qt.ItemDataRole.UserRole) == device_id:
                self._device_list.takeItem(i)
                break

        tab.deleteLater()

        if not self._gauge_tabs:
            self._show_placeholder()

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
        gauge_configs = [tab.get_session_config() for tab in self._gauge_tabs.values()]
        readings = []
        if include_data:
            for tab in self._gauge_tabs.values():
                readings.extend(tab.get_readings())

        try:
            save_session(path, gauge_configs, readings=readings if include_data else None)
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
        if not gauges:
            QMessageBox.information(self, "Empty session", "No gauges found in session file.")
            return

        names = ", ".join(f"{g['model']} ({g['port']})" for g in gauges)
        reply = QMessageBox.question(
            self,
            "Restore connections?",
            f"Reconnect {len(gauges)} gauge(s)?\n\n{names}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        for gauge_cfg in gauges:
            self._reconnect_from_session(gauge_cfg)

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

    def _show_tab_widget(self) -> None:
        if self._right_widget is not self._tab_widget:
            idx = self._splitter.indexOf(self._right_widget)
            self._splitter.replaceWidget(idx, self._tab_widget)
            self._right_widget = self._tab_widget

    def _show_placeholder(self) -> None:
        if self._right_widget is not self._placeholder:
            idx = self._splitter.indexOf(self._right_widget)
            self._splitter.replaceWidget(idx, self._placeholder)
            self._right_widget = self._placeholder

    def _set_status(self, message: str) -> None:
        self._status_label.setText(message)

    @pyqtSlot(int)
    def _on_device_selected(self, row: int) -> None:
        if row < 0:
            return
        item = self._device_list.item(row)
        device_id = item.data(Qt.ItemDataRole.UserRole)
        tab = self._gauge_tabs.get(device_id)
        if tab:
            self._tab_widget.setCurrentWidget(tab)

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
        for device_id, tab in list(self._gauge_tabs.items()):
            tab.worker.stop()
        for tab in self._gauge_tabs.values():
            tab.worker.wait(2000)

        if self._turbo_window:
            self._turbo_window.close()

        super().closeEvent(event)
