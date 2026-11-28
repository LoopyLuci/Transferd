"""The wormhole mailbox server: holds encrypted messages for a nameplate until both sides have read them.

It is a WebSocket server, served over TLS when it is on the network (`tls.py`). Every message is one JSON object. It sees
nameplates, side ids, phase names, sizes and timing. It never sees a code, a key, or the content of anything, because the
bodies are sealed by the clients. The wire protocol is described in `gui\\docs\\WORMHOLE.md`.

It is meant to be left running and to be used by computers it does not know:
- every refusal carries a machine-readable `code`, and one that can be retried says after how many seconds, `retry_after`;
- each address is held to a rate for allocating, claiming and adding, so one computer cannot fill the mailbox;
- there are caps on nameplates, on connections and on connections from one address, so the mailbox has a size it cannot exceed;
- a connection that has not bound within `bind_timeout` seconds is closed, so idle sockets do not pile up;
- pings close a connection that has stopped answering, so its nameplate is not held by a dead socket.

Run one for a user or a test:

    python -m transferd_gui.wormhole.server --host 127.0.0.1 --port 4470
    python -m transferd_gui.wormhole.server --host 0.0.0.0 --port 4470 --identity C:\\path\\to\\folder
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import secrets
import ssl
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Set, Tuple

from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from transferd_gui.wormhole import tls
from transferd_gui.wormhole.codes import NAMEPLATE_MAX, NAMEPLATE_MIN

log = logging.getLogger("transferd.wormhole.mailbox")

APP_ID = "transferd-wormhole/1"
PROTOCOL = 2
MOODS = ("happy", "lonely", "errory", "scary")
NAMEPLATE_TTL_S = 300.0
MAILBOX_IDLE_TTL_S = 3600.0
MAX_MESSAGE_BYTES = 65536
MAX_MESSAGES_PER_MAILBOX = 64
MAX_NAMEPLATES = 4096
MAX_CONNECTIONS = 1024
MAX_CONNECTIONS_PER_ADDRESS = 64
BIND_TIMEOUT_S = 30.0
BUSY_RETRY_S = 5.0
REAP_EVERY_S = 30.0
ALLOCATE_TRIES = 1000
#: Requests per address: kind -> (burst, tokens added per second). A burst is spent at once, then the rate applies.
DEFAULT_RATES: Dict[str, Tuple[float, float]] = {
    "allocate": (30.0, 0.5),  # thirty at once, then thirty a minute
    "claim": (120.0, 2.0),
    "add": (200.0, 50.0),
}
_HEX16 = re.compile(r"[0-9a-f]{16}")
_HEX4 = re.compile(r"[0-9a-f]{4}")
_PHASE = re.compile(r"[a-z]{1,16}")
_NAMEPLATE = re.compile(r"[0-9]{1,5}")
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


class Code:
    """The machine-readable code on every refusal. A client decides what to do from the code, not from the words."""

    BAD_REQUEST = "bad_request"
    NOT_BOUND = "not_bound"
    ALREADY_BOUND = "already_bound"
    WRONG_APP = "wrong_app"
    BAD_SIDE = "bad_side"
    UNKNOWN_MESSAGE = "unknown_message"
    BAD_NAMEPLATE = "bad_nameplate"
    NO_NAMEPLATE = "no_nameplate"
    CROWDED = "crowded"
    NO_MAILBOX = "no_mailbox"
    TOO_BIG = "too_big"
    BAD_ID = "bad_id"
    BAD_PHASE = "bad_phase"
    BAD_BODY = "bad_body"
    FULL = "full"
    RATE_LIMITED = "rate_limited"
    BUSY = "busy"


@dataclass
class _Stored:
    message_id: str
    side: str
    phase: str
    body: str  # hex, exactly as the sender sent it


@dataclass
class _Mailbox:
    mailbox_id: str
    last_active: float
    sides: Set[str] = field(default_factory=set)
    closed: Set[str] = field(default_factory=set)
    messages: List[_Stored] = field(default_factory=list)
    keys_seen: Set[tuple] = field(default_factory=set)  # (side, message id), so a retry is stored once
    acked: Dict[str, Set[str]] = field(default_factory=dict)  # side -> ids that side has acknowledged
    live: Dict[str, ServerConnection] = field(default_factory=dict)  # side -> the connection that has it open


@dataclass
class _Nameplate:
    number: int
    touched: float
    mailbox: Optional[_Mailbox] = None


@dataclass
class _Connection:
    address: str = "?"
    side: Optional[str] = None
    opened: Set[str] = field(default_factory=set)


@dataclass
class _Bucket:
    tokens: float
    updated: float


def _address_of(ws: ServerConnection) -> str:
    remote = getattr(ws, "remote_address", None)
    return str(remote[0]) if remote else "?"


class MailboxServer:
    """The mailbox. `clock` is injectable so tests can move time; `reap()` removes what has expired."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        nameplate_ttl: float = NAMEPLATE_TTL_S,
        mailbox_idle_ttl: float = MAILBOX_IDLE_TTL_S,
        max_message_bytes: int = MAX_MESSAGE_BYTES,
        max_messages: int = MAX_MESSAGES_PER_MAILBOX,
        max_nameplates: int = MAX_NAMEPLATES,
        max_connections: int = MAX_CONNECTIONS,
        max_connections_per_address: int = MAX_CONNECTIONS_PER_ADDRESS,
        bind_timeout: float = BIND_TIMEOUT_S,
        rates: Optional[Mapping[str, Tuple[float, float]]] = None,
    ) -> None:
        self._clock = clock
        self._nameplate_ttl = nameplate_ttl
        self._mailbox_idle_ttl = mailbox_idle_ttl
        self._max_message_bytes = max_message_bytes
        self._max_messages = max_messages
        self._max_nameplates = max_nameplates
        self._max_connections = max_connections
        self._max_per_address = max_connections_per_address
        self._bind_timeout = bind_timeout
        self._rates: Dict[str, Tuple[float, float]] = {**DEFAULT_RATES, **(rates or {})}
        for kind, (burst, refill) in self._rates.items():
            if burst < 1 or refill <= 0:
                raise ValueError(f"the rate for {kind} needs a burst of at least 1 and a positive refill")
        self._plates: Dict[int, _Nameplate] = {}
        self._boxes: Dict[str, _Mailbox] = {}
        self._buckets: Dict[Tuple[str, str], _Bucket] = {}
        self._connections = 0
        self._per_address: Dict[str, int] = {}
        #: The mood each side closed with, in order. A count of what the mailbox saw; it holds no message content.
        self.close_moods: List[str] = []
        self.secure = False
        self._server: Optional[Server] = None
        self._reaper: Optional[asyncio.Task] = None

    # ----- lifecycle --------------------------------------------------------------------------------

    async def start(self, host: str = "127.0.0.1", port: int = 0, *, tls: Optional[ssl.SSLContext] = None) -> int:
        """Listen on `host:port` (port 0 picks a free one), over TLS when `tls` is an SSL context. Returns the port."""
        self._server = await serve(
            self._handle,
            host,
            port,
            ssl=tls,
            max_size=2 * self._max_message_bytes + 8192,
            ping_interval=20,
            ping_timeout=20,
            close_timeout=5,
        )
        self.secure = tls is not None
        self._reaper = asyncio.get_running_loop().create_task(self._reap_forever())
        return self.port

    @property
    def port(self) -> int:
        assert self._server is not None, "start() first"
        return int(self._server.sockets[0].getsockname()[1])

    async def close(self) -> None:
        if self._reaper is not None:
            self._reaper.cancel()
            try:
                await self._reaper
            except (asyncio.CancelledError, Exception):
                pass
            self._reaper = None
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _reap_forever(self) -> None:
        while True:
            await asyncio.sleep(REAP_EVERY_S)
            self.reap()

    # ----- what is held ----------------------------------------------------------------------------

    def open_mailboxes(self) -> int:
        return len(self._boxes)

    def open_nameplates(self) -> int:
        return len(self._plates)

    def open_connections(self) -> int:
        return self._connections

    def reap(self) -> None:
        """Remove nameplates that were never claimed in time, mailboxes idle for too long, and rate buckets that have
        been unused long enough to be full again."""
        now = self._clock()
        for number, plate in list(self._plates.items()):
            if plate.mailbox is None and now - plate.touched > self._nameplate_ttl:
                del self._plates[number]
        for box_id, box in list(self._boxes.items()):
            if now - box.last_active > self._mailbox_idle_ttl:
                self._drop_box(box)
        for key, bucket in list(self._buckets.items()):
            if now - bucket.updated > 600.0:
                del self._buckets[key]

    def _drop_box(self, box: _Mailbox) -> None:
        self._boxes.pop(box.mailbox_id, None)
        for number, plate in list(self._plates.items()):
            if plate.mailbox is box:
                del self._plates[number]

    def _take(self, address: str, kind: str) -> float:
        """Spend one request of `kind` from `address`'s allowance. Returns 0.0 if it may proceed, or the seconds to wait."""
        burst, refill = self._rates[kind]
        now = self._clock()
        key = (address, kind)
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = _Bucket(tokens=burst, updated=now)
            self._buckets[key] = bucket
        bucket.tokens = min(burst, bucket.tokens + (now - bucket.updated) * refill)
        bucket.updated = now
        if bucket.tokens >= 1.0:
            bucket.tokens -= 1.0
            return 0.0
        return max(0.01, (1.0 - bucket.tokens) / refill)

    def _admit(self, address: str) -> bool:
        return self._connections < self._max_connections and self._per_address.get(address, 0) < self._max_per_address

    def _count(self, address: str, delta: int) -> None:
        self._connections += delta
        left = self._per_address.get(address, 0) + delta
        if left > 0:
            self._per_address[address] = left
        else:
            self._per_address.pop(address, None)

    # ----- one connection --------------------------------------------------------------------------

    async def _handle(self, ws: ServerConnection) -> None:
        address = _address_of(ws)
        if not self._admit(address):
            log.info("refused a connection from %s: the mailbox is at its connection limit", address)
            await self._error(ws, Code.BUSY, "the mailbox is busy; try again later", None, retry_after=BUSY_RETRY_S)
            await ws.close(1013, "busy")
            return
        state = _Connection(address=address)
        self._count(address, 1)
        watchdog = asyncio.get_running_loop().call_later(self._bind_timeout, self._expire_unbound, ws, state)
        try:
            await self._send(ws, {
                "type": "welcome",
                "protocol": PROTOCOL,
                "limits": {
                    "max_message_bytes": self._max_message_bytes,
                    "max_messages_per_mailbox": self._max_messages,
                    "bind_timeout_s": self._bind_timeout,
                },
            })
            async for raw in ws:
                try:
                    message = json.loads(raw)
                except (TypeError, ValueError):
                    await self._error(ws, Code.BAD_REQUEST, "a message is a JSON object", None)
                    continue
                if not isinstance(message, dict):
                    await self._error(ws, Code.BAD_REQUEST, "a message is a JSON object", None)
                    continue
                await self._dispatch(ws, state, message)
        except ConnectionClosed:
            pass
        finally:
            watchdog.cancel()
            self._disconnect(ws, state)
            self._count(address, -1)

    def _expire_unbound(self, ws: ServerConnection, state: _Connection) -> None:
        if state.side is None:
            asyncio.ensure_future(ws.close(1008, "bind first"))

    def _disconnect(self, ws: ServerConnection, state: _Connection) -> None:
        if state.side is None:
            return
        for box_id in state.opened:
            box = self._boxes.get(box_id)
            if box is not None and box.live.get(state.side) is ws:
                del box.live[state.side]

    async def _dispatch(self, ws: ServerConnection, state: _Connection, message: dict) -> None:
        kind = message.get("type")
        if kind == "bind":
            await self._bind(ws, state, message)
            return
        if state.side is None:
            await self._error(ws, Code.NOT_BOUND, "bind before anything else", kind)
            return
        handler = {
            "allocate": self._allocate,
            "claim": self._claim,
            "release": self._release,
            "open": self._open,
            "add": self._add,
            "ack": self._ack,
            "close": self._close,
        }.get(kind)
        if handler is None:
            await self._error(ws, Code.UNKNOWN_MESSAGE, f"unknown message '{kind}'", kind)
            return
        if kind in self._rates:
            wait = self._take(state.address, kind)
            if wait > 0:
                message_id = message.get("id") if kind == "add" else None
                await self._error(
                    ws,
                    Code.RATE_LIMITED,
                    f"too many {kind} requests from this computer; try again in {wait:.2f} s",
                    kind,
                    message_id=message_id if isinstance(message_id, str) else None,
                    retry_after=wait,
                )
                return
        await handler(ws, state, message)

    async def _bind(self, ws: ServerConnection, state: _Connection, message: dict) -> None:
        if state.side is not None:
            await self._error(ws, Code.ALREADY_BOUND, "already bound", "bind")
            return
        if message.get("appid") != APP_ID:
            await self._error(ws, Code.WRONG_APP, f"this mailbox serves {APP_ID}", "bind")
            return
        side = message.get("side")
        if not isinstance(side, str) or not _HEX16.fullmatch(side):
            await self._error(ws, Code.BAD_SIDE, "a side id is 16 lower-case hex characters", "bind")
            return
        state.side = side

    async def _allocate(self, ws: ServerConnection, state: _Connection, message: dict) -> None:
        if len(self._plates) >= self._max_nameplates:
            await self._error(ws, Code.BUSY, "this mailbox holds as many codes as it allows; try again later", "allocate", retry_after=BUSY_RETRY_S)
            return
        now = self._clock()
        for _ in range(ALLOCATE_TRIES):
            number = secrets.randbelow(NAMEPLATE_MAX - NAMEPLATE_MIN + 1) + NAMEPLATE_MIN
            if number not in self._plates:
                self._plates[number] = _Nameplate(number, now)
                await self._send(ws, {"type": "allocated", "nameplate": str(number)})
                return
        await self._error(ws, Code.NO_NAMEPLATE, "no nameplate is free; try again later", "allocate", retry_after=BUSY_RETRY_S)

    async def _claim(self, ws: ServerConnection, state: _Connection, message: dict) -> None:
        number = self._nameplate_of(message.get("nameplate"))
        if number is None:
            await self._error(ws, Code.BAD_NAMEPLATE, "a nameplate is a number", "claim")
            return
        now = self._clock()
        plate = self._plates.get(number)
        if plate is None:
            if len(self._plates) >= self._max_nameplates:
                await self._error(ws, Code.BUSY, "this mailbox holds as many codes as it allows; try again later", "claim", retry_after=BUSY_RETRY_S)
                return
            plate = _Nameplate(number, now)
            self._plates[number] = plate
        plate.touched = now
        if plate.mailbox is None:
            plate.mailbox = _Mailbox(secrets.token_hex(16), now)
            self._boxes[plate.mailbox.mailbox_id] = plate.mailbox
        box = plate.mailbox
        if state.side not in box.sides:
            if len(box.sides) >= 2:
                await self._error(ws, Code.CROWDED, "crowded: this code is already used by two computers", "claim")
                return
            box.sides.add(state.side)
        box.last_active = now
        await self._send(ws, {"type": "claimed", "mailbox": box.mailbox_id})

    async def _release(self, ws: ServerConnection, state: _Connection, message: dict) -> None:
        number = self._nameplate_of(message.get("nameplate"))
        if number is None:
            await self._error(ws, Code.BAD_NAMEPLATE, "a nameplate is a number", "release")
            return
        plate = self._plates.get(number)
        if plate is not None and plate.mailbox is not None:
            box = plate.mailbox
            box.sides.discard(state.side)
            box.closed.discard(state.side)
            box.live.pop(state.side, None)
            if not box.sides:
                self._drop_box(box)
        elif plate is not None:
            del self._plates[number]
        await self._send(ws, {"type": "released"})

    async def _open(self, ws: ServerConnection, state: _Connection, message: dict) -> None:
        box = await self._member_box(ws, state, message, "open")
        if box is None:
            return
        box.live[state.side] = ws
        state.opened.add(box.mailbox_id)
        box.last_active = self._clock()
        acked = box.acked.get(state.side, set())
        for stored in list(box.messages):
            if stored.side == state.side or stored.message_id in acked:
                continue
            await self._send(ws, self._message_for(box, stored))
        await self._send(ws, {"type": "opened", "mailbox": box.mailbox_id})

    async def _add(self, ws: ServerConnection, state: _Connection, message: dict) -> None:
        box = await self._member_box(ws, state, message, "add")
        if box is None:
            return
        message_id = message.get("id")
        phase = message.get("phase")
        body = message.get("body")
        if not isinstance(message_id, str) or not _HEX4.fullmatch(message_id):
            await self._error(ws, Code.BAD_ID, "a message id is 4 hex characters", "add")
            return
        if not isinstance(phase, str) or not _PHASE.fullmatch(phase):
            await self._error(ws, Code.BAD_PHASE, "a phase is 1 to 16 lower-case letters", "add", message_id)
            return
        if not isinstance(body, str):
            await self._error(ws, Code.BAD_BODY, "a body is hex text", "add", message_id)
            return
        try:
            raw = bytes.fromhex(body)
        except ValueError:
            await self._error(ws, Code.BAD_BODY, "a body is hex text", "add", message_id)
            return
        if len(raw) > self._max_message_bytes:
            await self._error(ws, Code.TOO_BIG, f"a message is at most {self._max_message_bytes} bytes", "add", message_id)
            return
        box.last_active = self._clock()
        if (state.side, message_id) in box.keys_seen:
            # A retry of a message already stored: confirm it, store nothing.
            await self._send(ws, {"type": "added", "id": message_id})
            return
        if len(box.messages) >= self._max_messages:
            await self._error(ws, Code.FULL, "this mailbox is full", "add", message_id)
            return
        box.keys_seen.add((state.side, message_id))
        stored = _Stored(message_id, state.side, phase, body.lower())
        box.messages.append(stored)
        await self._send(ws, {"type": "added", "id": message_id})
        for side, peer in list(box.live.items()):
            if side != state.side:
                await self._send_quietly(peer, self._message_for(box, stored))

    async def _ack(self, ws: ServerConnection, state: _Connection, message: dict) -> None:
        box = await self._member_box(ws, state, message, "ack")
        if box is None:
            return
        message_id = message.get("id")
        if isinstance(message_id, str) and _HEX4.fullmatch(message_id):
            box.acked.setdefault(state.side, set()).add(message_id)
        box.last_active = self._clock()

    async def _close(self, ws: ServerConnection, state: _Connection, message: dict) -> None:
        box = await self._member_box(ws, state, message, "close")
        if box is None:
            return
        mood = message.get("mood", "happy")
        if mood not in MOODS:
            mood = "happy"
        log.info("mailbox %s: side closed (%s)", box.mailbox_id[:8], mood)
        self.close_moods.append(mood)
        box.closed.add(state.side)
        box.live.pop(state.side, None)
        state.opened.discard(box.mailbox_id)
        await self._send(ws, {"type": "closed"})
        if box.sides and box.sides <= box.closed:
            self._drop_box(box)

    # ----- helpers ---------------------------------------------------------------------------------

    async def _member_box(self, ws: ServerConnection, state: _Connection, message: dict, kind: str) -> Optional[_Mailbox]:
        box_id = message.get("mailbox")
        box = self._boxes.get(box_id) if isinstance(box_id, str) else None
        if box is None or state.side not in box.sides:
            await self._error(ws, Code.NO_MAILBOX, "no such mailbox for this side", kind)
            return None
        return box

    @staticmethod
    def _nameplate_of(value) -> Optional[int]:
        if not isinstance(value, str) or not _NAMEPLATE.fullmatch(value):
            return None
        number = int(value)
        if not NAMEPLATE_MIN <= number <= NAMEPLATE_MAX:
            return None
        return number

    @staticmethod
    def _message_for(box: _Mailbox, stored: _Stored) -> dict:
        return {
            "type": "message",
            "mailbox": box.mailbox_id,
            "id": stored.message_id,
            "side": stored.side,
            "phase": stored.phase,
            "body": stored.body,
        }

    @staticmethod
    async def _send(ws: ServerConnection, message: dict) -> None:
        await ws.send(json.dumps(message, separators=(",", ":")))

    @staticmethod
    async def _send_quietly(ws: ServerConnection, message: dict) -> None:
        try:
            await MailboxServer._send(ws, message)
        except ConnectionClosed:
            pass

    @staticmethod
    async def _error(
        ws: ServerConnection,
        code: str,
        text: str,
        orig: Optional[str],
        message_id: Optional[str] = None,
        retry_after: Optional[float] = None,
    ) -> None:
        reply: dict = {"type": "error", "code": code, "error": text, "orig": orig}
        if message_id is not None:
            reply["id"] = message_id
        if retry_after is not None:
            reply["retry_after"] = round(float(retry_after), 3)
        await MailboxServer._send(ws, reply)


