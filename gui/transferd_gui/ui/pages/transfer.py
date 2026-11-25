"""Files and streams: send a file or open a stream to a connected peer, accept or refuse what peers ask for, and see history."""

from __future__ import annotations

from typing import List, Optional

from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
)

from transferd_gui.backend.files import FILE_SERVICE, STREAM_SERVICE
from transferd_gui.model.fmt import clock, human_age, human_bytes, short_hex
from transferd_gui.model.snapshot import NodeSnapshot
from transferd_gui.ui.pages.base import Page
from transferd_gui.ui.widgets import ActionRow, Card, Notice, TableWithEmpty

REQUEST_HEADERS = ("Request", "Peer", "Service", "Waiting for")
HISTORY_HEADERS = ("Time", "Direction", "Peer", "Service", "Name", "Size", "Status", "Detail")


class TransferPage(Page):
    key = "transfer"
    title = "Files and streams"
    explainer = (
        "Send a file to a connected peer, or open a named stream to a service it offers. A peer must accept each request "
        "before data flows, and received files are written to the download folder."
    )

    def build(self) -> None:
        self._peer_ids: List[str] = []
        self._request_ids: List[int] = []

        peer_row = QHBoxLayout()
        peer_row.addWidget(QLabel("Peer"))
        self.peer = QComboBox()
        self.peer.setMinimumWidth(320)
        peer_row.addWidget(self.peer, 1)
        self.content.addLayout(peer_row)
        self.peer_notice = Notice("Connect to a peer first. Only peers with a ready session are listed.")
        self.content.addWidget(self.peer_notice)

        send = Card("Send a file")
        row = QHBoxLayout()
        self.path = QLineEdit()
        self.path.setPlaceholderText("path to a file")
        browse = QPushButton("Browse...")
        browse.clicked.connect(self._browse)
        row.addWidget(self.path, 1)
        row.addWidget(browse)
        send.body.addLayout(row)
        self.send_button = QPushButton("Send file")
        self.send_button.setObjectName("primary")
        self.send_button.clicked.connect(self._send_file)
        send.body.addWidget(ActionRow(self.send_button))
        send.body.addWidget(QLabel(
            "The file is held in memory until the session takes it, so the transfer limit in Settings applies. "
            "\"Handed to the session\" means the bytes were queued; the library does not report delivery here."
        ))
        self.send_notice = Notice()
        send.body.addWidget(self.send_notice)
        self.content.addWidget(send)

        stream = Card("Open a stream")
        row = QHBoxLayout()
        row.addWidget(QLabel("Service"))
        self.service_name = QLineEdit(STREAM_SERVICE)
        self.service_name.setMaximumWidth(220)
        row.addWidget(self.service_name)
        row.addStretch(1)
        stream.body.addLayout(row)
        self.payload = QPlainTextEdit()
        self.payload.setPlaceholderText("text to send on the stream (UTF-8)")
        self.payload.setMaximumHeight(90)
        self.reply = QPlainTextEdit()
        self.reply.setReadOnly(True)
        self.reply.setPlaceholderText("what the peer sent back appears here")
        self.reply.setMaximumHeight(90)
        self.stream_button = QPushButton("Send and read reply")
        self.stream_button.clicked.connect(self._open_stream)
        stream.body.addWidget(self.payload)
        stream.body.addWidget(ActionRow(self.stream_button))
        stream.body.addWidget(self.reply)
        self.stream_notice = Notice()
        stream.body.addWidget(self.stream_notice)
        self.content.addWidget(stream)

        requests = Card("Incoming requests")
        requests.body.addWidget(QLabel(
            f"A peer asks to open a service; accept it to receive. '{FILE_SERVICE}' writes a file to the download folder."
        ))
        self.requests = TableWithEmpty(REQUEST_HEADERS, "Nothing is waiting. Requests from peers appear here.")
        self.accept_button = QPushButton("Accept selected")
        self.accept_button.clicked.connect(lambda: self._decide(True))
        self.refuse_button = QPushButton("Refuse selected")
        self.refuse_button.clicked.connect(lambda: self._decide(False))
        requests.body.addWidget(self.requests)
        requests.body.addWidget(ActionRow(self.accept_button, self.refuse_button))
        self.request_notice = Notice()
        requests.body.addWidget(self.request_notice)
        self.content.addWidget(requests)

        history = Card("History")
        self.history = TableWithEmpty(HISTORY_HEADERS, "No transfers yet. Sent and received files and streams are listed here.")
        history.body.addWidget(self.history)
        self.content.addWidget(history)

    def apply(self, snapshot: NodeSnapshot) -> None:
        self._refresh_peers(snapshot)
        self._refresh_requests(snapshot)
        rows = []
        for record in reversed(snapshot.transfers):
            rows.append(
                (
                    clock(record.when),
                    record.direction,
                    short_hex(record.peer_id, 8, 4),
                    record.service,
                    record.name or "-",
                    human_bytes(record.size),
                    record.status,
                    record.detail,
                )
            )
        self.history.set_rows(rows)

    def _refresh_peers(self, snapshot: NodeSnapshot) -> None:
        ready = snapshot.ready_peers()
        ids = [session.peer_id for session in ready]
        if ids == self._peer_ids:
            return
        current = self.selected_peer()
        self._peer_ids = ids
        self.peer.blockSignals(True)
        self.peer.clear()
        for session in ready:
            self.peer.addItem(f"{session.peer_name} ({short_hex(session.peer_id, 8, 4)})")
        if current in ids:
            self.peer.setCurrentIndex(ids.index(current))
        self.peer.blockSignals(False)
        self.peer_notice.setVisible(not ids)
        for button in (self.send_button, self.stream_button):
            button.setEnabled(bool(ids))

    def _refresh_requests(self, snapshot: NodeSnapshot) -> None:
        self._request_ids = [r.request_id for r in snapshot.requests]
        rows = [
            (str(r.request_id), r.peer_name, r.service, human_age(snapshot.taken_at - r.received_at))
            for r in snapshot.requests
        ]
        self.requests.set_rows(rows)
        waiting = bool(rows)
        self.accept_button.setEnabled(waiting)
        self.refuse_button.setEnabled(waiting)

    def selected_peer(self) -> Optional[str]:
        index = self.peer.currentIndex()
        return self._peer_ids[index] if 0 <= index < len(self._peer_ids) else None

    # ----- actions ------------------------------------------------------------------------------

    def _browse(self) -> None:
        name, _filter = QFileDialog.getOpenFileName(self, "Choose a file to send")
        if name:
            self.path.setText(name)

    def _send_file(self) -> None:
        peer = self.selected_peer()
        path = self.path.text().strip()
        if peer is None:
            self.send_notice.set_text("Choose a connected peer first.", "error")
            return
        if not path:
            self.send_notice.set_text("Choose a file first.", "error")
            return
        self.send_notice.set_text("Sending. The peer must accept the request within 15 s.", "muted")
        self.run(self.service.node.send_file(peer, path), "send a file", self._sent)

    def _sent(self, ok: bool, value) -> None:
        self.send_notice.set_text("Handed to the session." if ok else f"Not sent: {value}", "ok" if ok else "error")

    def _open_stream(self) -> None:
        peer = self.selected_peer()
        service = self.service_name.text().strip()
        if peer is None:
            self.stream_notice.set_text("Choose a connected peer first.", "error")
            return
        if not service:
            self.stream_notice.set_text("Name the service to open.", "error")
            return
        payload = self.payload.toPlainText().encode("utf-8")
        self.stream_notice.set_text("Opening the stream. The peer must accept it within 15 s.", "muted")
        self.run(self.service.node.open_stream(peer, service, payload), "open a stream", self._streamed)

    def _streamed(self, ok: bool, value) -> None:
        if ok:
            self.reply.setPlainText(bytes(value).decode("utf-8", errors="replace"))
            self.stream_notice.set_text(f"Reply: {human_bytes(len(value))}.", "ok")
        else:
            self.stream_notice.set_text(f"Not opened: {value}", "error")

    def _decide(self, accept: bool) -> None:
        row = self.requests.selected_row()
        if row < 0 or row >= len(self._request_ids):
            self.request_notice.set_text("Select a request first.", "error")
            return
        request_id = self._request_ids[row]
        if accept:
            self.run(self.service.node.accept_request(request_id), "accept a request", self._decided("accepted"))
        else:
            self.run(self.service.node.refuse_request(request_id), "refuse a request", self._decided("refused"))

    def _decided(self, verb: str):
        def done(ok: bool, value) -> None:
            self.request_notice.set_text(f"Request {verb}." if ok else f"Not {verb}: {value}", "ok" if ok else "error")

        return done

