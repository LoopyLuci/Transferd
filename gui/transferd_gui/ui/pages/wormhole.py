"""The Wormhole screen: send a file, a folder or a text with a short code, or type a code and receive what is offered.

Nothing is sent until the other computer has typed the code and the receiving user has accepted the offer. The code is
shown as soon as the mailbox has it, and stays on screen until the attempt ends or is cancelled.

Each attempt has a generation number. Cancelling bumps it, so a result or status line that arrives late from a cancelled
attempt is ignored, and a sender or receiver that finished after its attempt was cancelled is closed instead of shown.
"""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QFileDialog,
    QFormLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
)

from transferd_gui.backend.wormhole import WormholeService, mailbox_problem
from transferd_gui.model.settings import Settings
from transferd_gui.ui.pages.base import Page
from transferd_gui.ui.widgets import ActionRow, Card, Notice
from transferd_gui.wormhole.session import ReceiveResult, Receiver, SendResult, Sender


def describe_offer(receiver: Receiver) -> str:
    offer = receiver.offer_received
    if offer is None:
        return ""
    if offer.kind == "text":
        return f"A text of {offer.size} bytes is offered."
    if offer.kind == "directory":
        return f"A folder named {offer.name} ({offer.entries} entries, {offer.size} bytes) is offered."
    return f"A file named {offer.name} ({offer.size} bytes) is offered."


