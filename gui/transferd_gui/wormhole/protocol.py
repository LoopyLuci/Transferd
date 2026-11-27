"""The wormhole's messages: what each sealed phase carries, how it is checked, and how it is written.

Every message after `pake` is a JSON object. A message that does not have the expected fields, types and sizes is refused
with `ProtocolError`, before anything it says is used. The checks are strict on purpose: the other side is a stranger until
the phases have been opened, and even then its values reach the file system, the screen and the overlay.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict

from transferd.frames import VERSION_CURRENT as OVERLAY_VERSION
from transferd.identity import HYBRID_PK_LEN
from transferd_gui.backend.files import NAME_MAX
from transferd_gui.wormhole.errors import ProtocolError

PROTOCOL_VERSION = 1
APP = "transferd-wormhole/1"
FEATURES = ("parallel-streams", "sha256")
TEXT_MAX_BYTES = 16 * 1024
SIZE_MAX = 1 << 40
ENTRIES_MAX = 10_000
HINTS_MAX = 8
NAME_TEXT_MAX = 64
RELAY_MAX = 128
REASON_MAX = 200
JSON_MAX = 64 * 1024
KINDS = ("text", "file", "directory")
NODE_ID_HEX = 64
SHA256_HEX = 64
_HEX = re.compile(r"[0-9a-f]*")
_UNSAFE_NAME = re.compile(r"[\x00-\x1f\\/]")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ----- reading fields ---------------------------------------------------------------------------------


def _field(obj: Dict[str, Any], key: str, kind: type, where: str):
    if key not in obj:
        raise ProtocolError(f"{where}: '{key}' is missing")
    value = obj[key]
    if kind is int and isinstance(value, bool):
        raise ProtocolError(f"{where}: '{key}' is a number")
    if not isinstance(value, kind):
        raise ProtocolError(f"{where}: '{key}' is not a {kind.__name__}")
    return value


def _text(obj: Dict[str, Any], key: str, where: str, *, maximum: int, allow_empty: bool = False) -> str:
    value = _field(obj, key, str, where)
    if not allow_empty and not value:
        raise ProtocolError(f"{where}: '{key}' is empty")
    if len(value.encode("utf-8", errors="strict")) > maximum:
        raise ProtocolError(f"{where}: '{key}' is longer than {maximum}")
    return value


def _number(obj: Dict[str, Any], key: str, where: str, *, maximum: int) -> int:
    value = _field(obj, key, int, where)
    if not 0 <= value <= maximum:
        raise ProtocolError(f"{where}: '{key}' is outside 0 to {maximum}")
    return value


def _hex(obj: Dict[str, Any], key: str, where: str, *, length: int) -> str:
    value = _text(obj, key, where, maximum=length)
    if len(value) != length or not _HEX.fullmatch(value):
        raise ProtocolError(f"{where}: '{key}' is {length} lower-case hex characters")
    return value


def safe_offered_name(name: str) -> str:
    """A name from the other side, reduced to something that can be a file name on any computer."""
    if not isinstance(name, str) or _UNSAFE_NAME.search(name):
        raise ProtocolError("a name has a path separator or a control character")
    base = name.strip(" .")
    if not base:
        raise ProtocolError("a name is empty")
    if len(base.encode("utf-8")) > NAME_MAX:
        raise ProtocolError(f"a name is longer than {NAME_MAX} bytes")
    return base


# ----- messages ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Version:
    protocol: int
    app: str
    overlay: int
    features: tuple

    @classmethod
    def ours(cls) -> "Version":
        return cls(PROTOCOL_VERSION, APP, OVERLAY_VERSION, FEATURES)

    @classmethod
    def from_dict(cls, obj: Dict[str, Any]) -> "Version":
        where = "version"
        protocol = _number(obj, "protocol", where, maximum=255)
        app = _text(obj, "app", where, maximum=64)
        overlay = _number(obj, "overlay", where, maximum=255)
        raw = _field(obj, "features", list, where)
        if len(raw) > 16 or not all(isinstance(f, str) and len(f) <= 32 for f in raw):
            raise ProtocolError("version: 'features' is a short list of names")
        return cls(protocol, app, overlay, tuple(raw))

    def to_dict(self) -> Dict[str, Any]:
        return {"protocol": self.protocol, "app": self.app, "overlay": self.overlay, "features": list(self.features)}

    def check(self) -> None:
        """Refuse a version we cannot speak. Both sides must agree on the protocol and on the overlay."""
        if self.app != APP:
            raise ProtocolError(f"the other side speaks '{self.app}', not {APP}")
        if self.protocol != PROTOCOL_VERSION:
            raise ProtocolError(f"the other side speaks protocol {self.protocol}; this app speaks {PROTOCOL_VERSION}")
        if self.overlay != OVERLAY_VERSION:
            raise ProtocolError(f"the other side's overlay is version {self.overlay}; this app's is {OVERLAY_VERSION}")


@dataclass(frozen=True)
class Hint:
    host: str
    port: int

    @classmethod
    def from_dict(cls, obj: Any) -> "Hint":
        if not isinstance(obj, dict):
            raise ProtocolError("identity: a hint is an object")
        if obj.get("kind") != "direct":
            raise ProtocolError("identity: an unknown hint kind")
        host = _text(obj, "host", "identity hint", maximum=255)
        port = _number(obj, "port", "identity hint", maximum=65535)
        if port == 0:
            raise ProtocolError("identity: a hint's port is 1 to 65535")
        return cls(host, port)

    def to_dict(self) -> Dict[str, Any]:
        return {"kind": "direct", "host": self.host, "port": self.port}


@dataclass(frozen=True)
class PeerIdentity:
    """The other side's ephemeral identity for one attempt, and where it can be reached."""

    node_id: str
    public_key: bytes
    name: str
    relay: str
    hints: tuple = field(default_factory=tuple)

    @classmethod
    def from_dict(cls, obj: Dict[str, Any]) -> "PeerIdentity":
        where = "identity"
        node_id = _hex(obj, "node_id", where, length=NODE_ID_HEX)
        public_hex = _hex(obj, "public_key", where, length=HYBRID_PK_LEN * 2)
        name = _text(obj, "name", where, maximum=NAME_TEXT_MAX, allow_empty=True)
        relay = _text(obj, "relay", where, maximum=RELAY_MAX, allow_empty=True)
        raw_hints = _field(obj, "hints", list, where)
        if len(raw_hints) > HINTS_MAX:
            raise ProtocolError(f"identity: at most {HINTS_MAX} hints")
        hints = tuple(Hint.from_dict(h) for h in raw_hints)
        return cls(node_id, bytes.fromhex(public_hex), name, relay, hints)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "node_id": self.node_id,
            "public_key": self.public_key.hex(),
            "name": self.name,
            "relay": self.relay,
            "hints": [h.to_dict() for h in self.hints],
        }


