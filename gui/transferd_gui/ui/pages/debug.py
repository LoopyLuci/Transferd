"""Debugging: choose a device, connect a route to it over Wi-Fi or USB, then read its information or log, run a command on it,
or forward a port. The device decides what it allows. A request a device's user is asked about waits here until it is
answered, and every operation running now is listed with its state."""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton, QSpinBox

from transferd_gui.debug import wire
from transferd_gui.debug.usb import AdbDevice
from transferd_gui.model.fmt import short_hex
from transferd_gui.model.settings import Settings, SettingsError
from transferd_gui.model.snapshot import NodeSnapshot
from transferd_gui.ui.pages.base import Page
from transferd_gui.ui.widgets import ActionRow, Card, Notice, Pill, TableWithEmpty

#: Lines kept in each text view. Older lines are dropped, so a long log never grows without limit.
VIEW_LINES = 2000
INFO_SERVICE = wire.service_name("info")
TRANSPORT_LABELS = {
    "auto": "USB when it is plugged in, otherwise Wi-Fi",
    "usb_only": "USB only",
    "wifi_only": "Wi-Fi only",
}


def is_node_id(text: str) -> bool:
    return len(text) == 64 and all(c in "0123456789abcdef" for c in text)


def device_state(snapshot: NodeSnapshot, device_id: str) -> Tuple[str, str]:
    """What the chosen device is, for this computer, as a short word and a tone."""
    if not device_id:
        return "no device chosen", "idle"
    session = snapshot.session_for(device_id)
    if session is None:
        return "not connected", "warn"
    if not session.routes_up:
        return "no live route", "warn"
    if INFO_SERVICE not in session.services:
        return "does not offer debugging", "warn"
    return "ready", "ok"


def _text(value: Any) -> str:
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", errors="replace")
    return str(value)


