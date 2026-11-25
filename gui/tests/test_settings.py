"""Settings: validation messages, persistence, and the rule that an invalid file is never overwritten."""

import json

import pytest

from transferd_gui.model.settings import (
    Settings,
    SettingsError,
    load_settings,
    parse_host_port,
    save_settings,
    updated,
    validate,
)
from support import make_settings


def valid_raw(tmp_path):
    from dataclasses import asdict

    return asdict(make_settings(tmp_path))


def test_defaults_are_valid(tmp_path):
    settings = make_settings(tmp_path)
    assert validate(valid_raw(tmp_path)) == settings
    assert settings.allow_incoming is False
    assert settings.theme == "system"


def test_each_bad_value_is_named_in_the_message(tmp_path):
    raw = valid_raw(tmp_path)
    raw.update(listen_port=70000, theme="purple", relay_address="no-port", allow_incoming="yes")
    with pytest.raises(SettingsError) as info:
        validate(raw)
    text = " ".join(info.value.problems)
    assert "listen_port" in text
    assert "theme" in text
    assert "relay_address" in text
    assert "allow_incoming" in text


def test_unknown_setting_is_refused(tmp_path):
    raw = valid_raw(tmp_path)
    raw["fancy"] = True
    with pytest.raises(SettingsError, match="unknown setting 'fancy'"):
        validate(raw)


def test_paths_must_be_absolute(tmp_path):
    raw = valid_raw(tmp_path)
    raw["download_dir"] = "relative/folder"
    with pytest.raises(SettingsError, match="download_dir must be an absolute path"):
        validate(raw)


def test_numbers_must_be_in_range_and_whole(tmp_path):
    raw = valid_raw(tmp_path)
    raw["max_transfer_mib"] = 0
    with pytest.raises(SettingsError, match="max_transfer_mib must be between"):
        validate(raw)
    raw = valid_raw(tmp_path)
    raw["event_log_size"] = 100.5
    with pytest.raises(SettingsError, match="event_log_size must be a whole number"):
        validate(raw)


def test_booleans_are_not_numbers(tmp_path):
    raw = valid_raw(tmp_path)
    raw["listen_port"] = True
    with pytest.raises(SettingsError, match="listen_port must be a number"):
        validate(raw)


def test_node_name_length_and_control_characters(tmp_path):
    raw = valid_raw(tmp_path)
    raw["node_name"] = "x" * 65
    with pytest.raises(SettingsError, match="node_name"):
        validate(raw)
    raw["node_name"] = "bad\tname"
    with pytest.raises(SettingsError, match="node_name"):
        validate(raw)


@pytest.mark.parametrize(
    "text, expected",
    [
        ("relay.example.net:7000", ("relay.example.net", 7000)),
        ("192.0.2.10:1", ("192.0.2.10", 1)),
        ("[2001:db8::1]:7000", ("2001:db8::1", 7000)),
        ("  host:65535  ", ("host", 65535)),
    ],
)
def test_host_port_accepts_valid_addresses(text, expected):
    assert parse_host_port(text) == expected


@pytest.mark.parametrize("text", ["", "host", "host:0", "host:70000", "host:abc", ":7000", "[::1]7000", "a b:7000"])
def test_host_port_rejects_bad_addresses(text):
    with pytest.raises(ValueError):
        parse_host_port(text)


def test_missing_file_gives_the_defaults(tmp_path):
    defaults = make_settings(tmp_path)
    assert load_settings(tmp_path / "absent.json", defaults) == defaults


def test_save_then_load_round_trips(tmp_path):
    path = tmp_path / "settings.json"
    changed = updated(make_settings(tmp_path), {"node_name": "desk", "listen_port": 7100, "theme": "dark"})
    save_settings(path, changed)
    assert json.loads(path.read_text(encoding="utf-8"))["node_name"] == "desk"
    assert load_settings(path, make_settings(tmp_path)) == changed


def test_invalid_file_is_rejected_and_left_untouched(tmp_path):
    path = tmp_path / "settings.json"
    bad = '{"listen_port": 99999, "theme": "dark"}'
    path.write_text(bad, encoding="utf-8")
    with pytest.raises(SettingsError, match="listen_port"):
        load_settings(path, make_settings(tmp_path))
    assert path.read_text(encoding="utf-8") == bad


def test_file_that_is_not_json_is_rejected(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(SettingsError, match="not readable JSON"):
        load_settings(path, make_settings(tmp_path))


def test_updated_changes_nothing_when_a_value_is_bad(tmp_path):
    current = make_settings(tmp_path)
    with pytest.raises(SettingsError):
        updated(current, {"theme": "neon", "node_name": "fine"})
    assert current.theme == "system"
    assert current.node_name == ""


def test_save_refuses_invalid_settings_without_writing(tmp_path):
    path = tmp_path / "settings.json"
    bad = Settings(data_dir="relative", download_dir=str(tmp_path))
    with pytest.raises(SettingsError):
        save_settings(path, bad)
    assert not path.exists()
