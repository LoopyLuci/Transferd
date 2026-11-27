"""One wormhole attempt, from both ends: the sender's side and the receiver's side, as two state machines.

    sender:   start() -> the code to show;  run() -> sends pake, version, identity, offer; then the transfer; then done
    receiver: connect(code) -> pake, version, identity;  offer() -> what is offered;  answer(accept);  finish(folder)

Each phase is one message from each side, and the order is fixed. A phase that does not arrive in time raises `TimedOut`;
a phase that does not open raises `OpenError` (the code is wrong, or the message was changed); a phase that is not what the
protocol allows raises `ProtocolError`. Every attempt ends by closing its mailbox with a mood: `happy`, `lonely` (the other
side never came), `scary` (the code was wrong), or `errory`.

The transfer itself is the `Transit` the caller gives. The overlay implementation is `endpoint.OverlayTransit`; tests use a
transit that keeps the bytes in memory.
"""

from __future__ import annotations

import asyncio
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Optional, Protocol

from transferd_gui.wormhole import keys, protocol as P, transfer
from transferd_gui.wormhole.codes import Code, generate
from transferd_gui.wormhole.errors import (
    MailboxError,
    OpenError,
    ProtocolError,
    TimedOut,
    TransferError,
    WormholeError,
)
from transferd_gui.wormhole.keys import Pake, open_sealed, seal
from transferd_gui.wormhole.mailbox import Incoming, MailboxClient

PHASE_TIMEOUT_S = 120.0
PAKE_TIMEOUT_S = 60.0
ANSWER_TIMEOUT_S = 600.0


class Transit(Protocol):
    """Moves a file over the overlay, once both sides have agreed to it. The session calls it only after `accept`."""

    def local_identity(self) -> P.PeerIdentity:
        """This side's identity for the attempt, with the places to reach it. Valid after `open`."""

    def expect_peer(self, peer: P.PeerIdentity) -> None:
        """The other side's identity has been read and checked. Only that identity may connect from now on."""

    async def open(self) -> None:
        """Start listening and, if a relay is set, register with it. Called before the identities are sent."""

    async def send(self, peer: P.PeerIdentity, offer: P.Offer, source: Path) -> None:
        """Send `source` to the peer."""

    async def receive(self, peer: P.PeerIdentity, offer: P.Offer, folder: Path) -> Path:
        """Receive the file the offer describes into `folder`, and return where it was written."""

    async def close(self) -> None:
        """Stop listening and drop every connection of this attempt."""


@dataclass(frozen=True)
class SendResult:
    ok: bool
    reason: str = ""
    declined: bool = False


@dataclass(frozen=True)
class ReceiveResult:
    kind: str
    path: Optional[Path] = None
    text: str = ""
    sha256: str = ""


