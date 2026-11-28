"""The mailbox client: one side's connection to a wormhole mailbox, with what a real network needs.

- Addresses. `MailboxClient` takes the list from Settings (see `addresses.py`) and tries it in order, for a few rounds, with a
  growing and randomised pause between rounds. Plain ws:// is only ever used for this computer.
- Pins. An address with a pin must present the pinned certificate. That is checked after the handshake and before anything
  is sent, so a different server never sees this side's nameplate.
- One mailbox. Two sides can only meet on the same server. So once connected, this client stays on that mailbox: a
  reconnect goes back to it, and a fallback to another one happens only before a meeting has begun.
- Refusals for rate. A request refused with `rate_limited` is sent again after the time the mailbox asked for, and so is a
  message refused that way, rather than failing its caller.

A message that was sent but not confirmed is kept and sent again after `reconnect`. A message that arrived is delivered
once, however many times the mailbox replays it. An acknowledgement that could not be sent is sent after the next connect.
The server side of the protocol is `transferd_gui.wormhole.server`.
"""

from __future__ import annotations

import asyncio
import json
import random
import ssl
from dataclasses import dataclass
from typing import Dict, Optional, Sequence, Set, Tuple, Union

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, WebSocketException

from transferd_gui.wormhole import keys, tls
from transferd_gui.wormhole.addresses import MailboxAddress, parse_list
from transferd_gui.wormhole.errors import MailboxError
from transferd_gui.wormhole.server import APP_ID, MAX_MESSAGE_BYTES, MOODS, PROTOCOL

CONNECT_ATTEMPTS = 3
BACKOFF_S = 0.5
BACKOFF_CAP_S = 8.0
#: A request refused for its rate is sent again this many times at most, and only when the wait asked for is this short.
RATE_RETRIES = 4
RATE_WAIT_MAX_S = 10.0
RATE_CODE = "rate_limited"


class _PinMismatch(MailboxError):
    """The mailbox presented a certificate other than the pinned one. Trying that address again cannot help."""


@dataclass(frozen=True)
class Incoming:
    """A message from the other side, as the mailbox gave it. The caller acks it once it has been dealt with."""

    side: str
    phase: str
    message_id: str
    body: bytes


