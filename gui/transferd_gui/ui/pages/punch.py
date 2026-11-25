"""Locate and hole punch: where a peer is seen from outside. Starting a hole punch is not available in this build."""

from __future__ import annotations

from PySide6.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QPushButton

from transferd_gui.backend import capabilities
from transferd_gui.model.fmt import NO_DATA, human_age
from transferd_gui.model.snapshot import NodeSnapshot
from transferd_gui.ui.pages.base import Page
from transferd_gui.ui.widgets import ActionRow, Banner, Card, Notice


class PunchPage(Page):
    key = "punch"
    title = "Locate and hole punch"
    explainer = (
        "Locate asks the relay where a peer is seen from outside and which addresses a hole punch would try. A hole punch "
        "then opens a direct UDP path through a NAT. Locating works here; starting a punch does not, in this build."
    )

    def build(self) -> None:
        locate = Card("Locate a peer")
        row = QHBoxLayout()
        self.peer_id = QLineEdit()
        self.peer_id.setPlaceholderText("peer node id (64 hex characters)")
        self.locate_button = QPushButton("Locate")
        self.locate_button.clicked.connect(self._locate)
        row.addWidget(self.peer_id, 1)
        row.addWidget(self.locate_button)
        locate.body.addLayout(row)
        self.result = QLabel(f"Result: {NO_DATA}")
        self.result.setWordWrap(True)
        self.candidates = QLabel()
        self.candidates.setWordWrap(True)
        self.notice = Notice()
        locate.body.addWidget(self.result)
        locate.body.addWidget(self.candidates)
        locate.body.addWidget(self.notice)
        self.content.addWidget(locate)

        punch = Card("Hole punch")
        punch.body.addWidget(Banner(capabilities.reason(capabilities.HOLE_PUNCH_START)))
        self.start_button = QPushButton("Start hole punch")
        self.start_button.setEnabled(False)
        self.start_button.setToolTip(capabilities.reason(capabilities.HOLE_PUNCH_START))
        punch.body.addWidget(ActionRow(self.start_button))
        self.punched = QLabel()
        punch.body.addWidget(self.punched)
        self.content.addWidget(punch)

    def apply(self, snapshot: NodeSnapshot) -> None:
        self.locate_button.setEnabled(snapshot.relay.state == "registered")
        punched = sum(1 for route in snapshot.all_routes if route.kind == "holepunch")
        self.punched.setText(f"Punched routes in use: {punched if punched else 'none'}")
        result = snapshot.locate
        if result is None:
            self.result.setText(f"Result: {NO_DATA}")
            self.candidates.setText("")
            return
        peer = "not registered at the relay" if result.peer_addr is None else f"seen at {result.peer_addr}"
        self.result.setText(
            f"Peer {result.peer_id[:12]} is {peer}. This node is seen at {result.self_addr} "
            f"(located {human_age(snapshot.taken_at - result.when)} ago)."
        )
        self.candidates.setText("Addresses a punch would try: " + (", ".join(result.punch_candidates) or NO_DATA))

    def _locate(self) -> None:
        peer = self.peer_id.text().strip().lower()
        if len(peer) != 64 or any(c not in "0123456789abcdef" for c in peer):
            self.notice.set_text("A node id is 64 lowercase hex characters.", "error")
            return
        self.notice.set_text("Locating...", "muted")
        self.run(self.service.node.locate(peer), "locate a peer", self._located)

    def _located(self, ok: bool, value) -> None:
        self.notice.set_text("Located." if ok else f"Could not locate: {value}", "ok" if ok else "error")
