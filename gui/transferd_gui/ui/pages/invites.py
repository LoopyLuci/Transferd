"""Invites: make a signed invite for someone else to accept, or accept one to connect to its maker."""

from __future__ import annotations

import time

from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QSpinBox

from transferd.errors import InviteError
from transferd.invite import accept_invite

from transferd_gui.model.fmt import NO_DATA
from transferd_gui.model.snapshot import NodeSnapshot
from transferd_gui.ui.pages.base import Page
from transferd_gui.ui.widgets import ActionRow, Card, CopyField, Notice

MIN_TTL = 30
MAX_TTL = 86_400
DEFAULT_TTL = 900


class InvitesPage(Page):
    key = "invites"
    title = "Invites"
    explainer = (
        "An invite is a signed text that tells someone how to reach this node. Make one and send it to a peer, or paste one "
        "you were sent to connect to its maker. Invites expire, and are checked before anything is dialled."
    )

    def build(self) -> None:
        make = Card("Make an invite")
        row = QHBoxLayout()
        row.addWidget(QLabel("Valid for (seconds)"))
        self.ttl = QSpinBox()
        self.ttl.setRange(MIN_TTL, MAX_TTL)
        self.ttl.setValue(DEFAULT_TTL)
        row.addWidget(self.ttl)
        row.addStretch(1)
        make.body.addLayout(row)
        self.make_button = QPushButton("Make invite")
        self.make_button.setObjectName("primary")
        self.make_button.clicked.connect(self._make)
        self.copy_button = QPushButton("Copy invite")
        self.copy_button.setEnabled(False)
        self.copy_button.clicked.connect(self._copy)
        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setPlaceholderText("The invite appears here. Copy it and send it to the peer.")
        self.output.setMaximumHeight(90)
        make.body.addWidget(ActionRow(self.make_button, self.copy_button))
        make.body.addWidget(self.output)
        self.make_notice = Notice()
        make.body.addWidget(self.make_notice)
        self.content.addWidget(make)

        accept = Card("Accept an invite")
        self.paste = QPlainTextEdit()
        self.paste.setPlaceholderText("Paste an invite that starts with tdov2:")
        self.paste.setMaximumHeight(90)
        self.verify_button = QPushButton("Check invite")
        self.verify_button.clicked.connect(self._verify)
        self.connect_button = QPushButton("Connect to inviter")
        self.connect_button.setObjectName("primary")
        self.connect_button.setEnabled(False)
        self.connect_button.clicked.connect(self._connect)
        accept.body.addWidget(self.paste)
        accept.body.addWidget(ActionRow(self.verify_button, self.connect_button))
        self.inviter_name = QLabel(f"Inviter: {NO_DATA}")
        self.inviter_id = CopyField("Node id", head=14, tail=10)
        self.inviter_key = CopyField("Key fingerprint", head=14, tail=10)
        self.inviter_addresses = QLabel(f"Addresses: {NO_DATA}")
        self.inviter_addresses.setWordWrap(True)
        for widget in (self.inviter_name, self.inviter_id, self.inviter_key, self.inviter_addresses):
            accept.body.addWidget(widget)
        self.accept_notice = Notice()
        accept.body.addWidget(self.accept_notice)
        self.content.addWidget(accept)
        self._verified = ""

    def apply(self, snapshot: NodeSnapshot) -> None:
        self.make_button.setEnabled(self.service.running and snapshot.identity.ready)

    def _make(self) -> None:
        self.make_notice.set_text("Making the invite...", "muted")
        self.run(self.service.node.make_invite(self.ttl.value()), "make an invite", self._made)

    def _made(self, ok: bool, value) -> None:
        if not ok:
            self.make_notice.set_text(f"Could not make an invite: {value}", "error")
            return
        self.output.setPlainText(value.text)
        self.copy_button.setEnabled(True)
        expires = time.strftime("%H:%M:%S", time.localtime(value.expires_at))
        self.make_notice.set_text(f"Valid until {expires}. The peer must be able to reach this node.", "ok")

    def _copy(self) -> None:
        QGuiApplication.clipboard().setText(self.output.toPlainText())
        self.make_notice.set_text("Invite copied to the clipboard.", "ok")

    def _verify(self) -> None:
        text = self.paste.toPlainText().strip()
        try:
            invite = accept_invite(text)
        except InviteError as exc:
            self._verified = ""
            self.connect_button.setEnabled(False)
            self.accept_notice.set_text(f"Not valid: {exc}", "error")
            return
        self._verified = text
        self.inviter_name.setText(f"Inviter: {invite.name}")
        self.inviter_id.set_value(invite.node_id)
        self.inviter_key.set_value(invite.key_fingerprint.hex())
        addresses = ", ".join(invite.direct + invite.relays) or "none listed"
        self.inviter_addresses.setText(f"Addresses: {addresses}")
        self.connect_button.setEnabled(True)
        self.accept_notice.set_text("The signature checks out. Connecting also checks the inviter's key.", "ok")

    def _connect(self) -> None:
        if not self._verified:
            self.accept_notice.set_text("Check the invite first.", "error")
            return
        self.accept_notice.set_text("Connecting...", "muted")
        self.run(self.service.node.dial_invite(self._verified), "accept an invite", self._connected)

    def _connected(self, ok: bool, value) -> None:
        if ok:
            self.accept_notice.set_text(f"Connected to {str(value)[:12]}.", "ok")
        else:
            self.accept_notice.set_text(f"Could not connect: {value}", "error")
