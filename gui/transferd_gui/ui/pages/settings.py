"""Settings: the values the GUI keeps between runs. Invalid values are refused with a message and nothing is saved."""

from __future__ import annotations

from typing import Any, Dict

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QWidget,
)

from transferd_gui.backend import capabilities
from transferd_gui.debug.policy import MODES
from transferd_gui.model.settings import DEBUG_TRANSPORTS, THEMES, Settings, SettingsError
from transferd_gui.ui.pages.base import Page
from transferd_gui.ui.pages.debug import TRANSPORT_LABELS
from transferd_gui.ui.widgets import ActionRow, Banner, Card, Notice

MODE_LABELS = {"allow": "Allow", "ask": "Ask the device's user", "deny": "Deny"}


class SettingsPage(Page):
    key = "settings"
    title = "Settings"
    explainer = (
        "Values saved between runs. A value that does not check out is refused with a message, and nothing is saved until "
        "every value is valid. Unavailable options are shown disabled, with the reason."
    )

    def build(self) -> None:
        form_card = Card("Node and folders")
        form = QFormLayout()
        self.node_name = QLineEdit()
        self.data_dir = QLineEdit()
        self.data_browse = QPushButton("Browse...")
        self.data_browse.clicked.connect(lambda: self._browse(self.data_dir))
        self.download_dir = QLineEdit()
        self.download_browse = QPushButton("Browse...")
        self.download_browse.clicked.connect(lambda: self._browse(self.download_dir))
        form.addRow("Node name", self.node_name)
        form.addRow("Data directory", self._with_button(self.data_dir, self.data_browse))
        form.addRow("Download folder", self._with_button(self.download_dir, self.download_browse))
        form_card.body.addLayout(form)
        self.content.addWidget(form_card)

        network = Card("Network")
        net = QFormLayout()
        self.relay_address = QLineEdit()
        self.relay_address.setPlaceholderText("relay.example.net:7000")
        self.advertised = QLineEdit()
        self.advertised.setPlaceholderText("address peers can reach this node at (for invites)")
        self.listen_port = QSpinBox()
        self.listen_port.setRange(0, 65535)
        self.listen_port.setSpecialValueText("any free port")
        self.allow_incoming = QCheckBox("Accept incoming connections")
        self.auto_accept = QCheckBox("Accept incoming files without asking")
        net.addRow("Relay address", self.relay_address)
        net.addRow("Advertised address", self.advertised)
        net.addRow("Listen port", self.listen_port)
        net.addRow("", self.allow_incoming)
        net.addRow("", self.auto_accept)
        network.body.addLayout(net)
        self.content.addWidget(network)

        timing = Card("Timeouts, limits and display")
        form2 = QFormLayout()
        self.relay_timeout = QDoubleSpinBox()
        self.relay_timeout.setRange(0.5, 60.0)
        self.relay_timeout.setSuffix(" s")
        self.connect_timeout = QDoubleSpinBox()
        self.connect_timeout.setRange(1.0, 120.0)
        self.connect_timeout.setSuffix(" s")
        self.max_mib = QSpinBox()
        self.max_mib.setRange(1, 512)
        self.max_mib.setSuffix(" MiB")
        self.theme = QComboBox()
        self.theme.addItems(list(THEMES))
        self.log_size = QSpinBox()
        self.log_size.setRange(50, 10000)
        self.window_s = QSpinBox()
        self.window_s.setRange(60, 3600)
        self.window_s.setSuffix(" s")
        form2.addRow("Relay request timeout", self.relay_timeout)
        form2.addRow("Peer connect timeout", self.connect_timeout)
        form2.addRow("Transfer limit", self.max_mib)
        form2.addRow("Theme", self.theme)
        form2.addRow("Event log size", self.log_size)
        form2.addRow("Chart history", self.window_s)
        timing.body.addLayout(form2)
        self.content.addWidget(timing)

        debugging = Card("Debugging: what a listed computer may do with this device")
        debugging.body.addWidget(QLabel(
            "Off until you turn it on. Only the computers listed here can debug this device. For each kind of operation, "
            "allow it, ask this device's user each time, or deny it."
        ))
        device_form = QFormLayout()
        self.debug_enabled = QCheckBox("Let listed computers debug this device")
        self.debug_clients = QLineEdit()
        self.debug_clients.setPlaceholderText("computer node ids, separated by commas (64 hex characters each)")
        self.mode_boxes: Dict[str, QComboBox] = {}
        for op, label in (
            ("info", "Device information"),
            ("logcat", "Log"),
            ("shell", "Commands"),
            ("forward", "Port forwarding"),
        ):
            box = QComboBox()
            for mode in MODES:
                box.addItem(MODE_LABELS[mode], mode)
            self.mode_boxes[op] = box
            device_form.addRow(label, box)
        self.debug_forward_ports = QLineEdit()
        self.debug_forward_ports.setPlaceholderText("ports computers may forward to, e.g. 8080, 5555")
        self.debug_log_command = QLineEdit()
        self.debug_log_command.setPlaceholderText("command that prints the log, e.g. logcat -v time")
        self.debug_shell_timeout = QDoubleSpinBox()
        self.debug_shell_timeout.setRange(1.0, 600.0)
        self.debug_shell_timeout.setSuffix(" s")
        self.debug_shell_output = QSpinBox()
        self.debug_shell_output.setRange(16, 16384)
        self.debug_shell_output.setSuffix(" KiB")
        self.debug_streams = QSpinBox()
        self.debug_streams.setRange(1, 64)
        device_form.addRow("", self.debug_enabled)
        device_form.addRow("Listed computers", self.debug_clients)
        device_form.addRow("Forward ports", self.debug_forward_ports)
        device_form.addRow("Log command", self.debug_log_command)
        device_form.addRow("Command time limit", self.debug_shell_timeout)
        device_form.addRow("Command output kept", self.debug_shell_output)
        device_form.addRow("Operations open per computer", self.debug_streams)
        debugging.body.addLayout(device_form)
        self.content.addWidget(debugging)

        this_computer = Card("Debugging from this computer")
        this_form = QFormLayout()
        self.debug_device_id = QLineEdit()
        self.debug_device_id.setPlaceholderText("node id of the device to debug (64 hex characters)")
        self.debug_transport = QComboBox()
        for transport in DEBUG_TRANSPORTS:
            self.debug_transport.addItem(TRANSPORT_LABELS[transport], transport)
        self.debug_wifi = QLineEdit()
        self.debug_wifi.setPlaceholderText("device address for Wi-Fi, host:port")
        self.usb_adb = QLineEdit()
        self.usb_adb.setPlaceholderText("adb")
        self.usb_port = QSpinBox()
        self.usb_port.setRange(0, 65535)
        self.usb_port.setSpecialValueText("not set")
        self.usb_port.setToolTip("The port the device's Transferd node listens on, as the phone sees it.")
        this_form.addRow("Device", self.debug_device_id)
        this_form.addRow("Transport", self.debug_transport)
        this_form.addRow("Wi-Fi address", self.debug_wifi)
        this_form.addRow("adb program", self.usb_adb)
        this_form.addRow("Device port (USB)", self.usb_port)
        this_computer.body.addLayout(this_form)
        self.content.addWidget(this_computer)

        wormhole = Card("Wormhole: send a file or text with a code")
        wormhole_form = QFormLayout()
        self.wormhole_mailbox = QLineEdit()
        self.wormhole_mailbox.setPlaceholderText("wss://mailbox.example.net:4470")
        self.wormhole_mailbox.setToolTip("Empty keeps wormholes off. The mailbox sees nameplates and timing, never a code, key or file.")
        self.wormhole_streams = QSpinBox()
        self.wormhole_streams.setRange(1, 8)
        self.wormhole_streams.setToolTip("How many parallel streams a file is spread over on the overlay.")
        wormhole_form.addRow("Mailbox server", self.wormhole_mailbox)
        wormhole_form.addRow("Streams per file", self.wormhole_streams)
        wormhole.body.addLayout(wormhole_form)
        wormhole.body.addWidget(Notice("The relay and the download folder above are used for wormholes too.", "muted"))
        self.content.addWidget(wormhole)

        unavailable = Card("Options this build cannot change")
        unavailable.body.addWidget(Banner(capabilities.reason(capabilities.CUSTOM_RELAY_TOKEN)))
        unavailable.body.addWidget(Banner(capabilities.reason(capabilities.RELAY_UDP_PORT)))
        unavailable.body.addWidget(Banner(capabilities.reason(capabilities.SESSION_TIMEOUTS)))
        self.content.addWidget(unavailable)

        self.save_button = QPushButton("Save")
        self.save_button.setObjectName("primary")
        self.save_button.clicked.connect(self._save)
        self.revert_button = QPushButton("Revert")
        self.revert_button.clicked.connect(lambda: self._fill(self.service.settings))
        self.content.addWidget(ActionRow(self.save_button, self.revert_button))
        self.notice = Notice()
        self.content.addWidget(self.notice)
        self._fill(self.service.settings)
        if self.service.settings_problem:
            self.notice.set_text(self.service.settings_problem, "error")

    @staticmethod
    def _with_button(field: QLineEdit, button: QPushButton) -> QWidget:
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(field, 1)
        row.addWidget(button)
        box = QWidget()
        box.setLayout(row)
        return box

    def _fill(self, settings: Settings) -> None:
        self.node_name.setText(settings.node_name)
        self.data_dir.setText(settings.data_dir)
        self.download_dir.setText(settings.download_dir)
        self.relay_address.setText(settings.relay_address)
        self.advertised.setText(settings.advertised_address)
        self.listen_port.setValue(settings.listen_port)
        self.allow_incoming.setChecked(settings.allow_incoming)
        self.auto_accept.setChecked(settings.auto_accept_files)
        self.relay_timeout.setValue(settings.relay_request_timeout)
        self.connect_timeout.setValue(settings.peer_connect_timeout)
        self.max_mib.setValue(settings.max_transfer_mib)
        self.theme.setCurrentText(settings.theme)
        self.log_size.setValue(settings.event_log_size)
        self.window_s.setValue(settings.telemetry_window_s)
        self.debug_enabled.setChecked(settings.debug_enabled)
        self.debug_clients.setText(settings.debug_clients)
        for op, box in self.mode_boxes.items():
            self._set_combo(box, getattr(settings, f"debug_{op}_mode"))
        self.debug_forward_ports.setText(settings.debug_forward_ports)
        self.debug_log_command.setText(settings.debug_log_command)
        self.debug_shell_timeout.setValue(settings.debug_shell_timeout_s)
        self.debug_shell_output.setValue(settings.debug_shell_output_kib)
        self.debug_streams.setValue(settings.debug_streams_per_client)
        self.debug_device_id.setText(settings.debug_device_id)
        self._set_combo(self.debug_transport, settings.debug_transport)
        self.debug_wifi.setText(settings.debug_wifi_address)
        self.usb_adb.setText(settings.usb_adb_path)
        self.usb_port.setValue(settings.usb_device_port)
        self.wormhole_mailbox.setText(settings.wormhole_mailbox)
        self.wormhole_streams.setValue(settings.wormhole_streams)

    @staticmethod
    def _set_combo(box: QComboBox, value: str) -> None:
        box.setCurrentIndex(max(box.findData(value), 0))

    def _collect(self) -> Dict[str, Any]:
        return {
            "node_name": self.node_name.text().strip(),
            "data_dir": self.data_dir.text().strip(),
            "download_dir": self.download_dir.text().strip(),
            "relay_address": self.relay_address.text().strip(),
            "advertised_address": self.advertised.text().strip(),
            "listen_port": self.listen_port.value(),
            "allow_incoming": self.allow_incoming.isChecked(),
            "auto_accept_files": self.auto_accept.isChecked(),
            "relay_request_timeout": float(self.relay_timeout.value()),
            "peer_connect_timeout": float(self.connect_timeout.value()),
            "max_transfer_mib": self.max_mib.value(),
            "theme": self.theme.currentText(),
            "event_log_size": self.log_size.value(),
            "telemetry_window_s": self.window_s.value(),
            "debug_enabled": self.debug_enabled.isChecked(),
            "debug_clients": self.debug_clients.text().strip(),
            "debug_info_mode": self.mode_boxes["info"].currentData(),
            "debug_logcat_mode": self.mode_boxes["logcat"].currentData(),
            "debug_shell_mode": self.mode_boxes["shell"].currentData(),
            "debug_forward_mode": self.mode_boxes["forward"].currentData(),
            "debug_forward_ports": self.debug_forward_ports.text().strip(),
            "debug_log_command": self.debug_log_command.text().strip(),
            "debug_shell_timeout_s": float(self.debug_shell_timeout.value()),
            "debug_shell_output_kib": self.debug_shell_output.value(),
            "debug_streams_per_client": self.debug_streams.value(),
            "debug_device_id": self.debug_device_id.text().strip().lower(),
            "debug_transport": self.debug_transport.currentData(),
            "debug_wifi_address": self.debug_wifi.text().strip(),
            "usb_adb_path": self.usb_adb.text().strip(),
            "usb_device_port": self.usb_port.value(),
            "wormhole_mailbox": self.wormhole_mailbox.text().strip(),
            "wormhole_streams": self.wormhole_streams.value(),
        }

    def _save(self) -> None:
        try:
            saved = self.service.change_settings(self._collect())
        except SettingsError as exc:
            lines = [f"- {problem}" for problem in exc.problems]
            self.notice.set_text("\n".join(["Not saved:", *lines]), "error")
            return
        except OSError as exc:
            self.notice.set_text(f"Not saved: the settings file could not be written ({exc.strerror or exc}).", "error")
            return
        self.notice.set_text("Saved.", "ok")
        self.context.settings_saved(saved)

    def on_settings(self, settings: Settings) -> None:
        self._fill(settings)

    def _browse(self, field: QLineEdit) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose a folder", field.text() or "")
        if folder:
            field.setText(folder)
