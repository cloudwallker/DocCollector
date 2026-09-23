"""Shared light theme for the DocCollector desktop interface.

Call :func:`apply_theme` once after creating QApplication and before showing
windows. Widgets may opt into semantic variants by setting their ``variant``
dynamic property to ``primary``, ``quiet``, or ``danger``.
"""

from __future__ import annotations

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication


STYLE_SHEET = """
QMainWindow, QDialog {
    background-color: #F5F7FB;
    color: #1A2B46;
}
QLabel {
    color: #253750;
}
QLabel[role="sectionTitle"] {
    color: #162A47;
    font-size: 14px;
    font-weight: 600;
}
QLabel[role="muted"] {
    color: #607188;
}
QLabel[role="status"] {
    color: #245792;
    font-weight: 600;
}
QFrame[role="card"] {
    background-color: #FFFFFF;
    border: 1px solid #DCE4EF;
    border-radius: 9px;
}

QMenuBar {
    background-color: #F5F7FB;
    color: #253750;
    border-bottom: 1px solid #E2E8F1;
    padding: 3px 7px;
}
QMenuBar::item {
    border-radius: 5px;
    padding: 5px 9px;
}
QMenuBar::item:selected {
    background-color: #E5EFFC;
    color: #194A89;
}
QMenu {
    background-color: #FFFFFF;
    color: #253750;
    border: 1px solid #DCE4EF;
    padding: 5px;
}
QMenu::item {
    border-radius: 4px;
    padding: 5px 22px;
}
QMenu::item:selected {
    background-color: #E5EFFC;
    color: #194A89;
}

QPushButton {
    background-color: #FFFFFF;
    color: #243651;
    border: 1px solid #CCD8E7;
    border-radius: 7px;
    padding: 6px 12px;
    min-height: 26px;
}
QPushButton:hover {
    background-color: #EDF4FE;
    border-color: #92B6E9;
}
QPushButton:pressed, QPushButton:checked {
    background-color: #DCEBFD;
    border-color: #6E9CD9;
}
QPushButton:focus {
    border-color: #2468CB;
}
QPushButton:disabled {
    background-color: #F0F3F7;
    color: #8C9AB0;
    border-color: #DFE5ED;
}
QPushButton[variant="primary"] {
    background-color: #245FB8;
    color: #FFFFFF;
    border-color: #245FB8;
    font-weight: 600;
}
QPushButton[variant="primary"]:hover {
    background-color: #1E539F;
    border-color: #1E539F;
}
QPushButton[variant="primary"]:pressed {
    background-color: #18437F;
    border-color: #18437F;
}
QPushButton[variant="primary"]:focus {
    border-color: #113C7B;
}
QPushButton[variant="primary"]:disabled {
    background-color: #D8E3F2;
    color: #778AA5;
    border-color: #D8E3F2;
}
QPushButton[variant="quiet"] {
    background-color: transparent;
    border-color: transparent;
    color: #365C91;
}
QPushButton[variant="quiet"]:hover {
    background-color: #E9F1FC;
    border-color: #E9F1FC;
}
QPushButton[variant="danger"] {
    color: #A63B42;
    border-color: #EAC6C9;
}
QPushButton[variant="danger"]:hover {
    background-color: #FFF0F0;
    border-color: #D7959B;
}
QPushButton[variant="danger"]:disabled {
    background-color: #F0F3F7;
    color: #8C9AB0;
    border-color: #DFE5ED;
}
QDialogButtonBox QPushButton {
    min-width: 72px;
}

QLineEdit, QComboBox, QDoubleSpinBox, QDateEdit {
    background-color: #FFFFFF;
    color: #1A2B46;
    border: 1px solid #CBD6E5;
    border-radius: 7px;
    padding: 5px 9px;
    min-height: 27px;
    selection-background-color: #245FB8;
    selection-color: #FFFFFF;
}
QLineEdit:hover, QComboBox:hover, QDoubleSpinBox:hover, QDateEdit:hover {
    border-color: #92B6E9;
}
QLineEdit:focus, QComboBox:focus, QDoubleSpinBox:focus, QDateEdit:focus {
    border-color: #2468CB;
    background-color: #FFFFFF;
}
QLineEdit:disabled, QComboBox:disabled, QDoubleSpinBox:disabled, QDateEdit:disabled {
    background-color: #F0F3F7;
    color: #8C9AB0;
    border-color: #DFE5ED;
}
QComboBox QAbstractItemView {
    background-color: #FFFFFF;
    color: #1A2B46;
    border: 1px solid #CBD6E5;
    selection-background-color: #DCEBFD;
    selection-color: #1A2B46;
}
QPlainTextEdit, QTextEdit, QListWidget {
    background-color: #FFFFFF;
    color: #1A2B46;
    border: 1px solid #D7E0EC;
    border-radius: 7px;
    padding: 5px;
    selection-background-color: #DCEBFD;
    selection-color: #1A2B46;
}
QPlainTextEdit:focus, QTextEdit:focus, QListWidget:focus {
    border-color: #86AEE3;
}
QListWidget::item {
    padding: 5px;
}
QListWidget::item:selected {
    background-color: #DCEBFD;
    color: #17355B;
}
QCheckBox {
    color: #253750;
    spacing: 7px;
}
QCheckBox:disabled {
    color: #8C9AB0;
}

QTableView, QTableWidget {
    background-color: #FFFFFF;
    alternate-background-color: #F8FAFD;
    color: #1A2B46;
    gridline-color: #E7EDF4;
    border: 1px solid #DCE4EF;
    border-radius: 7px;
    selection-background-color: #DCEBFD;
    selection-color: #17355B;
}
QHeaderView::section {
    background-color: #EFF3F9;
    color: #36506D;
    font-weight: 600;
    border: none;
    border-right: 1px solid #E0E7F0;
    border-bottom: 1px solid #DCE4EF;
    padding: 7px 8px;
}
QHeaderView::section:hover {
    background-color: #E7F0FC;
}
QTableView::item, QTableWidget::item {
    padding: 4px 7px;
}
QTableView::item:hover, QTableWidget::item:hover {
    background-color: #EDF4FD;
}
QTableView::item:selected, QTableWidget::item:selected,
QTableView::item:selected:hover, QTableWidget::item:selected:hover {
    background-color: #DCEBFD;
    color: #17355B;
}

QSplitter::handle {
    background-color: #E8EDF5;
}
QSplitter::handle:hover {
    background-color: #B7CEE9;
}
QSplitter::handle:horizontal {
    width: 6px;
}
QSplitter::handle:vertical {
    height: 6px;
}
QGroupBox {
    color: #36506D;
    font-weight: 600;
    border: 1px solid #DCE4EF;
    border-radius: 7px;
    margin-top: 11px;
    padding: 9px 7px 6px;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 5px;
}
QProgressBar {
    background-color: #E8EEF6;
    color: #1A2B46;
    border: 1px solid #D7E2F0;
    border-radius: 7px;
    text-align: center;
    min-height: 16px;
}
QProgressBar::chunk {
    background-color: #3478D4;
    border-radius: 6px;
}
QStatusBar {
    background-color: #F5F7FB;
    color: #607188;
    border-top: 1px solid #E0E7F0;
}
QStatusBar::item {
    border: none;
}
QToolTip {
    background-color: #243651;
    color: #FFFFFF;
    border: 1px solid #243651;
    padding: 5px 7px;
}
"""