class _Exchange:
    """What both sides share: the mailbox, the keys once they exist, and the buffer of phases that came early."""

    def __init__(self, mailbox_url: str, side: str, transit: Transit, on_state: Optional[Callable[[str], None]]) -> None:
        self.side = keys.check_side(side)
        self.mailbox = MailboxClient(mailbox_url, self.side)
        self.transit = transit
        self._say = on_state or (lambda text: None)
        self._keys: Optional[keys.Keys] = None
        self._peer: Optional[str] = None
        self._buffer: Dict[str, Incoming] = {}

    def _state(self, text: str) -> None:
        self._say(text)

    async def _reconnect(self) -> None:
        await self.mailbox.reconnect()
        await self.mailbox.flush()

    async def _next(self, phase: str, timeout: float) -> Incoming:
        """The next message of `phase` from the other side. Other phases that arrive first are kept for later."""
        if phase in self._buffer:
            return self._buffer.pop(phase)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            if not self.mailbox.connected:
                try:
                    await self._reconnect()
                except MailboxError:
                    pass
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimedOut(f"the other computer did not send '{phase}' in time")
            incoming = await self.mailbox.receive(min(remaining, 1.0))
            if incoming is None:
                continue
            if self._peer is not None and incoming.side != self._peer:
                await self.mailbox.ack(incoming.message_id)
                continue
            if incoming.phase == phase:
                return incoming
            self._buffer.setdefault(incoming.phase, incoming)

    async def _send_sealed(self, phase: str, message: Dict) -> None:
        if self._keys is None:
            raise ProtocolError("the key exchange has not finished")
        message_id = keys.new_message_id()
        body = seal(self._keys, self.side, phase, message_id, P.encode(message))
        try:
            await self.mailbox.add(phase, body, message_id=message_id)
        except MailboxError:
            await self._reconnect()

    async def _recv_sealed(self, phase: str, timeout: Optional[float] = None) -> Dict:
        if self._keys is None:
            raise ProtocolError("the key exchange has not finished")
        incoming = await self._next(phase, PHASE_TIMEOUT_S if timeout is None else timeout)
        plain = open_sealed(self._keys, incoming.side, phase, incoming.message_id, incoming.body)
        await self.mailbox.ack(incoming.message_id)
        return P.decode(plain)

    async def _send_pake(self, pake: Pake) -> None:
        try:
            await self.mailbox.add("pake", pake.outbound)
        except MailboxError:
            await self._reconnect()

    async def _agree(self, pake: Pake) -> None:
        """Send our pake, read theirs, and derive the keys. A malformed one is refused."""
        await self._send_pake(pake)
        incoming = await self._next("pake", PAKE_TIMEOUT_S)
        self._peer = incoming.side
        await self.mailbox.ack(incoming.message_id)
        self._keys = pake.finish(incoming.body)

    async def _versions(self) -> None:
        await self._send_sealed("version", P.Version.ours().to_dict())
        version = P.Version.from_dict(await self._recv_sealed("version"))
        version.check()

    async def _identities(self) -> P.PeerIdentity:
        await self.transit.open()
        await self._send_sealed("identity", self.transit.local_identity().to_dict())
        peer = P.PeerIdentity.from_dict(await self._recv_sealed("identity"))
        self.transit.expect_peer(peer)
        return peer

    async def _end(self, mood: str) -> None:
        try:
            await self.mailbox.close(mood)
        except MailboxError:
            pass
        try:
            await self.transit.close()
        except Exception:
            pass

    @staticmethod
    def _mood_for(error: BaseException) -> str:
        if isinstance(error, OpenError):
            return "scary"
        if isinstance(error, TimedOut):
            return "lonely"
        return "errory"


class Sender(_Exchange):
    """The side that has something to send. It shows the code, then waits for the other side to type it."""

    def __init__(
        self,
        mailbox_url: str,
        *,
        transit: Transit,
        offer: P.Offer,
        source: Optional[Path] = None,
        on_state: Optional[Callable[[str], None]] = None,
        answer_timeout: float = ANSWER_TIMEOUT_S,
    ) -> None:
        super().__init__(mailbox_url, keys.new_side(), transit, on_state)
        self.offer = offer
        self.source = source
        self.code: Optional[Code] = None
        self._pake: Optional[Pake] = None
        self._answer_timeout = answer_timeout

    async def start(self) -> Code:
        """Connect, reserve a nameplate, and return the code to show. Safe to call once."""
        if self.code is not None:
            return self.code
        if self.offer.kind != "text" and self.source is None:
            raise ProtocolError("a file or folder offer needs its source")
        try:
            self._state("Connecting to the mailbox")
            await self.mailbox.connect()
            nameplate = await self.mailbox.allocate()
            await self.mailbox.claim(nameplate)
            await self.mailbox.open()
        except BaseException:
            await self._end("errory")
            raise
        self.code = generate(nameplate)
        self._pake = Pake(self.code.password())
        self._state("Waiting for the other computer to type the code")
        return self.code

    async def run(self) -> SendResult:
        if self.code is None or self._pake is None:
            raise ProtocolError("start() before run()")
        try:
            self._state("Exchanging keys")
            await self._agree(self._pake)
            await self._versions()
            self._state("Exchanging connection details")
            peer = await self._identities()
            self._state("Waiting for the other computer to accept")
            await self._send_sealed("offer", self.offer.to_dict())
            answer = P.Answer.from_dict(await self._recv_sealed("answer", self._answer_timeout))
            if not answer.accept:
                await self._end("happy")
                return SendResult(False, answer.reason or "the other computer declined", declined=True)
            if self.offer.kind != "text":
                self._state("Sending")
                await self.transit.send(peer, self.offer, self.source)  # type: ignore[arg-type]
            done = P.Done.from_dict(await self._recv_sealed("done"))
            if not done.ok:
                raise TransferError(done.reason or "the other computer did not accept the transfer")
            await self._end("happy")
            self._state("Sent")
            return SendResult(True)
        except WormholeError as exc:
            await self._end(self._mood_for(exc))
            raise
        except BaseException:
            await self._end("errory")
            raise

    async def abandon(self) -> None:
        """Stop before the other side has come: the code stops working and the mailbox is closed."""
        await self._end("lonely")


