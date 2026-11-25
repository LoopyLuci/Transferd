"""Colour tokens, and the light and dark themes built from them.

Every colour in the app is a named token. Widgets read tokens; nothing hard-codes a colour. The theme follows the system
setting unless the user chooses light or dark in Settings. `apply` sets the stylesheet and the palette together, so widgets
Qt paints itself (tables, combo boxes) match the rest.
"""

from __future__ import annotations

from typing import Dict

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

LIGHT: Dict[str, object] = {
    "window": "#f4f5f7",
    "card": "#ffffff",
    "border": "#d8dce2",
    "text": "#1d2129",
    "text_muted": "#5b6470",
    "accent": "#2563eb",
    "ok": "#15803d",
    "warn": "#b45309",
    "error": "#b91c1c",
    "idle": "#6b7280",
    "banner": "#fff6e0",
    "banner_border": "#e7b64d",
    "banner_text": "#5c4300",
    "input": "#ffffff",
    "row_alt": "#f7f8fa",
    "selection": "#dbe7ff",
    "chart_grid": "#e5e7eb",
    "chart_axis": "#9ca3af",
    "series": ["#2563eb", "#059669", "#d97706", "#7c3aed", "#db2777", "#0891b2"],
}

DARK: Dict[str, object] = {
    "window": "#16181d",
    "card": "#1f2229",
    "border": "#343945",
    "text": "#e6e8eb",
    "text_muted": "#9aa3af",
    "accent": "#60a5fa",
    "ok": "#4ade80",
    "warn": "#fbbf24",
    "error": "#f87171",
    "idle": "#9ca3af",
    "banner": "#2b2415",
    "banner_border": "#7a5a16",
    "banner_text": "#f5d48a",
    "input": "#14161b",
    "row_alt": "#1b1e24",
    "selection": "#1e3a5f",
    "chart_grid": "#2c3038",
    "chart_axis": "#6b7280",
    "series": ["#60a5fa", "#34d399", "#fbbf24", "#a78bfa", "#f472b6", "#22d3ee"],
}

_STYLE = """
QWidget {{ color: {text}; }}
QMainWindow, QWidget#page, QScrollArea, QScrollArea > QWidget > QWidget {{ background: {window}; }}
QFrame#card {{ background: {card}; border: 1px solid {border}; border-radius: 8px; }}
QFrame#banner {{ background: {banner}; border: 1px solid {banner_border}; border-radius: 8px; }}
QLabel#banner_text {{ color: {banner_text}; }}
QLabel#title {{ font-size: 20px; font-weight: 600; }}
QLabel#heading {{ font-size: 14px; font-weight: 600; }}
QLabel#muted, QLabel#explainer {{ color: {text_muted}; }}
QLabel#empty {{ color: {text_muted}; padding: 12px; }}
QLabel[tone="ok"] {{ color: {ok}; font-weight: 600; }}
QLabel[tone="warn"] {{ color: {warn}; font-weight: 600; }}
QLabel[tone="error"] {{ color: {error}; font-weight: 600; }}
QLabel[tone="idle"] {{ color: {idle}; font-weight: 600; }}
QLabel[tone="accent"] {{ color: {accent}; font-weight: 600; }}
QListWidget#nav {{ background: {card}; border: none; border-right: 1px solid {border}; padding: 6px 0; }}
QListWidget#nav::item {{ padding: 6px 14px; border-radius: 6px; margin: 1px 8px; }}
QListWidget#nav::item:selected {{ background: {selection}; color: {text}; }}
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit, QTextEdit {{
    background: {input}; border: 1px solid {border}; border-radius: 6px; padding: 4px 6px; }}
QPushButton {{ background: {card}; border: 1px solid {border}; border-radius: 6px; padding: 5px 12px; }}
QPushButton:hover {{ border-color: {accent}; }}
QPushButton:disabled {{ color: {idle}; }}
QPushButton#primary {{ background: {accent}; color: #ffffff; border: none; }}
QPushButton#primary:disabled {{ background: {border}; color: {idle}; }}
QTableWidget {{ background: {card}; alternate-background-color: {row_alt}; border: 1px solid {border};
    gridline-color: {border}; border-radius: 6px; }}
QHeaderView::section {{ background: {row_alt}; color: {text_muted}; border: none;
    border-bottom: 1px solid {border}; padding: 4px 6px; }}
QStatusBar {{ background: {card}; border-top: 1px solid {border}; }}
"""


def stylesheet(tokens: Dict[str, object]) -> str:
    return _STYLE.format(**{k: v for k, v in tokens.items() if isinstance(v, str)})


def resolve(mode: str, system_dark: bool) -> str:
    """The theme that applies: `dark` or `light`. `system` follows the operating system."""
    if mode == "dark":
        return "dark"
    if mode == "light":
        return "light"
    return "dark" if system_dark else "light"


def system_prefers_dark(app: QApplication) -> bool:
    """Whether the operating system asks for dark colours. Uses Qt's colour scheme when it has one, and otherwise the
    lightness of the window colour."""
    try:
        from PySide6.QtCore import Qt

        return app.styleHints().colorScheme() == Qt.ColorScheme.Dark
    except AttributeError:
        return app.palette().color(QPalette.ColorRole.Window).lightness() < 128


def apply(app: QApplication, mode: str) -> Dict[str, object]:
    """Make `mode` (`system`, `light` or `dark`) the look of the app. Returns the token set now in use."""
    name = resolve(mode, system_prefers_dark(app))
    tokens = DARK if name == "dark" else LIGHT
    app.setStyle("Fusion")
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(tokens["window"]))
    palette.setColor(QPalette.ColorRole.Base, QColor(tokens["input"]))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(tokens["row_alt"]))
    palette.setColor(QPalette.ColorRole.Text, QColor(tokens["text"]))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(tokens["text"]))
    palette.setColor(QPalette.ColorRole.Button, QColor(tokens["card"]))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(tokens["text"]))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(tokens["accent"]))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    app.setPalette(palette)
    app.setStyleSheet(stylesheet(tokens))
    _CURRENT["tokens"] = tokens
    return tokens


def current_tokens() -> Dict[str, object]:
    """The token set last applied. Charts read it while painting."""
    return _CURRENT["tokens"]


_CURRENT: Dict[str, Dict[str, object]] = {"tokens": LIGHT}
