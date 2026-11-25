"""Events: the node's log, newest last, filtered by level."""

from __future__ import annotations

from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QPushButton

from transferd_gui.model.eventlog import LogEntry
from transferd_gui.model.fmt import clock
from transferd_gui.model.snapshot import NodeSnapshot
from transferd_gui.ui.pages.base import Page
from transferd_gui.ui.widgets import Card, TableWithEmpty

HEADERS = ("Time", "Level", "Source", "Message")
FILTERS = (("All", None), ("Warnings and errors", "warning"), ("Errors only", "error"))


class EventsPage(Page):
    key = "events"
    title = "Events"
    explainer = (
        "A record of what the node did and what went wrong, newest last. The buffer is bounded, so the oldest entries "
        "are dropped first. Its size is set in Settings."
    )

    def build(self) -> None:
        bar = QHBoxLayout()
        bar.addWidget(QLabel("Show"))
        self.filter = QComboBox()
        for label, _level in FILTERS:
            self.filter.addItem(label)
        self.filter.currentIndexChanged.connect(lambda _i: self._refresh())
        self.clear_button = QPushButton("Clear log")
        self.clear_button.clicked.connect(self._clear)
        bar.addWidget(self.filter)
        bar.addStretch(1)
        bar.addWidget(self.clear_button)
        self.content.addLayout(bar)
        card = Card("Log")
        self.table = TableWithEmpty(HEADERS, "No events yet. Actions and session changes are recorded here.")
        card.body.addWidget(self.table)
        self.content.addWidget(card)

    def _level(self) -> str | None:
        return FILTERS[self.filter.currentIndex()][1]

    def _refresh(self) -> None:
        entries = self.service.log_entries(self._level())
        self.table.set_rows(
            (clock(e.when), e.level, e.source, e.message) for e in reversed(entries)
        )

    def apply(self, snapshot: NodeSnapshot) -> None:
        self._refresh()

    def on_log(self, entry: LogEntry) -> None:
        if self._level() is None or entry.level == self._level() or (
            self._level() == "warning" and entry.level == "error"
        ):
            self._refresh()

    def _clear(self) -> None:
        self.service.clear_log()
        self._refresh()
