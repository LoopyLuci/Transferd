"""The main window: a sidebar of sections, a stack of screens, and a status bar that says what is connected.

The window is the only place that hears every snapshot. It records the telemetry, hands the snapshot to each screen, and
keeps the status bar and the theme in step with the settings.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Type

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from transferd_gui.backend.service import TransferdService
from transferd_gui.model.eventlog import LogEntry
from transferd_gui.model.settings import Settings
from transferd_gui.model.snapshot import NodeSnapshot
from transferd_gui.model.telemetry import TelemetryStore, record_routes
from transferd_gui.ui import theme
from transferd_gui.ui.bridge import Bridge
from transferd_gui.ui.pages.base import Context, Page
from transferd_gui.ui.pages.dashboard import DashboardPage
from transferd_gui.ui.pages.dns import DnsPage
from transferd_gui.ui.pages.events import EventsPage
from transferd_gui.ui.pages.ghost import GhostPage
from transferd_gui.ui.pages.identity import IdentityPage
from transferd_gui.ui.pages.invites import InvitesPage
from transferd_gui.ui.pages.peers import PeersPage
from transferd_gui.ui.pages.punch import PunchPage
from transferd_gui.ui.pages.relay import RelayPage
from transferd_gui.ui.pages.settings import SettingsPage
from transferd_gui.ui.pages.transfer import TransferPage
from transferd_gui.ui.status import relay_status
from transferd_gui.ui.widgets import Notice, Pill

#: The sidebar: each section and the screens in it, in order.
NAVIGATION: Sequence[tuple] = (
    ("Overview", (DashboardPage,)),
    ("Network", (IdentityPage, RelayPage, PeersPage, InvitesPage)),
    ("Transfer", (TransferPage, PunchPage)),
    ("Tools", (DnsPage, GhostPage)),
    ("System", (EventsPage, SettingsPage)),
)
#: Ghost key files are read about every this many snapshots (about every five seconds).
GHOST_EVERY = 5
_KEY_ROLE = Qt.ItemDataRole.UserRole


class MainWindow(QMainWindow):
    """The window. Owns the screens and routes snapshots and log entries to them."""

    def __init__(self, service: TransferdService, bridge: Bridge, telemetry: TelemetryStore) -> None:
        super().__init__()
        self.setWindowTitle("Transferd")
        self.resize(1200, 780)
        self.service = service
        self.context = Context(service=service, bridge=bridge, telemetry=telemetry, settings_saved=self._settings_saved)
        self._pages: List[Page] = []
        self._by_key: Dict[str, Page] = {}
        self._snapshots_seen = 0

        self.nav = QListWidget()
        self.nav.setObjectName("nav")
        self.nav.setFixedWidth(210)
        self.stack = QStackedWidget()
        self._build_navigation()
        self.nav.currentItemChanged.connect(self._on_nav)

        self.problem = Notice()
        self.problem.setVisible(False)
        right = QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.addWidget(self.problem)
        right.addWidget(self.stack, 1)
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(self.nav)
        right_box = QWidget()
        right_box.setLayout(right)
        body.addWidget(right_box, 1)
        root = QWidget()
        root.setLayout(body)
        self.setCentralWidget(root)

        self._build_status_bar()
        if service.settings_problem:
            self.problem.set_text(service.settings_problem + " The file is left as it is on disk.", "error")
            self.problem.setVisible(True)

        bridge.snapshot.connect(self._on_snapshot)
        bridge.logged.connect(self._on_log)
        self._apply_theme()
        self._watch_system_theme()
        self._select("dashboard")

    # ----- construction -------------------------------------------------------------------------

    def _build_navigation(self) -> None:
        for section, page_classes in NAVIGATION:
            header = QListWidgetItem(section.upper())
            header.setFlags(Qt.ItemFlag.NoItemFlags)
            self.nav.addItem(header)
            for page_class in page_classes:
                page = self._make_page(page_class)
                item = QListWidgetItem(page.title)
                item.setData(_KEY_ROLE, page.key)
                self.nav.addItem(item)
                self.stack.addWidget(page)

    def _make_page(self, page_class: Type[Page]) -> Page:
        page = page_class(self.context)
        self._pages.append(page)
        self._by_key[page.key] = page
        return page

    def _build_status_bar(self) -> None:
        status = self.statusBar()
        self.node_label = QLabel("Starting...")
        self.relay_pill = Pill("not connected")
        self.peers_label = QLabel("0 peers")
        status.addPermanentWidget(QLabel("Node:"))
        status.addPermanentWidget(self.node_label)
        status.addPermanentWidget(QLabel("   Relay:"))
        status.addPermanentWidget(self.relay_pill)
        status.addPermanentWidget(QLabel("   "))
        status.addPermanentWidget(self.peers_label)

    def _watch_system_theme(self) -> None:
        """Follow the operating system's light or dark setting when the app's theme is `system`."""
        hints = QGuiApplication.styleHints()
        signal = getattr(hints, "colorSchemeChanged", None)
        if signal is not None:
            signal.connect(lambda _scheme: self._apply_theme())

    # ----- navigation ---------------------------------------------------------------------------

    def _select(self, key: str) -> None:
        for row in range(self.nav.count()):
            item = self.nav.item(row)
            if item.data(_KEY_ROLE) == key:
                self.nav.setCurrentRow(row)
                return

    def _on_nav(self, item: Optional[QListWidgetItem], _previous) -> None:
        if item is None or item.data(_KEY_ROLE) is None:
            return
        page = self._by_key[item.data(_KEY_ROLE)]
        self.stack.setCurrentWidget(page)
        page.on_show()

    def page(self, key: str) -> Page:
        """The screen with this key. Used by tests and by the window itself."""
        return self._by_key[key]

    def page_keys(self) -> List[str]:
        return list(self._by_key)

    # ----- routing ------------------------------------------------------------------------------

    def _on_snapshot(self, snapshot: NodeSnapshot) -> None:
        record_routes(self.context.telemetry, snapshot.taken_at, snapshot.all_routes)
        for page in self._pages:
            page.apply(snapshot)
        self._update_status(snapshot)
        self._snapshots_seen += 1
        if self._snapshots_seen % GHOST_EVERY == 1:
            self._update_ghost_summary()

    def _update_status(self, snapshot: NodeSnapshot) -> None:
        self.node_label.setText(snapshot.identity.name if snapshot.identity.ready else "starting")
        text, tone = relay_status(snapshot.relay)
        self.relay_pill.set_status(f"relay {text}", tone)
        ready = len(snapshot.ready_peers())
        self.peers_label.setText(f"{ready} peer{'s' if ready != 1 else ''} ready")

    def _update_ghost_summary(self) -> None:
        status = self.service.ghost().status()
        if not status.available:
            text = "Not available in this build: " + status.reason
        elif not status.has_key:
            text = "No ghost key yet. Create one on the Ghost keys screen."
        elif status.certificate is None:
            text = "Key exists; no certificate yet."
        else:
            text = "Certificate " + ("valid." if status.certificate.valid else "not valid: " + status.certificate.detail)
        dashboard = self._by_key["dashboard"]
        assert isinstance(dashboard, DashboardPage)
        dashboard.ghost_summary(text)

    def _on_log(self, entry: LogEntry) -> None:
        for page in self._pages:
            page.on_log(entry)

    def _settings_saved(self, settings: Settings) -> None:
        self.context.telemetry.resize(settings.telemetry_window_s)
        self.problem.setVisible(False)
        self._apply_theme()
        for page in self._pages:
            page.on_settings(settings)

    def _apply_theme(self) -> None:
        app = QApplication.instance()
        if app is not None:
            theme.apply(app, self.service.settings.theme)
        self.update()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt's name
        self.service.stop()
        super().closeEvent(event)

