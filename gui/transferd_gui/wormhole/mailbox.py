"""The mailbox client: one side's connection to a wormhole mailbox, with the retries a dropped connection needs.

A message that was sent but not confirmed is kept and sent again after `reconnect`. A message that arrived is delivered
once, however many times the mailbox replays it. An acknowledgement that could not be sent is sent after the next
connect. The server side of the protocol is `transferd_gui.wormhole.server`.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Dict, Optional, Sequence, Set, Tuple
from urllib.parse import urlparse

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, WebSocketException

from transferd_gui.wormhole import keys
from transferd_gui.wormhole.errors import MailboxError
from transferd_gui.wormhole.server import APP_ID, MAX_MESSAGE_BYTES, MOODS

_LOOPBACK = {"127.0.0.1", "localhost", "::1"}


@dataclass(frozen=True)
class Incoming:
    """A message from the other side, as the mailbox gave it. The caller acks it once it has been dealt with."""

    side: str
    phase: str
    message_id: str
    body: bytes


class MailboxClient:
    """One side's connection. Call `connect`, then `allocate` or `claim`, then `open`, then `add` and `receive`."""

    def __init__(self, url: str, side: str, *, timeout: float = 15.0) -> None:
        self.url = url
        self.side = keys.check_side(side)
        self.timeout = timeout
        self.nameplate: Optional[int] = None
        self.mailbox_id: Optional[str] = None
        self.limits: dict = {"max_message_bytes": MAX_MESSAGE_BYTES}
        self.connected = False
        self._ws: Optional[ClientConnection] = None
        self._reader: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        self._waiter: Optional[Tuple[str, Set[str], asyncio.Future]] = None
        self._inbox: asyncio.Queue = asyncio.Queue()
        self._seen: Set[Tuple[str, str]] = set()
        self._added: Dict[str, asyncio.Future] = {}
        self._unconfirmed: Dict[str, dict] = {}
        self._pending_acks: Set[str] = set()
        self._opened = False

    # ----- connection --------------------------------------------------------------------------------

    async def connect(self) -> None:
        host = urlparse(self.url).hostname or ""
        try:
            self._ws = await connect(
                self.url,
                max_size=2 * MAX_MESSAGE_BYTES + 8192,
                open_timeout=self.timeout,
                ping_interval=20,
                ping_timeout=20,
                proxy=None if host in _LOOPBACK else True,
            )
        except (OSError, WebSocketException, TimeoutError) as exc:
            raise MailboxError(f"could not reach the mailbox at {self.url}") from exc
        self.connected = True
        self._reader = asyncio.get_running_loop().create_task(self._read_loop(self._ws))
        await self._send({"type": "bind", "appid": APP_ID, "side": self.side})

    async def disconnect(self) -> None:
        self.connected = False
        if self._reader is not None:
            self._reader.cancel()
            try:
                await self._reader
            except (asyncio.CancelledError, Exception):
                pass
            self._reader = None
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

    async def reconnect(self) -> None:
        """Connect again, rejoin the mailbox, and send what the last connection did not confirm."""
        await self.disconnect()
        await self.connect()
        if self.mailbox_id is not None and self._opened:
            await self._request({"type": "open", "mailbox": self.mailbox_id}, ("opened",))
        for payload in list(self._unconfirmed.values()):
            await self._send(payload)
        for message_id in list(self._pending_acks):
            await self._send(self._ack_payload(message_id))
        self._pending_acks.clear()

    # ----- the mailbox -------------------------------------------------------------------------------

    async def allocate(self) -> int:
        reply = await self._request({"type": "allocate"}, ("allocated",))
        self.nameplate = int(reply["nameplate"])
        return self.nameplate

    async def claim(self, nameplate: int) -> str:
        reply = await self._request({"type": "claim", "nameplate": str(nameplate)}, ("claimed",))
        self.nameplate = nameplate
        self.mailbox_id = str(reply["mailbox"])
        return self.mailbox_id

    async def open(self) -> None:
        """Join the mailbox. The messages the other side sent before this are delivered first, then new ones."""
        if self.mailbox_id is None:
            raise MailboxError("claim a nameplate before opening its mailbox")
        await self._request({"type": "open", "mailbox": self.mailbox_id}, ("opened",))
        self._opened = True

    async def add(self, phase: str, body: bytes, message_id: Optional[str] = None) -> str:
        """Send one message. Returns once the mailbox has stored it. Raises `MailboxError` if it is not confirmed in time;
        it is still kept, and `reconnect` followed by `flush` sends it again.

        The caller may choose the id, because the id is part of what the body is sealed to. Otherwise one is made."""
        if self.mailbox_id is None:
            raise MailboxError("claim a nameplate before sending")
        limit = int(self.limits.get("max_message_bytes", MAX_MESSAGE_BYTES))
        if len(body) > limit:
            raise MailboxError(f"a message is at most {limit} bytes; this one is {len(body)}")
        if message_id is None:
            message_id = keys.new_message_id()
            while message_id in self._unconfirmed or message_id in self._added:
                message_id = keys.new_message_id()
        elif message_id in self._unconfirmed or message_id in self._added:
            raise MailboxError("this message id is already in use")
        payload = {
            "type": "add",
            "mailbox": self.mailbox_id,
            "id": message_id,
            "phase": phase,
            "body": body.hex(),
        }
        future = asyncio.get_running_loop().create_future()
        self._added[message_id] = future
        self._unconfirmed[message_id] = payload
        if self.connected:
            await self._send(payload)
        try:
            # Shielded: a timeout here must not cancel the future, because `flush` and a later reply still use it.
            await asyncio.wait_for(asyncio.shield(future), self.timeout)
        except asyncio.TimeoutError as exc:
            raise MailboxError("the mailbox did not confirm a message in time") from exc
        return message_id

    async def flush(self) -> None:
        """Wait until every message this client sent has been confirmed. Sends again the ones that were not."""
        if self.connected:
            for payload in list(self._unconfirmed.values()):
                await self._send(payload)
        futures = [future for mid, future in self._added.items() if mid in self._unconfirmed]
        if not futures:
            return
        done, pending = await asyncio.wait(futures, timeout=self.timeout)
        if pending:
            raise MailboxError("some messages are still not confirmed by the mailbox")
        for future in done:
            if future.exception() is not None:
                raise future.exception()

    async def receive(self, timeout: float) -> Optional[Incoming]:
        """The next message from the other side, or None if none came in `timeout` seconds."""
        try:
            return await asyncio.wait_for(self._inbox.get(), timeout)
        except asyncio.TimeoutError:
            return None

    async def ack(self, message_id: str) -> None:
        """Tell the mailbox a message has been dealt with, so it is not sent again. Kept and sent after a reconnect if the
        connection is down now."""
        if self.connected and self.mailbox_id is not None:
            try:
                await self._send(self._ack_payload(message_id))
                return
            except MailboxError:
                pass
        self._pending_acks.add(message_id)

    async def close(self, mood: str = "happy") -> None:
        """Close the mailbox for this side, and disconnect. The mailbox is removed once both sides have closed."""
        if mood not in MOODS:
            raise MailboxError(f"a mood is one of {', '.join(MOODS)}")
        if self.connected and self.mailbox_id is not None:
            try:
                await self._request({"type": "close", "mailbox": self.mailbox_id, "mood": mood}, ("closed",))
            except MailboxError:
                pass
        await self.disconnect()

    async def release(self) -> None:
        if self.connected and self.nameplate is not None:
            await self._request({"type": "release", "nameplate": str(self.nameplate)}, ("released",))

    # ----- plumbing ----------------------------------------------------------------------------------

    def _ack_payload(self, message_id: str) -> dict:
        return {"type": "ack", "mailbox": self.mailbox_id, "id": message_id}

    async def _send(self, payload: dict) -> None:
        if self._ws is None or not self.connected:
            raise MailboxError("not connected to the mailbox")
        try:
            await self._ws.send(json.dumps(payload, separators=(",", ":")))
        except ConnectionClosed as exc:
            self.connected = False
            raise MailboxError("the mailbox connection closed") from exc

    async def _request(self, payload: dict, expect: Sequence[str]) -> dict:
        async with self._lock:
            future = asyncio.get_running_loop().create_future()
            self._waiter = (payload["type"], set(expect), future)
            try:
                await self._send(payload)
                return await asyncio.wait_for(future, self.timeout)
            except asyncio.TimeoutError as exc:
                raise MailboxError(f"the mailbox did not answer '{payload['type']}' in time") from exc
            finally:
                self._waiter = None

    async def _read_loop(self, ws: ClientConnection) -> None:
        try:
            async for raw in ws:
                try:
                    message = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if isinstance(message, dict):
                    self._on_message(message)
        except (ConnectionClosed, WebSocketException, OSError):
            pass
        finally:
            if self._ws is ws:
                self.connected = False
            self._fail_waiter("the mailbox connection closed")

    def _on_message(self, message: dict) -> None:
        kind = message.get("type")
        if kind == "message":
            self._on_incoming(message)
        elif kind == "added":
            message_id = message.get("id")
            self._unconfirmed.pop(message_id, None)
            future = self._added.pop(message_id, None)
            if future is not None and not future.done():
                future.set_result(None)
        elif kind == "welcome":
            limits = message.get("limits")
            if isinstance(limits, dict):
                self.limits.update(limits)
        elif kind == "error":
            self._on_error(message)
        elif self._waiter is not None:
            request, expect, future = self._waiter
            if kind in expect and not future.done():
                future.set_result(message)

    def _on_incoming(self, message: dict) -> None:
        side = message.get("side")
        message_id = message.get("id")
        phase = message.get("phase")
        body = message.get("body")
        if not (isinstance(side, str) and isinstance(message_id, str) and isinstance(phase, str) and isinstance(body, str)):
            return
        if (side, message_id) in self._seen:
            return
        try:
            raw = bytes.fromhex(body)
        except ValueError:
            return
        self._seen.add((side, message_id))
        self._inbox.put_nowait(Incoming(side, phase, message_id, raw))

    def _on_error(self, message: dict) -> None:
        text = str(message.get("error", "the mailbox refused a request"))
        orig = message.get("orig")
        if orig == "add":
            # The mailbox refused one message: it will not take it, so it is not sent again, and its caller is told.
            message_id = message.get("id")
            self._unconfirmed.pop(message_id, None)
            future = self._added.pop(message_id, None)
            if future is not None and not future.done():
                future.set_exception(MailboxError(text))
            return
        if self._waiter is not None and (orig is None or orig == self._waiter[0]):
            _request, _expect, future = self._waiter
            if not future.done():
                future.set_exception(MailboxError(text))

    def _fail_waiter(self, text: str) -> None:
        if self._waiter is not None:
            _request, _expect, future = self._waiter
            if not future.done():
                future.set_exception(MailboxError(text))
