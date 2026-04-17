"""
Serial Communicator — main application entry point (v2, PyQt6).

Creates the QApplication, constructs the MainWindow, and starts the event loop.
"""

from __future__ import annotations

import logging
import sys

from PyQt6.QtWidgets import QApplication

from GUI.main_window import MainWindow

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger(__name__)


def main() -> None:
    app = QApplication(sys.argv)
    app.setApplicationName("Serial Communicator")
    app.setOrganizationName("INFICON")
    app.setApplicationVersion("2.0.0")

    window = MainWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