async def run_forever(host: str, port: int, identity_dir: Optional[str] = None) -> None:
    """Serve until interrupted. With `identity_dir`, the mailbox is served over TLS with the certificate kept there."""
    context = None
    fingerprint = ""
    if identity_dir:
        identity = tls.load_or_create(Path(identity_dir))
        context = tls.server_context(identity)
        fingerprint = identity.fingerprint
    server = MailboxServer()
    bound = await server.start(host, port, tls=context)
    scheme = "wss" if context is not None else "ws"
    log.info("mailbox listening on %s://%s:%d", scheme, host, bound)
    print(f"Transferd wormhole mailbox listening on {scheme}://{host}:{bound}", flush=True)
    if fingerprint:
        print(f"Its fingerprint, to pin in the other computers' settings: {fingerprint}", flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        await server.close()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run a Transferd wormhole mailbox.")
    parser.add_argument("--host", default="127.0.0.1", help="address to listen on (default 127.0.0.1, this computer only)")
    parser.add_argument("--port", type=int, default=4470, help="port to listen on (default 4470)")
    parser.add_argument(
        "--identity",
        metavar="FOLDER",
        help="folder for the mailbox's certificate and key, made on first run; serves wss:// with a pinned certificate",
    )
    args = parser.parse_args(argv)
    if args.host not in _LOOPBACK and not args.identity:
        print(
            "a mailbox that other computers can reach must be served over TLS: give --identity FOLDER, "
            "or keep --host 127.0.0.1",
            file=sys.stderr,
        )
        return 2
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(run_forever(args.host, args.port, args.identity))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