class DebugPage(Page):
    key = "debug"
    title = "Debugging"
    explainer = (
        "Debug a device over USB, over Wi-Fi, or both. Choose the device, connect a route to it, then read its information "
        "or log, run a command on it, or forward a port. The device's own settings decide what it allows."
    )

    def build(self) -> None:
        self._shown: Dict[str, Any] = {}
        self._peer_ids: List[str] = []
        self._usb_serials: List[str] = []
        self._stream_handles: List[int] = []
        self._request_ids: List[int] = []
        self._log_handle: Optional[int] = None
        # The log's lines arrive on the backend thread; this turns each one into a call on the UI thread.
        self._log_sink = self.bridge.on_ui(self._on_log_message)

        self._build_this_device()
        self._build_choice()
        self._build_connection()
        self._build_log()
        self._build_shell()
        self._build_forward()
        self._build_operations()
        self._build_requests()
        self.on_settings(self.service.settings)

    # ----- construction -------------------------------------------------------------------------

    def _build_this_device(self) -> None:
        card = Card("This device")
        self.device_side = Notice()
        card.body.addWidget(self.device_side)
        self.content.addWidget(card)

    def _build_choice(self) -> None:
        card = Card("Device to debug")
        card.body.addWidget(QLabel("Pick a peer you are connected to, or paste its node id. Everything below uses this device."))
        row = QHBoxLayout()
        self.peer_combo = QComboBox()
        self.peer_combo.setMinimumWidth(260)
        self.device_edit = QLineEdit()
        self.device_edit.setPlaceholderText("device node id (64 hex characters)")
        self.use_button = QPushButton("Use this device")
        self.use_button.clicked.connect(self._use_device)
        row.addWidget(self.peer_combo, 1)
        row.addWidget(self.device_edit, 2)
        row.addWidget(self.use_button)
        card.body.addLayout(row)
        status = QHBoxLayout()
        self.device_pill = Pill("no device chosen", "idle")
        self.ask_button = QPushButton("Ask the device for its information")
        self.ask_button.clicked.connect(self._ask_info)
        status.addWidget(self.device_pill)
        status.addWidget(self.ask_button)
        status.addStretch(1)
        card.body.addLayout(status)
        self.choice_notice = Notice()
        card.body.addWidget(self.choice_notice)
        self.content.addWidget(card)

    def _build_connection(self) -> None:
        card = Card("Connection")
        self.transport_text = QLabel()
        self.transport_text.setWordWrap(True)
        card.body.addWidget(self.transport_text)
        self.routes = TableWithEmpty(
            ("Route", "Kind", "State", "Received", "Sent"),
            "No route to this device yet. Connect over Wi-Fi or USB below.",
        )
        card.body.addWidget(self.routes)

        self.wifi_button = QPushButton("Connect over Wi-Fi")
        self.wifi_button.clicked.connect(self._connect_wifi)
        card.body.addWidget(ActionRow(self.wifi_button))

        card.body.addWidget(QLabel("USB: plug the phone in, allow USB debugging on it, then look for it."))
        self.usb_state = Notice()
        card.body.addWidget(self.usb_state)
        self.usb_table = TableWithEmpty(("Serial", "State", "Model", "What to do"), "No USB devices looked for yet.")
        card.body.addWidget(self.usb_table)
        self.usb_scan_button = QPushButton("Look for USB devices")
        self.usb_scan_button.clicked.connect(self._scan_usb)
        self.usb_connect_button = QPushButton("Connect the selected USB device")
        self.usb_connect_button.clicked.connect(self._connect_usb)
        card.body.addWidget(ActionRow(self.usb_scan_button, self.usb_connect_button))

        self.connection_notice = Notice()
        card.body.addWidget(self.connection_notice)
        self.content.addWidget(card)

    def _build_log(self) -> None:
        card = Card("Log")
        card.body.addWidget(QLabel("The device's log as it is written. Filter by text if you want (optional)."))
        row = QHBoxLayout()
        self.log_filter = QLineEdit()
        self.log_filter.setPlaceholderText("only lines containing (optional)")
        self.log_start = QPushButton("Start")
        self.log_start.clicked.connect(self._start_log)
        self.log_stop = QPushButton("Stop")
        self.log_stop.clicked.connect(self._stop_log)
        self.log_clear = QPushButton("Clear")
        self.log_clear.clicked.connect(lambda: self.log_view.clear())
        row.addWidget(self.log_filter, 1)
        row.addWidget(self.log_start)
        row.addWidget(self.log_stop)
        row.addWidget(self.log_clear)
        card.body.addLayout(row)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(VIEW_LINES)
        self.log_view.setMinimumHeight(160)
        card.body.addWidget(self.log_view)
        self.log_notice = Notice()
        card.body.addWidget(self.log_notice)
        self.content.addWidget(card)

    def _build_shell(self) -> None:
        card = Card("Run a command")
        card.body.addWidget(QLabel(
            "The command runs on the device. Its user is asked first, unless the device's settings allow commands."
        ))
        row = QHBoxLayout()
        self.command = QLineEdit()
        self.command.setPlaceholderText("command to run on the device")
        self.command.returnPressed.connect(self._run_shell)
        self.shell_timeout = QSpinBox()
        self.shell_timeout.setRange(1, 600)
        self.shell_timeout.setValue(60)
        self.shell_timeout.setSuffix(" s")
        self.shell_timeout.setToolTip("The command is stopped after this long.")
        self.shell_run = QPushButton("Run")
        self.shell_run.clicked.connect(self._run_shell)
        row.addWidget(self.command, 1)
        row.addWidget(self.shell_timeout)
        row.addWidget(self.shell_run)
        card.body.addLayout(row)
        self.shell_output = QPlainTextEdit()
        self.shell_output.setReadOnly(True)
        self.shell_output.setMaximumBlockCount(VIEW_LINES)
        self.shell_output.setMinimumHeight(120)
        card.body.addWidget(self.shell_output)
        self.shell_notice = Notice()
        card.body.addWidget(self.shell_notice)
        self.content.addWidget(card)

    def _build_forward(self) -> None:
        card = Card("Forward a port")
        card.body.addWidget(QLabel(
            "Connections to a port on this computer are carried to a port on the device, for example a debugger's port. "
            "The device must list the port in its settings."
        ))
        row = QHBoxLayout()
        self.local_port = QSpinBox()
        self.local_port.setRange(0, 65535)
        self.local_port.setSpecialValueText("any free port")
        self.local_port.setPrefix("this computer: ")
        self.device_port = QSpinBox()
        self.device_port.setRange(1, 65535)
        self.device_port.setValue(8080)
        self.device_port.setPrefix("device: ")
        self.forward_start = QPushButton("Start forwarding")
        self.forward_start.clicked.connect(self._start_forward)
        row.addWidget(self.local_port)
        row.addWidget(self.device_port)
        row.addWidget(self.forward_start)
        row.addStretch(1)
        card.body.addLayout(row)
        self.forward_notice = Notice()
        card.body.addWidget(self.forward_notice)
        self.content.addWidget(card)

    def _build_operations(self) -> None:
        card = Card("Operations")
        self.streams = TableWithEmpty(
            ("Operation", "Device", "Detail", "State", "Note"),
            "No debugging operations yet. Logs, commands and forwards you start are listed here.",
        )
        card.body.addWidget(self.streams)
        self.stop_stream = QPushButton("Stop selected")
        self.stop_stream.clicked.connect(self._stop_selected)
        card.body.addWidget(ActionRow(self.stop_stream))
        self.operations_notice = Notice()
        card.body.addWidget(self.operations_notice)
        self.content.addWidget(card)

    def _build_requests(self) -> None:
        card = Card("Requests from computers")
        card.body.addWidget(QLabel(
            "A computer asks to debug this device, or to run a command on it. Accept or refuse each request. "
            "Requests wait here until you answer them."
        ))
        self.requests = TableWithEmpty(("From", "Operation", "Asked at"), "No computer is waiting to debug this device.")
        card.body.addWidget(self.requests)
        self.accept_button = QPushButton("Accept")
        self.accept_button.clicked.connect(lambda: self._answer(True))
        self.refuse_button = QPushButton("Refuse")
        self.refuse_button.clicked.connect(lambda: self._answer(False))
        card.body.addWidget(ActionRow(self.accept_button, self.refuse_button))
        self.requests_notice = Notice()
        card.body.addWidget(self.requests_notice)
        self.content.addWidget(card)

    # ----- snapshots and settings ---------------------------------------------------------------

    def apply(self, snapshot: NodeSnapshot) -> None:
        device = self._device()
        self._show_device_side(snapshot)
        self._show_peers(snapshot)
        state, tone = device_state(snapshot, device)
        self.device_pill.set_status(state, tone)
        self._show_routes(snapshot, device)
        self._show_usb(snapshot)
        self._show_streams(snapshot)
        self._show_requests(snapshot)
        self._follow_log(snapshot)
        self._enable_actions(device)

    def on_settings(self, settings: Settings) -> None:
        address = settings.debug_wifi_address or "not set (Settings, under Debugging from this computer)"
        self.transport_text.setText(
            f"Transport: {TRANSPORT_LABELS.get(settings.debug_transport, settings.debug_transport)}. "
            f"Wi-Fi address: {address}."
        )

    def _show_device_side(self, snapshot: NodeSnapshot) -> None:
        debug = snapshot.debug
        if not debug.agent_enabled:
            text = "Debugging is off on this device, so no computer can debug it. Turn it on in Settings, under Debugging."
        elif not debug.clients:
            text = "Debugging is on, but no computer is on this device's list yet. Add one in Settings, under Debugging."
        else:
            count = len(debug.clients)
            text = f"Debugging is on. {count} computer{'s' if count != 1 else ''} on the list can use this device."
        self.device_side.set_text(text, "muted")

    def _show_peers(self, snapshot: NodeSnapshot) -> None:
        ids = [s.peer_id for s in snapshot.sessions]
        if ids == self._peer_ids:
            return
        names = {s.peer_id: s.peer_name for s in snapshot.sessions}
        current = self.peer_combo.currentData()
        self._peer_ids = ids
        self.peer_combo.clear()
        for peer_id in ids:
            self.peer_combo.addItem(f"{names[peer_id]} ({short_hex(peer_id, 10, 6)})", peer_id)
        index = self.peer_combo.findData(current)
        if index >= 0:
            self.peer_combo.setCurrentIndex(index)

    def _show_routes(self, snapshot: NodeSnapshot, device: str) -> None:
        session = snapshot.session_for(device) if device else None
        rows = []
        if session is not None:
            for route in session.routes:
                status = "closed" if route.closed else ("up" if route.up else "down")
                rows.append((route.label or route.kind, route.kind, status, f"{route.received_bytes} bytes", f"{route.sent_bytes} bytes"))
        self._refresh("routes", self.routes, rows)

    def _show_usb(self, snapshot: NodeSnapshot) -> None:
        debug = snapshot.debug
        self.usb_state.set_text(debug.usb_detail or "USB has not been checked yet.", "muted")
        rows, serials = [], []
        for device in debug.usb_devices:
            help_text = AdbDevice(device.serial, device.state, device.model).help
            rows.append((device.serial, device.state, device.model or "unknown model", help_text))
            serials.append(device.serial)
        self._usb_serials = serials
        self._refresh("usb", self.usb_table, rows)

    def _show_streams(self, snapshot: NodeSnapshot) -> None:
        rows, handles = [], []
        for stream in sorted(snapshot.debug.streams, key=lambda s: s.handle):
            rows.append((stream.kind, short_hex(stream.peer_id, 10, 6), stream.detail, stream.state, stream.note))
            handles.append(stream.handle)
        self._stream_handles = handles
        self._refresh("streams", self.streams, rows)

    def _show_requests(self, snapshot: NodeSnapshot) -> None:
        rows, ids = [], []
        for request in snapshot.requests:
            op = wire.op_of(request.service)
            if op is None:
                continue  # file and stream requests are answered on the Transfer screen
            rows.append((request.peer_name, op, time.strftime("%H:%M:%S", time.localtime(request.received_at))))
            ids.append(request.request_id)
        self._request_ids = ids
        self._refresh("requests", self.requests, rows)

    def _follow_log(self, snapshot: NodeSnapshot) -> None:
        if self._log_handle is None:
            return
        stream = next((s for s in snapshot.debug.streams if s.handle == self._log_handle), None)
        if stream is None or stream.state != "running":
            self._log_handle = None
            detail = stream.note if stream is not None and stream.note else "the log ended"
            self.log_notice.set_text(f"The log stopped: {detail}.", "muted")

    def _enable_actions(self, device: str) -> None:
        self.ask_button.setEnabled(bool(device))
        self.wifi_button.setEnabled(bool(device))
        self.usb_connect_button.setEnabled(bool(device) and bool(self._usb_serials))
        self.log_start.setEnabled(bool(device) and self._log_handle is None)
        self.log_stop.setEnabled(self._log_handle is not None)
        self.shell_run.setEnabled(bool(device))
        self.forward_start.setEnabled(bool(device))
        self.stop_stream.setEnabled(bool(self._stream_handles))
        self.accept_button.setEnabled(bool(self._request_ids))
        self.refuse_button.setEnabled(bool(self._request_ids))

    def _refresh(self, name: str, table: TableWithEmpty, rows: Sequence[Tuple[str, ...]]) -> None:
        """Replace a table's rows only when they changed, so a selected row survives the once-a-second update."""
        rows = [tuple(r) for r in rows]
        if self._shown.get(name) == rows:
            return
        self._shown[name] = rows
        table.set_rows(rows)

    def _device(self) -> str:
        return self.service.settings.debug_device_id

    def _need_device(self, notice: Notice) -> Optional[str]:
        device = self._device()
        if not device:
            notice.set_text("Choose the device first: pick a connected peer, or paste its node id, then press Use this device.", "error")
            return None
        return device

    # ----- device choice ------------------------------------------------------------------------

    def _use_device(self) -> None:
        typed = self.device_edit.text().strip().lower()
        chosen = typed or (self.peer_combo.currentData() or "")
        if not chosen:
            self.choice_notice.set_text("Pick a connected peer, or paste its node id first.", "error")
            return
        if not is_node_id(chosen):
            self.choice_notice.set_text("A node id is 64 lowercase hex characters.", "error")
            return
        try:
            self.service.change_settings({"debug_device_id": chosen})
        except (SettingsError, OSError) as exc:
            self.choice_notice.set_text(f"Not changed: {exc}", "error")
            return
        self.device_edit.clear()
        self.choice_notice.set_text(f"Debugging {short_hex(chosen, 10, 6)} from now on.", "ok")

    def _ask_info(self) -> None:
        device = self._need_device(self.choice_notice)
        if device is None:
            return
        self.choice_notice.set_text("Asking the device...", "muted")
        self.run(self.service.node.debug_info(device), "ask the device for its information", self._info_done)

    def _info_done(self, ok: bool, value: Any) -> None:
        if not ok:
            self.choice_notice.set_text(f"The device did not answer: {value}", "error")
            return
        keys = ("node", "system", "machine", "python", "protocol", "log_source")
        lines = [f"{key.replace('_', ' ')}: {value[key]}" for key in keys if key in value]
        self.choice_notice.set_text("Device information. " + "; ".join(lines) + ".", "ok")

    # ----- connection ---------------------------------------------------------------------------

    def _connect_wifi(self) -> None:
        device = self._need_device(self.connection_notice)
        if device is None:
            return
        self.connection_notice.set_text("Connecting over Wi-Fi...", "muted")
        self.run(self.service.node.debug_connect_wifi(), "connect over Wi-Fi", self._connected("Wi-Fi"))

    def _scan_usb(self) -> None:
        self.run(self.service.node.debug_scan_usb(), "look for USB devices", self._scanned)

    def _scanned(self, ok: bool, value: Any) -> None:
        if ok:
            count = len(value)
            self.connection_notice.set_text(f"Found {count} USB device{'s' if count != 1 else ''}.", "ok")
        else:
            self.connection_notice.set_text(f"Could not look for USB devices: {value}", "error")

    def _connect_usb(self) -> None:
        device = self._need_device(self.connection_notice)
        if device is None:
            return
        row = self.usb_table.selected_row()
        if row < 0 or row >= len(self._usb_serials):
            self.connection_notice.set_text("Select a USB device in the table first.", "error")
            return
        serial = self._usb_serials[row]
        self.connection_notice.set_text(f"Connecting through {serial}...", "muted")
        self.run(self.service.node.debug_connect_usb(serial), "connect over USB", self._connected("USB"))

    def _connected(self, via: str):
        def done(ok: bool, value: Any) -> None:
            if ok:
                self.connection_notice.set_text(f"Connected over {via}. The route shows in the table above.", "ok")
            else:
                self.connection_notice.set_text(f"Could not connect over {via}: {value}", "error")

        return done

    # ----- log ----------------------------------------------------------------------------------

    def _start_log(self) -> None:
        device = self._need_device(self.log_notice)
        if device is None:
            return
        self.log_notice.set_text("Starting the log...", "muted")
        self.run(
            self.service.node.debug_logcat_start(device, self.log_filter.text().strip(), self._log_sink),
            "start the device's log",
            self._log_started,
        )

    def _log_started(self, ok: bool, value: Any) -> None:
        if ok:
            self._log_handle = int(value)
            self.log_notice.set_text("Reading the log. Stop it when you have what you need.", "ok")
        else:
            self.log_notice.set_text(f"The log did not start: {value}", "error")

    def _stop_log(self) -> None:
        if self._log_handle is None:
            return
        self.run(self.service.node.debug_stop(self._log_handle), "stop the log")
        self._log_handle = None
        self.log_notice.set_text("Log stopped.", "muted")

    def _on_log_message(self, message: Dict[str, Any]) -> None:
        kind = message.get("t")
        if kind == "line":
            self.log_view.appendPlainText(str(message.get("text", "")))
        elif kind == "drop":
            count = message.get("count", 0)
            self.log_view.appendPlainText(f"[{count} line{'s' if count != 1 else ''} skipped: the log came faster than it could be shown]")
        elif kind == "error":
            self.log_notice.set_text(str(message.get("text", "the log failed")), "error")

    # ----- shell --------------------------------------------------------------------------------

    def _run_shell(self) -> None:
        device = self._need_device(self.shell_notice)
        if device is None:
            return
        command = self.command.text().strip()
        if not command:
            self.shell_notice.set_text("Type a command first.", "error")
            return
        self.shell_notice.set_text("Running. If the device's user is asked, accept the request on the device or in Requests below.", "muted")
        self.run(
            self.service.node.debug_shell(device, command, float(self.shell_timeout.value())),
            "run a command on the device",
            self._shell_done,
        )

    def _shell_done(self, ok: bool, value: Any) -> None:
        if not ok:
            self.shell_notice.set_text(f"The command did not run: {value}", "error")
            return
        out = _text(value.get("stdout", b""))
        err = _text(value.get("stderr", b""))
        self.shell_output.setPlainText(out + (("\n--- stderr ---\n" + err) if err else ""))
        code = value.get("code")
        finished = f"exit code {code}" if code is not None else "no exit code"
        reason = value.get("reason") or "finished"
        self.shell_notice.set_text(f"Finished: {finished} ({reason}).", "ok" if code == 0 else "muted")

    # ----- forwarding and operations ------------------------------------------------------------

    def _start_forward(self) -> None:
        device = self._need_device(self.forward_notice)
        if device is None:
            return
        self.run(
            self.service.node.debug_forward_start(device, self.local_port.value(), self.device_port.value()),
            "start a port forward",
            self._forward_started,
        )

    def _forward_started(self, ok: bool, value: Any) -> None:
        if ok:
            self.forward_notice.set_text("Forwarding started. Its address and state are in Operations below.", "ok")
        else:
            self.forward_notice.set_text(f"Forwarding did not start: {value}", "error")

    def _stop_selected(self) -> None:
        row = self.streams.selected_row()
        if row < 0 or row >= len(self._stream_handles):
            self.operations_notice.set_text("Select an operation in the table first.", "error")
            return
        self.run(self.service.node.debug_stop(self._stream_handles[row]), "stop a debug operation", self._stopped)

    def _stopped(self, ok: bool, value: Any) -> None:
        if ok:
            self.operations_notice.set_text("Stopped.", "ok")
        else:
            self.operations_notice.set_text(f"Not stopped: {value}", "error")

    # ----- requests from computers --------------------------------------------------------------

    def _answer(self, accept: bool) -> None:
        row = self.requests.selected_row()
        if row < 0 or row >= len(self._request_ids):
            self.requests_notice.set_text("Select a request in the table first.", "error")
            return
        request_id = self._request_ids[row]
        node = self.service.node
        if accept:
            self.run(node.accept_request(request_id), "accept a debug request", self._answered(True))
        else:
            self.run(node.refuse_request(request_id), "refuse a debug request", self._answered(False))

    def _answered(self, accepted: bool):
        def done(ok: bool, value: Any) -> None:
            if ok:
                self.requests_notice.set_text("Accepted." if accepted else "Refused.", "ok")
            else:
                self.requests_notice.set_text(f"Not done: {value}", "error")

        return done
