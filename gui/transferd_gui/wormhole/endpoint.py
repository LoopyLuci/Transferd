"""The overlay transit: a wormhole's file moves over Transferd's overlay, on a direct TCP route, through a relay, or both.

The sender dials; the receiver serves. Both sides have an ephemeral identity for the attempt, which was sent under the
phase keys, so every connection is checked against the identity the code named. Routes that come up become links of one
overlay session. A route that is cut is handled by the overlay: what was in flight on it is sent again on another route.

Over the session, the sender opens two kinds of stream:

    wormhole.control   one JSON header line, then the receiver writes `{"have": n}` lines back (its progress)
    wormhole.data      frames: offset (8 bytes, big-endian), length (4 bytes), then the bytes of one chunk

Chunk `i` of `CHUNK` bytes goes on data stream `i % streams`. The sender stays within `WINDOW_BYTES` of what the receiver has
reported, so a large file is never queued in memory. A data stream ends with the sender's FIN.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import socket
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from transferd.frames import ServiceInfo
from transferd.handshake import handshake_initiator, handshake_responder
from transferd.identity import HybridIdentity, relay_token_for
from transferd.link import Link, RouteKind, TcpLink
from transferd.overlay import OverlaySession, SessionSetup, spawn
from transferd.relay_hub import RelayHub

from transferd_gui.wormhole import protocol as P
from transferd_gui.wormhole.errors import TimedOut, TransferError
from transferd_gui.wormhole.transfer import CHUNK

CONTROL = "wormhole.control"
DATA = "wormhole.data"
STREAMS_DEFAULT = 4
STREAMS_MAX = 8
WINDOW_BYTES = 8 * 1024 * 1024
PROGRESS_BYTES = 1024 * 1024
IDLE_TIMEOUT_S = 120.0
DIAL_TIMEOUT_S = 10.0
READY_TIMEOUT_S = 15.0
ACCEPT_TIMEOUT_S = 300.0
FRAME = struct.Struct(">QI")
_PROGRESS_LINE = 1 << 16


@dataclass
class _Receive:
    """The receiver's state for one transfer: the header once it is read, the file, and which chunks have arrived."""

    header_ready: asyncio.Event
    done: asyncio.Event
    have: set
    have_bytes: int = 0
    reported: int = 0
    size: int = 0
    streams_expected: int = 0
    streams_ended: int = 0
    file: Optional[object] = None
    activity: float = 0.0
    error: Optional[BaseException] = None


