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
    QLineEdit,
    QPushButton,
    QSpinBox,
    QWidget,
)

from transferd_gui.backend import capabilities
from transferd_gui.model.settings import THEMES, Settings, SettingsError
from transferd_gui.ui.pages.base import Page
from transferd_gui.ui.widgets import ActionRow, Banner, Card, Notice


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
