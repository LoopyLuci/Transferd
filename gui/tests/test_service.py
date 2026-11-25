"""The service facade: the node runs on its own thread, settings are checked before they are saved, and failures of node
actions reach the event log and are handed back to the caller."""

from __future__ import annotations

import json
import threading

import pytest

from support import make_settings, wait_until
from transferd_gui.backend.node import NodeError
from transferd_gui.backend.service import TransferdService
from transferd_gui.model.settings import SettingsError, load_settings


def make_service(tmp_path, snapshots=None):
    settings_path = tmp_path / "anchor" / "settings.json"
    defaults = make_settings(tmp_path / "app")
    seen = snapshots.append if snapshots is not None else None
    service = TransferdService(settings_path, defaults, on_snapshot=seen)
    return service, settings_path, defaults


def test_start_publishes_a_snapshot_with_a_loaded_identity_and_stop_ends_the_backend(tmp_path):
    snapshots = []
    service, _, _ = make_service(tmp_path, snapshots=snapshots)
    service.start()
    try:
        assert service.running
        assert wait_until(lambda: service.latest() is not None, timeout=10)
        snap = service.latest()
        assert snap.identity.ready
        assert len(snap.identity.node_id) == 64
        assert snapshots, "the snapshot callback was not called"
    finally:
        service.stop()
    assert not service.running


def test_change_settings_saves_the_file_and_applies_it(tmp_path):
    service, path, defaults = make_service(tmp_path)
    service.change_settings({"theme": "dark", "event_log_size": 200})
    assert load_settings(path, defaults).theme == "dark"
    assert service.settings.theme == "dark"
    assert service.settings.event_log_size == 200
    assert any(entry.message == "settings saved" for entry in service.log_entries())


def test_invalid_settings_raise_and_the_saved_file_is_left_unchanged(tmp_path):
    service, path, _ = make_service(tmp_path)
    service.change_settings({"theme": "light"})
    before = path.read_bytes()
    with pytest.raises(SettingsError, match="listen_port"):
        service.change_settings({"listen_port": 70000, "theme": "dark"})
    assert path.read_bytes() == before
    assert service.settings.theme == "light"


def test_a_valid_save_clears_a_problem_left_by_an_invalid_file(tmp_path):
    path = tmp_path / "anchor" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"data_dir": "relative/path", "download_dir": "x", "listen_port": 99999}), encoding="utf-8")
    service = TransferdService(path, make_settings(tmp_path / "app"))
    assert service.settings_problem
    service.change_settings({"theme": "dark"})
    assert service.settings_problem == ""


def test_an_invalid_saved_file_is_reported_and_the_defaults_are_used(tmp_path):
    path = tmp_path / "anchor" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"data_dir": "relative/path", "download_dir": "x", "listen_port": 99999}), encoding="utf-8")
    original = path.read_text(encoding="utf-8")
    service = TransferdService(path, make_settings(tmp_path / "app"))
    assert service.settings_problem.startswith("Saved settings were not used")
    assert service.settings.listen_port == 0
    assert path.read_text(encoding="utf-8") == original


def test_a_failed_node_action_is_logged_and_handed_back(tmp_path):
    service, _, _ = make_service(tmp_path)
    service.start()
    try:
        results = []
        done = threading.Event()

        async def refused():
            raise NodeError("the peer said no")

        service.run(refused(), "test action", lambda ok, value: (results.append((ok, value)), done.set()))
        assert done.wait(10)
        assert results == [(False, "the peer said no")]
        assert any(e.level == "error" and e.message == "test action: the peer said no" for e in service.log_entries())
    finally:
        service.stop()


def test_an_unexpected_failure_is_logged_with_its_message(tmp_path):
    service, _, _ = make_service(tmp_path)
    service.start()
    try:
        results = []
        done = threading.Event()

        async def broken():
            raise RuntimeError("boom")

        service.run(broken(), "test other", lambda ok, value: (results.append((ok, value)), done.set()))
        assert done.wait(10)
        assert results[0][0] is False
        assert any(e.level == "error" and e.message.startswith("test other failed:") for e in service.log_entries())
    finally:
        service.stop()


def test_a_successful_node_action_hands_back_its_value(tmp_path):
    service, _, _ = make_service(tmp_path)
    service.start()
    try:
        results = []
        done = threading.Event()

        async def answer():
            return 42

        service.run(answer(), "test answer", lambda ok, value: (results.append((ok, value)), done.set()))
        assert done.wait(10)
        assert results == [(True, 42)]
    finally:
        service.stop()


def test_a_node_action_on_a_stopped_backend_is_refused_and_logged(tmp_path):
    service, _, _ = make_service(tmp_path)

    async def idle():
        return 1

    assert service.run(idle(), "idle action") is None
    assert any(e.message == "idle action: the backend is not running" for e in service.log_entries())
