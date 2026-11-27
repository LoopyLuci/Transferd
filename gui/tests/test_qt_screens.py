"""The screens, built offscreen. Every screen is checked in an empty state (no node data yet) and in a populated state
(a real loopback peer is connected, a file has moved and an invite has been made). Nothing here is mocked: the window,
the service and both nodes are the real code.

Offscreen means no window is shown on a display. Visual layout is not checked by these tests; see gui/README.md.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QLabel

from support import free_port, make_settings
from transferd_gui.backend import ghost
from transferd_gui.backend.ghost import GhostUnavailable
from transferd_gui.backend.node import Node
from transferd_gui.backend.service import TransferdService
from transferd_gui.model import telemetry as telemetry_model
from transferd_gui.ui import theme
from transferd_gui.ui.bridge import Bridge
from transferd_gui.ui.charts import LineChart
from transferd_gui.ui.main_window import NAVIGATION, MainWindow
from transferd_gui.ui.pages.dashboard import SESSION_HEADERS
from transferd_gui.ui.pages.peers import HEADERS as PEER_HEADERS
from transferd_gui.ui.status import relay_status
from transferd_gui.ui.widgets import Banner, CopyField, TableWithEmpty

EXPECTED_KEYS = [page_class.key for _, page_classes in NAVIGATION for page_class in page_classes]
EXPECTED_SECTIONS = ["OVERVIEW", "NETWORK", "TRANSFER", "TOOLS", "DEBUG", "SYSTEM"]


def build_window(qapp, folder: Path, **changes):
    """A real main window over a real service. The service is not started: callers start it when they need the node."""
    settings_path = folder / "anchor" / "settings.json"
    defaults = make_settings(folder / "app", **changes)
    bridge = Bridge()
    store = telemetry_model.TelemetryStore(600)
    service = TransferdService(settings_path, defaults, on_snapshot=bridge.snapshot_callback, on_log=bridge.log_callback)
    window = MainWindow(service, bridge, store)
    return window, service


def dispose(qapp, win) -> None:
    """Close a window and destroy it now. A theme change re-polishes every window that still exists, so windows left
    alive make each later test slower than the one before it."""
    win.close()
    win.deleteLater()
    qapp.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def pump(qapp, predicate, timeout: float = 20.0) -> bool:
    """Process Qt events (signals from the backend thread arrive this way) until `predicate` holds or time runs out."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.02)
    qapp.processEvents()
    return predicate()


def empty_snapshot(folder: Path):
    """The snapshot of a node that has not started. This is what the window sees before the node is up."""
    return Node(make_settings(folder), lambda *_args: None).snapshot()


@pytest.fixture
def window(qapp, tmp_path):
    win, _service = build_window(qapp, tmp_path)
    yield win
    dispose(qapp, win)


# ----- navigation and layout --------------------------------------------------------------------


def test_sidebar_lists_every_screen_under_its_section_in_the_documented_order(window):
    sections, keys = [], []
    for row in range(window.nav.count()):
        item = window.nav.item(row)
        key = item.data(Qt.ItemDataRole.UserRole)
        if key is None:
            sections.append(item.text())
        else:
            keys.append(key)
    assert sections == EXPECTED_SECTIONS
    assert keys == EXPECTED_KEYS
    assert window.page_keys() == EXPECTED_KEYS


def test_selecting_a_sidebar_entry_shows_that_screen(window):
    for row in range(window.nav.count()):
        item = window.nav.item(row)
        key = item.data(Qt.ItemDataRole.UserRole)
        if key is None:
            continue
        window.nav.setCurrentRow(row)
        assert window.stack.currentWidget() is window.page(key)


def test_every_screen_has_a_title_that_matches_the_sidebar(window):
    for row in range(window.nav.count()):
        item = window.nav.item(row)
        key = item.data(Qt.ItemDataRole.UserRole)
        if key is not None:
            assert item.text() == window.page(key).title
    assert len(EXPECTED_KEYS) == 13


# ----- empty state of every screen --------------------------------------------------------------


@pytest.mark.parametrize("key", EXPECTED_KEYS)
def test_empty_state_of_each_screen(window, tmp_path, key):
    window._on_snapshot(empty_snapshot(tmp_path / "empty"))
    page = window.page(key)
    page.on_show()
    for table in page.findChildren(TableWithEmpty):
        assert table.row_count() == 0
        assert not table.empty.isHidden(), "the empty-state text should show when there are no rows"
    for chart in page.findChildren(LineChart):
        assert chart.has_data() is False


