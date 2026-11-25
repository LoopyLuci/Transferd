"""Small reusable widgets: cards, status pills, copy fields, tables with an empty state, banners and explainers.

Each widget shows a value it was given and nothing more. A missing value is shown as "no data yet" by the caller, using
`model.fmt.NO_DATA`, so every screen says the same thing when nothing has been measured.
"""

from __future__ import annotations

from typing import Iterable, Optional, Sequence

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from transferd_gui.model.fmt import short_hex


def restyle(widget: QWidget) -> None:
    """Make Qt re-read a dynamic property (such as a tone) after it changed."""
    widget.style().unpolish(widget)
    widget.style().polish(widget)


class Explainer(QLabel):
    """One or two plain sentences saying what a screen is for."""

    def __init__(self, text: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(text, parent)
        self.setObjectName("explainer")
        self.setWordWrap(True)


class Pill(QLabel):
    """A short status word with a tone: ok, warn, error, idle or accent. Colour is never the only signal: the word says it."""

    def __init__(self, text: str = "", tone: str = "idle", parent: Optional[QWidget] = None) -> None:
        super().__init__(text, parent)
        self.set_status(text, tone)

    def set_status(self, text: str, tone: str) -> None:
        self.setText(text)
        self.setProperty("tone", tone)
        restyle(self)


class Card(QFrame):
    """A titled panel. Put content in `body`."""

    def __init__(self, title: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("card")
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(8)
        self.title = QLabel(title)
        self.title.setObjectName("heading")
        outer.addWidget(self.title)
        self.body = QVBoxLayout()
        self.body.setSpacing(6)
        outer.addLayout(self.body)


class Banner(QFrame):
    """A notice that a feature is not available in this build, with the reason. Used for every disabled feature."""

    HEADING = "Not available in this build"

    def __init__(self, reason: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("banner")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        heading = QLabel(self.HEADING)
        heading.setObjectName("banner_text")
        heading.setStyleSheet("font-weight: 600;")
        self._reason = QLabel(reason)
        self._reason.setObjectName("banner_text")
        self._reason.setWordWrap(True)
        layout.addWidget(heading)
        layout.addWidget(self._reason)

    def set_reason(self, reason: str) -> None:
        self._reason.setText(reason)


class Notice(QLabel):
    """A line of text about a state, coloured by tone. Used for results of actions."""

    def __init__(self, text: str = "", tone: str = "muted", parent: Optional[QWidget] = None) -> None:
        super().__init__(text, parent)
        self.setWordWrap(True)
        self.set_text(text, tone)

    def set_text(self, text: str, tone: str = "muted") -> None:
        self.setText(text)
        self.setObjectName("muted" if tone == "muted" else "")
        self.setProperty("tone", "" if tone == "muted" else tone)
        restyle(self)


class CopyField(QWidget):
    """A value shown shortened, with a button that copies the whole value to the clipboard."""

    def __init__(self, label: str, parent: Optional[QWidget] = None, head: int = 12, tail: int = 8) -> None:
        super().__init__(parent)
        self._full = ""
        self._head, self._tail = head, tail
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        name = QLabel(label)
        name.setObjectName("muted")
        name.setMinimumWidth(110)
        self.value = QLineEdit()
        self.value.setReadOnly(True)
        self.value.setCursorPosition(0)
        self.copy_button = QPushButton("Copy")
        self.copy_button.clicked.connect(self.copy)
        self.copy_button.setEnabled(False)
        row.addWidget(name)
        row.addWidget(self.value, 1)
        row.addWidget(self.copy_button)

    @property
    def full_value(self) -> str:
        return self._full

    def set_value(self, full: str, placeholder: str = "no data yet") -> None:
        self._full = full
        self.copy_button.setEnabled(bool(full))
        if full:
            self.value.setText(short_hex(full, self._head, self._tail))
            self.value.setToolTip(full)
        else:
            self.value.setText(placeholder)
            self.value.setToolTip("")

    def copy(self) -> None:
        if self._full:
            QGuiApplication.clipboard().setText(self._full)


class TableWithEmpty(QWidget):
    """A table that shows its empty-state text instead of an empty grid when it has no rows."""

    def __init__(self, headers: Sequence[str], empty_text: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.empty = QLabel(empty_text)
        self.empty.setObjectName("empty")
        self.empty.setWordWrap(True)
        self.table = QTableWidget(0, len(headers))
        self.table.setHorizontalHeaderLabels(list(headers))
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(True)
        self.table.setMinimumHeight(120)
        layout.addWidget(self.empty)
        layout.addWidget(self.table)
        self._set_empty(True)

    def _set_empty(self, empty: bool) -> None:
        self.empty.setVisible(empty)
        self.table.setVisible(not empty)

    def set_rows(self, rows: Iterable[Sequence[str]], tones: Optional[Sequence[Optional[str]]] = None) -> None:
        """Replace the rows. `tones` optionally colours the first cell of each row by tone name."""
        rows = [list(r) for r in rows]
        self.table.setRowCount(0)
        self._set_empty(not rows)
        for index, cells in enumerate(rows):
            self.table.insertRow(index)
            for column, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.table.setItem(index, column, item)
        self.table.resizeRowsToContents()

    def row_count(self) -> int:
        return self.table.rowCount()

    def selected_row(self) -> int:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        return rows[0].row() if rows else -1

    def cell(self, row: int, column: int) -> str:
        item = self.table.item(row, column)
        return item.text() if item is not None else ""


class ActionRow(QWidget):
    """A horizontal row of buttons with an optional stretch at the end."""

    def __init__(self, *buttons: QPushButton, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        for button in buttons:
            row.addWidget(button)
        row.addStretch(1)

