"""End to end, with faults in the middle: the real mailbox server, the real overlay sessions and, for the relay tests, the real
relay binary.

- A route is cut while a file is moving over two routes. The overlay carries what was in flight on the cut route over the
  other one, and the file arrives intact.
- The sender's mailbox connection is cut before the other side has arrived. The sender reconnects, and the transfer still
  completes.
- A file moves through the relay, with no direct address offered at all. Needs TRANSFERD_RELAYD (see gui/README.md).
- A larger file through the relay: a known stall, recorded as an expected failure with the reproduction (see WORMHOLE.md).

Faults are injected with a TCP forwarder that counts the bytes it passes and can drop every connection through it. Nothing
in the library is patched.
"""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import List, Optional, Tuple

import pytest

from test_wormhole_mailbox import run
from transferd_gui.wormhole import protocol as P
from transferd_gui.wormhole.endpoint import OverlayTransit
from transferd_gui.wormhole.session import Receiver, Sender
from transferd_gui.wormhole.transfer import prepare_file

MIB = 1024 * 1024
RELAYD = os.environ.get("TRANSFERD_RELAYD", "")
needs_relayd = pytest.mark.skipif(
    not RELAYD or not Path(RELAYD).exists(),
    reason="set TRANSFERD_RELAYD to the built relayd binary",
)


class CutProxy:
    """A TCP forwarder in front of a listener. It counts the bytes it passes and can drop every connection through it."""

    def __init__(self) -> None:
        self.port = 0
        self.forwarded = 0
        self.accepted = 0
        self._target = 0
        self._server: Optional[asyncio.AbstractServer] = None
        self._pairs: List[Tuple[asyncio.StreamWriter, asyncio.StreamWriter]] = []

    async def start(self, target_port: int) -> None:
        self._target = target_port
        self._server = await asyncio.start_server(self._accept, "127.0.0.1", 0)
        self.port = int(self._server.sockets[0].getsockname()[1])

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.accepted += 1
        try:
            up_reader, up_writer = await asyncio.open_connection("127.0.0.1", self._target)
        except OSError:
            writer.close()
            return
        self._pairs.append((writer, up_writer))
        await asyncio.gather(self._pipe(reader, up_writer), self._pipe(up_reader, writer))

    async def _pipe(self, source: asyncio.StreamReader, sink: asyncio.StreamWriter) -> None:
        try:
            while True:
                data = await source.read(65536)
                if not data:
                    break
                self.forwarded += len(data)
                sink.write(data)
                await sink.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            sink.close()

    def cut(self, stop_accepting: bool = True) -> None:
        """Drop every connection through the proxy now. With `stop_accepting`, it also stops listening, so a route that
        was cut cannot come back through it."""
        if stop_accepting and self._server is not None:
            self._server.close()
        for client, upstream in self._pairs:
            client.transport.abort()
            upstream.transport.abort()
        self._pairs = []

    def close(self) -> None:
        self.cut(stop_accepting=True)


class FrontedReceiver(OverlayTransit):
    """A receiver whose only offered addresses are proxies in front of its listener. Every route the sender can use runs
    through a proxy the test can cut; the receiver's own listener stays open behind them."""

    def __init__(self, fronts: int, **kwargs) -> None:
        super().__init__(advertise=["127.0.0.1"], listen_host="127.0.0.1", **kwargs)
        self.fronts = [CutProxy() for _ in range(fronts)]

    async def open(self) -> None:
        first = self._server is None
        await super().open()
        if first:
            for front in self.fronts:
                await front.start(self._port)

    def local_identity(self) -> P.PeerIdentity:
        base = super().local_identity()
        if not self.fronts or any(front.port == 0 for front in self.fronts):
            return base
        return replace(base, hints=tuple(P.Hint("127.0.0.1", front.port) for front in self.fronts))


def loopback(**kwargs) -> OverlayTransit:
    return OverlayTransit(advertise=["127.0.0.1"], listen_host="127.0.0.1", **kwargs)


def payload(size: int) -> bytes:
    return bytes((i * 131 + (i >> 9)) & 0xFF for i in range(size))


def port_of(url: str) -> int:
    return int(url.rsplit(":", 1)[1])


async def settle(*tasks: asyncio.Future) -> None:
    """Stop tasks that may still be running, and collect their results, so a failure in one side is never left unretrieved."""
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def cut_first_when_both_carry(
    fronts: List[CutProxy], sent_task: asyncio.Future, minimum: int, transit: OverlayTransit
) -> Tuple[List[int], list]:
    """Wait until every route has carried `minimum` bytes, then cut the first one. Returns what each carried before the cut,
    and the sender's route counters as they were while the transfer still ran (the session is gone once it has ended)."""
    while min(front.forwarded for front in fronts) < minimum:
        if sent_task.done():
            raise AssertionError("the transfer finished before every route had carried data")
        await asyncio.sleep(0.005)
    before = [front.forwarded for front in fronts]
    fronts[0].cut()
    latest: list = []
    while not sent_task.done():
        reports = transit.route_reports()
        if reports:
            latest = reports
        if any(report.retx_bytes for report in latest):
            break
        await asyncio.sleep(0.005)
    return before, latest


# ----- faults in the middle ---------------------------------------------------------------------------


