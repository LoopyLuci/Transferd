"""The Wormhole screen, offscreen, with two real windows. Each window has its own service, node and backend thread, and both
meet on a real mailbox server that runs on its own loop for the length of a test. Nothing is mocked: the sends, the codes,
the handshake and the answers all go through the page's buttons and the real session code.

Visual layout is not checked here; see gui/README.md.
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from pathlib import Path

import pytest
from PySide6.QtCore import QEvent

from support import free_port, make_settings
from transferd_gui.backend.service import TransferdService
from transferd_gui.backend.wormhole import NO_MAILBOX
from transferd_gui.model import telemetry as telemetry_model
from transferd_gui.ui.bridge import Bridge
from transferd_gui.ui.main_window import MainWindow
from transferd_gui.wormhole.addresses import display_fingerprint
from transferd_gui.wormhole.codes import generate, parse as parse_code
from transferd_gui.wormhole.server import MailboxServer

WAIT = 30.0
TEXT = "hello from the sender"


class MailboxHost:
    """The mailbox server, on a thread of its own with its own event loop."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, name="mailbox-host", daemon=True)
        self.server = MailboxServer()
        self.url = ""

    def start(self) -> None:
        self.thread.start()
        port = asyncio.run_coroutine_threadsafe(self.server.start("127.0.0.1", 0), self.loop).result(10)
        self.url = f"ws://127.0.0.1:{port}"

    def stop(self) -> None:
        asyncio.run_coroutine_threadsafe(self.server.close(), self.loop).result(10)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(10)
        self.loop.close()


def build_window(qapp, folder: Path, **changes):
    settings_path = folder / "anchor" / "settings.json"
    defaults = make_settings(folder / "app", **changes)
    bridge = Bridge()
    store = telemetry_model.TelemetryStore(600)
    service = TransferdService(settings_path, defaults, on_snapshot=bridge.snapshot_callback, on_log=bridge.log_callback)
    window = MainWindow(service, bridge, store)
    return window, service


def dispose(qapp, window) -> None:
    window.close()
    window.deleteLater()
    qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def pump(qapp, predicate, timeout: float = WAIT) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.02)
    qapp.processEvents()
    return predicate()


@pytest.fixture
def mailbox():
    host = MailboxHost()
    host.start()
    yield host
    host.stop()


@pytest.fixture
def pair(qapp, tmp_path, mailbox):
    """Two windows on one mailbox: the sender and the receiver. Each has a started service."""
    sides = {}
    for name in ("sender", "receiver"):
        window, service = build_window(qapp, tmp_path / name, wormhole_mailbox=mailbox.url)
        service.start()
        sides[name] = (window, service)
    yield sides["sender"], sides["receiver"]
    for window, service in sides.values():
        service.stop()
        dispose(qapp, window)


def wormhole_page(window):
    return window.page("wormhole")


# ----- availability -----------------------------------------------------------------------------


def test_without_a_mailbox_the_screen_says_so_and_nothing_can_start(qapp, tmp_path):
    window, service = build_window(qapp, tmp_path)
    try:
        page = wormhole_page(window)
        assert page.setup_notice.text() == NO_MAILBOX
        for button in (page.send_file_button, page.send_folder_button, page.send_text_button, page.look_up_button):
            assert button.isEnabled() is False
        assert page.code_input.isEnabled() is False
        assert page.text_input.isEnabled() is False
    finally:
        dispose(qapp, window)


def test_setting_a_mailbox_enables_the_screen_once_settings_are_saved(qapp, tmp_path):
    window, service = build_window(qapp, tmp_path)
    try:
        service.change_settings({"wormhole_mailbox": "wss://mailbox.example.net:4470"})
        window._settings_saved(service.settings)
        page = wormhole_page(window)
        assert page.send_text_button.isEnabled() is True
        assert "mailbox.example.net:4470" in page.setup_notice.text()
    finally:
        dispose(qapp, window)


def test_empty_inputs_are_refused_without_starting_anything(qapp, pair):
    (sender, _), _ = pair
    page = wormhole_page(sender)
    page.code_input.setText("")
    page.look_up_button.click()
    assert page.receive_status.text() == "Type the code first."
    page.text_input.setPlainText("   ")
    page.send_text_button.click()
    assert page.send_status.text() == "Type a text first."
    assert page.code_label.text() == ""


# ----- a text, end to end -----------------------------------------------------------------------


