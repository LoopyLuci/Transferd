"""Hosting a mailbox on this computer. The server runs on the backend loop, with the settings' port.

Off until Settings turns it on. With it on:
- a wormhole with no mailbox set in Settings meets on this computer's own mailbox;
- with `wormhole_host_network` also on, other computers can reach it. It serves wss:// with a certificate this computer made
  for itself, and the fingerprint of that certificate is what the other computers pin. The share line is the address and the
  fingerprint together, ready to paste into their Settings.

Without the network setting, it listens on 127.0.0.1 only, and the address is ws://127.0.0.1, which nothing else can reach.
The status is read from the Qt thread, so it is kept under a lock; the server itself is only touched on the backend loop.
"""

from __future__ import annotations

import socket
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Optional, Tuple

from transferd_gui.model.settings import Settings
from transferd_gui.wormhole import tls
from transferd_gui.wormhole.server import MailboxServer

LOCAL_HOST = "127.0.0.1"
NETWORK_BIND = "0.0.0.0"


@dataclass(frozen=True)
class HostStatus:
    """What the hosted mailbox is doing, for the screens to show."""

    enabled: bool = False  # Settings has hosting turned on
    running: bool = False  # the server is listening now
    port: int = 0  # the port it listens on (or was asked to, when it could not)
    network: bool = False  # reachable from other computers, over wss:// with a pinned certificate
    fingerprint: str = ""  # the pin for the other computers; "" unless it is on the network
    error: str = ""  # why it is not running, in words a user can act on

    def share_line(self, host: str) -> str:
        """The address another computer adds to its mailbox list, given this computer's network `host`."""
        if not (self.running and self.network and host):
            return ""
        return f"wss://{host}:{self.port}#{self.fingerprint}"


def network_host() -> str:
    """This computer's address on the local network, as other computers would reach it. Empty when it cannot be found."""
    try:
        # Connecting a UDP socket sends nothing; it only makes the system pick the interface it would use.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("10.255.255.255", 1))
            address = probe.getsockname()[0]
            if address and not address.startswith("127."):
                return address
    except OSError:
        pass
    try:
        return socket.gethostname()
    except OSError:
        return ""


class MailboxHost:
    """Starts, stops and reports the mailbox this computer hosts. One instance per app."""

    def __init__(self, folder: Path, *, network_bind: str = NETWORK_BIND) -> None:
        self._folder = folder
        self._network_bind = network_bind
        self._lock = threading.Lock()
        self._server: Optional[MailboxServer] = None
        self._config: Optional[Tuple[int, bool]] = None
        self._status = HostStatus()

    # ----- the backend loop ----------------------------------------------------------------------------

    async def apply(self, settings: Settings) -> None:
        """Make the hosting match the settings: stop it, start it, or restart it when the port or the network changed."""
        if not settings.wormhole_host:
            await self.stop()
            self._set(HostStatus())
            return
        wanted = (settings.wormhole_host_port, settings.wormhole_host_network)
        if self._server is not None and self._config == wanted:
            return
        await self.stop()
        await self._start(*wanted)

    async def stop(self) -> None:
        server, self._server = self._server, None
        self._config = None
        if server is not None:
            await server.close()
        with self._lock:
            self._status = replace(self._status, running=False)

    async def _start(self, port: int, network: bool) -> None:
        self._set(HostStatus(enabled=True, port=port, network=network))
        fingerprint = ""
        context = None
        if network:
            try:
                identity = tls.load_or_create(self._folder)
                context = tls.server_context(identity)
                fingerprint = identity.fingerprint
            except Exception as exc:  # a folder that cannot be written, or a certificate that cannot be read or made
                self._set(HostStatus(enabled=True, port=port, network=True, error=f"could not make the mailbox's certificate: {exc}"))
                return
        server = MailboxServer()
        try:
            bound = await server.start(self._network_bind if network else LOCAL_HOST, port, tls=context)
        except OSError as exc:
            reason = exc.strerror or str(exc)
            self._set(HostStatus(enabled=True, port=port, network=network, error=f"could not listen on port {port}: {reason}"))
            return
        self._server = server
        self._config = (port, network)
        self._set(HostStatus(enabled=True, running=True, port=bound, network=network, fingerprint=fingerprint))

    # ----- reading (any thread) --------------------------------------------------------------------------

    def status(self) -> HostStatus:
        with self._lock:
            return self._status

    def address_here(self) -> str:
        """The address this computer's own wormholes meet on when no mailbox is set. Empty when nothing is hosted."""
        status = self.status()
        if not status.running:
            return ""
        if status.network:
            return f"wss://{LOCAL_HOST}:{status.port}#{status.fingerprint}"
        return f"ws://{LOCAL_HOST}:{status.port}"

    def share_line(self) -> str:
        """The line another computer pastes into its mailbox list. Empty unless the mailbox is on the network."""
        return self.status().share_line(network_host())

    def _set(self, status: HostStatus) -> None:
        with self._lock:
            self._status = status
