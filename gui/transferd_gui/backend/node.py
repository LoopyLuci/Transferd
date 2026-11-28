"""The node: the GUI's adapter over `transferd`. Everything here runs on the backend's asyncio loop.

A node owns one identity, an optional direct listener, at most one relay registration, and the sessions it has with peers
and the streams over them. The views reach it through `TransferdService`. `snapshot()` reads the live library objects into
plain values, and nothing else leaves this module.

Two things the library does not keep are observed here, without changing it: the bytes received on each route (by
counting the frames `try_recv` hands to the session) and each relay registration's outcome (by wrapping
`RelayClient.register`). See `observe.py`.
"""

from __future__ import annotations

import asyncio
import math
import os
import socket
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Deque, Dict, List, Optional, Set

from transferd.errors import InviteError, TransferdError
from transferd.handshake import handshake_initiator, handshake_responder
from transferd.identity import HybridIdentity, key_fingerprint, relay_token_for
from transferd.invite import MadeInvite, accept_invite, make_invite
from transferd.link import Link, RouteKind, TcpLink
from transferd.overlay import OverlaySession, SessionSetup, spawn
from transferd.punch import addr_text
from transferd.relay_hub import REREGISTER_EVERY, RelayHub

from transferd_gui.backend.debugnode import DebugNodeMixin
from transferd_gui.backend.errors import NodeError, describe
from transferd_gui.backend.files import (
    CHUNK,
    FILE_SERVICE,
    HeaderReader,
    encode_header,
    unique_path,
)
from transferd_gui.debug import wire
from transferd_gui.backend.observe import count_probe_answers, count_received, count_sent, observe_async
from transferd_gui.model.settings import NODE_NAME_MAX, Settings, parse_host_port
from transferd_gui.model.snapshot import (
    IdentitySnapshot,
    IncomingRequest,
    ListenerSnapshot,
    LocateResult,
    NodeSnapshot,
    RelaySnapshot,
    RouteSnapshot,
    SessionSnapshot,
    TransferRecord,
)

IDENTITY_FILE = "identity.seed"
MIB = 1024 * 1024
#: How often the node publishes a snapshot of itself.
SNAPSHOT_EVERY_S = 1.0
#: How long a reply to a stream we opened may take before we stop waiting for it.
REPLY_WAIT_S = 10.0
#: How long an incoming stream may stay silent before it is closed.
IDLE_S = 30.0
#: The most reply bytes kept from a stream we opened.
REPLY_LIMIT = MIB
#: How many finished transfers the node remembers.
HISTORY = 100
#: Source names used in the event log.
SOURCE_NODE, SOURCE_RELAY, SOURCE_PEER, SOURCE_TRANSFER = "node", "relay", "peer", "transfer"

Log = Callable[[str, str, str], None]
Publish = Callable[[NodeSnapshot], None]


@dataclass
class _Peer:
    node_id: str
    session: OverlaySession
    links: List[Link]
    events: asyncio.Queue
    via: str
    established_at: float
    relay_token: Optional[bytes] = None
    watcher: Optional[asyncio.Task] = None


@dataclass
class _Pending:
    request_id: int
    peer_id: str
    session: OverlaySession
    stream: int
    service: str
    received_at: float


@dataclass
class _RelayRecord:
    address: str = ""
    state: str = "off"
    detail: str = "not connected"
    first_ok: Optional[float] = None
    last_ok: Optional[float] = None
    ok: int = 0
    failures: int = 0


