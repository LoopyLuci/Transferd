"""The file convention the GUI speaks on a stream.

A file travels on one stream, under the service name `file`: one header line, the file's bytes, then the end of the
stream. The header is JSON, `{"name": "<file name>", "size": <bytes>}`, followed by a newline. The library treats service
names as opaque, so this is the GUI's own convention; another implementation must agree on it to exchange files.

The receiver keeps only the file's base name, with unsafe characters replaced, so a sender cannot write outside the
download folder.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

FILE_SERVICE = "file"
STREAM_SERVICE = "stream"
HEADER_MAX = 4096
CHUNK = 64 * 1024
NAME_MAX = 120
_UNSAFE = re.compile(r'[\x00-\x1f<>:"/\\|?*]')


class FileHeaderError(ValueError):
    """A header that is not the file convention: too long, not JSON, or missing its name or size."""


@dataclass(frozen=True)
class FileHeader:
    name: str
    size: int


def encode_header(name: str, size: int) -> bytes:
    return json.dumps({"name": safe_name(name), "size": size}, ensure_ascii=False).encode("utf-8") + b"\n"


class HeaderReader:
    """Reads the header from the front of a stream, and passes back the bytes that follow it."""

    def __init__(self) -> None:
        self._buffer = bytearray()
        self.header: Optional[FileHeader] = None

    def feed(self, data: bytes) -> bytes:
        """Take the next chunk. Returns the file bytes in it: none until the header is complete."""
        if self.header is not None:
            return data
        self._buffer += data
        newline = self._buffer.find(b"\n")
        if newline < 0:
            if len(self._buffer) > HEADER_MAX:
                raise FileHeaderError("the file header is longer than 4 KB")
            return b""
        raw = bytes(self._buffer[:newline])
        rest = bytes(self._buffer[newline + 1 :])
        self._buffer = bytearray()
        self.header = _parse(raw)
        return rest


def _parse(raw: bytes) -> FileHeader:
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise FileHeaderError("the file header is not JSON") from exc
    name = doc.get("name") if isinstance(doc, dict) else None
    size = doc.get("size") if isinstance(doc, dict) else None
    if not isinstance(name, str) or not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise FileHeaderError("the file header needs a name and a size of zero or more")
    return FileHeader(name=safe_name(name), size=size)


def safe_name(name: str) -> str:
    """The base name only, with characters that cannot appear in a file name replaced, and a fallback for empty names."""
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = _UNSAFE.sub("_", base).strip(" .")
    if not cleaned:
        return "file"
    return cleaned[:NAME_MAX]


def unique_path(folder: Path, name: str) -> Path:
    """`folder/name`, or `name (2)`, `name (3)` and so on when that file already exists."""
    candidate = folder / name
    stem, suffix = candidate.stem, candidate.suffix
    counter = 2
    while candidate.exists():
        candidate = folder / f"{stem} ({counter}){suffix}"
        counter += 1
    return candidate
