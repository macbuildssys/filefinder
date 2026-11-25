"""The look of the app: rounded corners, soft borders, roomy rows and thin scrollbars.

Colours are fixed (see PALETTE), so it looks the same on every system.
"""
from __future__ import annotations

import os

CHEVRON = os.path.join(os.path.dirname(__file__), "data", "chevron-down.png").replace("\\", "/")

STYLE = """
QLineEdit, QComboBox {
    border: 1px solid palette(mid);
    border-radius: 10px;
    padding: 8px 12px;
    background: palette(base);
}
QLineEdit { font-size: 11pt; padding: 10px 14px; }
QLineEdit:focus, QComboBox:focus, QComboBox:on { border: 1px solid palette(highlight); }
QComboBox { min-width: 110px; combobox-popup: 0; }   /* plain list under the box, so it scrolls normally */
QComboBox::drop-down { subcontrol-origin: padding; subcontrol-position: center right; border: none; width: 28px; }
QComboBox::down-arrow { image: url(CHEVRON_PATH); width: 12px; height: 12px; }
QComboBox QAbstractItemView {
    border: 1px solid palette(mid);
    border-radius: 8px;
    background: palette(base);
    selection-background-color: palette(highlight);
    selection-color: palette(highlighted-text);
    outline: 0;
}
QComboBox QAbstractItemView::item { min-height: 30px; padding: 0 8px; }

QTableView {
    border: 1px solid palette(midlight);
    border-radius: 10px;
    background: palette(base);
    gridline-color: rgba(128, 128, 128, 90);
    outline: 0;
}
QTableView::item { padding: 0 8px; border: none; }
QTableView::item:hover { background: palette(alternate-base); }
QTableView::item:selected { background: palette(highlight); color: palette(highlighted-text); }
QHeaderView::section {
    background: transparent;
    border: none;
    border-right: 1px solid rgba(128, 128, 128, 90);
    border-bottom: 1px solid rgba(128, 128, 128, 90);
    padding: 10px 8px;
    font-weight: 600;
}
QTableCornerButton::section { background: transparent; border: none; }

QScrollBar:vertical { width: 12px; background: transparent; margin: 2px; }
QScrollBar:horizontal { height: 12px; background: transparent; margin: 2px; }
QScrollBar::handle:vertical { background: palette(mid); border-radius: 4px; min-height: 36px; }
QScrollBar::handle:horizontal { background: palette(mid); border-radius: 4px; min-width: 36px; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }

QPushButton {
    border: 1px solid palette(mid);
    border-radius: 8px;
    padding: 6px 16px;
    background: palette(button);
}
QPushButton:hover { border: 1px solid palette(highlight); }
QPushButton:flat { border: none; color: palette(highlight); padding: 6px 12px; }

QGroupBox {
    border: 1px solid palette(midlight);
    border-radius: 10px;
    margin-top: 14px;
    padding: 12px 10px 8px 10px;
}
QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 4px; font-weight: 600; }
QListWidget { border: 1px solid palette(midlight); border-radius: 8px; background: palette(base); }

QMenu { border: 1px solid palette(mid); border-radius: 8px; padding: 6px; background: palette(base); }
QMenu::item { padding: 7px 26px 7px 14px; border-radius: 6px; }
QMenu::item:selected { background: palette(highlight); color: palette(highlighted-text); }
QMenu::separator { height: 1px; background: palette(midlight); margin: 5px 8px; }
"""

# One fixed light palette, so the app looks the same on every system, also when the
# system itself is set to dark mode. There is no light or dark setting.
PALETTE = {
    "Window": "#ebeff4", "WindowText": "#232629", "Base": "#ffffff", "AlternateBase": "#fbfcfd",
    "Text": "#232629", "Button": "#ebeff4", "ButtonText": "#232629", "BrightText": "#ffffff",
    "Highlight": "#3daee9", "HighlightedText": "#ffffff", "Link": "#2980b9",
    "ToolTipBase": "#ffffff", "ToolTipText": "#232629", "PlaceholderText": "#7f8c8d",
    "Light": "#ffffff", "Midlight": "#d2d2d2", "Mid": "#a7a8a9", "Dark": "#8e8f90",
    "Shadow": "#5b5c5d",
}

def apply(app) -> None:
    """Use the same base style and colours everywhere, then lay the stylesheet on top."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor, QPalette
    app.setStyle("Fusion")
    try:
        app.styleHints().setColorScheme(Qt.ColorScheme.Light)     # Qt 6.8 and newer
    except AttributeError:
        pass
    palette = QPalette()
    for name, colour in PALETTE.items():
        palette.setColor(getattr(QPalette.ColorRole, name), QColor(colour))
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText, QPalette.ColorRole.WindowText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor("#a7a8a9"))
    app.setPalette(palette)
    app.setStyleSheet(STYLE.replace("CHEVRON_PATH", CHEVRON))