class Node(DebugNodeMixin):
    """One Transferd node in the GUI: its identity, its listener, its relay registration and its peers."""

    def __init__(self, settings: Settings, log: Log, publish: Optional[Publish] = None) -> None:
        self._settings = settings
        self._log = log
        self._publish = publish
        self._identity: Optional[HybridIdentity] = None
        self._server: Optional[asyncio.base_events.Server] = None
        self._listen_port: Optional[int] = None
        self._listen_error = ""
        self._hub: Optional[RelayHub] = None
        self._relay = _RelayRecord(address=settings.relay_address)
        self._peers: Dict[str, _Peer] = {}
        self._pending: Dict[int, _Pending] = {}
        self._next_request = 1
        #: Per link id: frames and bytes in each direction, and probes answered. Counted as the session uses the link.
        self._link_rx: Dict[int, int] = {}
        self._link_tx: Dict[int, int] = {}
        self._link_answers: Dict[int, int] = {}
        self._transfers: Deque[TransferRecord] = deque(maxlen=HISTORY)
        self._locate: Optional[LocateResult] = None
        self._tasks: Set[asyncio.Task] = set()
        self._debug_setup()

    # ----- identity -----------------------------------------------------------------------------

    @property
    def identity(self) -> HybridIdentity:
        if self._identity is None:
            raise NodeError("the node has not started, so it has no identity yet")
        return self._identity

    def _load_identity(self) -> HybridIdentity:
        """The identity stored in the data directory. A new one is made on first start, and its seed is written owner-only."""
        folder = Path(self._settings.data_dir)
        path = folder / IDENTITY_FILE
        name = self._settings.node_name or socket.gethostname()[:NODE_NAME_MAX]
        try:
            if path.exists():
                seed = bytes.fromhex(path.read_text(encoding="ascii").strip())
                if len(seed) != 64:
                    raise NodeError(f"{path} does not hold a 64-byte identity seed")
                return HybridIdentity(seed, name)
            folder.mkdir(parents=True, exist_ok=True)
            seed = os.urandom(64)
            path.write_text(seed.hex() + "\n", encoding="ascii")
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass  # Windows keeps the folder's permissions; the file is still in the user's own data folder
        except (OSError, ValueError) as exc:
            raise NodeError(f"could not read or write the identity file {path}: {describe(exc)}") from exc
        self._log("info", SOURCE_NODE, "created a new node identity in the data directory")
        return HybridIdentity(seed, name)

    # ----- lifecycle ----------------------------------------------------------------------------

    async def start(self) -> None:
        """Load the identity, start the snapshot ticker, open the listener when incoming is allowed, and connect the relay
        when one is set. A relay that cannot be reached is logged and shown; it does not stop the node."""
        if self._identity is None:
            self._identity = self._load_identity()
        self._log("info", SOURCE_NODE, f"node {self.identity.node_id[:12]} started as '{self.identity.name}'")
        self._spawn(self._tick())
        self._debug_start()
        if self._settings.allow_incoming:
            await self._open_listener()
        if self._settings.relay_address:
            try:
                await self.connect_relay()
            except NodeError:
                pass  # already logged and shown on the Relay screen

    async def stop(self) -> None:
        """Close relay registration, sessions, listener and the ticker. Every waiting call is released."""
        await self._debug_stop()
        await self.disconnect_relay()
        for peer in list(self._peers.values()):
            await peer.session.shutdown()
        self._close_listener()
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def apply_settings(self, settings: Settings) -> None:
        """Take new settings. Changing whether incoming is allowed opens or closes the listener, and changing the relay
        address drops the current registration (the user connects again)."""
        old, self._settings = self._settings, settings
        self._debug_settings_changed(old, settings)
        if self._identity is not None and old.debug_enabled != settings.debug_enabled:
            await self._debug_readvertise()
            self._log("info", SOURCE_NODE, "debugging is " + ("on: this node answers debug requests from the computers on its list" if settings.debug_enabled else "off"))
        if self._identity is not None and settings.node_name != old.node_name:
            self._log("info", SOURCE_NODE, "the node name takes effect after a restart")
        if settings.relay_address != old.relay_address and self._hub is not None:
            await self.disconnect_relay()
            self._log("info", SOURCE_RELAY, "the relay address changed; connect again from the Relay screen")
        if self._identity is None:
            return
        if settings.allow_incoming and (self._server is None or settings.listen_port != old.listen_port):
            self._close_listener()
            await self._open_listener()
        elif not settings.allow_incoming and self._server is not None:
            self._close_listener()
            self._log("info", SOURCE_NODE, "stopped accepting incoming connections")

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _tick(self) -> None:
        while True:
            await asyncio.sleep(SNAPSHOT_EVERY_S)
            if self._publish is not None:
                self._publish(self.snapshot())

    # ----- listener -----------------------------------------------------------------------------

    async def _open_listener(self) -> None:
        port = self._settings.listen_port
        try:
            server = await asyncio.start_server(self._serve_direct, host="0.0.0.0", port=port)
        except OSError as exc:
            self._listen_error = f"could not listen on port {port}: {describe(exc)}"
            self._log("error", SOURCE_NODE, self._listen_error)
            return
        self._server = server
        self._listen_port = int(server.sockets[0].getsockname()[1])
        self._listen_error = ""
        self._log("info", SOURCE_NODE, f"accepting incoming connections on port {self._listen_port}")

    def _close_listener(self) -> None:
        if self._server is not None:
            self._server.close()
            self._server = None
            self._listen_port = None

    async def _serve_direct(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Answer a direct connection. Refused, with the connection closed, when incoming is not allowed."""
        where = writer.get_extra_info("peername")
        where_text = addr_text((where[0], where[1])) if where else "an unknown address"
        if not self._settings.allow_incoming:
            writer.close()
            return
        try:
            result = await asyncio.wait_for(handshake_responder(reader, writer, self.identity), self._settings.peer_connect_timeout)
        except Exception as exc:
            self._log("warning", SOURCE_PEER, f"refused a connection from {where_text}: {describe(exc)}")
            writer.close()
            return
        link = TcpLink(reader, writer, result.keys, kind=RouteKind.DIRECT, describe=f"tcp://{where_text}")
        link.start()
        existing = self._peers.get(result.peer_node_id)
        if existing is not None:
            # A second route to a peer we already have a session with (for example the same device over USB and Wi-Fi):
            # it becomes one more route of that session, not a refused connection.
            await self._add_route(existing, link)
            return
        self._start_peer(result.peer_node_id, [link], via="direct", relay_token=None)

    # ----- relay --------------------------------------------------------------------------------

    def _admit(self, node_id: str, key: bytes) -> bool:
        """Whether a peer that dials in through the relay may connect: the same rule as the direct listener."""
        return self._settings.allow_incoming

    async def connect_relay(self, address: Optional[str] = None) -> None:
        """Open the socket and register this node's token with the relay at `address` (or the setting)."""
        target = (address if address is not None else self._settings.relay_address).strip()
        if not target:
            raise NodeError("set a relay address in Settings first")
        try:
            host, port = parse_host_port(target)
        except ValueError as exc:
            raise NodeError(str(exc)) from exc
        if self._hub is not None:
            await self.disconnect_relay()
        self._relay = _RelayRecord(address=target, state="connecting", detail=f"registering with {target}")
        hub = RelayHub(
            (host, port),
            relay_token_for(self.identity.node_id),
            self.identity,
            admit=self._admit,
            on_adopt=self._adopt_relay,
            timeout=self._settings.relay_request_timeout,
        )
        observe_async(hub.client, "register", on_ok=self._registered, on_error=self._registration_failed)
        try:
            await hub.start()
        except Exception as exc:
            reason = describe(exc)
            self._relay.state = "failed"
            self._relay.detail = f"could not register with {target}: {reason}"
            self._log("error", SOURCE_RELAY, self._relay.detail)
            raise NodeError(self._relay.detail) from exc
        self._hub = hub
        self._log("info", SOURCE_RELAY, f"registered with relay {target}")

    def _registered(self) -> None:
        now = time.time()
        if self._relay.first_ok is None:
            self._relay.first_ok = now
        self._relay.last_ok = now
        self._relay.ok += 1
        self._relay.state = "registered"
        self._relay.detail = "registered"

    def _registration_failed(self, exc: BaseException) -> None:
        self._relay.failures += 1
        self._relay.state = "failed"
        self._relay.detail = f"the last registration failed: {describe(exc)}"
        self._log("warning", SOURCE_RELAY, self._relay.detail)

    async def disconnect_relay(self) -> None:
        hub, self._hub = self._hub, None
        for peer in [p for p in self._peers.values() if p.via == "relay"]:
            await peer.session.shutdown()
        if hub is not None:
            await hub.close()
            self._log("info", SOURCE_RELAY, "disconnected from the relay")
        self._relay.state = "off"
        self._relay.detail = "not connected"

    async def _adopt_relay(self, link, peer_id: str, peer_key: bytes, sender_token: bytes) -> None:
        """A peer dialled in through the relay and its handshake verified: make its link a session."""
        if not self._settings.allow_incoming or peer_id in self._peers:
            link.close()
            self._log("warning", SOURCE_PEER, f"closed a relay connection from {peer_id[:12]}: incoming is off or already connected")
            return
        self._start_peer(peer_id, [link], via="relay", relay_token=sender_token)

    async def locate(self, peer_id: str) -> LocateResult:
        """Ask the relay where this node and the peer appear from the outside, and which addresses a punch would try."""
        hub = self._require_hub()
        token = relay_token_for(peer_id)
        try:
            located = await hub.locate(token)
            candidates = await hub.punch_candidates(token)
        except TransferdError as exc:
            raise NodeError(f"could not locate {peer_id[:12]} through the relay: {describe(exc)}") from exc
        result = LocateResult(
            peer_id=peer_id,
            self_addr=addr_text(located.self_addr),
            peer_addr=addr_text(located.peer_addr) if located.peer_addr else None,
            when=time.time(),
            punch_candidates=tuple(c.addr for c in candidates),
        )
        self._locate = result
        self._log("info", SOURCE_RELAY, f"located {peer_id[:12]}: " + ("not registered at the relay" if result.peer_addr is None else f"at {result.peer_addr}"))
        return result

    def _require_hub(self) -> RelayHub:
        if self._hub is None:
            raise NodeError("connect to a relay first")
        return self._hub

    # ----- invites ------------------------------------------------------------------------------

    async def make_invite(self, ttl_s: int) -> MadeInvite:
        """An invite that tells a peer how to reach this node. It needs incoming allowed, and an address to put in it."""
        if not self._settings.allow_incoming:
            raise NodeError("turn on 'accept incoming connections' so a peer who accepts this invite can reach the node")
        relays = [self._settings.relay_address] if self._settings.relay_address else []
        direct = [self._settings.advertised_address] if self._settings.advertised_address else []
        if not relays and not direct:
            raise NodeError("set a relay address or an advertised address in Settings, so the invite says how to reach this node")
        made = make_invite(self.identity, relays, direct, ttl_s)
        self._log("info", SOURCE_NODE, f"made an invite that expires at {time.strftime('%H:%M:%S', time.localtime(made.expires_at))}")
        return made

    async def dial_invite(self, text: str) -> str:
        """Verify an invite, then connect to its inviter: direct addresses first, then the relay when this node is on one."""
        try:
            invite = accept_invite(text)
        except InviteError as exc:
            raise NodeError(f"the invite does not verify: {exc}") from exc
        if invite.node_id == self.identity.node_id:
            raise NodeError("that invite was made by this node")
        if invite.node_id in self._peers:
            return invite.node_id
        problems: List[str] = []
        for address in invite.direct:
            try:
                host, port = parse_host_port(address)
            except ValueError as exc:
                problems.append(f"{address}: {exc}")
                continue
            try:
                return await self.dial_direct(host, port, expect_node_id=invite.node_id, expect_fingerprint=invite.key_fingerprint)
            except (NodeError, TransferdError, OSError, asyncio.TimeoutError) as exc:
                problems.append(f"{address}: {describe(exc)}")
        if self._hub is not None:
            try:
                return await self.dial_relay(invite.node_id, expect_fingerprint=invite.key_fingerprint)
            except (NodeError, TransferdError, OSError, asyncio.TimeoutError) as exc:
                problems.append(f"through the relay: {describe(exc)}")
        elif invite.relays:
            problems.append(f"the invite names relay {invite.relays[0]}; connect to it from the Relay screen to reach through it")
        if not problems:
            raise NodeError("the invite lists no address this node can use to reach the inviter")
        raise NodeError("could not reach the inviter: " + "; ".join(problems))

    # ----- dialling -----------------------------------------------------------------------------

    async def dial_direct(
        self,
        host: str,
        port: int,
        expect_node_id: Optional[str] = None,
        expect_fingerprint: Optional[bytes] = None,
    ) -> str:
        """Connect to a peer's listener directly. Returns the peer's node id once its session is ready."""
        timeout = self._settings.peer_connect_timeout
        address = f"{host}:{port}"
        self._log("info", SOURCE_PEER, f"connecting directly to {address}")
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
        except (OSError, asyncio.TimeoutError) as exc:
            raise NodeError(f"could not reach {address}: {describe(exc)}") from exc
        try:
            result = await asyncio.wait_for(handshake_initiator(reader, writer, self.identity, expect_node_id), timeout)
        except Exception as exc:
            writer.close()
            raise NodeError(f"the handshake with {address} failed: {describe(exc)}") from exc
        if expect_fingerprint is not None and key_fingerprint(result.peer_hybrid_public_key) != expect_fingerprint:
            writer.close()
            raise NodeError(f"the key at {address} does not match the invite")
        if result.peer_node_id in self._peers:
            writer.close()
            raise NodeError(f"already connected to {result.peer_node_id[:12]}")
        link = TcpLink(reader, writer, result.keys, kind=RouteKind.DIRECT, describe=f"tcp://{address}")
        link.start()
        peer = self._start_peer(result.peer_node_id, [link], via="direct", relay_token=None)
        await self._await_ready(peer, timeout)
        return result.peer_node_id

    async def dial_relay(self, peer_id: str, expect_fingerprint: Optional[bytes] = None) -> str:
        """Connect to a peer through the relay this node is registered with."""
        hub = self._require_hub()
        if peer_id in self._peers:
            raise NodeError(f"already connected to {peer_id[:12]}")
        token = relay_token_for(peer_id)
        self._log("info", SOURCE_PEER, f"connecting to {peer_id[:12]} through the relay")
        try:
            keys, peer_key = await hub.handshake(peer_id, token)
        except TransferdError as exc:
            raise NodeError(f"the handshake through the relay failed: {describe(exc)}") from exc
        if expect_fingerprint is not None and key_fingerprint(peer_key) != expect_fingerprint:
            raise NodeError("the key the peer presented through the relay does not match the invite")
        link = hub.link(token, keys)
        peer = self._start_peer(peer_id, [link], via="relay", relay_token=token)
        await self._await_ready(peer, self._settings.peer_connect_timeout)
        return peer_id

    async def _await_ready(self, peer: _Peer, timeout: float) -> None:
        if not await peer.session.ready(timeout):
            self._drop_peer(peer, "the peer did not finish the session in time")
            await peer.session.shutdown()
            raise NodeError(f"{peer.node_id[:12]} did not finish the session within {timeout:g} s")

    # ----- peers and sessions -------------------------------------------------------------------

    def _start_peer(self, peer_id: str, links: List[Link], via: str, relay_token: Optional[bytes]) -> _Peer:
        """Make a session over these links, and watch its events. Call from the backend loop."""
        for link in links:
            self._observe_link(link)
        events: asyncio.Queue = asyncio.Queue()
        setup = SessionSetup(peer_node_id=peer_id, peer_name="", identity=self.identity, services=self._advertised_services())
        session = spawn(setup, links, events)
        peer = _Peer(
            node_id=peer_id,
            session=session,
            links=list(links),
            events=events,
            via=via,
            established_at=time.time(),
            relay_token=relay_token,
        )
        self._peers[peer_id] = peer
        peer.watcher = self._spawn(self._watch(peer))
        self._log("info", SOURCE_PEER, f"{via} session with {peer_id[:12]} started")
        return peer

    def _observe_link(self, link: Link) -> None:
        """Count what the session does with this link. Only observation: the library's behaviour does not change."""
        lid = link.id

        def received(frame) -> None:
            self._link_rx[lid] = self._link_rx.get(lid, 0) + len(frame.body)

        def sent(frame) -> None:
            self._link_tx[lid] = self._link_tx.get(lid, 0) + len(frame.body)

        def answered(_rtt_ms: float) -> None:
            self._link_answers[lid] = self._link_answers.get(lid, 0) + 1

        count_received(link, received)
        count_sent(link, sent)
        count_probe_answers(link, answered)

    async def _watch(self, peer: _Peer) -> None:
        """Read the session's events: stream requests become pending items, and the end of the session drops the peer."""
        while True:
            event = await peer.events.get()
            if event.kind == "open_request":
                self._new_request(peer, event.stream, event.service)
            elif event.kind == "down":
                self._drop_peer(peer, event.reason or "the session ended")
                return
            elif event.kind == "ready":
                self._log("info", SOURCE_PEER, f"session with {peer.node_id[:12]} is ready (protocol version {event.version})")
            elif event.kind == "refused":
                self._log("warning", SOURCE_PEER, f"{peer.node_id[:12]} refused the session: {event.reason}")
            elif event.kind == "route_changed":
                state = "up" if event.up else "down"
                self._log("info", SOURCE_PEER, f"a {event.route.value if event.route else ''} route to {peer.node_id[:12]} is {state}")
            elif event.kind == "punch":
                self._log("warning", SOURCE_PEER, f"{peer.node_id[:12]} sent a hole-punch message; hole punching is not available in this build")

    def _new_request(self, peer: _Peer, stream: int, service: str) -> None:
        request_id = self._next_request
        self._next_request += 1
        self._pending[request_id] = _Pending(request_id, peer.node_id, peer.session, stream, service, time.time())
        if wire.op_of(service) is not None:
            self._debug_incoming(request_id)
            return
        if service == FILE_SERVICE and self._settings.auto_accept_files:
            self._log("info", SOURCE_PEER, f"{peer.node_id[:12]} sends a file; accepting it (auto-accept is on)")
            self._spawn(self.accept_request(request_id))
            return
        self._log("info", SOURCE_PEER, f"{peer.node_id[:12]} asks to open '{service}' (request {request_id})")

    def _drop_peer(self, peer: _Peer, reason: str) -> None:
        if self._peers.get(peer.node_id) is not peer:
            return
        del self._peers[peer.node_id]
        for link in peer.links:
            link.close()
            for counter in (self._link_rx, self._link_tx, self._link_answers):
                counter.pop(link.id, None)
        if peer.relay_token is not None and self._hub is not None:
            self._hub.forget(peer.relay_token)
        for request_id in [r for r, p in self._pending.items() if p.peer_id == peer.node_id]:
            del self._pending[request_id]
        self._log("warning", SOURCE_PEER, f"session with {peer.node_id[:12]} ended: {reason}")

    async def disconnect_peer(self, peer_id: str) -> None:
        """End a session. Its links close and its peer leaves the list when the session reports it has ended."""
        peer = self._peers.get(peer_id)
        if peer is None:
            raise NodeError("that peer is not connected")
        await peer.session.shutdown()
        self._log("info", SOURCE_PEER, f"disconnected from {peer_id[:12]}")

    def _require_ready_peer(self, peer_id: str) -> _Peer:
        peer = self._peers.get(peer_id)
        if peer is None:
            raise NodeError("that peer is not connected")
        if not peer.session.is_usable():
            raise NodeError("that peer has no live route right now")
        return peer

    async def accept_request(self, request_id: int) -> None:
        """Let a waiting stream open, and start receiving it: a file when the service is `file`, otherwise a plain stream."""
        pending = self._pending.pop(request_id, None)
        if pending is None:
            raise NodeError("that request is no longer waiting")
        await pending.session.authorize(pending.stream, pending.service, True)
        if wire.op_of(pending.service) is not None:
            self._spawn(self._serve_debug(pending))
            return
        if pending.service == FILE_SERVICE:
            self._spawn(self._receive_file(pending))
        else:
            self._spawn(self._receive_stream(pending))

    async def refuse_request(self, request_id: int) -> None:
        pending = self._pending.pop(request_id, None)
        if pending is None:
            raise NodeError("that request is no longer waiting")
        await pending.session.authorize(pending.stream, pending.service, False, "refused by the user")
        if wire.op_of(pending.service) is not None:
            self._debug_audit("info", f"refused debug {wire.op_of(pending.service)} from {pending.peer_id[:12]} (by the user)")
            return
        self._log("info", SOURCE_TRANSFER, f"refused '{pending.service}' from {pending.peer_id[:12]}")

    def _limit_bytes(self) -> int:
        return self._settings.max_transfer_mib * MIB

    async def _receive_file(self, pending: _Pending) -> None:
        """Write an incoming file into the download folder. The file is removed again if the transfer does not finish."""
        session, stream, peer_id = pending.session, pending.stream, pending.peer_id
        reader = HeaderReader()
        sink = None
        path: Optional[Path] = None
        header = None
        written = 0
        name = "unknown"
        try:
            while True:
                chunk = await asyncio.wait_for(session.recv(stream), IDLE_S)
                if chunk is None:
                    break
                body = reader.feed(chunk)
                if header is None and reader.header is not None:
                    header = reader.header
                    name = header.name
                    if header.size > self._limit_bytes():
                        raise NodeError(f"the file is {header.size} bytes, over the {self._settings.max_transfer_mib} MiB limit")
                    folder = Path(self._settings.download_dir)
                    folder.mkdir(parents=True, exist_ok=True)
                    path = unique_path(folder, header.name)
                    sink = path.open("wb")
                if sink is not None and body:
                    written += len(body)
                    if written > header.size:
                        raise NodeError("the sender sent more bytes than its header promised")
                    await asyncio.to_thread(sink.write, body)
            if header is None:
                raise NodeError("the stream ended before a file header arrived")
            if written != header.size:
                raise NodeError(f"the stream ended after {written} of {header.size} bytes")
        except asyncio.TimeoutError:
            self._fail_transfer(path, sink, "received", peer_id, name, written, "timed out waiting for data")
        except Exception as exc:  # any failure ends this one transfer, and is recorded against it
            self._fail_transfer(path, sink, "received", peer_id, name, written, describe(exc))
        else:
            sink.close()
            self._record("received", peer_id, FILE_SERVICE, name, written, "done", str(path))
        finally:
            if sink is not None and not sink.closed:
                sink.close()
            await session.close(stream)

    def _fail_transfer(self, path: Optional[Path], sink, direction: str, peer_id: str, name: str, size: int, reason: str) -> None:
        if sink is not None:
            sink.close()
        if path is not None and path.exists():
            path.unlink(missing_ok=True)
        self._record(direction, peer_id, FILE_SERVICE, name, size, "failed", reason)

    async def _receive_stream(self, pending: _Pending) -> None:
        """Read a plain stream to its end and keep what it carried, up to the transfer limit."""
        session, stream = pending.session, pending.stream
        data = bytearray()
        status = "done"
        try:
            while True:
                chunk = await asyncio.wait_for(session.recv(stream), IDLE_S)
                if chunk is None:
                    break
                data += chunk
                if len(data) > self._limit_bytes():
                    status = "cut at the transfer limit"
                    break
        except asyncio.TimeoutError:
            status = "timed out waiting for the end of the stream"
        finally:
            await session.close(stream)
        self._record("received", pending.peer_id, pending.service, "", len(data), status, f"{len(data)} bytes")

    # ----- sending ------------------------------------------------------------------------------

    async def send_file(self, peer_id: str, path: str) -> None:
        """Stream a file to a connected peer. The bytes are handed to the session; this does not wait for delivery."""
        peer = self._require_ready_peer(peer_id)
        source = Path(path)
        if not source.is_file():
            raise NodeError(f"{source} is not a file")
        size = source.stat().st_size
        if size > self._limit_bytes():
            raise NodeError(f"{source.name} is {size} bytes, over the {self._settings.max_transfer_mib} MiB limit (see Settings)")
        session = peer.session
        stream = None
        try:
            stream = await self._open(session, peer_id, FILE_SERVICE)
            await session.send(stream, encode_header(source.name, size))
            remaining = size
            with source.open("rb") as handle:
                while remaining > 0:
                    chunk = await asyncio.to_thread(handle.read, min(CHUNK, remaining))
                    if not chunk:
                        raise NodeError(f"{source.name} got shorter while it was being sent")
                    remaining -= len(chunk)
                    await session.send(stream, chunk)
            await session.send(stream, b"", fin=True)
        except Exception as exc:
            if stream is not None:
                await session.close(stream)
            self._record("sent", peer_id, FILE_SERVICE, source.name, size, "failed", describe(exc))
            raise NodeError(f"sending {source.name} failed: {describe(exc)}") from exc
        self._record("sent", peer_id, FILE_SERVICE, source.name, size, "handed to the session", "")

    async def _open(self, session: OverlaySession, peer_id: str, service: str) -> int:
        """Ask the peer to open a stream. A refusal, a missing service or no answer becomes a readable NodeError."""
        try:
            return await session.open(service)
        except TransferdError as exc:
            raise NodeError(f"the peer did not open '{service}': {describe(exc)}") from exc

    async def open_stream(self, peer_id: str, service: str, payload: bytes) -> bytes:
        """Open a stream to a service the peer exposes, send `payload`, and return what comes back until the end."""
        peer = self._require_ready_peer(peer_id)
        if len(payload) > self._limit_bytes():
            raise NodeError(f"the payload is over the {self._settings.max_transfer_mib} MiB limit")
        session = peer.session
        stream = await self._open(session, peer_id, service)
        reply = bytearray()
        status = "done"
        try:
            await session.send(stream, payload, fin=True)
            while True:
                try:
                    chunk = await asyncio.wait_for(session.recv(stream), REPLY_WAIT_S)
                except asyncio.TimeoutError:
                    status = "no end to the reply within the wait"
                    break
                if chunk is None:
                    break
                reply += chunk
                if len(reply) > REPLY_LIMIT:
                    status = "reply cut at 1 MiB"
                    break
        finally:
            await session.close(stream)
        self._record("sent", peer_id, service, "", len(payload), status, f"{len(reply)} bytes came back")
        return bytes(reply)

    # ----- records ------------------------------------------------------------------------------

    def _record(self, direction: str, peer_id: str, service: str, name: str, size: int, status: str, detail: str) -> None:
        record = TransferRecord(time.time(), direction, peer_id, service, name, size, status, detail)
        self._transfers.append(record)
        level = "error" if status == "failed" else "info"
        label = name or service
        self._log(level, SOURCE_TRANSFER, f"{direction} {label} ({size} bytes) with {peer_id[:12]}: {status}" + (f" ({detail})" if detail else ""))

    # ----- snapshot -----------------------------------------------------------------------------

    def snapshot(self) -> NodeSnapshot:
        """The node as it is now. Every value is read from the library, or is None when it has not been measured."""
        now = time.time()
        sessions = tuple(self._session_snapshot(peer) for peer in self._peers.values())
        return NodeSnapshot(
            taken_at=now,
            identity=self._identity_snapshot(),
            listener=self._listener_snapshot(),
            relay=self._relay_snapshot(),
            sessions=sessions,
            requests=tuple(
                IncomingRequest(p.request_id, p.peer_id, self._peer_name(p.peer_id), p.service, p.received_at)
                for p in self._pending.values()
            ),
            transfers=tuple(self._transfers),
            locate=self._locate,
            debug=self.debug_snapshot(),
        )

    def _peer_name(self, peer_id: str) -> str:
        peer = self._peers.get(peer_id)
        return (peer.session.peer_name if peer is not None and peer.session.peer_name else peer_id[:12])

    def _identity_snapshot(self) -> IdentitySnapshot:
        if self._identity is None:
            return IdentitySnapshot(ready=False)
        identity = self._identity
        return IdentitySnapshot(
            ready=True,
            name=identity.name,
            node_id=identity.node_id,
            fingerprint=key_fingerprint(identity.public_key).hex(),
            ed25519_public=identity.ed25519_public_key.hex(),
            mldsa_public=identity.public_key[32:].hex(),
            relay_token=relay_token_for(identity.node_id).hex(),
        )

    def _listener_snapshot(self) -> ListenerSnapshot:
        if self._server is not None and self._listen_port is not None:
            return ListenerSnapshot(active=True, port=self._listen_port, detail=f"accepting on port {self._listen_port}")
        if self._listen_error:
            return ListenerSnapshot(active=False, detail=self._listen_error)
        return ListenerSnapshot(active=False, detail="off: incoming connections are not allowed")

    def _relay_snapshot(self) -> RelaySnapshot:
        record = self._relay
        next_at = record.last_ok + REREGISTER_EVERY if self._hub is not None and record.last_ok is not None else None
        return RelaySnapshot(
            configured=bool(self._settings.relay_address),
            address=record.address or self._settings.relay_address,
            state=record.state,
            detail=record.detail,
            registered_at=record.first_ok,
            last_registered_at=record.last_ok,
            registrations_ok=record.ok,
            registration_failures=record.failures,
            next_registration_at=next_at,
            renew_every_s=float(REREGISTER_EVERY),
        )

    def _session_snapshot(self, peer: _Peer) -> SessionSnapshot:
        session = peer.session
        reports = session.routes()
        routes = []
        for index, link in enumerate(peer.links):
            report = reports[index] if index < len(reports) else None
            routes.append(self._route_snapshot(peer, link, report))
        return SessionSnapshot(
            peer_id=peer.node_id,
            peer_name=session.peer_name or peer.node_id[:12],
            state=_session_state(session),
            version=session.version(),
            services=tuple(s.name for s in session.services),
            routes=tuple(routes),
            established_at=peer.established_at,
        )

    def _route_snapshot(self, peer: _Peer, link: Link, report) -> RouteSnapshot:
        """One route. The library's RTT of 0.0 means under the clock resolution once a probe was answered, and no data
        before that; the answer count says which."""
        metrics = link.stats.metrics()
        measured = self._link_answers.get(link.id, 0) > 0
        cost = metrics.cost()
        return RouteSnapshot(
            key=f"{peer.node_id[:12]}/{link.id}",
            peer_id=peer.node_id,
            link_id=link.id,
            kind=link.kind.value,
            up=link.is_up(),
            closed=link.is_closed(),
            label=link.describe,
            rtt_ms=metrics.rtt_ms if measured else None,
            srtt_ms=report.srtt_ms if report is not None and report.srtt_ms > 0.0 else None,
            loss=metrics.loss if measured else None,
            mbps=metrics.mbps if measured else None,
            cost_ms=cost if measured and math.isfinite(cost) else None,
            sent_bytes=self._link_tx.get(link.id, 0),
            received_bytes=self._link_rx.get(link.id, 0),
            in_flight=report.in_flight if report is not None else None,
            cwnd=report.cwnd if report is not None else None,
        )


def _session_state(session: OverlaySession) -> str:
    if not session.is_live():
        return "closed"
    if session.version() == 0:
        return "negotiating"
    if not session.is_usable():
        return "no route"
    return "ready"
