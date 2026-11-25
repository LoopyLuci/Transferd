"""Peers: the sessions this node has, how to dial a peer, and whether incoming connections are allowed."""

from __future__ import annotations

from PySide6.QtWidgets import QCheckBox, QHBoxLayout, QLabel, QLineEdit, QPushButton

from transferd_gui.model.fmt import human_age, short_hex
from transferd_gui.model.settings import Settings, SettingsError, parse_host_port
from transferd_gui.model.snapshot import NodeSnapshot
from transferd_gui.ui.pages.base import Page
from transferd_gui.ui.status import session_status
from transferd_gui.ui.widgets import ActionRow, Card, Notice, TableWithEmpty

HEADERS = ("Peer", "Name", "State", "Protocol", "Routes up", "Connected for")


class PeersPage(Page):
    key = "peers"
    title = "Peers"
    explainer = (
        "Peers are other nodes this node has a session with. Dial one directly by its address, or through the relay by "
        "its node id. Incoming connections stay off until you allow them."
    )

    def build(self) -> None:
        self.table = TableWithEmpty(HEADERS, "No peers connected. Dial a peer below, or accept an invite on the Invites screen.")
        sessions = Card("Sessions")
        sessions.body.addWidget(self.table)
        self.disconnect_button = QPushButton("Disconnect selected")
        self.disconnect_button.clicked.connect(self._disconnect)
        sessions.body.addWidget(ActionRow(self.disconnect_button))
        self.content.addWidget(sessions)

        incoming = Card("Incoming connections")
        self.allow = QCheckBox("Accept incoming connections")
        self.allow.toggled.connect(self._toggle_allow)
        self._syncing = False
        self.listener = QLabel()
        self.listener.setWordWrap(True)
        incoming.body.addWidget(self.allow)
        incoming.body.addWidget(self.listener)
        incoming.body.addWidget(QLabel(
            "While this is on, the listener accepts any peer whose handshake verifies, and the relay admits "
            "peers that dial in. Turn it off to refuse new connections."
        ))
        self.allow_notice = Notice()
        incoming.body.addWidget(self.allow_notice)
        self.content.addWidget(incoming)

        direct = Card("Dial a peer directly")
        direct.body.addWidget(QLabel("Address of the peer's listener, as host:port."))
        row = QHBoxLayout()
        self.address = QLineEdit()
        self.address.setPlaceholderText("192.0.2.10:7000")
        self.expect = QLineEdit()
        self.expect.setPlaceholderText("expected node id (optional)")
        self.dial_button = QPushButton("Connect")
        self.dial_button.clicked.connect(self._dial_direct)
        row.addWidget(self.address, 1)
        row.addWidget(self.expect, 1)
        row.addWidget(self.dial_button)
        direct.body.addLayout(row)
        self.direct_notice = Notice()
        direct.body.addWidget(self.direct_notice)
        self.content.addWidget(direct)

        relayed = Card("Dial a peer through the relay")
        relayed.body.addWidget(QLabel("Needs this node to be registered with the relay. The peer must be registered too."))
        row = QHBoxLayout()
        self.relay_peer = QLineEdit()
        self.relay_peer.setPlaceholderText("peer node id (64 hex characters)")
        self.relay_button = QPushButton("Connect through relay")
        self.relay_button.clicked.connect(self._dial_relay)
        row.addWidget(self.relay_peer, 1)
        row.addWidget(self.relay_button)
        relayed.body.addLayout(row)
        self.relay_notice = Notice()
        relayed.body.addWidget(self.relay_notice)
        self.content.addWidget(relayed)

    def apply(self, snapshot: NodeSnapshot) -> None:
        rows, ids = [], []
        for session in snapshot.sessions:
            state, _tone = session_status(session)
            rows.append(
                (
                    session.peer_name,
                    short_hex(session.peer_id, 10, 6),
                    state,
                    str(session.version) if session.version else "no data yet",
                    f"{session.routes_up} of {len(session.routes)}",
                    human_age(snapshot.taken_at - session.established_at),
                )
            )
            ids.append(session.peer_id)
        self._ids = ids
        self.table.set_rows(rows)
        self.disconnect_button.setEnabled(bool(ids))
        self._syncing = True
        self.allow.setChecked(self.service.settings.allow_incoming)
        self._syncing = False
        self.listener.setText(snapshot.listener.detail)
        self.dial_button.setEnabled(True)
        self.relay_button.setEnabled(snapshot.relay.state == "registered")

    def on_settings(self, settings: Settings) -> None:
        self._syncing = True
        self.allow.setChecked(settings.allow_incoming)
        self._syncing = False

    # ----- actions ------------------------------------------------------------------------------

    def _toggle_allow(self, checked: bool) -> None:
        if self._syncing:
            return
        try:
            self.service.change_settings({"allow_incoming": checked})
        except (SettingsError, OSError) as exc:
            self.allow_notice.set_text(f"Not changed: {exc}", "error")
            self._syncing = True
            self.allow.setChecked(not checked)
            self._syncing = False
            return
        self.allow_notice.set_text("Saved. Incoming connections are " + ("allowed." if checked else "refused."), "ok")

    def _disconnect(self) -> None:
        row = self.table.selected_row()
        if row < 0 or row >= len(self._ids):
            self.table.empty.setText("Select a peer first, then disconnect it.")
            return
        self.run(self.service.node.disconnect_peer(self._ids[row]), "disconnect a peer")

    def _dial_direct(self) -> None:
        try:
            host, port = parse_host_port(self.address.text())
        except ValueError as exc:
            self.direct_notice.set_text(str(exc), "error")
            return
        expect = self.expect.text().strip().lower() or None
        if expect is not None and (len(expect) != 64 or any(c not in "0123456789abcdef" for c in expect)):
            self.direct_notice.set_text("The expected node id is 64 lowercase hex characters, or empty.", "error")
            return
        self.direct_notice.set_text("Connecting...", "muted")
        self.run(self.service.node.dial_direct(host, port, expect_node_id=expect), "dial a peer", self._dialled("direct"))

    def _dial_relay(self) -> None:
        peer = self.relay_peer.text().strip().lower()
        if len(peer) != 64 or any(c not in "0123456789abcdef" for c in peer):
            self.relay_notice.set_text("A node id is 64 lowercase hex characters.", "error")
            return
        self.relay_notice.set_text("Connecting through the relay...", "muted")
        self.run(self.service.node.dial_relay(peer), "dial a peer through the relay", self._dialled("relay"))

    def _dialled(self, via: str):
        notice = self.direct_notice if via == "direct" else self.relay_notice

        def done(ok: bool, value) -> None:
            if ok:
                notice.set_text(f"Connected to {str(value)[:12]}.", "ok")
            else:
                notice.set_text(f"Could not connect: {value}", "error")

        return done

