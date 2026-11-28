"""The wormhole's messages: every field is checked before it is used, and a message from a stranger is refused, not repaired."""

from __future__ import annotations

import hashlib

import pytest

from transferd.identity import HYBRID_PK_LEN, HybridIdentity
from transferd_gui.wormhole import protocol as P
from transferd_gui.wormhole.errors import ProtocolError

SHA = hashlib.sha256(b"x").hexdigest()
NODE = "ab" * 32


def identity_dict(**overrides):
    base = {
        "node_id": NODE,
        "public_key": (b"\x01" * 2624).hex(),
        "name": "laptop",
        "relay": "",
        "hints": [{"kind": "direct", "host": "192.168.1.5", "port": 53123}],
    }
    base.update(overrides)
    return base


# ----- offers ----------------------------------------------------------------------------------------


def test_a_file_offer_round_trips():
    offer = P.Offer.for_file("report.pdf", 1234, SHA)
    assert P.Offer.from_dict(offer.to_dict()) == offer


def test_a_text_offer_round_trips_and_its_size_is_its_bytes():
    offer = P.Offer.for_text("héllo")
    again = P.Offer.from_dict(offer.to_dict())
    assert again.text == "héllo" and again.size == len("héllo".encode("utf-8"))


def test_a_directory_offer_needs_its_entry_count():
    with pytest.raises(ProtocolError, match="'entries' is missing"):
        P.Offer.from_dict({"kind": "directory", "name": "photos", "size": 1, "sha256": SHA})


@pytest.mark.parametrize(
    "name",
    ["../evil", "a/b", "a\\b", "bad\x00name", "   ", "", "..", "x" * 200],
)
def test_a_name_that_cannot_be_a_file_name_is_refused(name):
    with pytest.raises(ProtocolError):
        P.Offer.from_dict({"kind": "file", "name": name, "size": 1, "sha256": SHA})


def test_an_unknown_kind_is_refused():
    with pytest.raises(ProtocolError, match="kind"):
        P.Offer.from_dict({"kind": "script", "name": "x", "size": 1, "sha256": SHA})


@pytest.mark.parametrize("size", [-1, True, "12", 1 << 41])
def test_a_size_must_be_a_plain_number_in_range(size):
    with pytest.raises(ProtocolError):
        P.Offer.from_dict({"kind": "file", "name": "x", "size": size, "sha256": SHA})


def test_a_hash_must_be_sixty_four_lower_case_hex_characters():
    with pytest.raises(ProtocolError):
        P.Offer.from_dict({"kind": "file", "name": "x", "size": 1, "sha256": "ABC"})
    with pytest.raises(ProtocolError):
        P.Offer.from_dict({"kind": "file", "name": "x", "size": 1, "sha256": "z" * 64})


def test_a_text_longer_than_the_limit_is_refused_to_send_and_to_read():
    with pytest.raises(ProtocolError):
        P.Offer.for_text("x" * (P.TEXT_MAX_BYTES + 1))
    with pytest.raises(ProtocolError):
        P.Offer.from_dict({"kind": "text", "text": "x" * (P.TEXT_MAX_BYTES + 1)})


def test_a_name_from_the_other_side_is_reduced_to_a_file_name_or_refused():
    assert P.safe_offered_name("  report.pdf. ") == "report.pdf"
    with pytest.raises(ProtocolError):
        P.safe_offered_name("C:\\Windows\\x")


# ----- identities ------------------------------------------------------------------------------------


def test_a_real_hybrid_public_key_has_the_length_the_protocol_expects():
    identity = HybridIdentity.generate("laptop")
    assert len(identity.public_key) == HYBRID_PK_LEN == 2624
    assert P.PeerIdentity.from_dict(identity_dict(public_key=identity.public_key.hex())).public_key == identity.public_key


def test_an_identity_round_trips_through_its_dictionary():
    peer = P.PeerIdentity.from_dict(identity_dict())
    assert peer.node_id == NODE and len(peer.public_key) == 2624
    assert P.PeerIdentity.from_dict(peer.to_dict()) == peer


def test_an_identity_with_a_short_key_or_bad_node_id_is_refused():
    with pytest.raises(ProtocolError):
        P.PeerIdentity.from_dict(identity_dict(public_key="ab" * 10))
    with pytest.raises(ProtocolError):
        P.PeerIdentity.from_dict(identity_dict(node_id="xyz"))


def test_an_identity_may_carry_at_most_eight_hints():
    hints = [{"kind": "direct", "host": "10.0.0.1", "port": 1000 + i} for i in range(9)]
    with pytest.raises(ProtocolError, match="at most 8"):
        P.PeerIdentity.from_dict(identity_dict(hints=hints))


@pytest.mark.parametrize(
    "hint",
    [
        {"kind": "tailscale", "host": "x", "port": 1},
        {"kind": "direct", "host": "x", "port": 0},
        {"kind": "direct", "host": "x", "port": 70000},
        "not an object",
    ],
)
def test_a_bad_hint_is_refused(hint):
    with pytest.raises(ProtocolError):
        P.PeerIdentity.from_dict(identity_dict(hints=[hint]))


# ----- version, answer, done -------------------------------------------------------------------------


def test_our_own_version_passes_our_check():
    P.Version.ours().check()


def test_a_version_for_another_protocol_or_overlay_is_refused():
    ours = P.Version.ours()
    with pytest.raises(ProtocolError, match="protocol 2"):
        P.Version(2, P.APP, ours.overlay, ours.features).check()
    with pytest.raises(ProtocolError, match="overlay"):
        P.Version(ours.protocol, P.APP, ours.overlay + 1, ours.features).check()
    with pytest.raises(ProtocolError, match="speaks"):
        P.Version(ours.protocol, "someone-else/1", ours.overlay, ours.features).check()


def test_an_answer_needs_accept_as_a_boolean():
    assert P.Answer.from_dict({"accept": True}).accept is True
    with pytest.raises(ProtocolError):
        P.Answer.from_dict({"accept": "yes"})
    with pytest.raises(ProtocolError):
        P.Answer.from_dict({})


def test_a_done_message_carries_ok_and_optionally_a_hash():
    assert P.Done.from_dict({"ok": True, "sha256": SHA}).sha256 == SHA
    assert P.Done.from_dict({"ok": False, "reason": "no"}).reason == "no"
    with pytest.raises(ProtocolError):
        P.Done.from_dict({"ok": True, "sha256": "not hex"})


# ----- bytes -----------------------------------------------------------------------------------------


def test_encoding_is_the_same_bytes_every_time():
    assert P.encode({"b": 1, "a": 2}) == P.encode({"a": 2, "b": 1}) == b'{"a":2,"b":1}'


def test_bytes_that_are_not_a_json_object_are_refused():
    for raw in (b"[1,2]", b"not json", b"\xff\xfe", b'"just a string"'):
        with pytest.raises(ProtocolError):
            P.decode(raw)


def test_a_message_over_the_size_limit_is_refused_both_ways():
    with pytest.raises(ProtocolError):
        P.encode({"x": "y" * (P.JSON_MAX + 10)})
    with pytest.raises(ProtocolError):
        P.decode(b"{" + b" " * P.JSON_MAX + b"}")
