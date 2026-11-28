"""The mailbox addresses a wormhole may meet on, and the list Settings holds. Text only: no network and no TLS here, so Settings
can check a value without loading either.

An address is `ws://` (this computer only) or `wss://`, with an optional pin after a `#`. The pin is the SHA-256 fingerprint of
the mailbox's certificate, as the computer hosting the mailbox shows it:

    wss://mailbox.example.net:4470#9f2c...   a mailbox on the network, held to its pinned certificate
    wss://mailbox.example.net                no pin: the system's certificates are checked; the port is 4470
    ws://127.0.0.1:4470                      this computer only, never reached over the network

The `#` part is never sent to a server. The setting is a comma-separated list. The first address is tried first, and the
next only when the first cannot be reached.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Tuple
from urllib.parse import urlsplit

DEFAULT_PORT = 4470
MAX_MAILBOXES = 4
MAX_ADDRESS_LENGTH = 200
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_FINGERPRINT = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class MailboxAddress:
    scheme: str  # "ws" or "wss"
    host: str
    port: int
    pin: str = ""  # lower-case SHA-256 of the certificate; "" means the system's certificates are checked

    @property
    def loopback(self) -> bool:
        return self.host in LOOPBACK_HOSTS

    @property
    def url(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{self.scheme}://{host}:{self.port}"

    def text(self) -> str:
        """The address as it is written in Settings, pin included."""
        return f"{self.url}#{self.pin}" if self.pin else self.url


def normalise_fingerprint(text: str) -> str:
    """A fingerprint as lower-case hex. Spaces, colons and dashes are allowed, since people copy them that way."""
    cleaned = re.sub(r"[\s:\-]", "", text).lower()
    if not _FINGERPRINT.fullmatch(cleaned):
        raise ValueError("a fingerprint is 64 hexadecimal characters, as the computer hosting the mailbox shows it")
    return cleaned


def display_fingerprint(pin: str) -> str:
    """A fingerprint in groups of eight, easy to read out and compare."""
    return " ".join(pin[i : i + 8] for i in range(0, len(pin), 8))


def parse_address(text: str) -> MailboxAddress:
    """One address. Raises ValueError with a message a user can act on."""
    value = text.strip()
    if not value:
        raise ValueError("an address is empty")
    if len(value) > MAX_ADDRESS_LENGTH:
        raise ValueError(f"an address is at most {MAX_ADDRESS_LENGTH} characters")
    if any(c.isspace() for c in value):
        raise ValueError(f"an address has spaces in it: '{value}'")
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError as exc:
        raise ValueError(f"'{value}' does not end in a port from 1 to 65535") from exc
    scheme = parts.scheme.lower()
    if scheme not in ("ws", "wss"):
        raise ValueError(f"'{value}' must start with wss:// (another computer) or ws:// (this computer only)")
    host = parts.hostname
    if not host:
        raise ValueError(f"'{value}' needs a host name or an address")
    if parts.username or parts.password:
        raise ValueError(f"'{value}' has a user name or password; a mailbox address has neither")
    if parts.path not in ("", "/") or parts.query:
        raise ValueError(f"'{value}' is only a host and a port, with an optional #fingerprint after them")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError(f"'{value}' has a port outside 1 to 65535")
    pin = ""
    if parts.fragment:
        if scheme == "ws":
            raise ValueError("a fingerprint after # is for wss:// addresses; ws:// is not encrypted and has none")
        pin = normalise_fingerprint(parts.fragment)
    if scheme == "ws" and host not in LOOPBACK_HOSTS:
        raise ValueError(f"ws:// reaches only this computer; use wss:// for '{host}'")
    return MailboxAddress(scheme, host, port or DEFAULT_PORT, pin)


def parse_list(text: str) -> Tuple[MailboxAddress, ...]:
    """The comma-separated list in Settings, in the order given. An empty text is no list. Raises ValueError."""
    if not text.strip():
        return ()
    entries = text.split(",")
    if len(entries) > MAX_MAILBOXES:
        raise ValueError(f"at most {MAX_MAILBOXES} mailboxes, tried in the order given")
    addresses = []
    for index, entry in enumerate(entries, start=1):
        if not entry.strip():
            raise ValueError(f"entry {index} of the list is empty; separate addresses with commas and nothing else")
        address = parse_address(entry)
        if address in addresses:
            raise ValueError(f"'{address.url}' is listed twice")
        addresses.append(address)
    return tuple(addresses)
