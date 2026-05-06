"""Application-wide light/dark theme support."""

from __future__ import annotations

from dataclasses import dataclass

import pyqtgraph as pg
from PyQt6.QtCore import QSettings, Qt
from PyQt6.QtGui import QColor, QPalette
from PyQt6.QtWidgets import QApplication, QWidget


@dataclass(frozen=True)
class Theme:
    name: str
    window: str
    panel: str
    panel_alt: str
    control: str
    control_hover: str
    border: str
    text: str
    muted: str
    disabled: str
    selection: str
    selection_text: str
    plot_bg: str
    plot_fg: str
    value_bg: str
    danger_bg: str
    danger_fg: str
    success_bg: str
    success_fg: str


THEMES: dict[str, Theme] = {
    "dark": Theme(
        name="dark",
        window="#15181C",
        panel="#1D2329",
        panel_alt="#242B32",
        control="#27313A",
        control_hover="#33414D",
        border="#3B4650",
        text="#E7EDF3",
        muted="#9BA8B4",
        disabled="#69737C",
        selection="#24587D",
        selection_text="#FFFFFF",
        plot_bg="#10151A",
        plot_fg="#DCE7EF",
        value_bg="#202830",
        danger_bg="#3A1F1F",
        danger_fg="#FFBABA",
        success_bg="#173827",
        success_fg="#BDF4CE",
    ),
    "light": Theme(
        name="light",
        window="#F4F6F8",
        panel="#FFFFFF",
        panel_alt="#E9EEF3",
        control="#F7F9FB",
        control_hover="#E7EEF5",
        border="#B8C4CE",
        text="#1B232B",
        muted="#596774",
        disabled="#8B98A4",
        selection="#CFE7F8",
        selection_text="#13212C",
        plot_bg="#FFFFFF",
        plot_fg="#24313B",
        value_bg="#E9EEF3",
        danger_bg="#FCE5E5",
        danger_fg="#9B1C1C",
        success_bg="#DFF3E6",
        success_fg="#176B3A",
    ),
}

_SETTINGS_KEY = "ui/theme"
_DEFAULT_THEME = "dark"


def saved_theme_name() -> str:
    name = str(QSettings().value(_SETTINGS_KEY, _DEFAULT_THEME) or _DEFAULT_THEME).lower()
    return name if name in THEMES else _DEFAULT_THEME


def save_theme_name(name: str) -> None:
    QSettings().setValue(_SETTINGS_KEY, name if name in THEMES else _DEFAULT_THEME)


def theme_for(name: str | None = None) -> Theme:
    return THEMES.get((name or saved_theme_name()).lower(), THEMES[_DEFAULT_THEME])


def apply_theme(app: QApplication, name: str | None = None) -> Theme:
    theme = theme_for(name)
    save_theme_name(theme.name)
    app.setProperty("theme", theme.name)
    app.setPalette(_palette(theme))
    app.setStyleSheet(_qss(theme))
    pg.setConfigOption("background", theme.plot_bg)
    pg.setConfigOption("foreground", theme.plot_fg)
    return theme


def apply_theme_to_widget_tree(root: QWidget) -> None:
    """Ask widgets with theme-aware hooks to repaint mode-specific surfaces."""
    hook = getattr(root, "apply_theme", None)
    if callable(hook):
        hook()
    for child in root.findChildren(QWidget):
        hook = getattr(child, "apply_theme", None)
        if callable(hook):
            hook()


def current_theme(widget: QWidget | None = None) -> Theme:
    app = QApplication.instance()
    if app is not None:
        name = app.property("theme")
        if isinstance(name, str):
            return theme_for(name)
    if widget is not None:
        name = widget.property("theme")
        if isinstance(name, str):
            return theme_for(name)
    return theme_for()


def value_bar_style() -> str:
    theme = current_theme()
    return (
        f"color:{theme.text}; font-size:11px; padding:1px 6px;"
        f"background:{theme.value_bg}; border-top:1px solid {theme.border};"
    )


def muted_label_style(*, size: int = 11) -> str:
    return f"color:{current_theme().muted}; font-size:{size}px;"


def panel_frame_style() -> str:
    theme = current_theme()
    return f"QFrame {{ background:{theme.panel}; border:1px solid {theme.border}; border-radius:8px; }}"


def list_style(widget: str = "QListWidget", *, radius: int = 6) -> str:
    theme = current_theme()
    return (
        f"{widget} {{ background:{theme.panel}; color:{theme.text}; "
        f"border:1px solid {theme.border}; border-radius:{radius}px; }}"
        f"{widget}::item {{ padding:5px 4px; }}"
        f"{widget}::item:selected {{ background:{theme.selection}; color:{theme.selection_text}; }}"
    )


def line_edit_style() -> str:
    theme = current_theme()
    return (
        f"QLineEdit {{ background:{theme.panel}; color:{theme.text}; "
        f"border:1px solid {theme.border}; border-radius:3px; padding:3px 5px; }}"
        "QLineEdit:focus { border-color:#4C9BE8; }"
    )


def plot_background() -> str:
    return current_theme().plot_bg


def plot_foreground() -> str:
    return current_theme().plot_fg


