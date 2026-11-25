"""Identity: the node id and the keys that prove it. Long values are shortened on screen and copy whole."""

from __future__ import annotations

from PySide6.QtWidgets import QLabel

from transferd_gui.model.snapshot import NodeSnapshot
from transferd_gui.ui.pages.base import Page
from transferd_gui.ui.widgets import Card, CopyField


class IdentityPage(Page):
    key = "identity"
    title = "Identity"
    explainer = (
        "This node's identity. The node id is what peers use to find it; the keys are what they check. "
        "The secret seed is kept in the data directory and is never shown."
    )

    def build(self) -> None:
        card = Card("Keys")
        self.name = QLabel()
        self.node_id = CopyField("Node id", head=14, tail=10)
        self.fingerprint = CopyField("Fingerprint", head=14, tail=10)
        self.ed25519 = CopyField("Ed25519 public", head=14, tail=10)
        self.mldsa = CopyField("ML-DSA-87 public", head=14, tail=10)
        self.relay_token = CopyField("Relay token", head=14, tail=10)
        for widget in (self.name, self.node_id, self.fingerprint, self.ed25519, self.mldsa, self.relay_token):
            card.body.addWidget(widget)
        self.content.addWidget(card)

        where = Card("Data directory")
        self.data_dir = QLabel()
        self.data_dir.setWordWrap(True)
        where.body.addWidget(QLabel("Identity, received files and ghost key files live here."))
        where.body.addWidget(self.data_dir)
        self.content.addWidget(where)

    def apply(self, snapshot: NodeSnapshot) -> None:
        identity = snapshot.identity
        self.name.setText(f"Name: {identity.name}" if identity.ready else "Starting the node, so no identity yet.")
        self.node_id.set_value(identity.node_id)
        self.fingerprint.set_value(identity.fingerprint)
        self.ed25519.set_value(identity.ed25519_public)
        self.mldsa.set_value(identity.mldsa_public)
        self.relay_token.set_value(identity.relay_token)
        self.data_dir.setText(self.service.settings.data_dir)
