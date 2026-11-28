"""Wormhole codes: spelling, reading back, and the checks that catch a mistyped or misordered code."""

from __future__ import annotations

import pytest

from transferd_gui.wormhole.codes import Code, generate, parse, word_for
from transferd_gui.wormhole.errors import CodeError
from transferd_gui.wormhole.wordlist import EVEN_WORDS, LOOKUP, ODD_WORDS


def test_a_generated_code_reads_back_to_the_same_nameplate_and_secret():
    code = generate(4217, lambda n: bytes([0x2A, 0x7F, 0xF0]))
    again = parse(str(code))
    assert again == code
    assert again.nameplate == 4217
    assert again.secret == bytes([0x2A, 0x7F, 0xF0])


def test_the_words_alternate_between_the_even_and_odd_lists():
    code = Code(1, bytes([0, 0, 0]))
    assert code.words == (EVEN_WORDS[0], ODD_WORDS[0], EVEN_WORDS[0])
    assert str(code) == f"1-{EVEN_WORDS[0]}-{ODD_WORDS[0]}-{EVEN_WORDS[0]}"


def test_the_password_includes_the_nameplate():
    code = Code(4217, bytes([0x2A, 0x7F, 0xF0]))
    assert code.password() == str(code).encode("ascii")
    assert code.password().startswith(b"4217-")


def test_reading_is_forgiving_about_case_spaces_and_underscores():
    code = Code(4217, bytes([0x2A, 0x7F, 0xF0]))
    spelt = "-".join(code.words).upper()
    assert parse(f"  4217 {spelt.replace('-', '_')}  ") == code
    assert parse(f"4217--{spelt.replace('-', ' ')}") == code
    assert parse(f"0004217-{'-'.join(code.words)}") == code, "a nameplate with leading zeros reads the same"


def test_a_swapped_word_is_refused_with_its_position():
    code = Code(9, bytes([1, 2, 3]))
    w = list(code.words)
    w[1], w[2] = w[2], w[1]
    with pytest.raises(CodeError, match="word 2 is in the wrong place"):
        parse("9-" + "-".join(w))


def test_a_missing_word_is_refused_before_anything_is_sent():
    code = Code(9, bytes([1, 2, 3]))
    with pytest.raises(CodeError, match="a number and 3 words"):
        parse(f"9-{code.words[0]}-{code.words[1]}")


def test_an_extra_word_is_refused_too():
    code = Code(9, bytes([1, 2, 3]))
    with pytest.raises(CodeError, match="a number and 3 words"):
        parse(f"9-{'-'.join(code.words)}-{EVEN_WORDS[4]}")


def test_an_unknown_word_is_named_in_the_refusal():
    with pytest.raises(CodeError, match="'zebrafish' is not a word"):
        parse("9-zebrafish-aardvark-adroitness")


@pytest.mark.parametrize("nameplate", ["0", "65536", "99999999"])
def test_a_nameplate_outside_the_range_is_refused(nameplate):
    with pytest.raises(CodeError, match="must be from 1 to 65535"):
        parse(f"{nameplate}-aardvark-absurd-accrue")


def test_a_nameplate_that_is_not_a_number_is_refused():
    with pytest.raises(CodeError, match="starts with a number"):
        parse("abc-aardvark-absurd-accrue")


def test_a_code_that_is_not_text_is_refused():
    with pytest.raises(CodeError):
        parse(None)  # type: ignore[arg-type]


def test_a_byte_out_of_range_has_no_word():
    with pytest.raises(CodeError):
        word_for(0, 256)


def test_the_word_lists_are_the_full_byte_range_and_do_not_repeat():
    assert len(EVEN_WORDS) == 256 and len(ODD_WORDS) == 256
    assert len(LOOKUP) == 512, "every word is distinct, so a word names exactly one byte and one position"
    assert len(set(EVEN_WORDS) | set(ODD_WORDS)) == 512


def test_generate_asks_for_three_random_bytes_and_uses_them_as_the_secret():
    asked = []

    def fake(n):
        asked.append(n)
        return bytes([5, 6, 7])

    code = generate(12, fake)
    assert asked == [3]
    assert code.secret == bytes([5, 6, 7])


def test_a_code_with_a_bad_nameplate_or_secret_cannot_be_made():
    with pytest.raises(CodeError):
        Code(0, bytes([1, 2, 3]))
    with pytest.raises(CodeError):
        Code(5, bytes([1, 2]))
