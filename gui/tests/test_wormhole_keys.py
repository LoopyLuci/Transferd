"""The wormhole's keys: the SPAKE2 exchange agrees only when the codes match, and each sealed message opens only where it was sealed."""

from __future__ import annotations

import pytest

from transferd_gui.wormhole.codes import Code
from transferd_gui.wormhole.errors import OpenError, PakeError
from transferd_gui.wormhole.keys import Keys, Pake, new_message_id, new_side, open_sealed, seal


def exchange(code_a: Code, code_b: Code) -> tuple[Keys, Keys]:
    """Two sides run the key exchange in memory, each with its own code."""
    a = Pake(code_a.password())
    b = Pake(code_b.password())
    return a.finish(b.outbound), b.finish(a.outbound)


CODE = Code(4217, bytes([0x2A, 0x7F, 0xF0]))


def test_the_same_code_gives_both_sides_the_same_keys():
    keys_a, keys_b = exchange(CODE, CODE)
    side = new_side()
    assert keys_a.phase_key(side, "version") == keys_b.phase_key(side, "version")
    assert keys_a.phase_key(side, "identity") != keys_a.phase_key(side, "version"), "a key is per phase"


def test_a_different_code_gives_different_keys_and_nothing_opens():
    keys_a, keys_b = exchange(CODE, Code(4217, bytes([0x2A, 0x7F, 0xF1])))
    side_a = new_side()
    sealed = seal(keys_a, side_a, "version", new_message_id(), b'{"protocol": 1}')
    with pytest.raises(OpenError, match="the code is wrong"):
        open_sealed(keys_b, side_a, "version", "0000", sealed)


def test_a_different_nameplate_is_a_different_password():
    keys_a, keys_b = exchange(CODE, Code(4218, CODE.secret))
    side = new_side()
    sealed = seal(keys_a, side, "version", new_message_id(), b"x")
    with pytest.raises(OpenError):
        open_sealed(keys_b, side, "version", "0000", sealed)


def test_a_sealed_message_opens_for_the_side_that_sent_it():
    keys_a, keys_b = exchange(CODE, CODE)
    side_a = new_side()
    message_id = new_message_id()
    sealed = seal(keys_a, side_a, "identity", message_id, b'{"node_id": "x"}')
    assert open_sealed(keys_b, side_a, "identity", message_id, sealed) == b'{"node_id": "x"}'


def test_a_message_reflected_back_to_its_sender_does_not_open_as_the_other_side():
    keys_a, _keys_b = exchange(CODE, CODE)
    side_a, side_b = new_side(), new_side()
    sealed = seal(keys_a, side_a, "offer", new_message_id(), b"offer")
    with pytest.raises(OpenError):
        open_sealed(keys_a, side_b, "offer", "0000", sealed)


def test_a_message_in_another_phase_does_not_open():
    keys_a, keys_b = exchange(CODE, CODE)
    side = new_side()
    message_id = new_message_id()
    sealed = seal(keys_a, side, "offer", message_id, b"offer")
    with pytest.raises(OpenError):
        open_sealed(keys_b, side, "answer", message_id, sealed)


def test_a_message_under_another_id_does_not_open():
    keys_a, keys_b = exchange(CODE, CODE)
    side = new_side()
    sealed = seal(keys_a, side, "offer", "a1b2", b"offer")
    with pytest.raises(OpenError):
        open_sealed(keys_b, side, "offer", "a1b3", sealed)


def test_a_changed_byte_does_not_open():
    keys_a, keys_b = exchange(CODE, CODE)
    side = new_side()
    sealed = bytearray(seal(keys_a, side, "offer", "a1b2", b"offer text"))
    sealed[-1] ^= 0x01
    with pytest.raises(OpenError):
        open_sealed(keys_b, side, "offer", "a1b2", bytes(sealed))


def test_a_message_too_short_to_hold_its_nonce_and_tag_does_not_open():
    keys_a, _ = exchange(CODE, CODE)
    with pytest.raises(OpenError, match="too short"):
        open_sealed(keys_a, new_side(), "offer", "a1b2", b"short")


def test_each_seal_uses_a_fresh_nonce():
    keys_a, _ = exchange(CODE, CODE)
    side = new_side()
    first = seal(keys_a, side, "offer", "a1b2", b"same")
    second = seal(keys_a, side, "offer", "a1b2", b"same")
    assert first[:12] != second[:12]
    assert first != second


def test_a_malformed_key_exchange_message_is_refused():
    with pytest.raises(PakeError):
        Pake(CODE.password()).finish(b"\x00" * 33)
    with pytest.raises(PakeError):
        Pake(CODE.password()).finish(b"short")
    with pytest.raises(PakeError, match="empty"):
        Pake(CODE.password()).finish(b"")


def test_a_key_exchange_is_used_once_and_a_second_finish_says_so():
    pake = Pake(CODE.password())
    pake.finish(Pake(CODE.password()).outbound)
    with pytest.raises(PakeError, match="already finished"):
        pake.finish(Pake(CODE.password()).outbound)


def test_a_side_id_must_be_sixteen_lower_case_hex_characters():
    keys_a, _ = exchange(CODE, CODE)
    with pytest.raises(PakeError):
        keys_a.phase_key("ABCDEF0123456789", "offer")
    with pytest.raises(PakeError):
        keys_a.phase_key("short", "offer")


def test_the_root_key_must_be_thirty_two_bytes():
    with pytest.raises(PakeError):
        Keys(b"short")


def test_side_and_message_ids_are_random_and_well_formed():
    side = new_side()
    assert len(side) == 16 and all(c in "0123456789abcdef" for c in side)
    assert len(new_message_id()) == 4
    assert new_side() != new_side()