def apply_theme(app: QApplication) -> None:
    """Apply a readable, DPI-independent palette and widget states."""
    app.setStyle("Fusion")

    font = app.font()
    if 0 < font.pointSize() < 10:
        font.setPointSize(10)
        app.setFont(font)

    palette = QPalette()
    colors = {
        QPalette.Window: "#F5F7FB",
        QPalette.WindowText: "#1A2B46",
        QPalette.Base: "#FFFFFF",
        QPalette.AlternateBase: "#F8FAFD",
        QPalette.Text: "#1A2B46",
        QPalette.Button: "#FFFFFF",
        QPalette.ButtonText: "#243651",
        QPalette.Highlight: "#245FB8",
        QPalette.HighlightedText: "#FFFFFF",
        QPalette.Link: "#245FB8",
        QPalette.PlaceholderText: "#71839B",
        QPalette.Light: "#FFFFFF",
        QPalette.Midlight: "#E8EDF5",
        QPalette.Mid: "#CBD6E5",
        QPalette.Dark: "#607188",
    }
    for role, hex_color in colors.items():
        palette.setColor(role, QColor(hex_color))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        palette.setColor(QPalette.Disabled, role, QColor("#8C9AB0"))
    app.setPalette(palette)
    app.setStyleSheet(STYLE_SHEET)
