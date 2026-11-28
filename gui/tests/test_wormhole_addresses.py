"""The mailbox addresses in Settings: what is accepted, what is refused and why, the defaults, and the pins. Text only."""

from __future__ import annotations

import pytest

from transferd_gui.wormhole.addresses import (
    DEFAULT_PORT,
    MAX_MAILBOXES,
    MailboxAddress,
    display_fingerprint,
    normalise_fingerprint,
    parse_address,
    parse_list,
)

PIN = "9f2c" * 16  # 64 hex characters


def test_a_wss_address_with_a_pin_is_read_with_its_pin_in_lower_case():
    address = parse_address(f"wss://Mailbox.Example.net:4470#{PIN.upper()}")
    assert address == MailboxAddress("wss", "mailbox.example.net", 4470, PIN)
    assert address.url == "wss://mailbox.example.net:4470"
    assert address.text() == f"wss://mailbox.example.net:4470#{PIN}"


def test_the_port_defaults_to_4470_and_a_pin_is_optional():
    address = parse_address("wss://mailbox.example.net")
    assert address.port == DEFAULT_PORT
    assert address.pin == ""
    assert address.text() == "wss://mailbox.example.net:4470"


def test_a_fingerprint_may_be_copied_with_spaces_colons_or_dashes():
    spaced = " ".join(PIN[i : i + 8] for i in range(0, 64, 8))
    colons = ":".join(PIN[i : i + 2] for i in range(0, 64, 2))
    assert normalise_fingerprint(spaced) == PIN
    assert normalise_fingerprint(colons) == PIN
    assert normalise_fingerprint(PIN.upper()) == PIN


def test_a_fingerprint_of_the_wrong_length_or_with_other_characters_is_refused():
    with pytest.raises(ValueError, match="64 hexadecimal"):
        normalise_fingerprint("abcd")
    with pytest.raises(ValueError, match="64 hexadecimal"):
        normalise_fingerprint("zz" * 32)


def test_the_display_form_groups_a_fingerprint_in_eights():
    shown = display_fingerprint(PIN)
    assert shown.split(" ") == [PIN[i : i + 8] for i in range(0, 64, 8)]
    assert normalise_fingerprint(shown) == PIN


def test_plain_ws_is_accepted_for_this_computer_only():
    assert parse_address("ws://127.0.0.1:4470").loopback
    assert parse_address("ws://localhost:4470").loopback
    assert parse_address("ws://[::1]:4470").url == "ws://[::1]:4470"
    with pytest.raises(ValueError, match="reaches only this computer"):
        parse_address("ws://mailbox.example.net:4470")
    with pytest.raises(ValueError, match="reaches only this computer"):
        parse_address("ws://192.168.1.20:4470")


def test_a_fingerprint_on_a_ws_address_is_refused_because_ws_is_not_encrypted():
    with pytest.raises(ValueError, match="wss://"):
        parse_address(f"ws://127.0.0.1:4470#{PIN}")


@pytest.mark.parametrize(
    "text, problem",
    [
        ("mailbox.example.net:4470", "must start with wss://"),
        ("https://mailbox.example.net:4470", "must start with wss://"),
        ("wss://mailbox.example.net:70000", "1 to 65535"),
        ("wss://mailbox.example.net:port", "does not end in a port"),
        ("wss://user:secret@mailbox.example.net:4470", "user name or password"),
        ("wss://mailbox.example.net:4470/mailbox", "only a host and a port"),
        ("wss://mailbox.example.net:4470?x=1", "only a host and a port"),
        ("wss://mailbox.example.net:4470 extra", "spaces"),
        ("", "empty"),
        ("   ", "empty"),
    ],
)
def test_bad_addresses_are_refused_with_the_reason(text, problem):
    with pytest.raises(ValueError, match=problem):
        parse_address(text)


def test_an_empty_setting_is_no_list_at_all():
    assert parse_list("") == ()
    assert parse_list("   ") == ()


def test_a_list_keeps_its_order_and_tolerates_spaces_around_the_commas():
    addresses = parse_list(f"wss://first.example.net:4470#{PIN}, wss://second.example.net:4470")
    assert [a.host for a in addresses] == ["first.example.net", "second.example.net"]
    assert addresses[0].pin == PIN


def test_a_list_with_an_empty_entry_is_refused():
    with pytest.raises(ValueError, match="entry 2 of the list is empty"):
        parse_list("wss://a.example.net:4470,,wss://b.example.net:4470")
    with pytest.raises(ValueError, match="entry 2 of the list is empty"):
        parse_list("wss://a.example.net:4470,")


def test_a_list_longer_than_the_limit_is_refused():
    entries = ",".join(f"wss://m{i}.example.net:4470" for i in range(MAX_MAILBOXES + 1))
    with pytest.raises(ValueError, match="at most"):
        parse_list(entries)


def test_the_same_mailbox_listed_twice_is_refused():
    with pytest.raises(ValueError, match="listed twice"):
        parse_list("wss://a.example.net:4470,wss://A.example.net:4470")
