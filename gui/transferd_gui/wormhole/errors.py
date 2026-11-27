"""The errors a wormhole raises. Each one carries a message fit to show the user."""

from __future__ import annotations


class WormholeError(Exception):
    """Anything that stops a wormhole. The message is what the user is told."""


class CodeError(WormholeError, ValueError):
    """The code does not read back: a missing or misplaced word, an unknown word, or a bad nameplate."""


class PakeError(WormholeError):
    """The key exchange failed: a malformed message from the other side."""


class OpenError(WormholeError):
    """A sealed message did not open. The code is wrong, or the message was changed, or it is not for this exchange."""


class MailboxError(WormholeError):
    """The mailbox refused a request, or could not be reached, or closed the connection."""


class ProtocolError(WormholeError):
    """The other side sent something the protocol does not allow at this point."""


class TimedOut(WormholeError):
    """The other side did not do its part in time. Usually it was never there, or it closed the app."""


class TransferError(WormholeError):
    """A file or folder did not arrive whole: a hash that does not match, a size that is wrong, or an unsafe name."""
