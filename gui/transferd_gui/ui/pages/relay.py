"""Relay: registration with the relay, and locating a peer through it."""

from __future__ import annotations

from PySide6.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QPushButton

from transferd_gui.backend import capabilities
from transferd_gui.model.fmt import NO_DATA, human_age
from transferd_gui.model.snapshot import NodeSnapshot
from transferd_gui.ui.pages.base import Page
from transferd_gui.ui.status import relay_status, relay_next_text, time_text
from transferd_gui.ui.widgets import ActionRow, Banner, Card, Notice, Pill


class RelayPage(Page):
    key = "relay"
    title = "Relay"
    explainer = (
        "A relay is a public meeting point that forwards encrypted datagrams between nodes that cannot reach each other "
        "directly. Connect to register this node with the relay; the registration is renewed on a timer."
    )

    def build(self) -> None:
        status = Card("Registration")
        self.pill = Pill("not connected")
        self.address = QLabel()
        self.detail = QLabel()
        self.detail.setWordWrap(True)
        self.counts = QLabel()
        self.registered = QLabel()
        self.next_due = QLabel()
        self.connect_button = QPushButton("Connect")
        self.connect_button.setObjectName("primary")
        self.connect_button.clicked.connect(self._connect)
        self.disconnect_button = QPushButton("Disconnect")
        self.disconnect_button.clicked.connect(self._disconnect)
        self.notice = Notice()
        for widget in (self.pill, self.address, self.detail, self.registered, self.counts, self.next_due):
            status.body.addWidget(widget)
        status.body.addWidget(ActionRow(self.connect_button, self.disconnect_button))
        status.body.addWidget(self.notice)
        self.content.addWidget(status)

        locate = Card("Locate a peer through the relay")
        locate.body.addWidget(QLabel("Asks the relay where this node and the peer appear from outside, and which addresses a hole punch would try."))
        row = QHBoxLayout()
        self.peer_id = QLineEdit()
        self.peer_id.setPlaceholderText("peer node id (64 hex characters)")
        self.locate_button = QPushButton("Locate")
        self.locate_button.clicked.connect(self._locate)
        row.addWidget(self.peer_id, 1)
        row.addWidget(self.locate_button)
        locate.body.addLayout(row)
        self.located = QLabel()
        self.located.setWordWrap(True)
        locate.body.addWidget(self.located)
        self.locate_notice = Notice()
        locate.body.addWidget(self.locate_notice)
        self.content.addWidget(locate)

        options = Card("Relay token and port options")
        options.body.addWidget(Banner(capabilities.reason(capabilities.CUSTOM_RELAY_TOKEN)))
        options.body.addWidget(Banner(capabilities.reason(capabilities.RELAY_UDP_PORT)))
        self.content.addWidget(options)

    def apply(self, snapshot: NodeSnapshot) -> None:
        relay = snapshot.relay
        text, tone = relay_status(relay)
        self.pill.set_status(text, tone)
        self.address.setText(f"Relay address: {relay.address or 'not set (see Settings)'}")
        self.detail.setText(relay.detail)
        self.counts.setText(f"Registrations: {relay.registrations_ok} ok, {relay.registration_failures} failed")
        self.registered.setText(
            "Registered at: " + time_text(relay.last_registered_at)
            + (f" (first at {time_text(relay.registered_at)})" if relay.registered_at else "")
        )
        self.next_due.setText(f"Next renewal: {relay_next_text(relay, snapshot.taken_at)} (every {relay.renew_every_s:g} s)")
        connected = relay.state in ("connecting", "registered")
        self.connect_button.setEnabled(not connected and bool(relay.address or self.service.settings.relay_address))
        self.disconnect_button.setEnabled(connected or relay.state == "failed")
        self.locate_button.setEnabled(relay.state == "registered")
        self.located.setText(self._located_text(snapshot))

    def _located_text(self, snapshot: NodeSnapshot) -> str:
        result = snapshot.locate
        if result is None:
            return f"Locate result: {NO_DATA}"
        peer = (
            "not registered at the relay"
            if result.peer_addr is None
            else f"seen at {result.peer_addr}"
        )
        candidates = ", ".join(result.punch_candidates) or NO_DATA
        age = human_age(snapshot.taken_at - result.when)
        return (
            f"Peer {result.peer_id[:12]}: {peer}. This node is seen at {result.self_addr}. "
            f"Punch candidates: {candidates}. Located {age} ago."
        )

    def on_settings(self, settings) -> None:
        self.address.setText(f"Relay address: {settings.relay_address or 'not set (see Settings)'}")

    # ----- actions ------------------------------------------------------------------------------

    def _connect(self) -> None:
        self.notice.set_text("Connecting...", "muted")
        self.run(self.service.node.connect_relay(), "connect to the relay", self._connected)

    def _connected(self, ok: bool, value) -> None:
        self.notice.set_text("Registered with the relay." if ok else f"Could not connect: {value}", "ok" if ok else "error")

    def _disconnect(self) -> None:
        self.run(self.service.node.disconnect_relay(), "disconnect from the relay")
        self.notice.set_text("Disconnected.", "muted")

    def _locate(self) -> None:
        peer = self.peer_id.text().strip().lower()
        if len(peer) != 64 or any(c not in "0123456789abcdef" for c in peer):
            self.locate_notice.set_text("A node id is 64 lowercase hex characters.", "error")
            return
        self.locate_notice.set_text("Locating...", "muted")
        self.run(self.service.node.locate(peer), "locate a peer", self._located)

    def _located(self, ok: bool, value) -> None:
        self.locate_notice.set_text("Located." if ok else f"Could not locate: {value}", "ok" if ok else "error")

