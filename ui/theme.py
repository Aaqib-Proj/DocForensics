"""Application theme (ui/theme.py).

One token set per mode -- neutral surfaces, 1px borders, a single blue
accent -- applied as a Fusion palette plus one stylesheet, so every
widget draws from the same few colors. "system" follows the Windows
light/dark setting and updates live when it changes.

Widgets that paint colors themselves (the deviation list's badges, the
document canvas) read `tokens()` and listen to `manager().changed`.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontDatabase, QGuiApplication, QPalette
from PySide6.QtWidgets import QApplication, QStyleFactory

SYSTEM, LIGHT, DARK = "system", "light", "dark"


@dataclass(frozen=True)
class Tokens:
    dark: bool
    window: str  # app background
    surface: str  # panels, inputs, table
    surface_alt: str  # table alternate rows, headers
    hover: str
    border: str
    border_strong: str
    text: str
    muted: str
    accent: str
    accent_hover: str
    accent_text: str
    accent_subtle: str  # selection background
    canvas: str  # behind document pages
    page_border: str
    # severity: (text color, tinted background)
    critical: tuple[str, str]
    major: tuple[str, str]
    minor: tuple[str, str]
    typo: tuple[str, str]
    reorder: tuple[str, str]
    success: str
    danger: str
    # The two documents' role colors (text color, tinted background), so
    # "Original" and "Suspect" are told apart at a glance everywhere.
    original: tuple[str, str] = ("#1d4ed8", "#e6efff")
    suspect: tuple[str, str] = ("#0f766e", "#dcf3ef")

    def severity(self, name: str) -> tuple[str, str]:
        return getattr(self, name, self.typo)


LIGHT_TOKENS = Tokens(
    dark=False,
    window="#f3f5f8",
    surface="#ffffff",
    surface_alt="#f8fafc",
    hover="#eef2f7",
    border="#e2e7ee",
    border_strong="#c8d0db",
    text="#111827",
    muted="#64748b",
    accent="#2563eb",
    accent_hover="#1d4ed8",
    accent_text="#ffffff",
    accent_subtle="#e6efff",
    canvas="#e6eaf0",
    page_border="#cfd6e0",
    critical=("#c81e1e", "#fde8e8"),
    major=("#b54708", "#fef0e1"),
    minor=("#8a6100", "#fcf3d2"),
    typo=("#475569", "#eef1f5"),
    reorder=("#6d28d9", "#f1e9ff"),
    success="#15803d",
    danger="#c81e1e",
    original=("#1d4ed8", "#e6efff"),
    suspect=("#0f766e", "#dcf3ef"),
)

DARK_TOKENS = Tokens(
    dark=True,
    window="#0e131a",
    surface="#151b24",
    surface_alt="#19202a",
    hover="#212a36",
    border="#2a313c",
    border_strong="#3d4450",
    text="#e6edf3",
    muted="#8b949e",
    accent="#4493f8",
    accent_hover="#5ba1f9",
    accent_text="#0d1117",
    accent_subtle="#1c3553",
    canvas="#05080c",
    page_border="#2a313c",
    critical=("#ff7b72", "#3d1c1f"),
    major=("#f0883e", "#3b2414"),
    minor=("#d29922", "#352a10"),
    typo=("#9da7b3", "#262c35"),
    reorder=("#bc8cff", "#2b2143"),
    success="#3fb950",
    danger="#ff7b72",
    original=("#79a8ff", "#1b2c4d"),
    suspect=("#4fd1c5", "#13332f"),
)


class _ThemeManager(QObject):
    changed = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.mode = SYSTEM
        self.tokens = LIGHT_TOKENS
        self._watching = False

    def apply(self, mode: str) -> None:
        app = QApplication.instance()
        self.mode = mode if mode in (SYSTEM, LIGHT, DARK) else SYSTEM
        hints = QGuiApplication.styleHints()
        if not self._watching:
            hints.colorSchemeChanged.connect(self._on_system_scheme_changed)
            self._watching = True
        if self.mode == SYSTEM:
            dark = hints.colorScheme() == Qt.ColorScheme.Dark
        else:
            dark = self.mode == DARK
        self.tokens = DARK_TOKENS if dark else LIGHT_TOKENS
        app.setStyle(QStyleFactory.create("Fusion"))
        families = QFontDatabase.families()
        for family in ("Segoe UI Variable Text", "Segoe UI"):
            if family in families:
                app.setFont(QFont(family, 9))
                break
        app.setPalette(_palette(self.tokens))
        app.setStyleSheet(_stylesheet(self.tokens, _check_mark_path(self.tokens)))
        self.changed.emit()

    def _on_system_scheme_changed(self, _scheme) -> None:
        if self.mode == SYSTEM:
            self.apply(SYSTEM)


_manager: _ThemeManager | None = None


def manager() -> _ThemeManager:
    global _manager
    if _manager is None:
        _manager = _ThemeManager()
    return _manager


def tokens() -> Tokens:
    return manager().tokens


def apply(mode: str = SYSTEM) -> None:
    manager().apply(mode)


def _palette(t: Tokens) -> QPalette:
    p = QPalette()
    role = QPalette.ColorRole
    for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive):
        p.setColor(group, role.Window, QColor(t.window))
        p.setColor(group, role.WindowText, QColor(t.text))
        p.setColor(group, role.Base, QColor(t.surface))
        p.setColor(group, role.AlternateBase, QColor(t.surface_alt))
        p.setColor(group, role.Text, QColor(t.text))
        p.setColor(group, role.PlaceholderText, QColor(t.muted))
        p.setColor(group, role.Button, QColor(t.surface))
        p.setColor(group, role.ButtonText, QColor(t.text))
        p.setColor(group, role.Highlight, QColor(t.accent_subtle))
        p.setColor(group, role.HighlightedText, QColor(t.text))
        p.setColor(group, role.ToolTipBase, QColor(t.surface))
        p.setColor(group, role.ToolTipText, QColor(t.text))
        p.setColor(group, role.Link, QColor(t.accent))
        p.setColor(group, role.Mid, QColor(t.border))
        p.setColor(group, role.Midlight, QColor(t.border))
        p.setColor(group, role.Dark, QColor(t.border_strong))
    disabled = QPalette.ColorGroup.Disabled
    for r in (role.WindowText, role.Text, role.ButtonText):
        p.setColor(disabled, r, QColor(t.muted))
    p.setColor(disabled, role.Button, QColor(t.window))
    p.setColor(disabled, role.Base, QColor(t.window))
    return p


def _check_mark_path(t: Tokens) -> str:
    """A check-mark SVG in this theme's accent-text color (Qt stylesheets
    need a file for indicator images)."""
    folder = os.path.join(tempfile.gettempdir(), "docforensics-theme")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"check-{t.accent_text.lstrip('#')}.svg")
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(
                '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">'
                f'<path d="M3.5 8.5l3 3 6-7" fill="none" stroke="{t.accent_text}" '
                'stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>'
            )
    return path.replace("\\", "/")


def _stylesheet(t: Tokens, check_mark: str) -> str:
    return f"""
    QMainWindow, QDialog {{ background: {t.window}; }}
    QWidget {{ font-size: 9pt; }}
    QToolTip {{ background: {t.surface}; color: {t.text}; border: 1px solid {t.border}; padding: 4px 6px; }}

    QMenuBar {{ background: {t.window}; border-bottom: 1px solid {t.border}; padding: 2px 4px; }}
    QMenuBar::item {{ padding: 4px 10px; border-radius: 4px; background: transparent; }}
    QMenuBar::item:selected {{ background: {t.hover}; }}
    QMenu {{ background: {t.surface}; border: 1px solid {t.border}; padding: 4px; }}
    QMenu::item {{ padding: 5px 24px 5px 22px; border-radius: 4px; }}
    QMenu::item:selected {{ background: {t.hover}; color: {t.text}; }}
    QMenu::item:disabled {{ color: {t.muted}; }}
    QMenu::separator {{ height: 1px; background: {t.border}; margin: 4px 6px; }}

    QToolBar {{ background: {t.surface}; border: none; border-bottom: 1px solid {t.border};
               padding: 6px 10px; spacing: 2px; }}
    QToolBar::separator {{ width: 1px; background: {t.border}; margin: 6px 8px; }}
    QToolBar QToolButton {{ background: transparent; border: 1px solid transparent; border-radius: 6px;
        padding: 5px 9px; color: {t.text}; }}
    QToolBar QToolButton:hover {{ background: {t.hover}; border-color: transparent; }}
    QToolBar QToolButton:pressed {{ background: {t.border}; }}
    QToolBar QToolButton:checked {{ background: {t.accent_subtle}; color: {t.accent}; border-color: transparent; }}
    QToolBar QToolButton:disabled {{ background: transparent; color: {t.border_strong}; }}
    QLabel#brandName {{ font-weight: 700; font-size: 10.5pt; color: {t.text}; }}
    QLabel#brandSub {{ color: {t.muted}; font-size: 8pt; }}

    QPushButton, QToolButton {{
        background: {t.surface}; color: {t.text}; border: 1px solid {t.border_strong};
        border-radius: 6px; padding: 4px 12px; min-height: 18px;
    }}
    QPushButton:hover, QToolButton:hover {{ background: {t.hover}; border-color: {t.border_strong}; }}
    QPushButton:pressed, QToolButton:pressed {{ background: {t.border}; }}
    QPushButton:disabled, QToolButton:disabled {{ color: {t.muted}; background: {t.window}; border-color: {t.border}; }}
    QToolButton:checked {{ background: {t.accent_subtle}; border-color: {t.accent}; color: {t.text}; }}
    QToolButton[flat="true"] {{ border-color: transparent; background: transparent; padding: 3px 7px; }}
    QToolButton[flat="true"]:hover {{ background: {t.hover}; }}
    QToolButton[flat="true"]:checked {{ background: {t.accent_subtle}; }}
    QToolButton::menu-indicator {{ image: none; width: 0; }}

    QPushButton#primary, QToolButton#primary {{
        background: {t.accent}; color: {t.accent_text}; border-color: {t.accent}; font-weight: 600;
        padding: 5px 16px;
    }}
    QPushButton#accept {{ color: {t.success}; }}
    QPushButton#accept:hover {{ border-color: {t.success}; }}
    QPushButton#reject {{ color: {t.danger}; }}
    QPushButton#reject:hover {{ border-color: {t.danger}; }}
    QPushButton#accept:disabled, QPushButton#reject:disabled {{ color: {t.muted}; }}
    QLineEdit QToolButton {{ background: transparent; border: none; padding: 0; margin: 0; min-height: 0; }}
    QPushButton#primary:hover, QToolButton#primary:hover {{ background: {t.accent_hover}; border-color: {t.accent_hover}; }}
    QPushButton#primary:disabled, QToolButton#primary:disabled {{
        background: {t.window}; color: {t.muted}; border-color: {t.border};
    }}

    QToolButton[chip="true"] {{
        border-radius: 12px; padding: 3px 11px; background: {t.surface}; color: {t.muted};
        border: 1px solid {t.border};
    }}
    QToolButton[chip="true"]:hover {{ border-color: {t.border_strong}; color: {t.text}; }}
    QToolButton[chip="true"]:checked {{ background: {t.accent_subtle}; border-color: {t.accent_subtle};
        color: {t.accent}; font-weight: 600; }}
    QToolButton[chip="true"]:checked:hover {{ border-color: {t.accent}; }}
    QToolButton[chip="true"]:disabled {{ background: transparent; border-color: {t.border}; color: {t.border_strong};
        font-weight: 400; }}
    QToolButton[tab="true"] {{
        background: transparent; border: none; border-bottom: 2px solid transparent; border-radius: 0;
        color: {t.muted}; padding: 3px 10px 4px; font-weight: 600;
    }}
    QToolButton[tab="true"]:hover {{ color: {t.text}; background: transparent; }}
    QToolButton[tab="true"]:checked {{ color: {t.text}; border-bottom-color: {t.accent}; background: transparent; }}
    QToolButton[tab="true"]:disabled {{ color: {t.border_strong}; }}
    QToolButton[link="true"] {{ background: transparent; border: none; color: {t.accent}; padding: 3px 6px; }}
    QToolButton[link="true"]:hover {{ text-decoration: underline; background: transparent; }}
    QToolButton[nav="true"] {{ font-size: 13pt; padding: 0 8px; }}

    QLineEdit, QSpinBox, QComboBox {{
        background: {t.surface}; color: {t.text}; border: 1px solid {t.border};
        border-radius: 6px; padding: 3px 7px; min-height: 18px;
        selection-background-color: {t.accent_subtle}; selection-color: {t.text};
    }}
    QLineEdit:focus, QSpinBox:focus, QComboBox:focus {{ border-color: {t.accent}; }}
    QSpinBox::up-button, QSpinBox::down-button {{ width: 0; border: none; }}
    QComboBox::drop-down {{ border: none; width: 18px; }}
    QComboBox QAbstractItemView {{ background: {t.surface}; border: 1px solid {t.border};
        selection-background-color: {t.hover}; selection-color: {t.text}; }}

    QCheckBox {{ spacing: 7px; }}
    QCheckBox::indicator {{ width: 14px; height: 14px; border: 1px solid {t.border_strong};
        border-radius: 4px; background: {t.surface}; }}
    QCheckBox::indicator:hover {{ border-color: {t.accent}; }}
    QCheckBox::indicator:checked {{ background: {t.accent}; border-color: {t.accent}; image: url("{check_mark}"); }}
    QCheckBox::indicator:disabled {{ background: {t.window}; border-color: {t.border}; }}

    QTableWidget, QTableView {{
        background: {t.surface}; alternate-background-color: {t.surface_alt};
        border: 1px solid {t.border}; border-radius: 8px; gridline-color: {t.border};
        selection-background-color: {t.accent_subtle}; selection-color: {t.text}; outline: 0;
    }}
    QTableWidget::item {{ padding: 5px 10px; border: none; }}
    QTableWidget::item:hover {{ background: {t.hover}; }}
    QTableWidget::item:selected {{ background: {t.accent_subtle}; color: {t.text}; }}
    QHeaderView {{ background: {t.surface}; border: none; }}
    QHeaderView::section {{
        background: {t.surface}; color: {t.muted}; font-weight: 600; font-size: 8pt;
        border: none; border-bottom: 1px solid {t.border}; padding: 7px 10px;
    }}
    QHeaderView::section:hover {{ color: {t.text}; }}

    QDockWidget {{ color: {t.text}; }}
    QDockWidget::title {{ background: {t.surface}; padding: 8px 12px; border-top: 1px solid {t.border};
        border-bottom: 1px solid {t.border}; text-align: left; }}

    QStatusBar {{ background: {t.surface}; border-top: 1px solid {t.border}; color: {t.muted}; }}
    QStatusBar QLabel {{ padding: 2px 8px; }}
    QStatusBar::item {{ border: none; }}
    QProgressBar {{ background: {t.surface_alt}; border: 1px solid {t.border}; border-radius: 3px;
        max-height: 6px; min-height: 6px; }}
    QProgressBar::chunk {{ background: {t.accent}; border-radius: 2px; }}

    QSplitter::handle {{ background: {t.border}; }}
    QSplitter::handle:horizontal {{ width: 1px; }}
    QSplitter::handle:vertical {{ height: 1px; }}

    QGraphicsView {{ border: none; }}

    QScrollBar:vertical {{ background: transparent; width: 11px; margin: 0; }}
    QScrollBar:horizontal {{ background: transparent; height: 11px; margin: 0; }}
    QScrollBar::handle {{ background: {t.border_strong}; border-radius: 4px; margin: 2px; }}
    QScrollBar::handle:vertical {{ min-height: 28px; }}
    QScrollBar::handle:horizontal {{ min-width: 28px; }}
    QScrollBar::handle:hover {{ background: {t.muted}; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
    QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

    QLabel#muted {{ color: {t.muted}; }}
    QLabel#panelTitle {{ color: {t.muted}; font-weight: 600; font-size: 8pt; letter-spacing: 1px; }}
    QLabel#panelTitle[role="original"] {{ color: {t.original[0]}; background: {t.original[1]};
        border-radius: 4px; padding: 1px 7px; }}
    QLabel#panelTitle[role="suspect"] {{ color: {t.suspect[0]}; background: {t.suspect[1]};
        border-radius: 4px; padding: 1px 7px; }}
    QLabel#fileName {{ font-weight: 600; font-size: 10pt; }}
    QLabel#note {{ color: {t.major[0]}; }}
    QLabel#sectionTitle {{ color: {t.muted}; font-weight: 600; font-size: 8pt; letter-spacing: 1px; }}
    QLabel#summaryTitle {{ font-weight: 700; font-size: 10pt; }}
    QWidget#panelHeader {{ background: {t.surface}; border-bottom: 1px solid {t.border}; }}
    QWidget#panelNav {{ background: {t.surface}; border-bottom: 1px solid {t.border}; }}
    """