def test_identity_says_the_node_is_starting_before_it_has_an_identity(window, tmp_path):
    window._on_snapshot(empty_snapshot(tmp_path / "empty"))
    page = window.page("identity")
    assert page.name.text() == "Starting the node, so no identity yet."
    assert page.node_id.full_value == ""
    assert window.node_label.text() == "starting"


def test_dashboard_has_no_charted_data_and_no_sessions_when_empty(window, tmp_path):
    window._on_snapshot(empty_snapshot(tmp_path / "empty"))
    page = window.page("dashboard")
    assert page.sessions.row_count() == 0
    assert page.routes.row_count() == 0
    assert page.rtt_chart.has_data() is False
    assert page.traffic_chart.has_data() is False


def test_dns_controls_are_visible_but_disabled_with_the_reason(window):
    page = window.page("dns")
    assert page.name.isEnabled() is False
    assert page.kind.isEnabled() is False
    assert page.lookup_button.isEnabled() is False
    assert page.findChildren(Banner), "the DNS screen must explain why its controls are disabled"
    assert any(label.text() == Banner.HEADING for label in page.findChildren(QLabel))


def test_hole_punch_start_is_visible_but_disabled(window, tmp_path):
    window._on_snapshot(empty_snapshot(tmp_path / "empty"))
    page = window.page("punch")
    assert page.start_button.isEnabled() is False
    assert page.findChildren(Banner)


def test_ghost_register_is_visible_but_disabled_with_a_banner(window):
    page = window.page("ghost")
    page.on_show()
    assert page.register_button.isEnabled() is False
    assert page.findChildren(Banner)


def test_settings_shows_the_three_options_this_build_cannot_change(window):
    page = window.page("settings")
    banners = page.findChildren(Banner)
    assert len(banners) == 3
    assert page.data_dir.text() == window.service.settings.data_dir


def test_transfer_screen_says_to_connect_first_and_has_no_peer_selected(window):
    page = window.page("transfer")
    assert page.selected_peer() is None
    assert page.requests.row_count() == 0
    assert page.history.row_count() == 0


# ----- settings ---------------------------------------------------------------------------------


def test_settings_refuses_an_invalid_value_and_writes_nothing(window):
    page = window.page("settings")
    page.data_dir.setText("relative/folder")
    page.save_button.click()
    assert "Not saved:" in page.notice.text()
    assert "absolute" in page.notice.text()
    assert not window.service.settings_path.exists()


def test_settings_saves_a_valid_change_and_applies_the_theme(qapp, window, tmp_path):
    page = window.page("settings")
    page.data_dir.setText(str(tmp_path / "chosen" / "data"))
    page.download_dir.setText(str(tmp_path / "chosen" / "received"))
    page.theme.setCurrentText("dark")
    try:
        page.save_button.click()
        assert page.notice.text() == "Saved."
        assert window.service.settings_path.exists()
        assert window.service.settings.theme == "dark"
        assert theme.current_tokens() is theme.DARK
    finally:
        theme.apply(qapp, "light")


# ----- events -----------------------------------------------------------------------------------


def test_events_filter_shows_only_the_chosen_levels(window):
    window.service.change_settings({"theme": "system"})
    window.service.run(_idle(), "idle action")
    page = window.page("events")
    page._refresh()
    assert page.table.row_count() == 2
    page.filter.setCurrentIndex(2)  # Errors only
    assert page.table.row_count() == 1
    assert page.table.cell(0, 1) == "error"
    page.filter.setCurrentIndex(1)  # Warnings and errors
    assert page.table.row_count() == 1


def test_events_clear_log_empties_the_table(window):
    window.service.change_settings({"theme": "system"})
    page = window.page("events")
    page._refresh()
    assert page.table.row_count() >= 1
    page.clear_button.click()
    assert page.table.row_count() == 0


async def _idle():
    return None


# ----- ghost keys -------------------------------------------------------------------------------


def test_ghost_screen_shows_unavailable_and_disables_its_controls_when_the_module_is_missing(window, monkeypatch):
    def missing():
        raise GhostUnavailable("the ghost key module is missing from python/transferd")

    monkeypatch.setattr(ghost, "load", missing)
    page = window.page("ghost")
    page.on_show()
    assert page.unavailable.isHidden() is False
    assert any("module is missing" in label.text() for label in page.unavailable.findChildren(QLabel))
    for button in (page.create_button, page.save_issuer_button, page.request_button, page.finish_button, page.reset_button):
        assert button.isEnabled() is False


# ----- theme and copy ---------------------------------------------------------------------------


