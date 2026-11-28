"""Wormhole codes: a nameplate and three PGP words, such as `4217-ember-tundra-lattice`.

The nameplate is a decimal number the mailbox allocates. The three words spell three random bytes, which is the secret: 24
bits. The word at position `i` comes from the even list when `i` is even and the odd list when it is odd, so a dropped or
swapped word is seen when the code is read back.

Reading a code is forgiving about case, spaces and underscores, and strict about everything else.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Callable, Tuple

from transferd_gui.wormhole.errors import CodeError
from transferd_gui.wormhole.wordlist import EVEN_WORDS, LOOKUP, ODD_WORDS

NAMEPLATE_MIN = 1
NAMEPLATE_MAX = 65535
#: The number of words after the nameplate. Each word carries 8 bits of the secret.
WORD_COUNT = 3
_NUMBER = re.compile(r"[0-9]+")
_SEPARATORS = re.compile(r"[\s_\-]+")


@dataclass(frozen=True)
class Code:
    """A code: its nameplate and its secret bytes. The words are derived from the secret."""

    nameplate: int
    secret: bytes

    def __post_init__(self) -> None:
        if not NAMEPLATE_MIN <= self.nameplate <= NAMEPLATE_MAX:
            raise CodeError(f"the number must be from {NAMEPLATE_MIN} to {NAMEPLATE_MAX}")
        if len(self.secret) != WORD_COUNT:
            raise CodeError(f"a code has {WORD_COUNT} secret bytes, not {len(self.secret)}")

    @property
    def words(self) -> Tuple[str, ...]:
        return tuple(word_for(position, byte) for position, byte in enumerate(self.secret))

    def __str__(self) -> str:
        return "-".join([str(self.nameplate), *self.words])

    def password(self) -> bytes:
        """What the key exchange is run with. It includes the nameplate, so the mailbox cannot change it unnoticed."""
        return str(self).encode("ascii")


def word_for(position: int, byte: int) -> str:
    """The word that spells `byte` at `position`: even list for even positions, odd list for odd ones."""
    if not 0 <= byte <= 255:
        raise CodeError("a secret byte must be from 0 to 255")
    return ODD_WORDS[byte] if position % 2 else EVEN_WORDS[byte]


def generate(nameplate: int, random_bytes: Callable[[int], bytes] = os.urandom) -> Code:
    """A new code for a nameplate the mailbox has allocated. `random_bytes` is replaceable so tests can fix the code."""
    secret = random_bytes(WORD_COUNT)
    return Code(nameplate, bytes(secret))


def normalise(text: str) -> str:
    """Case, spaces and underscores are not significant. Runs of separators become one `-`."""
    return _SEPARATORS.sub("-", text.strip().lower()).strip("-")


def parse(text: str) -> Code:
    """Read a code back. Raises `CodeError` with the reason when it does not check out."""
    if not isinstance(text, str):
        raise CodeError("a code is text")
    parts = normalise(text).split("-")
    if len(parts) != WORD_COUNT + 1 or parts[0] == "":
        raise CodeError(f"a code is a number and {WORD_COUNT} words, such as 4217-ember-tundra-lattice")
    number, words = parts[0], parts[1:]
    if not _NUMBER.fullmatch(number):
        raise CodeError("the code starts with a number, the nameplate")
    nameplate = int(number)
    if not NAMEPLATE_MIN <= nameplate <= NAMEPLATE_MAX:
        raise CodeError(f"the number must be from {NAMEPLATE_MIN} to {NAMEPLATE_MAX}")
    secret = bytearray()
    for position, word in enumerate(words):
        entry = LOOKUP.get(word)
        if entry is None:
            raise CodeError(f"'{word}' is not a word in the code list")
        byte, parity = entry
        if parity != position % 2:
            raise CodeError(f"word {position + 1} is in the wrong place; a word may be missing or out of order")
        secret.append(byte)
    return Code(nameplate, bytes(secret))