class WormholePage(Page):
    key = "wormhole"
    title = "Wormhole"
    explainer = (
        "Send a file, a folder or a text with a short code, or type the code someone gave you. The mailbox server, the relay "
        "and the download folder come from Settings. Nothing is sent until the receiving side accepts the offer."
    )

    def build(self) -> None:
        self.wormhole = WormholeService(self.service)
        self._sender: Optional[Sender] = None
        self._send_future: Any = None
        self._send_gen = 0
        self._receiver: Optional[Receiver] = None
        self._receive_future: Any = None
        self._receive_gen = 0

        self.setup_notice = Notice()
        self.content.addWidget(self.setup_notice)

        send = Card("Send")
        self.send_file_button = QPushButton("Send a file")
        self.send_folder_button = QPushButton("Send a folder")
        self.send_file_button.clicked.connect(self._choose_file)
        self.send_folder_button.clicked.connect(self._choose_folder)
        self.text_input = QPlainTextEdit()
        self.text_input.setPlaceholderText("Or type a text to send, then press Send text.")
        self.text_input.setMaximumHeight(90)
        self.send_text_button = QPushButton("Send text")
        self.send_text_button.clicked.connect(self._send_text)
        send.body.addWidget(ActionRow(self.send_file_button, self.send_folder_button))
        send.body.addWidget(self.text_input)
        send.body.addWidget(ActionRow(self.send_text_button))
        self.code_label = QLabel("")
        self.code_label.setObjectName("title")
        self.code_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.copy_code_button = QPushButton("Copy code")
        self.copy_code_button.clicked.connect(self._copy_code)
        self.cancel_send_button = QPushButton("Cancel")
        self.cancel_send_button.clicked.connect(self._cancel_send)
        send.body.addWidget(self.code_label)
        send.body.addWidget(ActionRow(self.copy_code_button, self.cancel_send_button))
        self.send_status = Notice("Nothing is being sent.", "muted")
        send.body.addWidget(self.send_status)
        self.content.addWidget(send)

        receive = Card("Receive")
        self.code_input = QLineEdit()
        self.code_input.setPlaceholderText("the code, for example 4217-ember-tundra-lattice")
        self.look_up_button = QPushButton("Look up the code")
        self.look_up_button.clicked.connect(self._look_up)
        form = QFormLayout()
        form.addRow("Code", self.code_input)
        receive.body.addLayout(form)
        receive.body.addWidget(ActionRow(self.look_up_button))
        self.offer_label = Notice("", "muted")
        self.accept_button = QPushButton("Accept")
        self.decline_button = QPushButton("Decline")
        self.accept_button.clicked.connect(lambda: self._answer(True))
        self.decline_button.clicked.connect(lambda: self._answer(False))
        receive.body.addWidget(self.offer_label)
        receive.body.addWidget(ActionRow(self.accept_button, self.decline_button))
        self.received_text = QPlainTextEdit()
        self.received_text.setReadOnly(True)
        self.received_text.setMaximumHeight(120)
        self.received_text.setVisible(False)
        receive.body.addWidget(self.received_text)
        self.receive_status = Notice("Nothing is being received.", "muted")
        receive.body.addWidget(self.receive_status)
        self.stop_receive_button = QPushButton("Stop")
        self.stop_receive_button.clicked.connect(self._stop_receive)
        receive.body.addWidget(ActionRow(self.stop_receive_button))
        self.content.addWidget(receive)

        self._refresh_availability(self.service.settings)
        self._set_offer_buttons(False)
        self.cancel_send_button.setEnabled(False)
        self.stop_receive_button.setEnabled(False)

    # ----- availability -----------------------------------------------------------------------------

    def on_settings(self, settings: Settings) -> None:
        self._refresh_availability(settings)

    def _refresh_availability(self, settings: Settings) -> None:
        reason = mailbox_problem(settings)
        enabled = not reason
        if reason:
            self.setup_notice.set_text(reason, "error")
        else:
            self.setup_notice.set_text(f"The mailbox is {settings.wormhole_mailbox.strip()}.", "muted")
        for widget in (
            self.send_file_button,
            self.send_folder_button,
            self.send_text_button,
            self.text_input,
            self.look_up_button,
            self.code_input,
        ):
            widget.setEnabled(enabled)

    def _set_offer_buttons(self, on: bool) -> None:
        self.accept_button.setEnabled(on)
        self.decline_button.setEnabled(on)

    # ----- sending ----------------------------------------------------------------------------------

    def _choose_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Choose a file to send")
        if path:
            self.send_file(path)

    def _choose_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Choose a folder to send")
        if path:
            self.send_folder(path)

    def _send_text(self) -> None:
        text = self.text_input.toPlainText()
        if not text.strip():
            self.send_status.set_text("Type a text first.", "error")
            return
        self.send_text(text)

    def send_file(self, path: str) -> None:
        self._begin_send("file", path, "Preparing the file")

    def send_folder(self, path: str) -> None:
        self._begin_send("folder", path, "Zipping the folder")

    def send_text(self, text: str) -> None:
        self._begin_send("text", text, "Preparing the text")

    def _begin_send(self, kind: str, value: str, status: str) -> None:
        if self._send_future is not None:
            self.send_status.set_text("A send is already in progress. Cancel it first.", "error")
            return
        self._send_gen += 1
        gen = self._send_gen
        self.send_status.set_text(status + "...", "muted")
        self.cancel_send_button.setEnabled(True)
        future = self.service.run(
            self.wormhole.start_send(kind, value, self.bridge.on_ui(lambda text: self._say_send(gen, text))),
            "start a wormhole send",
            self.bridge.on_ui(lambda ok, result: self._send_started(gen, ok, result)),
        )
        if future is None:
            self._send_reset("The node is not running, so nothing can be sent.", "error")
            return
        self._send_future = future

    def _say_send(self, gen: int, text: str) -> None:
        if gen == self._send_gen:
            self.send_status.set_text(text, "muted")

    def _send_started(self, gen: int, ok: bool, result: Any) -> None:
        if gen != self._send_gen:
            # Cancelled while the code was being reserved: close the mailbox the sender opened.
            if ok:
                self.service.run(self.wormhole.abandon(result), "stop a wormhole send")
            return
        if not ok:
            self._send_reset(str(result), "error")
            return
        self._sender = result
        self.code_label.setText(str(result.code))
        self.send_status.set_text("Waiting for the other computer to type this code.", "muted")
        self._send_future = self.service.run(
            self.wormhole.run_send(result),
            "send a wormhole",
            self.bridge.on_ui(lambda ok2, value: self._send_finished(gen, ok2, value)),
        )

    def _send_finished(self, gen: int, ok: bool, result: Any) -> None:
        if gen != self._send_gen:
            return
        if not ok:
            self._send_reset(str(result), "error")
            return
        outcome: SendResult = result
        if outcome.declined:
            detail = f" Reason: {outcome.reason}" if outcome.reason else ""
            self._send_reset("Declined by the other computer." + detail, "muted")
        else:
            self._send_reset("Sent.", "ok")

    def _send_reset(self, text: str, tone: str) -> None:
        self._sender = None
        self._send_future = None
        self.cancel_send_button.setEnabled(False)
        self.code_label.setText("")
        self.send_status.set_text(text, tone)

    def _cancel_send(self) -> None:
        if self._send_future is None:
            return
        self._send_gen += 1
        # Cancelling ends the attempt on its own: the sender closes its mailbox whether it is reserving a code or sending.
        self._send_future.cancel()
        self._send_reset("Cancelled. The code no longer works.", "muted")

    def _copy_code(self) -> None:
        if self._sender is not None:
            QGuiApplication.clipboard().setText(str(self._sender.code))
            self.send_status.set_text("The code is on the clipboard.", "muted")

    # ----- receiving --------------------------------------------------------------------------------

    def _look_up(self) -> None:
        if self._receive_future is not None or self._receiver is not None:
            self.receive_status.set_text("A receive is already in progress. Stop it first.", "error")
            return
        code = self.code_input.text().strip()
        if not code:
            self.receive_status.set_text("Type the code first.", "error")
            return
        self._receive_gen += 1
        gen = self._receive_gen
        self._set_offer_buttons(False)
        self.received_text.setVisible(False)
        self.receive_status.set_text("Looking up the code...", "muted")
        self.stop_receive_button.setEnabled(True)
        future = self.service.run(
            self.wormhole.open_receive(code, self.bridge.on_ui(lambda text: self._say_receive(gen, text))),
            "look up a wormhole code",
            self.bridge.on_ui(lambda ok, result: self._looked_up(gen, ok, result)),
        )
        if future is None:
            self._receive_reset("The node is not running, so the code cannot be used.", "error")
            return
        self._receive_future = future

    def _say_receive(self, gen: int, text: str) -> None:
        if gen == self._receive_gen:
            self.receive_status.set_text(text, "muted")

    def _looked_up(self, gen: int, ok: bool, result: Any) -> None:
        if gen != self._receive_gen:
            if ok:
                self.service.run(self.wormhole.abandon(result), "stop a wormhole receive")
            return
        self._receive_future = None
        if not ok:
            self._receive_reset(str(result), "error")
            return
        self._receiver = result
        self.offer_label.set_text(describe_offer(result), "ok")
        self._set_offer_buttons(True)
        self.receive_status.set_text("Accept to receive it, or decline. Stop declines it and closes the code.", "muted")

    def _answer(self, accept: bool) -> None:
        if self._receiver is None or self._receive_future is not None:
            return
        gen = self._receive_gen
        self._set_offer_buttons(False)
        self.receive_status.set_text("Receiving..." if accept else "Declining...", "muted")
        reason = "" if accept else "the receiving user declined"
        self._receive_future = self.service.run(
            self.wormhole.finish_receive(self._receiver, accept, reason),
            "receive a wormhole",
            self.bridge.on_ui(lambda ok, result: self._received(gen, ok, result)),
        )
        if self._receive_future is None:
            self._receive_reset("The node is not running.", "error")

    def _received(self, gen: int, ok: bool, result: Any) -> None:
        if gen != self._receive_gen:
            return
        self._receive_future = None
        if not ok:
            self._receive_reset(str(result), "error")
            return
        outcome: ReceiveResult = result
        if outcome.kind == "declined":
            self._receive_reset("Declined.", "muted")
        elif outcome.kind == "text":
            self.received_text.setPlainText(outcome.text)
            self.received_text.setVisible(True)
            self._receive_reset("Received a text.", "ok")
        else:
            self._receive_reset(f"Saved to {outcome.path}", "ok")

    def _receive_reset(self, text: str, tone: str) -> None:
        self._receiver = None
        self._receive_future = None
        self._set_offer_buttons(False)
        self.stop_receive_button.setEnabled(False)
        self.offer_label.set_text("", "muted")
        self.receive_status.set_text(text, tone)

    def _stop_receive(self) -> None:
        if self._receive_future is not None:
            # Looking up or receiving: cancelling ends the attempt, and the other side is told it did not finish.
            self._receive_gen += 1
            self._receive_future.cancel()
            self._receive_reset("Stopped.", "muted")
        elif self._receiver is not None:
            # Waiting on the offer: decline it so the sender is told, and close the code.
            self._receive_gen += 1
            self.service.run(self.wormhole.abandon(self._receiver), "stop a wormhole receive")
            self._receive_reset("Stopped. The offer was declined.", "muted")
