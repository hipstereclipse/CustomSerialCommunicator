from __future__ import annotations

from dataclasses import dataclass

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QPushButton,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QHBoxLayout,
    QVBoxLayout,
    QWidget,
)

from serial_comm.command_utils import (
    default_poll_commands,
    is_pressure_command,
    is_primary_pressure_command,
)
from serial_comm.models import DeviceSpec
from GUI.theme import current_theme, list_style


@dataclass(frozen=True)
class PollCommandsTarget:
    """One connected gauge whose polling commands can be edited."""

    device_id: str
    label: str
    spec: DeviceSpec
    selected_commands: list[str]


class _PollCommandsEditor(QWidget):
    """Command checklist for a single gauge spec."""

    def __init__(
        self,
        spec: DeviceSpec,
        selected_commands: list[str] | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._spec = spec
        self._selected = set(selected_commands or [])

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        actions = QHBoxLayout()
        pressure_btn = QPushButton("Combined Pressure")
        pressure_btn.setToolTip("Select the combined/total pressure commands")
        pressure_btn.clicked.connect(self._select_default_pressure)
        all_pressure_btn = QPushButton("All Pressure")
        all_pressure_btn.setToolTip("Add pressure subsensor commands as separate traces")
        all_pressure_btn.clicked.connect(self._select_pressure_like)
        all_btn = QPushButton("Select All")
        all_btn.clicked.connect(lambda: self._set_all_checked(True))
        none_btn = QPushButton("Clear")
        none_btn.clicked.connect(lambda: self._set_all_checked(False))
        actions.addWidget(pressure_btn)
        actions.addWidget(all_pressure_btn)
        actions.addWidget(all_btn)
        actions.addWidget(none_btn)
        actions.addStretch()
        layout.addLayout(actions)

        self._tree = QTreeWidget()
        self._tree.setHeaderLabels(["Command", "Type", "Description"])
        self._tree.setRootIsDecorated(True)
        self._tree.setAlternatingRowColors(True)
        self._tree.setColumnWidth(0, 190)
        self._tree.setColumnWidth(1, 110)
        self.apply_theme()
        layout.addWidget(self._tree, 1)

        groups: dict[str, QTreeWidgetItem] = {}
        for name, command in self._spec.commands.items():
            if not command.read:
                continue
            group_name = self._group_for_command(name)
            group = groups.get(group_name)
            if group is None:
                group = QTreeWidgetItem([group_name, "", ""])
                group.setFlags(group.flags() & ~Qt.ItemFlag.ItemIsSelectable)
                self._tree.addTopLevelItem(group)
                group.setExpanded(True)
                groups[group_name] = group
            item = QTreeWidgetItem([
                name.replace("_", " "),
                self._type_label(name),
                command.description or "",
            ])
            item.setData(0, Qt.ItemDataRole.UserRole, name)
            item.setToolTip(0, command.description or name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                0,
                Qt.CheckState.Checked if name in self._selected else Qt.CheckState.Unchecked,
            )
            group.addChild(item)

    def selected_commands(self) -> list[str]:
        checked = set(self._checked_commands())
        return [
            name for name, command in self._spec.commands.items()
            if name in checked and command.read
        ]

    def _checked_commands(self) -> list[str]:
        commands: list[str] = []
        for i in range(self._tree.topLevelItemCount()):
            group = self._tree.topLevelItem(i)
            for j in range(group.childCount()):
                child = group.child(j)
                if child.checkState(0) == Qt.CheckState.Checked:
                    commands.append(child.data(0, Qt.ItemDataRole.UserRole))
        return commands

    def _set_all_checked(self, checked: bool) -> None:
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        for i in range(self._tree.topLevelItemCount()):
            group = self._tree.topLevelItem(i)
            for j in range(group.childCount()):
                group.child(j).setCheckState(0, state)

    def _select_pressure_like(self) -> None:
        for i in range(self._tree.topLevelItemCount()):
            group = self._tree.topLevelItem(i)
            for j in range(group.childCount()):
                child = group.child(j)
                name = child.data(0, Qt.ItemDataRole.UserRole)
                child.setCheckState(
                    0,
                    Qt.CheckState.Checked if self._is_pressure_like(name) else Qt.CheckState.Unchecked,
                )

    def _select_default_pressure(self) -> None:
        selected = set(default_poll_commands(self._spec))
        for i in range(self._tree.topLevelItemCount()):
            group = self._tree.topLevelItem(i)
            for j in range(group.childCount()):
                child = group.child(j)
                name = child.data(0, Qt.ItemDataRole.UserRole)
                child.setCheckState(
                    0,
                    Qt.CheckState.Checked if name in selected else Qt.CheckState.Unchecked,
                )

    def _group_for_command(self, name: str) -> str:
        if self._is_pressure_like(name):
            return "Pressure and Vacuum"
        command = self._spec.commands[name]
        data_type = (command.data_type or "").lower()
        if "spectrum" in name.lower() or "array" in data_type:
            return "Spectra and Arrays"
        if command.unit or data_type in {"uint8", "uint16_be", "uint32_be", "int32_be", "float32_be", "u_real", "u_integer"}:
            return "Numeric Telemetry"
        return "Status and Metadata"

    def _type_label(self, name: str) -> str:
        command = self._spec.commands[name]
        if is_primary_pressure_command(name, command):
            return "combined pressure"
        if self._is_pressure_like(name):
            return "subsensor pressure"
        if command.unit:
            return command.unit
        return command.data_type or "status"

    def _is_pressure_like(self, name: str) -> bool:
        command = self._spec.commands[name]
        return is_pressure_command(command)

    def apply_theme(self) -> None:
        self._tree.setStyleSheet(list_style("QTreeWidget", radius=8))


class PollCommandsDialog(QDialog):
    """Edit cyclic poll lists for one or more already-connected gauges."""

    def __init__(
        self,
        spec: DeviceSpec | list[PollCommandsTarget],
        selected_commands: list[str] | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        if isinstance(spec, list):
            self._targets = spec
        else:
            self._targets = [
                PollCommandsTarget(
                    device_id="",
                    label=spec.model,
                    spec=spec,
                    selected_commands=selected_commands or [],
                )
            ]

        title = "Edit Poll Commands"
        if len(self._targets) == 1:
            title = f"Edit Poll Commands - {self._targets[0].label}"
        self.setWindowTitle(title)
        self.setMinimumSize(700 if len(self._targets) > 1 else 620, 560 if len(self._targets) > 1 else 520)

        self._editors: dict[str, _PollCommandsEditor] = {}

        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        intro_text = (
            "Choose which read commands stay in each gauge's background polling cycle. "
            "Removing pressure will also remove that gauge from the combined pressure trend."
        )
        intro = QLabel(intro_text)
        intro.setWordWrap(True)
        self._intro = intro
        layout.addWidget(intro)

        if len(self._targets) == 1:
            target = self._targets[0]
            editor = _PollCommandsEditor(target.spec, target.selected_commands, self)
            self._editors[target.device_id] = editor
            layout.addWidget(editor, 1)
        else:
            tabs = QTabWidget()
            tabs.setDocumentMode(True)
            self._tabs = tabs
            for target in self._targets:
                editor = _PollCommandsEditor(target.spec, target.selected_commands, tabs)
                self._editors[target.device_id] = editor
                tabs.addTab(editor, self._tab_label(target))
                tabs.setTabToolTip(tabs.count() - 1, f"{target.label}\n{target.device_id}")
            layout.addWidget(tabs, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.apply_theme()

    def selected_commands(self) -> list[str]:
        target = self._targets[0]
        return self._editors[target.device_id].selected_commands()

    def selected_commands_by_device(self) -> dict[str, list[str]]:
        return {
            target.device_id: self._editors[target.device_id].selected_commands()
            for target in self._targets
        }

    def _tab_label(self, target: PollCommandsTarget) -> str:
        label = target.label.strip() or target.device_id
        if len(label) <= 28:
            return label
        return f"{label[:25]}..."

    def apply_theme(self) -> None:
        theme = current_theme(self)
        self._intro.setStyleSheet(f"color:{theme.muted};")
        tabs = getattr(self, "_tabs", None)
        if tabs is not None:
            tabs.setStyleSheet(
                f"QTabWidget::pane {{ border:1px solid {theme.border}; border-radius:6px; }}"
                f"QTabBar::tab {{ background:{theme.panel_alt}; color:{theme.text}; padding:7px 12px; "
                f"border:1px solid {theme.border}; border-bottom:none; }}"
                f"QTabBar::tab:selected {{ background:{theme.selection}; color:{theme.selection_text}; }}"
            )