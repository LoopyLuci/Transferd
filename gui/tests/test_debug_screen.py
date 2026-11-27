"""The Debug screen and the debugging settings, offscreen, with the real window and service. The screen is checked against
snapshots that describe each state. One test runs a real action, asking an unconnected device for its information, through
the service, the node and the bridge back to the screen.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from test_qt_screens import build_window, empty_snapshot, pump
from transferd_gui.model.snapshot import (
    DebugSnapshot,
    DebugStreamSnapshot,
    IncomingRequest,
    RouteSnapshot,
    SessionSnapshot,
    UsbDeviceSnapshot,
)

COMPUTER = "c" * 64
DEVICE = "d" * 64


@pytest.fixture
def screens(qapp, tmp_path: Path):
    window, service = build_window(qapp, tmp_path)
    yield window, service, tmp_path
    service.stop()
    window.close()
    window.deleteLater()


def snapshot_with(folder: Path, **parts):
    return replace(empty_snapshot(folder / "snap"), **parts)


def connected_device(device_id: str, services, routes_up: bool = True) -> SessionSnapshot:
    route = RouteSnapshot(
        key="route-1",
        peer_id=device_id,
        link_id=1,
        kind="direct",
        up=routes_up,
        closed=not routes_up,
        label="usb:PHONE1234",
    )
    return SessionSnapshot(
        peer_id=device_id,
        peer_name="phone",
        state="ready",
        version=1,
        services=tuple(services),
        routes=(route,),
        established_at=0.0,
    )


# ----- the device side and the choice of device --------------------------------------------------


def test_with_no_device_chosen_the_screen_says_so_and_the_device_is_off(screens):
    window, _service, tmp = screens
    window._on_snapshot(empty_snapshot(tmp / "empty"))
    page = window.page("debug")
    assert page.device_pill.text() == "no device chosen"
    assert "Debugging is off on this device" in page.device_side.text()


def test_the_device_state_follows_the_session_to_this_device(screens):
    window, service, tmp = screens
    service.change_settings({"debug_device_id": DEVICE})
    page = window.page("debug")

    page.apply(snapshot_with(tmp / "none"))
    assert page.device_pill.text() == "not connected"

    page.apply(snapshot_with(tmp / "noinfo", sessions=(connected_device(DEVICE, ["file", "stream"]),)))
    assert page.device_pill.text() == "does not offer debugging"

    page.apply(snapshot_with(tmp / "down", sessions=(connected_device(DEVICE, ["debug-info"], routes_up=False),)))
    assert page.device_pill.text() == "no live route"

    page.apply(snapshot_with(tmp / "ready", sessions=(connected_device(DEVICE, ["debug-info", "debug-shell"]),)))
    assert page.device_pill.text() == "ready"
    assert page.routes.row_count() == 1
    assert page.routes.cell(0, 0) == "usb:PHONE1234"


def test_a_typed_device_id_is_saved_and_a_bad_one_is_refused(screens):
    window, service, _tmp = screens
    page = window.page("debug")

    page.device_edit.setText("xyz")
    page._use_device()
    assert "64 lowercase hex" in page.choice_notice.text()
    assert service.settings.debug_device_id == ""

    page.device_edit.setText(DEVICE.upper())
    page._use_device()
    assert service.settings.debug_device_id == DEVICE, "the id is saved in lowercase"


def test_actions_that_need_a_device_say_to_choose_one_first(screens):
    window, _service, _tmp = screens
    page = window.page("debug")
    page._ask_info()
    assert "Choose the device first" in page.choice_notice.text()


# ----- the lists -----------------------------------------------------------------------------------


def test_requests_list_only_debug_requests_and_accept_is_enabled_for_them(screens):
    window, _service, tmp = screens
    requests = (
        IncomingRequest(7, COMPUTER, "laptop", "debug-shell", 1_700_000_000.0),
        IncomingRequest(8, COMPUTER, "laptop", "file", 1_700_000_001.0),
    )
    page = window.page("debug")
    page.apply(snapshot_with(tmp / "requests", requests=requests))
    assert page.requests.row_count() == 1
    assert page.requests.cell(0, 1) == "shell"
    assert page._request_ids == [7]
    assert page.accept_button.isEnabled()


def test_operations_list_shows_each_stream_with_its_state_and_note(screens):
    window, _service, tmp = screens
    streams = (
        DebugStreamSnapshot(1, DEVICE, "logcat", "all lines", 0.0, "running", ""),
        DebugStreamSnapshot(2, DEVICE, "forward", "127.0.0.1:5000 -> device port 9", 0.0, "failed", "port 9 is not on this device's forward list"),
    )
    page = window.page("debug")
    page.apply(snapshot_with(tmp / "streams", debug=DebugSnapshot(agent_enabled=True, streams=streams)))
    assert page.streams.row_count() == 2
    assert page.streams.cell(0, 0) == "logcat"
    assert page.streams.cell(1, 3) == "failed"
    assert "forward list" in page.streams.cell(1, 4)
    assert page.stop_stream.isEnabled()


def test_usb_list_tells_the_user_what_to_do_about_each_phone(screens):
    window, _service, tmp = screens
    usb = (UsbDeviceSnapshot("PHONE1234", "device", "Pixel 8"), UsbDeviceSnapshot("OLD9999", "unauthorized", ""))
    page = window.page("debug")
    page.apply(snapshot_with(tmp / "usb", debug=DebugSnapshot(usb_state="ready", usb_devices=usb, usb_detail="1 device(s) ready over USB")))
    assert page.usb_table.row_count() == 2
    assert page.usb_table.cell(0, 3) == "ready"
    assert "allow USB debugging" in page.usb_table.cell(1, 3)


def test_a_selected_row_survives_an_update_with_the_same_rows(screens):
    window, _service, tmp = screens
    streams = (
        DebugStreamSnapshot(1, DEVICE, "logcat", "all lines", 0.0, "running", ""),
        DebugStreamSnapshot(2, DEVICE, "shell", "hi", 0.0, "running", ""),
    )
    page = window.page("debug")
    snap = snapshot_with(tmp / "stable", debug=DebugSnapshot(agent_enabled=True, streams=streams))
    page.apply(snap)
    page.streams.table.selectRow(1)
    page.apply(snap)
    assert page.streams.selected_row() == 1, "a once-a-second update must not clear the user's selection"


# ----- the log, the shell, forwarding -------------------------------------------------------------


def test_log_lines_drops_and_errors_reach_the_screen(screens):
    window, _service, _tmp = screens
    page = window.page("debug")
    page._on_log_message({"t": "line", "text": "hello from the device"})
    page._on_log_message({"t": "drop", "count": 3})
    assert "hello from the device" in page.log_view.toPlainText()
    assert "3 lines skipped" in page.log_view.toPlainText()
    page._on_log_message({"t": "error", "text": "boom"})
    assert page.log_notice.text() == "boom"


def test_a_shell_result_shows_its_output_and_exit_code(screens):
    window, _service, _tmp = screens
    page = window.page("debug")
    page._shell_done(True, {"code": 0, "reason": "", "stdout": b"hi 0\n", "stderr": b"warn\n"})
    assert "hi 0" in page.shell_output.toPlainText()
    assert "--- stderr ---" in page.shell_output.toPlainText()
    assert page.shell_notice.text() == "Finished: exit code 0 (finished)."


# ----- a real action through the service and the node ----------------------------------------------


def test_asking_an_unconnected_device_reports_it_through_the_real_node(screens, qapp):
    window, service, _tmp = screens
    service.change_settings({"debug_device_id": DEVICE})
    service.start()
    try:
        page = window.page("debug")
        page._ask_info()
        assert pump(qapp, lambda: "Asking the device" not in page.choice_notice.text(), timeout=20)
        assert "not connected" in page.choice_notice.text()
    finally:
        service.stop()


# ----- settings ------------------------------------------------------------------------------------


def test_the_debugging_settings_are_saved_together(screens):
    window, service, _tmp = screens
    page = window.page("settings")
    page.debug_enabled.setChecked(True)
    page.debug_clients.setText(COMPUTER)
    page.mode_boxes["shell"].setCurrentIndex(page.mode_boxes["shell"].findData("deny"))
    page.debug_forward_ports.setText("8080, 5555")
    page.debug_shell_timeout.setValue(90.0)
    page._save()
    saved = service.settings
    assert saved.debug_enabled is True
    assert saved.debug_clients == COMPUTER
    assert saved.debug_shell_mode == "deny"
    assert saved.debug_forward_ports == "8080, 5555"
    assert saved.debug_shell_timeout_s == 90.0


def test_an_invalid_debugging_setting_is_refused_and_nothing_is_saved(screens):
    window, service, _tmp = screens
    page = window.page("settings")
    page.debug_enabled.setChecked(True)
    page.debug_clients.setText("not-a-node-id")
    page._save()
    assert page.notice.text().startswith("Not saved")
    assert service.settings.debug_enabled is False
    assert service.settings.debug_clients == ""
