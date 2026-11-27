"""The channel a debug operation runs over: bytes one way and the other, a clean end, and an error end.

The agent and the client are written against this interface and know nothing of overlay sessions. The GUI node adapts an
overlay stream to it (`backend/debugnode.py`), and `pipe_pair` makes an in-memory pair for tests.
"""

from __future__ import annotations

import asyncio
from typing import Optional, Protocol, Tuple


class Channel(Protocol):
    """One end of a debug stream."""

    async def read(self) -> Optional[bytes]:
        """The next bytes from the other end, or None when it has ended (cleanly, or by an error)."""

    async def write(self, data: bytes) -> None:
        """Send bytes to the other end."""

    async def end(self) -> None:
        """Half-close: this side has nothing more to send. The other end reads everything written before this, then the
        end. This side can still read until the other end ends too. Must not be a full close: a reply still on its way
        from the other end must arrive."""

    async def close(self) -> None:
        """Release the channel: nothing more is read or written. The owner calls it once its operation is finished."""

    async def reset(self, reason: str) -> None:
        """End this side with an error. The other end reads what was written before this, then an error."""


class ChannelReset(Exception):
    """The other end ended the stream with an error."""


class _Pipe:
    def __init__(self) -> None:
        self.items: asyncio.Queue = asyncio.Queue()


class PipeChannel:
    """One end of an in-memory channel. Writes arrive at the other end in order. Used by tests, and by anything that
    needs a channel without a network."""

    def __init__(self, inbox: _Pipe, outbox: _Pipe) -> None:
        self._inbox = inbox
        self._outbox = outbox
        self._ended = False
        self._peer_done = False

    async def read(self) -> Optional[bytes]:
        if self._peer_done:
            return None
        item = await self._inbox.items.get()
        if item is None:
            self._peer_done = True
            return None
        kind, value = item
        if kind == "reset":
            self._peer_done = True
            raise ChannelReset(value)
        return value

    async def write(self, data: bytes) -> None:
        if self._ended:
            raise ChannelReset("the stream is already ended")
        await self._outbox.items.put(("data", data))

    async def end(self) -> None:
        if not self._ended:
            self._ended = True
            await self._outbox.items.put(None)

    async def close(self) -> None:
        await self.end()
        self._peer_done = True

    async def reset(self, reason: str) -> None:
        if not self._ended:
            self._ended = True
            await self._outbox.items.put(("reset", reason))


def pipe_pair() -> Tuple[PipeChannel, PipeChannel]:
    """Two connected ends: what one writes, the other reads."""
    a_to_b, b_to_a = _Pipe(), _Pipe()
    return PipeChannel(b_to_a, a_to_b), PipeChannel(a_to_b, b_to_a)
