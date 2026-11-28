"""The keys of a wormhole: the SPAKE2 exchange, the phase keys, and the seal that protects every message after the first.

Everything here is pure: no network, no clock. The session runs these in order.

    pake = Pake(code.password())              # send pake.outbound() in phase "pake"
    keys = pake.finish(peer_pake_message)     # the root key; the same on both sides when the code is the same
    sealed = seal(keys, my_side, "version", message_id, plaintext)
    plaintext = open_sealed(keys, peer_side, "version", peer_message_id, sealed)
"""

from __future__ import annotations

import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from spake2 import SPAKE2_Symmetric

from transferd.crypto import blake3_derive_key
from transferd_gui.wormhole.errors import OpenError, PakeError

#: The identity both sides give SPAKE2. It names the protocol, so a code for another protocol cannot be used here.
APP_ID = b"transferd-wormhole/1"
ROOT_CONTEXT = "transferd-wormhole/1 root"
PHASE_CONTEXT = "transferd-wormhole/1 phase "
AAD_PREFIX = b"transferd-wormhole/1"
NONCE_BYTES = 12
TAG_BYTES = 16
SIDE_HEX = 16
MESSAGE_ID_HEX = 4


def new_side() -> str:
    """A side id: 16 random hex characters, one per attempt. It is not secret, and it is never reused."""
    return os.urandom(SIDE_HEX // 2).hex()


def new_message_id() -> str:
    """A message id: 4 random hex characters, unique per message within a mailbox."""
    return os.urandom(MESSAGE_ID_HEX // 2).hex()


def check_side(side: str) -> str:
    if not isinstance(side, str) or len(side) != SIDE_HEX or any(c not in "0123456789abcdef" for c in side):
        raise PakeError("a side id is 16 lower-case hex characters")
    return side


class Pake:
    """One side's half of SPAKE2. Make it, send `outbound`, then `finish` with the other side's message.

    A Pake is used once. The library allows one `finish` call and a bad message uses it up, so a second call is refused
    here, with a message, rather than failing inside the library.
    """

    def __init__(self, password: bytes) -> None:
        self._spake = SPAKE2_Symmetric(password, idSymmetric=APP_ID)
        self._outbound = self._spake.start()
        self._finished = False

    @property
    def outbound(self) -> bytes:
        return self._outbound

    def finish(self, peer_message: bytes) -> "Keys":
        """The root key. A malformed message raises `PakeError`; a wrong code does not show here, but in the next phase."""
        if self._finished:
            raise PakeError("the key exchange is already finished; start a new one")
        self._finished = True
        if not isinstance(peer_message, (bytes, bytearray)) or not peer_message:
            raise PakeError("the other side's key exchange message is empty")
        try:
            shared = self._spake.finish(bytes(peer_message))
        except (AssertionError, ValueError) as exc:
            raise PakeError("the other side's key exchange message is not valid") from exc
        return Keys(blake3_derive_key(ROOT_CONTEXT, shared))


class Keys:
    """The root key of one wormhole. Each side derives a separate key for each phase it sends in."""

    def __init__(self, root: bytes) -> None:
        if len(root) != 32:
            raise PakeError("the root key is 32 bytes")
        self._root = bytes(root)

    def phase_key(self, side: str, phase: str) -> bytes:
        """The key `side` uses to send in `phase`. Both sides can derive it; a side never accepts its own messages."""
        check_side(side)
        return blake3_derive_key(PHASE_CONTEXT + phase, self._root + side.encode("ascii"))


def _aad(side: str, phase: str, message_id: str) -> bytes:
    return b"\x00".join([AAD_PREFIX, side.encode("ascii"), phase.encode("ascii"), message_id.encode("ascii")])


def seal(keys: Keys, side: str, phase: str, message_id: str, plaintext: bytes) -> bytes:
    """Encrypt a message from `side` in `phase`. The result is nonce || ciphertext || tag."""
    nonce = os.urandom(NONCE_BYTES)
    key = keys.phase_key(side, phase)
    return nonce + AESGCM(key).encrypt(nonce, bytes(plaintext), _aad(side, phase, message_id))


def open_sealed(keys: Keys, side: str, phase: str, message_id: str, sealed: bytes) -> bytes:
    """Decrypt a message `side` sent in `phase`. Raises `OpenError` if it was changed, belongs elsewhere, or the code is wrong."""
    if len(sealed) < NONCE_BYTES + TAG_BYTES:
        raise OpenError("a message is too short to be real")
    nonce, body = sealed[:NONCE_BYTES], sealed[NONCE_BYTES:]
    key = keys.phase_key(side, phase)
    try:
        return AESGCM(key).decrypt(nonce, body, _aad(side, phase, message_id))
    except InvalidTag as exc:
        raise OpenError("the code is wrong, or the message was changed") from exc
