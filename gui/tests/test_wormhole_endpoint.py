"""The overlay transit on 127.0.0.1: a wormhole file moves over a real overlay session with the real hybrid handshake, and the
identity checks refuse a stranger. The mailbox is the real server. The relay path is tested in the end-to-end file."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from test_wormhole_mailbox import run
from transferd_gui.wormhole import endpoint
from transferd_gui.wormhole import protocol as P
from transferd_gui.wormhole.endpoint import OverlayTransit
from transferd_gui.wormhole.errors import TransferError
from transferd_gui.wormhole.session import Receiver, Sender
from transferd_gui.wormhole.transfer import prepare_file


def loopback(**kwargs) -> OverlayTransit:
    return OverlayTransit(advertise=["127.0.0.1"], listen_host="127.0.0.1", **kwargs)


async def move(url: str, source: Path, folder: Path, sender_transit: OverlayTransit, receiver_transit: OverlayTransit):
    offer, path = prepare_file(source)
    sender = Sender(url, transit=sender_transit, offer=offer, source=path, answer_timeout=30)
    code = await sender.start()
    send_task = asyncio.ensure_future(sender.run())
    receiver = Receiver(url, code, transit=receiver_transit)
    await receiver.connect()
    await receiver.offer()
    await receiver.answer(True)
    received = await receiver.finish(folder)
    sent = await send_task
    return sent, received


def payload(size: int) -> bytes:
    return bytes((i * 37 + (i >> 8)) & 0xFF for i in range(size))


def test_a_file_moves_over_a_direct_overlay_route(tmp_path):
    data = payload(3 * 1024 * 1024 + 123)
    source = tmp_path / "movie.bin"
    source.write_bytes(data)
    folder = tmp_path / "downloads"

    def scenario(server, url):
        async def go():
            return await move(url, source, folder, loopback(name="sender"), loopback(name="receiver"))

        return go()

    sent, received = run(scenario, timeout=60)
    assert sent.ok
    assert received.kind == "file"
    assert received.path.read_bytes() == data
    assert [p.name for p in folder.iterdir()] == ["movie.bin"], "only the finished file is left in the download folder"


def test_a_file_larger_than_the_window_is_paced_by_progress_and_still_arrives(tmp_path, monkeypatch):
    monkeypatch.setattr(endpoint, "WINDOW_BYTES", 256 * 1024)
    monkeypatch.setattr(endpoint, "PROGRESS_BYTES", 128 * 1024)
    data = payload(3 * 1024 * 1024)
    source = tmp_path / "paced.bin"
    source.write_bytes(data)
    folder = tmp_path / "downloads"

    def scenario(server, url):
        async def go():
            return await move(url, source, folder, loopback(name="sender", streams=4), loopback(name="receiver"))

        return go()

    sent, received = run(scenario, timeout=90)
    assert sent.ok
    assert received.path.read_bytes() == data


def test_an_empty_file_moves(tmp_path):
    source = tmp_path / "empty.txt"
    source.write_bytes(b"")
    folder = tmp_path / "downloads"

    def scenario(server, url):
        async def go():
            return await move(url, source, folder, loopback(name="sender"), loopback(name="receiver"))

        return go()

    sent, received = run(scenario, timeout=60)
    assert sent.ok
    assert received.path.read_bytes() == b""


def test_dialling_collects_every_route_that_comes_up_and_skips_a_dead_one(tmp_path):
    """Two addresses are offered; one has nothing listening. The dial keeps the route that works."""

    async def scenario():
        receiver = loopback(name="receiver")
        await receiver.open()
        try:
            live = receiver.local_identity()
            dead_port = 1  # nothing listens on port 1
            peer = P.PeerIdentity(
                live.node_id,
                live.public_key,
                "receiver",
                "",
                (P.Hint("127.0.0.1", dead_port), P.Hint("127.0.0.1", live.hints[0].port)),
            )
            sender = loopback(name="sender")
            links = await sender._dial(peer)
            try:
                assert len(links) == 1
            finally:
                for link in links:
                    link.close()
                await sender.close()
        finally:
            await receiver.close()

    asyncio.run(asyncio.wait_for(scenario(), 30))


def test_a_connection_from_anyone_but_the_expected_peer_is_not_admitted():
    """The receiver expects someone else. The sender does check the receiver's key, and the handshake with the real
    receiver completes on its side; the receiver then closes the connection and admits nothing."""

    async def scenario():
        receiver = loopback(name="receiver")
        await receiver.open()
        try:
            live = receiver.local_identity()
            stranger = P.PeerIdentity("cd" * 32, b"\x02" * 2624, "someone", "", ())
            receiver.expect_peer(stranger)
            peer = P.PeerIdentity(live.node_id, live.public_key, "receiver", "", live.hints)
            sender = loopback(name="sender")
            links = []
            try:
                links = await sender._dial(peer)
            except TransferError:
                pass  # the close may land before the sender reads the answer; either way nothing is admitted
            await asyncio.sleep(0.3)
            assert receiver._inbound.empty(), "the receiver admits no connection from anyone but the expected peer"
            for link in links:
                link.close()
            await sender.close()
        finally:
            await receiver.close()

    asyncio.run(asyncio.wait_for(scenario(), 30))


def test_a_direct_address_must_be_the_one_the_code_named():
    """The address answers, but with a different identity from the one the code named: the link is not made."""

    async def scenario():
        receiver = loopback(name="receiver")
        await receiver.open()
        try:
            live = receiver.local_identity()
            receiver.expect_peer(P.PeerIdentity(live.node_id, live.public_key, "receiver", "", ()))
            wrong_node = P.PeerIdentity("ef" * 32, live.public_key, "receiver", "", live.hints)
            sender = loopback(name="sender")
            with pytest.raises(TransferError):
                await sender._dial(wrong_node)
            await sender.close()
        finally:
            await receiver.close()

    asyncio.run(asyncio.wait_for(scenario(), 30))


def test_the_local_identity_lists_the_listening_port_for_each_advertised_address():
    async def scenario():
        transit = loopback(name="x")
        assert transit.local_identity().hints == (), "nothing is listening yet, so no place to reach"
        await transit.open()
        try:
            identity = transit.local_identity()
            assert identity.hints == (P.Hint("127.0.0.1", identity.hints[0].port),)
            assert identity.hints[0].port > 0
        finally:
            await transit.close()

    asyncio.run(asyncio.wait_for(scenario(), 15))


def test_a_relay_address_must_be_host_and_port():
    with pytest.raises(TransferError, match="host:port"):
        endpoint._parse_addr("no-port-here")
    assert endpoint._parse_addr("relay.example:7777") == ("relay.example", 7777)