def themed_plot(plot) -> None:
    theme = current_theme()
    try:
        plot.setBackground(theme.plot_bg)
    except AttributeError:
        return
    plot_item = getattr(plot, "getPlotItem", lambda: None)()
    if plot_item is not None:
        _style_plot_item(plot_item, theme)


def themed_graphics_layout(widget) -> None:
    theme = current_theme()
    try:
        widget.setBackground(theme.plot_bg)
    except AttributeError:
        return


def style_plot_item(plot_item) -> None:
    _style_plot_item(plot_item, current_theme())


def _style_plot_item(plot_item, theme: Theme) -> None:
    for axis_name in ("left", "right", "top", "bottom"):
        try:
            axis = plot_item.getAxis(axis_name)
            axis.setPen(theme.plot_fg)
            axis.setTextPen(theme.plot_fg)
        except Exception:
            pass


def _palette(theme: Theme) -> QPalette:
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(theme.window))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(theme.text))
    palette.setColor(QPalette.ColorRole.Base, QColor(theme.panel))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(theme.panel_alt))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(theme.panel))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor(theme.text))
    palette.setColor(QPalette.ColorRole.Text, QColor(theme.text))
    palette.setColor(QPalette.ColorRole.Button, QColor(theme.control))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(theme.text))
    palette.setColor(QPalette.ColorRole.BrightText, QColor("#FFFFFF"))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(theme.selection))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(theme.selection_text))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(theme.disabled))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(theme.disabled))
    return palette


def _qss(theme: Theme) -> str:
    return f"""
    QWidget {{ color:{theme.text}; background:{theme.window}; }}
    QMainWindow, QDialog {{ background:{theme.window}; }}
    QMenuBar, QMenu, QToolBar, QStatusBar {{ background:{theme.panel}; color:{theme.text}; }}
    QMenuBar::item:selected, QMenu::item:selected {{ background:{theme.selection}; color:{theme.selection_text}; }}
    QToolBar {{ border-bottom:1px solid {theme.border}; spacing:6px; }}
    QToolBar#MainToolBar {{ padding:4px 8px; spacing:6px; }}
    QToolBar#MainToolBar QToolButton {{ min-height:28px; min-width:72px; padding:4px 14px; border-radius:6px; font-weight:600; border:none; background:{theme.control}; }}
    QToolBar#MainToolBar QToolButton:hover {{ background:{theme.control_hover}; }}
    QToolBar#MainToolBar QToolButton:pressed {{ background:{theme.selection}; color:{theme.selection_text}; }}
    QToolBar#MainToolBar::separator {{ width:1px; background:{theme.border}; margin:4px 3px; }}
    QGroupBox {{ border:1px solid {theme.border}; border-radius:6px; margin-top:10px; padding-top:8px; font-weight:600; }}
    QGroupBox::title {{ subcontrol-origin: margin; left:8px; padding:0 4px; color:{theme.text}; }}
    QPushButton, QToolButton {{ background:{theme.control}; color:{theme.text}; border:1px solid {theme.border}; border-radius:5px; padding:5px 10px; }}
    QPushButton:hover, QToolButton:hover {{ background:{theme.control_hover}; }}
    QPushButton:pressed, QToolButton:pressed, QPushButton:checked, QToolButton:checked {{ background:{theme.selection}; color:{theme.selection_text}; }}
    QPushButton:disabled, QToolButton:disabled {{ color:{theme.disabled}; background:{theme.panel_alt}; }}
    QLineEdit, QTextEdit, QPlainTextEdit, QComboBox, QSpinBox, QDoubleSpinBox, QDateTimeEdit {{ background:{theme.panel}; color:{theme.text}; border:1px solid {theme.border}; border-radius:4px; padding:3px 5px; selection-background-color:{theme.selection}; selection-color:{theme.selection_text}; }}
    QComboBox QAbstractItemView {{ background:{theme.panel}; color:{theme.text}; selection-background-color:{theme.selection}; selection-color:{theme.selection_text}; }}
    QListWidget, QTreeWidget, QTableWidget {{ background:{theme.panel}; color:{theme.text}; alternate-background-color:{theme.panel_alt}; border:1px solid {theme.border}; border-radius:6px; selection-background-color:{theme.selection}; selection-color:{theme.selection_text}; }}
    QHeaderView::section {{ background:{theme.panel_alt}; color:{theme.text}; border:1px solid {theme.border}; padding:4px; }}
    QTabWidget::pane {{ border:1px solid {theme.border}; background:{theme.window}; }}
    QTabBar::tab {{ background:{theme.panel_alt}; color:{theme.text}; padding:6px 11px; border:1px solid {theme.border}; border-bottom:none; }}
    QTabBar::tab:selected {{ background:{theme.panel}; color:{theme.text}; }}
    QScrollArea, QSplitter {{ background:{theme.window}; }}
    QScrollBar:vertical, QScrollBar:horizontal {{ background:{theme.panel_alt}; border:none; }}
    QScrollBar::handle:vertical, QScrollBar::handle:horizontal {{ background:{theme.border}; border-radius:4px; min-height:20px; min-width:20px; }}
    QProgressBar {{ border:1px solid {theme.border}; border-radius:4px; text-align:center; background:{theme.panel}; color:{theme.text}; }}
    QProgressBar::chunk {{ background:#4C9BE8; border-radius:3px; }}
    """