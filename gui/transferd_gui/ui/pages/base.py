"""The base class of every screen: a title, one or two plain sentences, and content that scrolls.

A page reads snapshots and settings, and calls the service for actions. It never touches the backend thread directly:
action results come back on the UI thread through the bridge.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Coroutine, Optional

from PySide6.QtWidgets import QLabel, QScrollArea, QVBoxLayout, QWidget

from transferd_gui.backend.service import TransferdService
from transferd_gui.model.eventlog import LogEntry
from transferd_gui.model.settings import Settings
from transferd_gui.model.snapshot import NodeSnapshot
from transferd_gui.model.telemetry import TelemetryStore
from transferd_gui.ui.bridge import Bridge
from transferd_gui.ui.widgets import Explainer


@dataclass
class Context:
    """What every page is given: the service it acts through, the bridge to the UI thread, the telemetry, and a way to
    tell the window that settings were saved."""

    service: TransferdService
    bridge: Bridge
    telemetry: TelemetryStore
    settings_saved: Callable[[Settings], None]


class Page(QScrollArea):
    """A screen. Subclasses set `key`, `title` and `explainer`, build their widgets in `build`, and override `apply`."""

    key = ""
    title = ""
    explainer = ""

    def __init__(self, context: Context) -> None:
        super().__init__()
        self.context = context
        self.service = context.service
        self.bridge = context.bridge
        self.telemetry = context.telemetry
        inner = QWidget()
        inner.setObjectName("page")
        self.content = QVBoxLayout(inner)
        self.content.setContentsMargins(20, 16, 20, 20)
        self.content.setSpacing(12)
        heading = QLabel(self.title)
        heading.setObjectName("title")
        self.content.addWidget(heading)
        self.content.addWidget(Explainer(self.explainer))
        self.setWidget(inner)
        self.setWidgetResizable(True)
        self.setFrameShape(QScrollArea.Shape.NoFrame)
        self.build()
        self.content.addStretch(1)

    def build(self) -> None:
        """Add this page's widgets to `self.content`. Called once, at construction."""

    def apply(self, snapshot: NodeSnapshot) -> None:
        """Show a new snapshot. Called on the UI thread about once a second."""

    def on_settings(self, settings: Settings) -> None:
        """Settings were saved or changed."""

    def on_log(self, entry: LogEntry) -> None:
        """A new event log entry was written."""

    def on_show(self) -> None:
        """The page was opened in the sidebar. Pages that read files refresh here."""

    def run(self, coro: Coroutine[Any, Any, Any], what: str, on_done: Optional[Callable[[bool, Any], None]] = None) -> None:
        """Run a node action. The result reaches `on_done` on the UI thread, with `ok` and the value or the message."""
        callback = self.bridge.on_ui(on_done) if on_done is not None else None
        self.service.run(coro, what, callback)