def test_a_text_sent_with_a_code_is_received_once_the_receiver_accepts(qapp, pair):
    (sender, _), (receiver, _) = pair
    sending, receiving = wormhole_page(sender), wormhole_page(receiver)

    sending.text_input.setPlainText(TEXT)
    sending.send_text_button.click()
    assert pump(qapp, lambda: sending.code_label.text() != "")
    code = sending.code_label.text()
    assert parse_code(code).nameplate >= 1

    receiving.code_input.setText(code)
    receiving.look_up_button.click()
    assert pump(qapp, lambda: receiving.accept_button.isEnabled() and receiving.offer_label.text() != "")
    assert receiving.offer_label.text() == f"A text of {len(TEXT.encode())} bytes is offered."

    receiving.accept_button.click()
    assert pump(qapp, lambda: not receiving.received_text.isHidden())
    assert receiving.received_text.toPlainText() == TEXT
    assert receiving.receive_status.text() == "Received a text."
    assert pump(qapp, lambda: sending.send_status.text() == "Sent.")
    assert sending.code_label.text() == ""
    assert sending.cancel_send_button.isEnabled() is False


# ----- refusals and stopping --------------------------------------------------------------------


def test_declining_tells_the_sender_and_writes_nothing(qapp, pair):
    (sender, _), (receiver, service) = pair
    sending, receiving = wormhole_page(sender), wormhole_page(receiver)

    sending.send_text("no thank you")
    assert pump(qapp, lambda: sending.code_label.text() != "")
    receiving.code_input.setText(sending.code_label.text())
    receiving.look_up_button.click()
    assert pump(qapp, lambda: receiving.decline_button.isEnabled())

    receiving.decline_button.click()
    assert pump(qapp, lambda: receiving.receive_status.text() == "Declined.")
    assert pump(qapp, lambda: sending.send_status.text().startswith("Declined by the other computer."))
    assert "the receiving user declined" in sending.send_status.text()
    download = Path(service.settings.download_dir)
    assert not download.exists() or not any(download.iterdir())


def test_stopping_while_the_offer_waits_declines_it_and_the_sender_is_told(qapp, pair):
    (sender, _), (receiver, _) = pair
    sending, receiving = wormhole_page(sender), wormhole_page(receiver)

    sending.send_text("stop me")
    assert pump(qapp, lambda: sending.code_label.text() != "")
    receiving.code_input.setText(sending.code_label.text())
    receiving.look_up_button.click()
    assert pump(qapp, lambda: receiving.decline_button.isEnabled())

    receiving.stop_receive_button.click()
    assert receiving.receive_status.text() == "Stopped. The offer was declined."
    assert pump(qapp, lambda: sending.send_status.text().startswith("Declined by the other computer."))
    assert "stopped by the receiving user" in sending.send_status.text()


def test_a_wrong_code_is_refused_on_both_sides(qapp, pair):
    (sender, _), (receiver, _) = pair
    sending, receiving = wormhole_page(sender), wormhole_page(receiver)

    sending.send_text(TEXT)
    assert pump(qapp, lambda: sending.code_label.text() != "")
    right = parse_code(sending.code_label.text())
    wrong = generate(right.nameplate, lambda n: bytes([7]) * n)
    assert str(wrong) != str(right)

    receiving.code_input.setText(str(wrong))
    receiving.look_up_button.click()
    assert pump(qapp, lambda: "wrong" in receiving.receive_status.text().lower())
    assert receiving.accept_button.isEnabled() is False
    assert pump(qapp, lambda: "wrong" in sending.send_status.text().lower())
    assert sending.code_label.text() == ""


# ----- cancelling -------------------------------------------------------------------------------


def test_cancelling_a_send_clears_the_code_and_a_new_send_can_start(qapp, pair):
    (sender, _), _ = pair
    page = wormhole_page(sender)

    page.send_text(TEXT)
    assert pump(qapp, lambda: page.code_label.text() != "")
    page.send_text("second, while the first is waiting")
    assert "already in progress" in page.send_status.text()

    page.cancel_send_button.click()
    assert page.code_label.text() == ""
    assert page.cancel_send_button.isEnabled() is False
    assert page.send_status.text().startswith("Cancelled.")

    page.send_text("after the cancel")
    assert pump(qapp, lambda: page.code_label.text() != "")
    assert page.send_status.text() == "Waiting for the other computer to type this code."
    page.cancel_send_button.click()
    assert page.code_label.text() == ""