def _private(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_private
    except ValueError:
        return False


def _parse_addr(text: str) -> Tuple[str, int]:
    host, sep, port = text.rpartition(":")
    if not sep or not host or not port.isdigit() or not 0 < int(port) < 65536:
        raise TransferError(f"a relay address is host:port, not '{text}'")
    return host, int(port)


class OverlayTransit:
    """One attempt's transit. Create it, `open` it, and let the session drive it. `close` when the attempt ends."""

    def __init__(
        self,
        *,
        name: str = "",
        relay: str = "",
        listen_host: str = "0.0.0.0",
        listen_port: int = 0,
        advertise: Optional[List[str]] = None,
        streams: int = STREAMS_DEFAULT,
        dial_timeout: float = DIAL_TIMEOUT_S,
        ready_timeout: float = READY_TIMEOUT_S,
        accept_timeout: float = ACCEPT_TIMEOUT_S,
        idle_timeout: float = IDLE_TIMEOUT_S,
        on_state: Optional[Callable[[str], None]] = None,
    ) -> None:
        self.identity = HybridIdentity.generate(name)
        self._name = name
        self._relay = relay.strip()
        self._listen = (listen_host, listen_port)
        self._advertise = advertise
        self._streams = max(1, min(STREAMS_MAX, int(streams)))
        self._dial_timeout = dial_timeout
        self._ready_timeout = ready_timeout
        self._accept_timeout = accept_timeout
        self._idle_timeout = idle_timeout
        self._say = on_state or (lambda text: None)
        self._server: Optional[asyncio.AbstractServer] = None
        self._port = 0
        self._hub: Optional[RelayHub] = None
        self._hubs: Dict[str, RelayHub] = {}
        self._peer: Optional[P.PeerIdentity] = None
        self._inbound: asyncio.Queue = asyncio.Queue()
        self._session: Optional[OverlaySession] = None
        self._events: Optional[asyncio.Queue] = None
        self._streams_q: asyncio.Queue = asyncio.Queue()
        self._down = asyncio.Event()
        self._tasks: List[asyncio.Task] = []
        self._sent = 0
        self._have_reported = 0
        self._progress = asyncio.Event()
        self.routes_seen: List[str] = []

    # ----- what this side offers the other ----------------------------------------------------------

    def local_identity(self) -> P.PeerIdentity:
        hints = tuple(P.Hint(host, self._port) for host in self._local_hosts()) if self._server is not None else ()
        return P.PeerIdentity(self.identity.node_id, self.identity.public_key, self._name, self._relay, hints)

    def expect_peer(self, peer: P.PeerIdentity) -> None:
        self._peer = peer

    def _local_hosts(self) -> List[str]:
        if self._advertise is not None:
            return list(self._advertise)[:P.HINTS_MAX]
        hosts = set()
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                address = info[4][0]
                if not ipaddress.ip_address(address).is_loopback:
                    hosts.add(address)
        except OSError:
            pass
        return sorted(hosts)[: P.HINTS_MAX]

    def _admit(self, node_id: str, public_key: bytes) -> bool:
        """Only the peer named in the identity phase may connect, direct or through the relay."""
        peer = self._peer
        return peer is not None and node_id == peer.node_id and public_key == peer.public_key

    # ----- listening and relays ----------------------------------------------------------------------

    async def open(self) -> None:
        if self._server is None:
            self._server = await asyncio.start_server(self._on_tcp, self._listen[0], self._listen[1])
            self._port = int(self._server.sockets[0].getsockname()[1])
        if self._relay and self._hub is None:
            self._hub = await self._make_hub(self._relay)

    async def _make_hub(self, relay_addr: str) -> RelayHub:
        hub = RelayHub(
            _parse_addr(relay_addr),
            relay_token_for(self.identity.node_id),
            self.identity,
            admit=self._admit,
            on_adopt=self._on_adopt,
        )
        await hub.start()
        return hub

    async def _hub_for(self, relay_addr: str) -> RelayHub:
        if self._hub is not None and relay_addr == self._relay:
            return self._hub
        if relay_addr not in self._hubs:
            self._hubs[relay_addr] = await self._make_hub(relay_addr)
        return self._hubs[relay_addr]

    async def _on_tcp(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """A direct connection: the hybrid handshake, then the identity check, then a link for the session."""
        try:
            result = await asyncio.wait_for(handshake_responder(reader, writer, self.identity), self._dial_timeout)
            if not self._admit(result.peer_node_id, result.peer_hybrid_public_key):
                writer.close()
                return
            peername = writer.get_extra_info("peername") or ("", 0)
            kind = RouteKind.LAN if _private(str(peername[0])) else RouteKind.DIRECT
            link = TcpLink(reader, writer, result.keys, kind=kind, describe=f"direct {peername[0]}")
            link.start()
            await self._inbound.put(link)
        except Exception:
            writer.close()

    async def _on_adopt(self, link: Link, peer_id: str, peer_key: bytes, _sender: bytes) -> None:
        """A handshake that came through the relay from the named peer. The link is already started."""
        if self._admit(peer_id, peer_key):
            await self._inbound.put(link)

    # ----- dialling ----------------------------------------------------------------------------------

    async def _dial_hint(self, peer: P.PeerIdentity, hint: P.Hint) -> Link:
        reader, writer = await asyncio.open_connection(hint.host, hint.port)
        try:
            result = await handshake_initiator(reader, writer, self.identity, expect_node_id=peer.node_id)
            if result.peer_hybrid_public_key != peer.public_key:
                raise TransferError("the computer at that address is not the one the code named")
        except BaseException:
            writer.close()
            raise
        kind = RouteKind.LAN if _private(hint.host) else RouteKind.DIRECT
        link = TcpLink(reader, writer, result.keys, kind=kind, describe=f"direct {hint.host}:{hint.port}")
        link.start()
        return link

    async def _dial_relay(self, peer: P.PeerIdentity) -> Link:
        hub = await self._hub_for(peer.relay)
        token = relay_token_for(peer.node_id)
        keys, peer_key = await hub.handshake(peer.node_id, token)
        if peer_key != peer.public_key:
            raise TransferError("the relay reached a computer other than the one the code named")
        return hub.link(token, keys)

    async def _dial(self, peer: P.PeerIdentity) -> List[Link]:
        """Every route at once: each direct address, and the relay if the peer is on one. Returns the links that came up."""
        attempts = [asyncio.wait_for(self._dial_hint(peer, hint), self._dial_timeout) for hint in peer.hints]
        if peer.relay:
            attempts.append(asyncio.wait_for(self._dial_relay(peer), self._dial_timeout))
        if not attempts:
            raise TransferError("the other computer gave no way to reach it")
        self._say("Connecting")
        results = await asyncio.gather(*attempts, return_exceptions=True)
        links = [r for r in results if isinstance(r, Link)]
        if not links:
            raise TransferError("the other computer could not be reached by any route")
        return links

    # ----- the session -------------------------------------------------------------------------------

    async def _start_session(self, peer: P.PeerIdentity, links: List[Link]) -> OverlaySession:
        self._events = asyncio.Queue()
        setup = SessionSetup(
            peer_node_id=peer.node_id,
            peer_name=peer.name,
            identity=self.identity,
            services=[
                ServiceInfo(CONTROL, "the header and progress of a wormhole transfer"),
                ServiceInfo(DATA, "the chunks of a wormhole transfer"),
            ],
        )
        session = spawn(setup, links, self._events)
        self._session = session
        self._tasks.append(asyncio.get_running_loop().create_task(self._dispatch(session)))
        self._tasks.append(asyncio.get_running_loop().create_task(self._absorb(session)))
        if not await session.ready(self._ready_timeout):
            raise TransferError("the connection to the other computer did not come up")
        self._say("Connected")
        return session

    async def _absorb(self, session: OverlaySession) -> None:
        """Routes that arrive later join the same session, so a second route is used as soon as it is up."""
        while True:
            link = await self._inbound.get()
            await session.add_link(link)

    async def _dispatch(self, session: OverlaySession) -> None:
        """The session's events: streams the other side opens are authorised here, and a lost session is reported."""
        assert self._events is not None
        while True:
            event = await self._events.get()
            if event.kind == "open_request":
                if event.service in (CONTROL, DATA):
                    await session.authorize(event.stream, event.service, True)
                    await self._streams_q.put((event.stream, event.service))
                else:
                    await session.authorize(event.stream, event.service, False, "no such service")
            elif event.kind == "down":
                self._down.set()
                return
            elif event.kind == "route_changed":
                state = "up" if event.up else "down"
                self.routes_seen.append(f"{event.route.value} {state}")

    async def _next_inbound(self) -> Link:
        try:
            return await asyncio.wait_for(self._inbound.get(), self._accept_timeout)
        except asyncio.TimeoutError as exc:
            raise TimedOut("no connection from the other computer arrived in time") from exc

    # ----- the transit, as the session calls it ------------------------------------------------------

    async def send(self, peer: P.PeerIdentity, offer: P.Offer, source: Path) -> None:
        links = await self._dial(peer)
        session = await self._start_session(peer, links)
        self._say("Sending")
        await self._send_data(session, offer, source)

    async def receive(self, peer: P.PeerIdentity, offer: P.Offer, folder: Path) -> Path:
        self._say("Waiting for the connection")
        first = await self._next_inbound()
        session = await self._start_session(peer, [first])
        self._say("Receiving")
        return await self._receive_data(session, offer, folder)

    def route_reports(self) -> list:
        """The overlay's counters per route while the session is up: what each route carried and what it resent. Empty
        before the session starts and after it has closed."""
        return self._session.routes() if self._session is not None else []

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        self._tasks = []
        if self._session is not None:
            try:
                await self._session.shutdown()
            except Exception:
                pass
            self._session = None
        for hub in [self._hub, *self._hubs.values()]:
            if hub is not None:
                try:
                    await hub.close()
                except Exception:
                    pass
        self._hub = None
        self._hubs = {}
        if self._server is not None:
            self._server.close()
            self._server = None

    # ----- sending -----------------------------------------------------------------------------------

    async def _send_data(self, session: OverlaySession, offer: P.Offer, source: Path) -> None:
        size = source.stat().st_size
        if size != offer.size:
            raise TransferError("the file changed after it was offered")
        count = (size + CHUNK - 1) // CHUNK
        header = {
            "kind": offer.kind,
            "name": offer.name,
            "size": size,
            "sha256": offer.sha256,
            "streams": self._streams,
            "chunk": CHUNK,
        }
        control = await session.open(CONTROL)
        await session.send(control, P.encode(header) + b"\n")
        reader = asyncio.get_running_loop().create_task(self._read_progress(session, control))
        self._tasks.append(reader)
        try:
            streams = [await session.open(DATA) for _ in range(self._streams)]
            await asyncio.gather(
                *(self._pump(session, stream, lane, size, count, source) for lane, stream in enumerate(streams))
            )
        finally:
            reader.cancel()

    async def _pump(self, session: OverlaySession, stream: int, lane: int, size: int, count: int, source: Path) -> None:
        """Send the chunks of one lane: chunk `lane`, `lane + streams`, and so on, then the end of the stream."""
        stride = self._streams
        with open(source, "rb") as handle:
            for index in range(lane, count, stride):
                while self._sent - self._have_reported > WINDOW_BYTES:
                    self._progress.clear()
                    await self._wait(self._progress.wait())
                offset = index * CHUNK
                length = min(CHUNK, size - offset)
                handle.seek(offset)
                data = handle.read(length)
                if len(data) != length:
                    raise TransferError("the file changed while it was being sent")
                await session.send(stream, FRAME.pack(offset, length) + data)
                self._sent += length
        await session.send(stream, b"", fin=True)

    async def _wait(self, awaitable) -> None:
        """Wait for `awaitable`, but not for longer than the idle timeout, and not past a lost session."""
        waiter = asyncio.ensure_future(awaitable)
        down = asyncio.ensure_future(self._down.wait())
        try:
            done, _ = await asyncio.wait({waiter, down}, timeout=self._idle_timeout, return_when=asyncio.FIRST_COMPLETED)
            if not done:
                raise TransferError("the other computer stopped taking data")
            if down in done and waiter not in done:
                raise TransferError("the connection was lost")
        finally:
            waiter.cancel()
            down.cancel()

    async def _read_progress(self, session: OverlaySession, control: int) -> None:
        buffer = b""
        while True:
            chunk = await session.recv(control)
            if chunk is None:
                return
            buffer += chunk
            if len(buffer) > _PROGRESS_LINE:
                raise TransferError("the other computer sent too much progress")
            while b"\n" in buffer:
                line, _, buffer = buffer.partition(b"\n")
                try:
                    have = int(json.loads(line.decode("utf-8"))["have"])
                except (ValueError, KeyError, TypeError):
                    continue
                if have > self._have_reported:
                    self._have_reported = have
                    self._progress.set()

    # ----- receiving ---------------------------------------------------------------------------------

    async def _receive_data(self, session: OverlaySession, offer: P.Offer, folder: Path) -> Path:
        folder.mkdir(parents=True, exist_ok=True)
        part = folder / (offer.name + ".part")
        state = _Receive(asyncio.Event(), asyncio.Event(), set(), activity=asyncio.get_running_loop().time())
        consumers: List[asyncio.Task] = []
        control_stream: List[int] = []
        loop = asyncio.get_running_loop()

        def maybe_done() -> None:
            """Finished when every chunk has arrived and every data stream has ended. Waiting for the ends means the
            sender's last frames are all in before the session is torn down, even for an empty file."""
            count = (state.size + CHUNK - 1) // CHUNK
            if (
                state.header_ready.is_set()
                and len(state.have) == count
                and state.streams_expected > 0
                and state.streams_ended >= state.streams_expected
            ):
                state.done.set()

        async def control_reader(stream: int) -> None:
            control_stream.append(stream)
            buffer = b""
            while not state.header_ready.is_set():
                chunk = await session.recv(stream)
                if chunk is None:
                    raise TransferError("the header never arrived")
                buffer += chunk
                if b"\n" in buffer:
                    line, _, _rest = buffer.partition(b"\n")
                    header = self._check_header(line, offer)
                    state.size = header["size"]
                    state.streams_expected = header["streams"]
                    state.file = open(part, "w+b")
                    state.file.truncate(state.size)
                    state.header_ready.set()
                    maybe_done()
            # Nothing more is expected from the sender on the control stream; keep reading until it ends.
            while True:
                if await session.recv(stream) is None:
                    return

        async def data_reader(stream: int) -> None:
            buffer = bytearray()
            while True:
                chunk = await session.recv(stream)
                if chunk is None:
                    break
                buffer += chunk
                await state.header_ready.wait()
                state.activity = loop.time()
                while len(buffer) >= FRAME.size:
                    offset, length = FRAME.unpack_from(buffer, 0)
                    if offset % CHUNK or offset >= state.size or length != min(CHUNK, state.size - offset):
                        raise TransferError("a chunk is out of range for this file")
                    if len(buffer) < FRAME.size + length:
                        break
                    payload = bytes(buffer[FRAME.size : FRAME.size + length])
                    del buffer[: FRAME.size + length]
                    index = offset // CHUNK
                    if index in state.have:
                        continue
                    state.file.seek(offset)
                    state.file.write(payload)
                    state.have.add(index)
                    state.have_bytes += length
                    if state.have_bytes - state.reported >= PROGRESS_BYTES:
                        await self._report(session, control_stream, state)
                    maybe_done()
            if buffer:
                raise TransferError("a data stream ended in the middle of a chunk")
            state.streams_ended += 1
            maybe_done()

        async def consume() -> None:
            while True:
                stream, service = await self._streams_q.get()
                if service == CONTROL:
                    consumers.append(loop.create_task(control_reader(stream)))
                else:
                    consumers.append(loop.create_task(data_reader(stream)))

        consumer = loop.create_task(consume())
        try:
            waiting = asyncio.ensure_future(state.done.wait())
            down = asyncio.ensure_future(self._down.wait())
            while True:
                failed = [t for t in consumers if t.done() and not t.cancelled() and t.exception() is not None]
                if failed:
                    raise failed[0].exception()  # type: ignore[misc]
                done, _ = await asyncio.wait({waiting, down}, timeout=1.0, return_when=asyncio.FIRST_COMPLETED)
                if waiting in done or state.done.is_set():
                    break
                if down in done:
                    raise TransferError("the connection was lost")
                if loop.time() - state.activity > self._idle_timeout:
                    raise TransferError("the other computer stopped sending")
            waiting.cancel()
            down.cancel()
            if len(state.have) != (state.size + CHUNK - 1) // CHUNK:
                raise TransferError("some of the data did not arrive")
            await self._report(session, control_stream, state, final=True)
            if state.file is not None:
                state.file.flush()
                state.file.close()
            return part
        except BaseException:
            if state.file is not None:
                state.file.close()
            try:
                part.unlink()
            except OSError:
                pass
            raise
        finally:
            consumer.cancel()
            for task in consumers:
                task.cancel()

    async def _report(self, session: OverlaySession, control_stream: List[int], state: _Receive, final: bool = False) -> None:
        if not control_stream:
            return
        state.reported = state.have_bytes
        line = json.dumps({"have": state.have_bytes if not final else state.size}).encode("utf-8") + b"\n"
        try:
            await session.send(control_stream[0], line)
        except Exception:
            if final:
                raise

    @staticmethod
    def _check_header(line: bytes, offer: P.Offer) -> dict:
        try:
            header = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise TransferError("the header is not JSON") from exc
        if not isinstance(header, dict):
            raise TransferError("the header is not an object")
        if header.get("kind") != offer.kind or header.get("name") != offer.name:
            raise TransferError("the header does not match the offer")
        if header.get("size") != offer.size or header.get("sha256") != offer.sha256:
            raise TransferError("the header does not match the offer")
        if header.get("chunk") != CHUNK:
            raise TransferError("the chunk size is not one this app speaks")
        streams = header.get("streams")
        if not isinstance(streams, int) or not 1 <= streams <= STREAMS_MAX:
            raise TransferError("the stream count is out of range")
        return header


__all__ = ["CONTROL", "DATA", "OverlayTransit", "STREAMS_DEFAULT", "WINDOW_BYTES"]
