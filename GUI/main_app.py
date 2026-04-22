"""
Serial Communicator — main application entry point (v2, PyQt6).

Creates the QApplication, constructs the MainWindow, and starts the event loop.
"""

from __future__ import annotations

import faulthandler
import logging
import sys
import traceback

from PyQt6.QtWidgets import QApplication, QMessageBox

from GUI.main_window import MainWindow

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)


def _install_excepthook() -> None:
    """Catch uncaught Python exceptions (especially in Qt slots) and log them.

    Without this, PyQt6 may silently swallow exceptions raised from slots or
    simply abort the process with no traceback, which hides crashes from
    users.
    """

    def _hook(exc_type, exc_value, exc_tb) -> None:
        text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        logger.error("Uncaught exception:\n%s", text)
        try:
            QMessageBox.critical(
                None,
                "Unexpected error",
                f"{exc_type.__name__}: {exc_value}\n\nSee console for details.",
            )
        except Exception:
            pass

    sys.excepthook = _hook


def main() -> None:
    # Dump a native stack trace on hard crashes (segfault, abort, etc.)
    faulthandler.enable()
    _install_excepthook()

    app = QApplication(sys.argv)
    app.setApplicationName("Serial Communicator")
    app.setOrganizationName("INFICON")
    app.setApplicationVersion("2.0.0")

    # Apply persisted settings before creating any windows
    from GUI.settings_dialog import apply_log_level, apply_log_file
    apply_log_level()
    apply_log_file()

    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