class MailboxClient:
    """One side's connection. Call `connect`, then `allocate` or `claim`, then `open`, then `add` and `receive`.

    `mailboxes` is the Settings list as text, or a sequence of addresses. A single `ws://` or `wss://` address is a list of one.
    """

    def __init__(
        self,
        mailboxes: Union[str, Sequence[MailboxAddress]],
        side: str,
        *,
        timeout: float = 15.0,
        attempts: int = CONNECT_ATTEMPTS,
        backoff: float = BACKOFF_S,
        backoff_cap: float = BACKOFF_CAP_S,
    ) -> None:
        if isinstance(mailboxes, str):
            try:
                addresses = parse_list(mailboxes)
            except ValueError as exc:
                raise MailboxError(f"the mailbox setting: {exc}") from exc
        else:
            addresses = tuple(mailboxes)
        if not addresses:
            raise MailboxError("no mailbox is set; add one in Settings (Wormhole)")
        self.addresses: Tuple[MailboxAddress, ...] = addresses
        self.url: Optional[str] = None
        self.side = keys.check_side(side)
        self.timeout = timeout
        self.attempts = max(1, int(attempts))
        self.backoff = backoff
        self.backoff_cap = backoff_cap
        self.nameplate: Optional[int] = None
        self.mailbox_id: Optional[str] = None
        self.limits: dict = {"max_message_bytes": MAX_MESSAGE_BYTES}
        self.connected = False
        self._current: Optional[MailboxAddress] = None
        self._ws: Optional[ClientConnection] = None
        self._reader: Optional[asyncio.Task] = None
        self._background: Set[asyncio.Task] = set()
        self._lock = asyncio.Lock()
        self._welcome: Optional[asyncio.Future] = None
        self._waiter: Optional[Tuple[str, Set[str], asyncio.Future]] = None
        self._inbox: asyncio.Queue = asyncio.Queue()
        self._seen: Set[Tuple[str, str]] = set()
        self._added: Dict[str, asyncio.Future] = {}
        self._unconfirmed: Dict[str, dict] = {}
        self._pending_acks: Set[str] = set()
        self._opened = False

    # ----- connection --------------------------------------------------------------------------------

    async def connect(self) -> None:
        """Connect and check the mailbox's greeting. Tries the addresses in order, for `attempts` rounds. Once a mailbox has
        been used, every later connection goes to that same mailbox only."""
        candidates = (self._current,) if self._current is not None else self.addresses
        refused: Set[MailboxAddress] = set()
        last: Optional[MailboxError] = None
        for attempt in range(self.attempts):
            for address in candidates:
                if address in refused:
                    continue
                try:
                    await self._open(address)
                    return
                except _PinMismatch as exc:
                    refused.add(address)
                    last = exc
                except MailboxError as exc:
                    last = exc
            if len(refused) == len(candidates) or attempt + 1 == self.attempts:
                break
            hint = last.retry_after if last is not None and last.code in ("busy", RATE_CODE) else 0.0
            await asyncio.sleep(self._pause(attempt, hint))
        raise last if last is not None else MailboxError("no mailbox answered")

    async def _open(self, address: MailboxAddress) -> None:
        if address.scheme == "ws" and not address.loopback:
            raise MailboxError(f"ws:// reaches only this computer; {address.url} is refused")
        try:
            ws = await connect(
                address.url,
                ssl=_context_for(address),
                max_size=2 * MAX_MESSAGE_BYTES + 8192,
                open_timeout=self.timeout,
                ping_interval=20,
                ping_timeout=20,
                close_timeout=5,
                proxy=None if address.loopback else True,
            )
        except ssl.SSLError as exc:
            raise MailboxError(f"the mailbox at {address.url} did not present a certificate this computer accepts") from exc
        except (OSError, WebSocketException, TimeoutError) as exc:
            raise MailboxError(f"could not reach the mailbox at {address.url}") from exc
        if address.pin:
            seen = tls.peer_fingerprint(ws)
            if seen != address.pin:
                await _close_quietly(ws)
                raise _PinMismatch(
                    f"the mailbox at {address.url} is not the one pinned in Settings: it presented a different certificate"
                )
        self._ws = ws
        self.connected = True
        loop = asyncio.get_running_loop()
        self._welcome = loop.create_future()
        self._reader = loop.create_task(self._read_loop(ws))
        try:
            welcome = await asyncio.wait_for(self._welcome, self.timeout)
        except asyncio.TimeoutError as exc:
            await self.disconnect()
            raise MailboxError(f"the mailbox at {address.url} did not greet this computer in time") from exc
        except MailboxError:
            await self.disconnect()
            raise
        if welcome.get("protocol", PROTOCOL) != PROTOCOL:
            await self.disconnect()
            raise MailboxError(
                f"the mailbox at {address.url} speaks protocol {welcome.get('protocol')}; this computer speaks {PROTOCOL}"
            )
        limits = welcome.get("limits")
        if isinstance(limits, dict):
            self.limits.update(limits)
        self.url = address.url
        self._current = address
        await self._send({"type": "bind", "appid": APP_ID, "side": self.side})

    def _pause(self, attempt: int, hint: float = 0.0) -> float:
        """How long to wait before the next round: exponential, capped, randomised, and never less than the mailbox asked."""
        growth = min(self.backoff_cap, self.backoff * (2 ** attempt))
        return max(min(hint, RATE_WAIT_MAX_S), growth * (0.5 + random.random() / 2))

    async def disconnect(self) -> None:
        self.connected = False
        for task in list(self._background):
            task.cancel()
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
        """Connect again to the same mailbox, rejoin it, and send what the last connection did not confirm."""
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
            for attempt in range(RATE_RETRIES + 1):
                future = asyncio.get_running_loop().create_future()
                self._waiter = (payload["type"], set(expect), future)
                try:
                    await self._send(payload)
                    return await asyncio.wait_for(future, self.timeout)
                except asyncio.TimeoutError as exc:
                    raise MailboxError(f"the mailbox did not answer '{payload['type']}' in time") from exc
                except MailboxError as exc:
                    if exc.code != RATE_CODE or attempt == RATE_RETRIES or exc.retry_after > RATE_WAIT_MAX_S:
                        raise
                    await asyncio.sleep(exc.retry_after)
                finally:
                    self._waiter = None
        raise MailboxError(f"the mailbox kept refusing '{payload['type']}'")

    def _resend_later(self, message_id: str, delay: float) -> None:
        task = asyncio.get_running_loop().create_task(self._resend(message_id, delay))
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _resend(self, message_id: str, delay: float) -> None:
        await asyncio.sleep(delay)
        payload = self._unconfirmed.get(message_id)
        if payload is not None and self.connected:
            try:
                await self._send(payload)
            except MailboxError:
                pass

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
            if self._welcome is not None and not self._welcome.done():
                self._welcome.set_result(message)
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
        code = str(message.get("code", ""))
        retry = _seconds(message.get("retry_after"))
        orig = message.get("orig")
        if self._welcome is not None and not self._welcome.done():
            # Before the greeting, the mailbox is refusing the connection itself (for example, that it is busy).
            self._welcome.set_exception(MailboxError(text, code=code, retry_after=retry))
            return
        if orig == "add":
            message_id = message.get("id")
            if code == RATE_CODE and message_id in self._unconfirmed:
                # The mailbox will take this message after the wait it asked for, so it is sent again rather than failed.
                self._resend_later(message_id, retry)
                return
            # Any other refusal: the mailbox will not take the message, so it is not sent again, and its caller is told.
            self._unconfirmed.pop(message_id, None)
            future = self._added.pop(message_id, None)
            if future is not None and not future.done():
                future.set_exception(MailboxError(text, code=code, retry_after=retry))
            return
        if self._waiter is not None and (orig is None or orig == self._waiter[0]):
            _request, _expect, future = self._waiter
            if not future.done():
                future.set_exception(MailboxError(text, code=code, retry_after=retry))

    def _fail_waiter(self, text: str) -> None:
        if self._welcome is not None and not self._welcome.done():
            self._welcome.set_exception(MailboxError(text))
        if self._waiter is not None:
            _request, _expect, future = self._waiter
            if not future.done():
                future.set_exception(MailboxError(text))


def _context_for(address: MailboxAddress) -> Optional[ssl.SSLContext]:
    if address.scheme == "ws":
        return None
    return tls.pinned_context() if address.pin else tls.system_context()


def _seconds(value) -> float:
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return 0.0


async def _close_quietly(ws: ClientConnection) -> None:
    try:
        await ws.close()
    except Exception:
        pass