@dataclass(frozen=True)
class Offer:
    """What the sender is sending. A file's bytes travel over the overlay; a text is inside this message."""

    kind: str
    name: str = ""
    size: int = 0
    sha256: str = ""
    text: str = ""
    entries: int = 0

    @classmethod
    def for_text(cls, text: str) -> "Offer":
        data = text.encode("utf-8")
        if len(data) > TEXT_MAX_BYTES:
            raise ProtocolError(f"a text is at most {TEXT_MAX_BYTES} bytes")
        return cls("text", "", len(data), sha256_hex(data), text, 0)

    @classmethod
    def for_file(cls, name: str, size: int, sha256: str) -> "Offer":
        return cls("file", safe_offered_name(name), size, sha256, "", 0)

    @classmethod
    def for_directory(cls, name: str, size: int, sha256: str, entries: int) -> "Offer":
        return cls("directory", safe_offered_name(name), size, sha256, "", entries)

    @classmethod
    def from_dict(cls, obj: Dict[str, Any]) -> "Offer":
        where = "offer"
        kind = _field(obj, "kind", str, where)
        if kind not in KINDS:
            raise ProtocolError(f"offer: the kind is not one of {', '.join(KINDS)}")
        if kind == "text":
            text = _text(obj, "text", where, maximum=TEXT_MAX_BYTES, allow_empty=True)
            size = len(text.encode("utf-8"))
            return cls("text", "", size, sha256_hex(text.encode("utf-8")), text, 0)
        name = safe_offered_name(_field(obj, "name", str, where))
        size = _number(obj, "size", where, maximum=SIZE_MAX)
        sha = _hex(obj, "sha256", where, length=SHA256_HEX)
        entries = _number(obj, "entries", where, maximum=ENTRIES_MAX) if kind == "directory" else 0
        return cls(kind, name, size, sha, "", entries)

    def to_dict(self) -> Dict[str, Any]:
        if self.kind == "text":
            return {"kind": "text", "text": self.text}
        out: Dict[str, Any] = {"kind": self.kind, "name": self.name, "size": self.size, "sha256": self.sha256}
        if self.kind == "directory":
            out["entries"] = self.entries
        return out


@dataclass(frozen=True)
class Answer:
    accept: bool
    reason: str = ""

    @classmethod
    def from_dict(cls, obj: Dict[str, Any]) -> "Answer":
        accept = _field(obj, "accept", bool, "answer")
        reason = _text(obj, "reason", "answer", maximum=REASON_MAX, allow_empty=True) if "reason" in obj else ""
        return cls(accept, reason)

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"accept": self.accept}
        if self.reason:
            out["reason"] = self.reason[:REASON_MAX]
        return out


@dataclass(frozen=True)
class Done:
    ok: bool
    sha256: str = ""
    reason: str = ""

    @classmethod
    def from_dict(cls, obj: Dict[str, Any]) -> "Done":
        ok = _field(obj, "ok", bool, "done")
        sha = _hex(obj, "sha256", "done", length=SHA256_HEX) if obj.get("sha256") else ""
        reason = _text(obj, "reason", "done", maximum=REASON_MAX, allow_empty=True) if "reason" in obj else ""
        return cls(ok, sha, reason)

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"ok": self.ok}
        if self.sha256:
            out["sha256"] = self.sha256
        if self.reason:
            out["reason"] = self.reason[:REASON_MAX]
        return out


# ----- bytes ------------------------------------------------------------------------------------------


def encode(message: Any) -> bytes:
    """A message as bytes. Sorted keys and no spaces, so the same message is always the same bytes."""
    raw = json.dumps(message, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(raw) > JSON_MAX:
        raise ProtocolError(f"a message is at most {JSON_MAX} bytes")
    return raw


def decode(raw: bytes) -> Dict[str, Any]:
    """Bytes to a JSON object. Anything else is refused."""
    if len(raw) > JSON_MAX:
        raise ProtocolError(f"a message is at most {JSON_MAX} bytes")
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProtocolError("a message is not JSON text") from exc
    if not isinstance(obj, dict):
        raise ProtocolError("a message is a JSON object")
    return obj


def parse(raw: bytes, what) -> Any:
    """Decode bytes and build the message type `what` from them, checking every field."""
    return what.from_dict(decode(raw))
