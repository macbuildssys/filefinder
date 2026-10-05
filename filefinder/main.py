"""Start the app."""
from __future__ import annotations

import os
import sys

from .config import DB_PATH, default_config, load_config, save_config
from .db import connect, init_db


def main() -> int:
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication, QDialog

    from .ui import MainWindow, SettingsDialog

    app = QApplication(sys.argv)
    app.setApplicationName("File Finder")
    app.setDesktopFileName("filefinder")
    app.setWindowIcon(QIcon(os.path.join(os.path.dirname(__file__), "data", "filefinder.png")))

    cfg = load_config()
    if cfg is None:   # first run: let the person choose the folders before anything is read
        dialog = SettingsDialog(default_config(), first_run=True)
        if dialog.exec() != QDialog.Accepted:
            return 0
        cfg = dialog.result_config()
        save_config(cfg)

    conn = connect(DB_PATH)
    init_db(conn)
    conn.close()

    window = MainWindow(cfg, DB_PATH)
    window.show()
    window.start_indexing()
    return app.exec()
