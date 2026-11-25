"""DNS lookup: names a paired node's resolver answers over the overlay. Not available in this build."""

from __future__ import annotations

from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QLineEdit, QPushButton

from transferd_gui.backend import capabilities
from transferd_gui.ui.pages.base import Page
from transferd_gui.ui.widgets import Banner, Card


class DnsPage(Page):
    key = "dns"
    title = "DNS lookup"
    explainer = (
        "Look up a name through a paired node's resolver, carried over the overlay. The controls are shown so the feature "
        "is visible; they are disabled because this build cannot run it."
    )

    def build(self) -> None:
        lookup = Card("Lookup")
        lookup.body.addWidget(Banner(capabilities.reason(capabilities.MESH_DNS)))
        row = QHBoxLayout()
        self.name = QLineEdit()
        self.name.setPlaceholderText("name, for example printer.home")
        self.name.setEnabled(False)
        self.kind = QComboBox()
        self.kind.addItems(["A", "AAAA", "TXT"])
        self.kind.setEnabled(False)
        self.lookup_button = QPushButton("Look up")
        self.lookup_button.setEnabled(False)
        row.addWidget(self.name, 1)
        row.addWidget(self.kind)
        row.addWidget(self.lookup_button)
        lookup.body.addLayout(row)
        lookup.body.addWidget(QLabel("No answer is shown: nothing in this build can ask a paired node's resolver."))
        self.content.addWidget(lookup)