class Receiver(_Exchange):
    """The side that types a code. It reads what is offered, and only then the user decides."""

    def __init__(
        self,
        mailbox_url: str,
        code: Code,
        *,
        transit: Transit,
        on_state: Optional[Callable[[str], None]] = None,
    ) -> None:
        super().__init__(mailbox_url, keys.new_side(), transit, on_state)
        self.code = code
        self.offer_received: Optional[P.Offer] = None
        self.peer: Optional[P.PeerIdentity] = None
        self._accepted = False

    async def connect(self) -> None:
        """Join the code's mailbox and make the keys. Refused if the code is wrong, once the first sealed phase is read."""
        try:
            self._state("Connecting to the mailbox")
            await self.mailbox.connect()
            await self.mailbox.claim(self.code.nameplate)
            await self.mailbox.open()
            self._state("Exchanging keys")
            await self._agree(Pake(self.code.password()))
            await self._versions()
            self._state("Exchanging connection details")
            self.peer = await self._identities()
        except WormholeError as exc:
            await self._end(self._mood_for(exc))
            raise
        except BaseException:
            await self._end("errory")
            raise

    async def offer(self) -> P.Offer:
        """What the other side is offering. The caller shows it and then calls `answer`."""
        if self.peer is None:
            raise ProtocolError("connect() first")
        try:
            self._state("Waiting for the offer")
            self.offer_received = P.Offer.from_dict(await self._recv_sealed("offer"))
            return self.offer_received
        except WormholeError as exc:
            await self._end(self._mood_for(exc))
            raise
        except BaseException:
            await self._end("errory")
            raise

    async def answer(self, accept: bool, reason: str = "") -> None:
        if self.offer_received is None:
            raise ProtocolError("offer() first")
        self._accepted = bool(accept)
        await self._send_sealed("answer", P.Answer(bool(accept), reason).to_dict())

    async def finish(self, folder: Path) -> ReceiveResult:
        """Receive what was accepted into `folder`, check it, and tell the sender how it went.

        The bytes arrive in a hidden staging folder inside `folder`. Only a file that has checked is placed, under a name
        that does not exist yet; a folder is unpacked from its checked archive. Nothing else is left in `folder`."""
        if self.offer_received is None:
            raise ProtocolError("offer() first")
        offer = self.offer_received
        if not self._accepted:
            await self._end("happy")
            return ReceiveResult("declined")
        try:
            if offer.kind == "text":
                await self._send_sealed("done", P.Done(True).to_dict())
                await self._end("happy")
                return ReceiveResult("text", None, offer.text, offer.sha256)
            self._state("Receiving")
            assert self.peer is not None
            folder.mkdir(parents=True, exist_ok=True)
            staging = Path(tempfile.mkdtemp(prefix=".wormhole-", dir=folder))
            try:
                received = await self.transit.receive(self.peer, offer, staging)
                checked = self._verified(received, offer)
                if offer.kind == "directory":
                    path = transfer.unpack_folder(checked, folder, offer.name, expected_entries=offer.entries)
                else:
                    path = transfer.place_file(checked, folder, offer.name)
            finally:
                transfer.remove_quietly(staging)
            await self._send_sealed("done", P.Done(True, sha256=offer.sha256).to_dict())
            await self._end("happy")
            self._state("Received")
            return ReceiveResult(offer.kind, path, "", offer.sha256)
        except WormholeError as exc:
            await self._send_quietly("done", P.Done(False, reason=str(exc)[:200]).to_dict())
            await self._end(self._mood_for(exc))
            raise
        except BaseException:
            await self._send_quietly("done", P.Done(False, reason="the transfer failed").to_dict())
            await self._end("errory")
            raise

    async def _send_quietly(self, phase: str, message: Dict) -> None:
        try:
            await self._send_sealed(phase, message)
        except WormholeError:
            pass

    async def abandon(self) -> None:
        """Stop while the offer waits for the user. An offer that was read is declined, so the sender is told, then the
        attempt is closed. Before an offer is read there is nothing to decline."""
        if self.offer_received is not None and not self._accepted:
            await self._send_quietly("answer", P.Answer(False, "stopped by the receiving user").to_dict())
        await self._end("happy" if self.peer is not None else "lonely")

    @staticmethod
    def _verified(received: Path, offer: P.Offer) -> Path:
        """The received bytes are checked against the offer's hash before anything is made of them."""
        if not received.is_file() or transfer.sha256_file(received) != offer.sha256:
            raise TransferError("the received data does not match what was offered")
        return received


__all__ = ["ANSWER_TIMEOUT_S", "PHASE_TIMEOUT_S", "Receiver", "ReceiveResult", "SendResult", "Sender", "Transit"]