# ----- the mailbox card and hosting -------------------------------------------------------------------


def test_hosting_on_this_computer_gives_a_working_mailbox_with_no_list(qapp, tmp_path):
    port = free_port()
    window, service = build_window(qapp, tmp_path)
    service.start()
    try:
        service.change_settings({"wormhole_host": True, "wormhole_host_port": port})
        page = wormhole_page(window)
        # The page reads the hosting state when the log reports it, so wait for the page rather than the backend.
        assert pump(qapp, lambda: page.setup_notice.text() == f"The mailbox is ws://127.0.0.1:{port}."), page.setup_notice.text()
        assert page.send_text_button.isEnabled() is True
        assert "for this computer only" in page.host_notice.text()
        assert page.share_edit.isHidden(), "a mailbox on this computer alone has no share line"
        page.send_text(TEXT)
        assert pump(qapp, lambda: page.code_label.text() != "")
        page.cancel_send_button.click()
    finally:
        service.stop()
        dispose(qapp, window)


def test_hosting_on_the_network_shows_the_fingerprint_and_the_share_line(qapp, tmp_path):
    port = free_port()
    window, service = build_window(qapp, tmp_path)
    service.start()
    # The app listens on every interface; this test keeps the mailbox to this computer.
    service.mailbox_host._network_bind = "127.0.0.1"
    try:
        service.change_settings({"wormhole_host": True, "wormhole_host_port": port, "wormhole_host_network": True})
        page = wormhole_page(window)
        assert pump(qapp, lambda: page.share_edit.text() != "")
        status = service.mailbox_host.status()
        assert display_fingerprint(status.fingerprint) in page.host_notice.text()
        assert page.share_edit.isHidden() is False
        share = page.share_edit.text()
        assert share.startswith("wss://") and f":{port}#" in share
        assert share.endswith("#" + status.fingerprint)
        assert page.setup_notice.text() == f"The mailbox is wss://127.0.0.1:{port}#{status.fingerprint}."
    finally:
        service.stop()
        dispose(qapp, window)


def test_turning_hosting_off_removes_the_share_line_and_the_mailbox(qapp, tmp_path):
    port = free_port()
    window, service = build_window(qapp, tmp_path)
    service.start()
    service.mailbox_host._network_bind = "127.0.0.1"
    try:
        service.change_settings({"wormhole_host": True, "wormhole_host_port": port, "wormhole_host_network": True})
        assert pump(qapp, lambda: service.mailbox_host.status().running)
        page = wormhole_page(window)
        service.change_settings({"wormhole_host": False})
        assert pump(qapp, lambda: not service.mailbox_host.status().running)
        assert pump(qapp, lambda: page.share_edit.isHidden())
        assert page.setup_notice.text() == NO_MAILBOX
        assert "not hosting a mailbox" in page.host_notice.text()
    finally:
        service.stop()
        dispose(qapp, window)


def test_a_port_already_in_use_is_shown_on_the_screen(qapp, tmp_path):
    port = free_port()
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", port))
    blocker.listen()
    window, service = build_window(qapp, tmp_path)
    service.start()
    try:
        service.change_settings({"wormhole_host": True, "wormhole_host_port": port})
        page = wormhole_page(window)
        assert pump(qapp, lambda: f"could not listen on port {port}" in page.host_notice.text())
        assert page.setup_notice.text() == NO_MAILBOX
    finally:
        blocker.close()
        service.stop()
        dispose(qapp, window)


def test_the_settings_page_saves_the_hosting_choice_and_refuses_a_bad_mailbox(qapp, tmp_path):
    port = free_port()
    window, service = build_window(qapp, tmp_path)
    service.start()
    try:
        settings_page = window.page("settings")
        settings_page.wormhole_mailbox.setText("ws://mailbox.example.net:4470")
        settings_page.save_button.click()
        assert "reaches only this computer" in settings_page.notice.text()
        assert service.settings.wormhole_mailbox == ""

        settings_page.wormhole_mailbox.setText("")
        settings_page.wormhole_host.setChecked(True)
        settings_page.wormhole_host_port.setValue(port)
        settings_page.save_button.click()
        assert service.settings.wormhole_host is True
        assert service.settings.wormhole_host_port == port
        assert pump(qapp, lambda: service.mailbox_host.status().running)
    finally:
        service.stop()
        dispose(qapp, window)
