"""Ghost keys: create this node's key, get it certified by an issuer, and check the certificate.

The steps follow the protocol: create a key; load the issuer's public key; ask for a certificate, which gives a blinded
request to send to the issuer; send the issuer's answer back to finish. Nothing here signs as an issuer.
"""

from __future__ import annotations

from PySide6.QtWidgets import QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton

from transferd_gui.backend import capabilities
from transferd_gui.backend.ghost import GhostError, GhostStatus, GhostUnavailable
from transferd_gui.ui.pages.base import Page
from transferd_gui.ui.widgets import ActionRow, Banner, Card, CopyField, Notice, Pill


class GhostPage(Page):
    key = "ghost"
    title = "Ghost keys"
    explainer = (
        "A ghost key proves that an issuer vouched for you without the issuer learning which key it is. Create a key, "
        "load the issuer's public key, send the blinded request to the issuer, then finish with the answer."
    )

    def build(self) -> None:
        self.unavailable = Banner("")
        self.unavailable.setVisible(False)
        self.content.addWidget(self.unavailable)

        status = Card("Status")
        self.key_public = CopyField("Public key", head=14, tail=10)
        self.issuer = QLabel()
        self.pending = QLabel()
        self.certificate = Pill("no certificate yet", "idle")
        self.certificate_detail = QLabel()
        self.certificate_detail.setWordWrap(True)
        for widget in (self.key_public, self.issuer, self.pending, self.certificate, self.certificate_detail):
            status.body.addWidget(widget)
        self.content.addWidget(status)

        self.create_button = QPushButton("Create ghost key")
        self.create_button.clicked.connect(self._create)
        create = Card("1. Create a ghost key")
        create.body.addWidget(ActionRow(self.create_button))
        self.create_notice = Notice()
        create.body.addWidget(self.create_notice)
        self.content.addWidget(create)

        issuer = Card("2. Load the issuer's public key")
        self.issuer_text = QPlainTextEdit()
        self.issuer_text.setPlaceholderText("transferd-ghostissuerpub-v1:...")
        self.issuer_text.setMaximumHeight(80)
        self.save_issuer_button = QPushButton("Save issuer")
        self.save_issuer_button.clicked.connect(self._save_issuer)
        issuer.body.addWidget(self.issuer_text)
        issuer.body.addWidget(ActionRow(self.save_issuer_button))
        self.issuer_notice = Notice()
        issuer.body.addWidget(self.issuer_notice)
        self.content.addWidget(issuer)

        request = Card("3. Ask the issuer for a certificate")
        self.request_button = QPushButton("Make request")
        self.request_button.clicked.connect(self._request)
        request.body.addWidget(ActionRow(self.request_button))
        self.request_out = CopyField("Request (hex)", head=16, tail=10)
        request.body.addWidget(self.request_out)
        request.body.addWidget(QLabel("Send this request to the issuer. Keep the pending request file until the answer arrives."))
        self.request_notice = Notice()
        request.body.addWidget(self.request_notice)
        self.content.addWidget(request)

        finish = Card("4. Finish with the issuer's answer")
        self.answer = QLineEdit()
        self.answer.setPlaceholderText("issuer's answer (hex)")
        self.finish_button = QPushButton("Finish certificate")
        self.finish_button.clicked.connect(self._finish)
        finish.body.addWidget(self.answer)
        finish.body.addWidget(ActionRow(self.finish_button))
        self.finish_notice = Notice()
        finish.body.addWidget(self.finish_notice)
        self.content.addWidget(finish)

        reset_card = Card("Reset")
        self.reset_button = QPushButton("Delete ghost key files")
        self.reset_button.clicked.connect(self._reset)
        reset_card.body.addWidget(ActionRow(self.reset_button))
        reset_card.body.addWidget(QLabel("Deletes the key, issuer, pending request and certificate files. This cannot be undone."))
        self.reset_notice = Notice()
        reset_card.body.addWidget(self.reset_notice)
        self.content.addWidget(reset_card)

        register = Card("Register with a ghost key")
        register.body.addWidget(Banner(capabilities.reason(capabilities.GHOST_REGISTER)))
        self.register_button = QPushButton("Register with ghost key")
        self.register_button.setEnabled(False)
        register.body.addWidget(ActionRow(self.register_button))
        self.content.addWidget(register)

    def on_show(self) -> None:
        self.refresh()

    def refresh(self) -> None:
        status = self.context.service.ghost().status()
        self._show(status)

    def _show(self, status: GhostStatus) -> None:
        available = status.available
        self.unavailable.setVisible(not available)
        if not available:
            self.unavailable.set_reason(status.reason)
        for button in (self.create_button, self.save_issuer_button, self.request_button, self.finish_button, self.reset_button):
            button.setEnabled(available)
        self.issuer_text.setEnabled(available)
        self.answer.setEnabled(available)
        self.key_public.set_value(status.public_key if status.has_key else "", placeholder="no ghost key yet")
        self.create_button.setEnabled(available and not status.has_key)
        self.issuer.setText(f"Issuer: {status.issuer_name or 'not loaded'}")
        self.pending.setText("Pending request: " + ("waiting for the issuer's answer" if status.pending else "none"))
        cert = status.certificate
        if cert is None:
            self.certificate.set_status("no certificate yet", "idle")
            self.certificate_detail.setText("")
        else:
            self.certificate.set_status("valid" if cert.valid else "not valid", "ok" if cert.valid else "warn")
            self.certificate_detail.setText(f"Issuer {cert.issuer_id[:16]}, tier {cert.tier}: {cert.detail}")

    # ----- actions ------------------------------------------------------------------------------

    @staticmethod
    def _try(action, notice: Notice, ok_text: str):
        """Run one ghost key operation. Its failure is written to `notice`; success returns the value."""
        try:
            value = action()
        except (GhostUnavailable, GhostError) as exc:
            notice.set_text(str(exc), "error")
            return None
        notice.set_text(ok_text, "ok")
        return value

    def _create(self) -> None:
        self._try(self.context.service.ghost().create_key, self.create_notice, "Created. Its public key is shown above.")
        self.refresh()

    def _save_issuer(self) -> None:
        name = self._try(
            lambda: self.context.service.ghost().save_issuer(self.issuer_text.toPlainText()),
            self.issuer_notice,
            "Issuer saved.",
        )
        if name is not None:
            self.issuer_notice.set_text(f"Issuer '{name}' saved.", "ok")
        self.refresh()

    def _request(self) -> None:
        blinded = self._try(
            lambda: self.context.service.ghost().request_certificate(),
            self.request_notice,
            "Request made. Send it to the issuer.",
        )
        if blinded is not None:
            self.request_out.set_value(blinded)
        self.refresh()

    def _finish(self) -> None:
        answer = self.answer.text().strip()
        if not answer:
            self.finish_notice.set_text("Paste the issuer's answer first.", "error")
            return
        status = self._try(
            lambda: self.context.service.ghost().finish_certificate(answer),
            self.finish_notice,
            "Certificate saved.",
        )
        if status is not None and not status.valid:
            self.finish_notice.set_text(f"Certificate saved but it does not verify: {status.detail}", "warn")
        self.refresh()

    def _reset(self) -> None:
        answer = QMessageBox.question(
            self,
            "Delete ghost key files",
            "Delete the ghost key, issuer, pending request and certificate files? This cannot be undone.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.context.service.ghost().reset()
        self.reset_notice.set_text("Ghost key files deleted.", "ok")
        self.refresh()
