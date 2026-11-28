"""The debug protocol's messages (see `gui/docs/DEBUG_PROTOCOL.md`): one JSON object per line on an overlay stream.

Nothing here touches a socket or a session. It turns messages into bytes and bytes back into messages, and it refuses
anything that breaks a limit, so a misbehaving peer cannot make the other side buffer without bound.
"""

from __future__ import annotations

import base64
import json
from typing import Any, Dict, List, Optional

PROTOCOL_VERSION = 1
#: Overlay services for the debug operations. No dots: a Rust node's `expose` refuses them.
SERVICE_PREFIX = "debug-"
OPS = ("info", "logcat", "shell", "forward")

MAX_REQUEST = 4096
MAX_LINE = 64 * 1024
MAX_TEXT = 4096
MAX_SHELL_COMMAND = 2048
MAX_SHELL_TIMEOUT_S = 600.0

#: Every error code the protocol uses. A receiver shows an unknown code as it is.
ERROR_CODES = (
    "denied",
    "disabled",
    "not_allowed",
    "bad_request",
    "unsupported_version",
    "unavailable",
    "failed",
    "too_many",
    "timeout",
)


class WireError(ValueError):
    """A message that breaks the protocol: not JSON, not an object, too long, or missing a required field."""


def service_name(op: str) -> str:
    if op not in OPS:
        raise WireError(f"unknown debug operation '{op}'")
    return SERVICE_PREFIX + op


def op_of(service: str) -> Optional[str]:
    """The operation a service name stands for, or None when it is not a debug service."""
    if not service.startswith(SERVICE_PREFIX):
        return None
    op = service[len(SERVICE_PREFIX):]
    return op if op in OPS else None


def encode(message: Dict[str, Any], limit: int = MAX_LINE) -> bytes:
    """One message as a line of UTF-8 JSON ending in a newline. Raises WireError when it is too long for `limit`."""
    try:
        text = json.dumps(message, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise WireError(f"the message cannot be encoded: {exc}") from exc
    data = text.encode("utf-8") + b"\n"
    if len(data) > limit:
        raise WireError(f"the message is {len(data)} bytes, over the {limit} byte limit")
    return data


def request(args: Dict[str, Any]) -> bytes:
    """The first line a client sends on a debug stream."""
    return encode({"v": PROTOCOL_VERSION, "args": args}, MAX_REQUEST)


def parse(line: bytes) -> Dict[str, Any]:
    """One received line as a message. It must be a JSON object with a string `t` (for responses) or a `v` (for requests)."""
    try:
        message = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise WireError(f"a line is not UTF-8 JSON: {exc}") from exc
    if not isinstance(message, dict):
        raise WireError("a line is not a JSON object")
    return message


class LineDecoder:
    """Turns bytes that arrive in pieces into whole messages, one line at a time.

    `push` adds bytes; `take` returns the next complete message, or None when the buffer holds no whole line. Bytes
    after the line just taken stay buffered: `rest` returns them. That is how a raw forward reads the bytes that came
    in with its request line, without parsing them as JSON.
    """

    def __init__(self, limit: int = MAX_LINE) -> None:
        self._limit = limit
        self._buffer = bytearray()

    def push(self, chunk: bytes) -> None:
        self._buffer += chunk

    def take(self) -> Optional[Dict[str, Any]]:
        end = self._buffer.find(b"\n")
        if end < 0:
            if len(self._buffer) > self._limit:
                raise WireError(f"a line is longer than {self._limit} bytes")
            return None
        if end > self._limit:
            raise WireError(f"a line is longer than {self._limit} bytes")
        line = bytes(self._buffer[:end])
        del self._buffer[: end + 1]
        return parse(line)

    def take_all(self) -> List[Dict[str, Any]]:
        """Every complete message now buffered."""
        messages = []
        while True:
            message = self.take()
            if message is None:
                return messages
            messages.append(message)

    def feed(self, chunk: bytes) -> List[Dict[str, Any]]:
        self.push(chunk)
        return self.take_all()

    def rest(self) -> bytes:
        """The buffered bytes that are not yet a message, emptied. For a raw stream after its first line."""
        data = bytes(self._buffer)
        self._buffer.clear()
        return data


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def unb64(text: str) -> bytes:
    try:
        return base64.b64decode(text.encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError) as exc:
        raise WireError(f"bytes are not base64: {exc}") from exc


def clip_text(text: str, limit: int = MAX_TEXT) -> tuple[str, bool]:
    """`text` cut to `limit` characters, and whether it was cut."""
    if len(text) <= limit:
        return text, False
    return text[:limit], True
