"""The wormhole's two sides, run against each other through the real mailbox server, with an in-memory transit so the state
machine is tested without the overlay. The overlay transit is tested with the endpoint."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Dict

import pytest

from test_wormhole_mailbox import run
from transferd.identity import HYBRID_PK_LEN
from transferd_gui.wormhole import protocol as P
from transferd_gui.wormhole import session
from transferd_gui.wormhole.codes import Code
from transferd_gui.wormhole.errors import OpenError, TimedOut, TransferError
from transferd_gui.wormhole.session import Receiver, Sender
from transferd_gui.wormhole.transfer import prepare_file, prepare_folder


class Hub:
    """The bytes that the in-memory transit carries from the sender to the receiver."""

    def __init__(self) -> None:
        self.data: Dict[str, bytes] = {}
        self.ready = asyncio.Event()


class MemoryTransit:
    def __init__(self, hub: Hub, name: str) -> None:
        self.hub = hub
        self.closed = False
        self._identity = P.PeerIdentity("ab" * 32, b"\x01" * HYBRID_PK_LEN, name, "", ())

    def local_identity(self) -> P.PeerIdentity:
        return self._identity

    def expect_peer(self, peer: P.PeerIdentity) -> None:
        self.expected = peer

    async def open(self) -> None:
        return None

    async def send(self, peer, offer, source: Path) -> None:
        self.hub.data[offer.name] = source.read_bytes()
        self.hub.ready.set()

    async def receive(self, peer, offer, folder: Path) -> Path:
        await asyncio.wait_for(self.hub.ready.wait(), 10)
        path = folder / offer.name
        path.write_bytes(self.hub.data[offer.name])
        return path

    async def close(self) -> None:
        self.closed = True


def pair(url: str, offer: P.Offer, source=None, *, answer_timeout: float = 10.0):
    hub = Hub()
    sender = Sender(url, transit=MemoryTransit(hub, "sender"), offer=offer, source=source, answer_timeout=answer_timeout)
    return sender, hub


async def connected_receiver(url: str, code: Code, hub: Hub) -> Receiver:
    receiver = Receiver(url, code, transit=MemoryTransit(hub, "receiver"))
    await receiver.connect()
    return receiver


# ----- transfers -------------------------------------------------------------------------------------


def test_a_text_is_offered_accepted_and_arrives_with_no_transit(tmp_path):
    def scenario(server, url):
        async def go():
            offer = P.Offer.for_text("hello, there")
            sender, hub = pair(url, offer)
            code = await sender.start()

            async def receiving():
                receiver = await connected_receiver(url, code, hub)
                got = await receiver.offer()
                assert got.kind == "text" and got.text == "hello, there"
                await receiver.answer(True)
                return await receiver.finish(tmp_path)

            send_result, receive_result = await asyncio.gather(sender.run(), receiving())
            assert send_result.ok and not send_result.declined
            assert receive_result.kind == "text" and receive_result.text == "hello, there"
            assert not hub.data, "a text never travels over the transit"
            return server.close_moods

        return go()

    moods = run(scenario)
    assert moods == ["happy", "happy"], "both sides close the mailbox happy"


def test_a_file_arrives_with_the_same_bytes_and_its_hash_is_checked(tmp_path):
    source = tmp_path / "report.bin"
    source.write_bytes(bytes(range(256)) * 1000)
    offer, path = prepare_file(source)
    folder = tmp_path / "downloads"
    folder.mkdir()

    def scenario(server, url):
        async def go():
            sender, hub = pair(url, offer, path)
            code = await sender.start()

            async def receiving():
                receiver = await connected_receiver(url, code, hub)
                assert (await receiver.offer()).kind == "file"
                await receiver.answer(True)
                return await receiver.finish(folder)

            send_result, receive_result = await asyncio.gather(sender.run(), receiving())
            assert send_result.ok
            return receive_result

        return go()

    result = run(scenario)
    assert result.kind == "file"
    assert result.path.read_bytes() == source.read_bytes()
    assert result.sha256 == offer.sha256


def test_a_folder_arrives_as_the_same_tree(tmp_path):
    tree = tmp_path / "photos"
    (tree / "2026" / "may").mkdir(parents=True)
    (tree / "readme.txt").write_text("hello", encoding="utf-8")
    (tree / "2026" / "may" / "a.bin").write_bytes(b"\x00\x01" * 4096)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    offer, archive = prepare_folder(tree, scratch)
    folder = tmp_path / "downloads"
    folder.mkdir()

    def scenario(server, url):
        async def go():
            sender, hub = pair(url, offer, archive)
            code = await sender.start()

            async def receiving():
                receiver = await connected_receiver(url, code, hub)
                assert (await receiver.offer()).kind == "directory"
                await receiver.answer(True)
                return await receiver.finish(folder)

            send_result, receive_result = await asyncio.gather(sender.run(), receiving())
            assert send_result.ok
            return receive_result

        return go()

    result = run(scenario)
    assert result.kind == "directory"
    assert result.path.name == "photos"
    assert (result.path / "readme.txt").read_text(encoding="utf-8") == "hello"
    assert (result.path / "2026" / "may" / "a.bin").read_bytes() == b"\x00\x01" * 4096
    assert not archive.exists() or archive.parent == scratch
    assert not any(p.name.endswith(".zip") for p in folder.iterdir()), "the archive is not left in the download folder"


def test_a_declined_offer_tells_the_sender_and_sends_nothing(tmp_path):
    source = tmp_path / "x.bin"
    source.write_bytes(b"secret")
    offer, path = prepare_file(source)
    hubs = []

    def scenario(server, url):
        async def go():
            sender, hub = pair(url, offer, path)
            hubs.append(hub)
            code = await sender.start()

            async def receiving():
                receiver = await connected_receiver(url, code, hub)
                await receiver.offer()
                await receiver.answer(False, "not now")
                return await receiver.finish(tmp_path / "downloads")

            return await asyncio.gather(sender.run(), receiving())

        return go()

    send_result, receive_result = run(scenario)
    assert send_result.declined and not send_result.ok
    assert send_result.reason == "not now"
    assert receive_result.kind == "declined"
    assert not hubs[0].data, "nothing was sent over the transit"
    assert not (tmp_path / "downloads").exists() or not any((tmp_path / "downloads").iterdir())


# ----- refusals and failures -------------------------------------------------------------------------


def test_a_wrong_code_is_refused_on_both_sides_and_closes_scary(tmp_path):
    def scenario(server, url):
        async def go():
            offer = P.Offer.for_text("hi")
            sender, hub = pair(url, offer)
            code = await sender.start()
            wrong = Code(code.nameplate, bytes((b + 1) % 256 for b in code.secret))

            async def receiving():
                receiver = Receiver(url, wrong, transit=MemoryTransit(hub, "receiver"))
                await receiver.connect()

            results = await asyncio.gather(sender.run(), receiving(), return_exceptions=True)
            return results, server.close_moods

        return go()

    results, moods = run(scenario)
    assert isinstance(results[0], OpenError), "the sender sees the wrong code when the receiver's version does not open"
    assert isinstance(results[1], OpenError)
    assert moods.count("scary") == 2, "both sides close the mailbox as scary"


def test_a_sender_whose_receiver_never_comes_times_out_as_lonely(tmp_path, monkeypatch):
    monkeypatch.setattr(session, "PAKE_TIMEOUT_S", 0.6)
    monkeypatch.setattr(session, "PHASE_TIMEOUT_S", 0.6)

    def scenario(server, url):
        async def go():
            sender, _hub = pair(url, P.Offer.for_text("hi"))
            await sender.start()
            with pytest.raises(TimedOut):
                await sender.run()
            return server.close_moods

        return go()

    assert run(scenario) == ["lonely"]


def test_a_receiver_that_never_answers_makes_the_sender_wait_then_stop(tmp_path):
    def scenario(server, url):
        async def go():
            hub = Hub()
            sender = Sender(url, transit=MemoryTransit(hub, "sender"), offer=P.Offer.for_text("hi"), answer_timeout=0.8)
            code = await sender.start()
            send_task = asyncio.ensure_future(sender.run())
            receiver = await connected_receiver(url, code, hub)
            # The receiver reads the offer, and never answers it.
            reading = asyncio.ensure_future(receiver.offer())
            with pytest.raises(TimedOut):
                await send_task
            reading.cancel()
            return server.close_moods

        return go()

    assert run(scenario) == ["lonely"], "the sender gave up waiting for an answer; the mailbox closed lonely"


def test_an_offer_that_does_not_match_the_received_bytes_is_removed_and_reported(tmp_path):
    source = tmp_path / "good.bin"
    source.write_bytes(b"the real bytes")
    offer, path = prepare_file(source)
    folder = tmp_path / "downloads"
    folder.mkdir()

    class Tampering(MemoryTransit):
        async def receive(self, peer, offer, folder: Path) -> Path:
            await asyncio.wait_for(self.hub.ready.wait(), 10)
            target = folder / offer.name
            target.write_bytes(b"something else")
            return target

    def scenario(server, url):
        async def go():
            hub = Hub()
            sender = Sender(url, transit=MemoryTransit(hub, "sender"), offer=offer, source=path)
            code = await sender.start()
            # The sender runs alongside the receiver, since each phase waits for the other side.
            send_task = asyncio.ensure_future(sender.run())
            receiver = Receiver(url, code, transit=Tampering(hub, "receiver"))
            await receiver.connect()
            await receiver.offer()
            await receiver.answer(True)
            with pytest.raises(TransferError, match="does not match"):
                await receiver.finish(folder)
            with pytest.raises(TransferError):
                await send_task
            return list(folder.iterdir())

        return go()

    assert run(scenario) == [], "the bad file is removed, and nothing is left in the folder"


def test_the_code_must_be_started_before_it_runs():
    def scenario(server, url):
        async def go():
            sender, _hub = pair(url, P.Offer.for_text("x"))
            with pytest.raises(Exception, match="start"):
                await sender.run()

        return go()

    run(scenario)
