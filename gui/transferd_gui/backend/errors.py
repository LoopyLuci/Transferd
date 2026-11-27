"""The errors the node adapter raises, and how an exception is shown to a user.

Kept apart from `node.py` so that the debugging part of the node can use them without importing the node.
"""

from __future__ import annotations

import asyncio

from transferd.errors import Closed, Denied, NoSuchService, RateLimited, Unreachable


class NodeError(Exception):
    """A failure with a message a user can act on. The service logs it and the views show it."""


def describe(exc: BaseException) -> str:
    """A short, readable reason for an exception."""
    if isinstance(exc, Unreachable):
        return "the peer did not answer the open request in time (it must accept within 15 s)"
    if isinstance(exc, Denied):
        return "the peer refused the stream"
    if isinstance(exc, NoSuchService):
        return "the peer does not offer that service"
    if isinstance(exc, RateLimited):
        return "the peer is rate limiting that service"
    if isinstance(exc, Closed):
        return "the session was closed"
    if isinstance(exc, asyncio.TimeoutError):
        return "timed out"
    if isinstance(exc, ConnectionRefusedError):
        return "connection refused"
    if isinstance(exc, OSError):
        return exc.strerror or str(exc)
    return str(exc) or type(exc).__name__