def test_light_and_dark_tokens_share_every_name_and_applying_switches_them(qapp):
    assert set(theme.LIGHT) == set(theme.DARK)
    try:
        assert theme.apply(qapp, "dark") is theme.DARK
        assert theme.current_tokens() is theme.DARK
        assert theme.apply(qapp, "light") is theme.LIGHT
        assert theme.current_tokens() is theme.LIGHT
        assert theme.resolve("system", True) == "dark"
        assert theme.resolve("system", False) == "light"
    finally:
        theme.apply(qapp, "light")


def test_copy_field_puts_the_full_value_on_the_clipboard(qapp):
    field = CopyField("Node id", head=8, tail=4)
    value = "ab" * 32
    field.set_value(value)
    assert field.full_value == value
    field.copy()
    assert QGuiApplication.clipboard().text() == value


# ----- populated state: a real peer -------------------------------------------------------------


def test_every_screen_shows_real_data_once_a_peer_is_connected(qapp, tmp_path):
    port = free_port()
    window, service = build_window(
        qapp,
        tmp_path / "a",
        allow_incoming=True,
        listen_port=port,
        advertised_address=f"127.0.0.1:{port}",
    )
    peer = TransferdService(
        tmp_path / "peer" / "anchor" / "settings.json",
        make_settings(tmp_path / "peer", auto_accept_files=True),
    )
    service.start()
    peer.start()
    try:
        assert pump(qapp, lambda: service.latest() is not None and service.latest().identity.ready)
        host = service.latest()
        assert host.listener.port == port
        host_id = host.identity.node_id

        dialled = threading.Event()
        results = []
        peer.run(
            peer.node.dial_direct("127.0.0.1", port, expect_node_id=host_id),
            "dial the window node",
            lambda ok, value: (results.append((ok, value)), dialled.set()),
        )
        assert dialled.wait(20)
        assert results == [(True, host_id)]
        peer_id = peer.node.identity.node_id

        dashboard = window.page("dashboard")
        assert pump(qapp, lambda: dashboard.sessions.row_count() == 1)
        assert any(peer_id[:10] in dashboard.sessions.cell(0, c) for c in range(len(SESSION_HEADERS)))
        assert dashboard.node_id.full_value == host_id
        assert pump(qapp, lambda: dashboard.routes.row_count() >= 1)
        assert pump(qapp, lambda: dashboard.traffic_chart.has_data(), timeout=20.0)
        assert window.peers_label.text() == "1 peer ready"

        identity = window.page("identity")
        assert identity.node_id.full_value == host_id
        assert identity.fingerprint.full_value
        assert identity.name.text().startswith("Name: ")

        relay_text, _tone = relay_status(host.relay)
        assert window.page("relay").pill.text() == relay_text

        peers = window.page("peers")
        assert peers.table.row_count() == 1
        assert any(peer_id[:10] in peers.table.cell(0, c) for c in range(len(PEER_HEADERS)))

        source = tmp_path / "hello.txt"
        source.write_bytes(b"hello from the window node\n" * 80)
        sent = threading.Event()
        outcome = []
        service.run(
            service.node.send_file(peer_id, str(source)),
            "send the file",
            lambda ok, value: (outcome.append(ok), sent.set()),
        )
        assert sent.wait(20)
        assert outcome == [True]
        received = Path(peer.settings.download_dir) / "hello.txt"
        assert pump(qapp, lambda: received.exists() and received.stat().st_size == source.stat().st_size)
        assert received.read_bytes() == source.read_bytes()

        transfer = window.page("transfer")
        assert transfer.selected_peer() == peer_id
        assert pump(qapp, lambda: transfer.history.row_count() >= 1)
        assert any(transfer.history.cell(r, 4) == "hello.txt" for r in range(transfer.history.row_count()))
        assert transfer.requests.row_count() == 0

        invites = window.page("invites")
        invites.make_button.click()
        assert pump(qapp, lambda: invites.output.toPlainText() != "")
        assert invites.output.toPlainText().strip()

        punch = window.page("punch")
        assert punch.start_button.isEnabled() is False

        ghost_page = window.page("ghost")
        service.ghost().create_key()
        ghost_page.refresh()
        assert ghost_page.key_public.full_value != ""
        assert ghost_page.create_button.isEnabled() is False

        events = window.page("events")
        assert pump(qapp, lambda: events.table.row_count() >= 1)

        settings_page = window.page("settings")
        assert settings_page.data_dir.text() == service.settings.data_dir
        assert len(settings_page.findChildren(Banner)) == 3
    finally:
        dispose(qapp, window)
        peer.stop()