def test_a_route_cut_mid_transfer_is_carried_on_the_other_route(tmp_path):
    data = payload(32 * MIB + 77)
    source = tmp_path / "large.bin"
    source.write_bytes(data)
    folder = tmp_path / "downloads"
    minimum = 256 * 1024

    def scenario(server, url):
        async def go():
            offer, path = prepare_file(source)
            sender_transit = loopback(name="sender", streams=4)
            receiver_transit = FrontedReceiver(2, name="receiver", streams=4)
            sender = Sender(url, transit=sender_transit, offer=offer, source=path, answer_timeout=60)
            code = await sender.start()
            sent_task = asyncio.ensure_future(sender.run())
            receiver = Receiver(url, code, transit=receiver_transit)
            try:
                await receiver.connect()
                await receiver.offer()
                await receiver.answer(True)
                finish = asyncio.ensure_future(receiver.finish(folder))
                cutter = asyncio.ensure_future(
                    cut_first_when_both_carry(receiver_transit.fronts, sent_task, minimum, sender_transit)
                )
                received = await finish
                sent = await sent_task
                before, reports = await cutter
                after = [front.forwarded for front in receiver_transit.fronts]
                return sent, received, before, after, reports
            finally:
                for front in receiver_transit.fronts:
                    front.close()
                await settle(sent_task)

        return go()

    sent, received, before, after, reports = run(scenario, timeout=180)
    assert sent.ok
    assert received.path.read_bytes() == data
    assert all(count >= minimum for count in before), "both routes must have carried data before the cut"
    assert after[1] > before[1], "the route that was not cut kept carrying the rest of the file"
    assert reports, "the sender still had its session when the transfer ended"
    assert sum(report.retx_bytes for report in reports) > 0, "frames in flight on the cut route were sent again on another route"


def test_the_mailbox_connection_cut_before_the_other_side_arrives_is_reconnected(tmp_path):
    data = payload(3 * MIB + 5)
    source = tmp_path / "after-reconnect.bin"
    source.write_bytes(data)
    folder = tmp_path / "downloads"

    def scenario(server, url):
        async def go():
            front = CutProxy()
            await front.start(port_of(url))
            sent_task: Optional[asyncio.Future] = None
            try:
                offer, path = prepare_file(source)
                sender = Sender(
                    f"ws://127.0.0.1:{front.port}",
                    transit=loopback(name="sender"),
                    offer=offer,
                    source=path,
                    answer_timeout=60,
                )
                code = await sender.start()
                # Drop the sender's mailbox connection before the other side has even claimed the code. The proxy keeps
                # listening, so the sender has to reconnect through it.
                front.cut(stop_accepting=False)
                sent_task = asyncio.ensure_future(sender.run())
                receiver = Receiver(url, code, transit=loopback(name="receiver"))
                await receiver.connect()
                await receiver.offer()
                await receiver.answer(True)
                received = await receiver.finish(folder)
                sent = await sent_task
                return sent, received, front.accepted
            finally:
                front.close()
                if sent_task is not None:
                    await settle(sent_task)

        return go()

    sent, received, accepted = run(scenario, timeout=120)
    assert sent.ok
    assert received.path.read_bytes() == data
    assert accepted >= 2, "the sender reconnected to the mailbox through the proxy"


# ----- the relay --------------------------------------------------------------------------------------


def _free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@contextmanager
def relay_process():
    port = _free_udp_port()
    env = {**os.environ, "RELAYD_BIND": f"127.0.0.1:{port}", "RELAYD_DIFFICULTY": "12"}
    proc = subprocess.Popen([RELAYD], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        line = proc.stdout.readline()
        assert "listening" in line, proc.stderr.read()
        yield f"127.0.0.1:{port}"
    finally:
        proc.kill()


def relay_transfer(tmp_path: Path, data: bytes, timeout: float):
    """Move `data` with the receiver offering no direct address, so the relay is the only route. Returns what the sender
    and the receiver each got back."""
    source = tmp_path / "via-relay.bin"
    source.write_bytes(data)
    folder = tmp_path / "downloads"
    with relay_process() as relay_addr:

        def scenario(server, url):
            async def go():
                offer, path = prepare_file(source)
                sender_transit = OverlayTransit(name="sender", relay=relay_addr, streams=2)
                receiver_transit = OverlayTransit(name="receiver", relay=relay_addr, advertise=[], streams=2)
                sender = Sender(url, transit=sender_transit, offer=offer, source=path, answer_timeout=60)
                code = await sender.start()
                sent_task = asyncio.ensure_future(sender.run())
                receiver = Receiver(url, code, transit=receiver_transit)
                try:
                    await receiver.connect()
                    assert receiver_transit.local_identity().hints == (), "the receiver offers no direct address"
                    await receiver.offer()
                    await receiver.answer(True)
                    received = await receiver.finish(folder)
                    sent = await sent_task
                    return sent, received
                finally:
                    await settle(sent_task)

            return go()

        return run(scenario, timeout=timeout)


@needs_relayd
def test_a_file_moves_through_the_relay_when_no_direct_address_is_offered(tmp_path):
    data = payload(MIB)
    sent, received = relay_transfer(tmp_path, data, timeout=120)
    assert sent.ok
    # The receiver offers no direct address, so the relay was the only route the sender could use: the file arriving
    # proves the relay carried it.
    assert received.path.read_bytes() == data


@needs_relayd
@pytest.mark.xfail(
    strict=True,
    reason="known stall: over the relay, a transfer above about 1 MiB stops with the route at total loss. "
    "Reproduction and timings: gui/docs/WORMHOLE.md, section Known limits.",
)
def test_a_file_of_two_mib_moves_through_the_relay(tmp_path):
    data = payload(2 * MIB + 9)
    sent, received = relay_transfer(tmp_path, data, timeout=60)
    assert sent.ok
    assert received.path.read_bytes() == data
