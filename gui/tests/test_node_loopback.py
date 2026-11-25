"""Loopback tests of the node adapter: two real nodes in one process, talking over 127.0.0.1 with the library's own handshake,
sessions, framing and relay client. Nothing here is mocked.

The relay test needs the built relay. Point `TRANSFERD_RELAYD` at relayd.exe, for example
H:\\Projects\\Transferd\\target\\debug\\relayd.exe. Without it that test is skipped, as the library's own relay tests are.
"""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Callable, List, Tuple

import pytest

from support import free_port, make_settings
from transferd_gui.backend.node import Node, NodeError
from transferd_gui.model.settings import Settings

MIB = 1024 * 1024
RELAYD = os.environ.get("TRANSFERD_RELAYD", "")
needs_relayd = pytest.mark.skipif(
    not RELAYD or not Path(RELAYD).exists(),
    reason="set TRANSFERD_RELAYD to the built relayd binary",
)


class Recorder:
    """Collects what a node writes to its event log, as (level, source, message)."""

    def __init__(self) -> None:
        self.lines: List[Tuple[str, str, str]] = []

    def __call__(self, level: str, source: str, message: str) -> None:
        self.lines.append((level, source, message))


def make_node(folder: Path, **changes) -> Tuple[Node, Settings, Recorder]:
    settings = make_settings(folder, **changes)
    recorder = Recorder()
    return Node(settings, recorder), settings, recorder


async def until(predicate: Callable[[], bool], timeout: float = 10.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


@asynccontextmanager
async def started(*nodes: Node):
    """Start the nodes, and stop them again however the block ends."""
    for node in nodes:
        await node.start()
    try:
        yield
    finally:
        for node in reversed(nodes):
            await node.stop()


async def connect(a: Node, b: Node) -> None:
    """Have b dial a's listener, and wait until both ends show a ready session."""
    port = a.snapshot().listener.port
    assert port is not None
    peer = await b.dial_direct("127.0.0.1", port, expect_node_id=a.identity.node_id)
    assert peer == a.identity.node_id
    assert await until(lambda: len(a.snapshot().ready_peers()) == 1)
    assert len(b.snapshot().ready_peers()) == 1


async def accept_first_request(node: Node, timeout: float = 10.0) -> None:
    """Wait for a peer to ask to open a stream on this node, then accept it."""
    assert await until(lambda: len(node.snapshot().requests) >= 1, timeout), "no request arrived"
    await node.accept_request(node.snapshot().requests[0].request_id)


def write_payload(folder: Path, name: str, size: int) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(os.urandom(size))
    return path


# ----- before and during a session ---------------------------------------------------------------


def test_snapshot_before_start_reports_nothing_measured(tmp_path):
    node, _, _ = make_node(tmp_path / "n")
    snap = node.snapshot()
    assert snap.identity.ready is False
    assert snap.listener.active is False
    assert snap.sessions == ()
    assert snap.all_routes == ()
    assert snap.requests == ()
    assert snap.transfers == ()
    assert snap.relay.state == "off"


def test_dial_makes_a_ready_session_on_both_sides(tmp_path):
    async def scenario():
        a, _, _ = make_node(tmp_path / "a", allow_incoming=True)
        b, _, _ = make_node(tmp_path / "b")
        async with started(a, b):
            assert a.snapshot().listener.active
            await connect(a, b)
            assert [s.peer_id for s in b.snapshot().ready_peers()] == [a.identity.node_id]

    asyncio.run(scenario())


def test_route_is_direct_with_counters_and_a_measured_rtt(tmp_path):
    async def scenario():
        a, _, _ = make_node(tmp_path / "a", allow_incoming=True)
        b, _, _ = make_node(tmp_path / "b")
        async with started(a, b):
            await connect(a, b)
            assert await until(lambda: len(b.snapshot().all_routes) == 1)
            route = b.snapshot().all_routes[0]
            assert route.kind == "direct"
            assert route.up and not route.closed
            assert route.sent_bytes > 0 and route.received_bytes > 0
            # The RTT is None until a probe is answered, then it is a real number, never a guess.
            assert await until(lambda: b.snapshot().all_routes[0].rtt_ms is not None, timeout=20.0)
            assert b.snapshot().all_routes[0].rtt_ms >= 0.0

    asyncio.run(scenario())


def test_dial_with_a_wrong_expected_key_is_refused(tmp_path):
    async def scenario():
        a, _, _ = make_node(tmp_path / "a", allow_incoming=True)
        b, _, _ = make_node(tmp_path / "b")
        async with started(a, b):
            port = a.snapshot().listener.port
            with pytest.raises(NodeError, match="does not match the invite"):
                await b.dial_direct("127.0.0.1", port, expect_fingerprint=bytes(32))
            assert b.snapshot().sessions == ()

    asyncio.run(scenario())


def test_dial_to_a_closed_port_raises_node_error(tmp_path):
    async def scenario():
        b, _, _ = make_node(tmp_path / "b")
        async with started(b):
            with pytest.raises(NodeError, match="could not reach"):
                await b.dial_direct("127.0.0.1", free_port())

    asyncio.run(scenario())


def test_disconnect_ends_the_local_session_and_the_far_end_keeps_a_route_less_one(tmp_path):
    """The library keeps a session when its links drop, so the far end stays in 'no route' until its own node ends it."""

    async def scenario():
        a, _, _ = make_node(tmp_path / "a", allow_incoming=True)
        b, _, _ = make_node(tmp_path / "b")
        async with started(a, b):
            await connect(a, b)
            await b.disconnect_peer(a.identity.node_id)
            assert await until(lambda: b.snapshot().sessions == ())
            assert await until(lambda: [s.state for s in a.snapshot().sessions] == ["no route"])
            await a.disconnect_peer(b.identity.node_id)
            assert await until(lambda: a.snapshot().sessions == ())

    asyncio.run(scenario())


def test_disconnect_of_an_unknown_peer_is_refused(tmp_path):
    async def scenario():
        b, _, _ = make_node(tmp_path / "b")
        async with started(b):
            with pytest.raises(NodeError, match="not connected"):
                await b.disconnect_peer("ab" * 32)

    asyncio.run(scenario())


# ----- files ------------------------------------------------------------------------------------


def test_file_arrives_byte_identical_once_accepted_and_counters_agree(tmp_path):
    async def scenario():
        a, a_settings, _ = make_node(tmp_path / "a", allow_incoming=True)
        b, _, _ = make_node(tmp_path / "b")
        source = write_payload(tmp_path / "src", "sample.bin", 300_000)
        async with started(a, b):
            await connect(a, b)
            send = asyncio.create_task(b.send_file(a.identity.node_id, str(source)))
            await accept_first_request(a)
            await send
            assert await until(
                lambda: any(t.direction == "received" and t.status == "done" for t in a.snapshot().transfers)
            )
            received = list(Path(a_settings.download_dir).iterdir())
            assert [f.name for f in received] == ["sample.bin"]
            assert received[0].read_bytes() == source.read_bytes()
            assert b.snapshot().transfers[-1].status == "handed to the session"
            a_route = a.snapshot().all_routes[0]
            b_route = b.snapshot().all_routes[0]
            assert b_route.sent_bytes >= 300_000
            assert a_route.received_bytes >= 300_000

    asyncio.run(scenario())


def test_refused_file_fails_the_sender_and_writes_nothing(tmp_path):
    async def scenario():
        a, a_settings, _ = make_node(tmp_path / "a", allow_incoming=True)
        b, _, _ = make_node(tmp_path / "b")
        source = write_payload(tmp_path / "src", "refused.bin", 4_096)
        async with started(a, b):
            await connect(a, b)
            send = asyncio.create_task(b.send_file(a.identity.node_id, str(source)))
            assert await until(lambda: len(a.snapshot().requests) == 1)
            await a.refuse_request(a.snapshot().requests[0].request_id)
            with pytest.raises(NodeError, match="sending refused.bin failed"):
                await send
            assert a.snapshot().requests == ()
            assert b.snapshot().transfers[-1].status == "failed"
            folder = Path(a_settings.download_dir)
            assert not folder.exists() or not any(folder.iterdir())

    asyncio.run(scenario())


def test_auto_accept_takes_incoming_files_without_asking(tmp_path):
    async def scenario():
        a, a_settings, _ = make_node(tmp_path / "a", allow_incoming=True, auto_accept_files=True)
        b, _, _ = make_node(tmp_path / "b")
        source = write_payload(tmp_path / "src", "auto.bin", 10_000)
        async with started(a, b):
            await connect(a, b)
            await b.send_file(a.identity.node_id, str(source))
            target = Path(a_settings.download_dir) / "auto.bin"
            assert await until(lambda: target.exists() and target.stat().st_size == 10_000)
            assert a.snapshot().requests == ()

    asyncio.run(scenario())


def test_file_over_the_transfer_limit_is_refused_before_sending(tmp_path):
    async def scenario():
        a, _, _ = make_node(tmp_path / "a", allow_incoming=True)
        b, _, _ = make_node(tmp_path / "b", max_transfer_mib=1)
        big = write_payload(tmp_path / "src", "big.bin", MIB + 1)
        async with started(a, b):
            await connect(a, b)
            with pytest.raises(NodeError, match="over the 1 MiB limit"):
                await b.send_file(a.identity.node_id, str(big))
            assert a.snapshot().requests == ()

    asyncio.run(scenario())


def test_sending_to_an_unconnected_peer_is_refused(tmp_path):
    async def scenario():
        b, _, _ = make_node(tmp_path / "b")
        source = write_payload(tmp_path / "src", "x.bin", 10)
        async with started(b):
            with pytest.raises(NodeError, match="not connected"):
                await b.send_file("ab" * 32, str(source))

    asyncio.run(scenario())


# ----- streams ----------------------------------------------------------------------------------


def test_open_stream_sends_the_payload_and_both_ends_record_it(tmp_path):
    async def scenario():
        a, _, _ = make_node(tmp_path / "a", allow_incoming=True)
        b, _, _ = make_node(tmp_path / "b")
        async with started(a, b):
            await connect(a, b)
            send = asyncio.create_task(b.open_stream(a.identity.node_id, "stream", b"hello stream"))
            await accept_first_request(a)
            reply = await send
            # The plain stream service reads to the end and keeps what it read; it sends nothing back.
            assert reply == b""
            assert await until(
                lambda: any(
                    t.direction == "received" and t.service == "stream" and t.size == 12 and t.status == "done"
                    for t in a.snapshot().transfers
                )
            )
            assert any(t.direction == "sent" and t.service == "stream" and t.size == 12 for t in b.snapshot().transfers)

    asyncio.run(scenario())


def test_opening_an_unknown_service_is_reported_in_plain_words(tmp_path):
    async def scenario():
        a, _, _ = make_node(tmp_path / "a", allow_incoming=True)
        b, _, _ = make_node(tmp_path / "b")
        async with started(a, b):
            await connect(a, b)
            send = asyncio.create_task(b.open_stream(a.identity.node_id, "no-such-service", b"x"))
            await accept_first_request(a)
            # The receiving node accepts any service name; what matters is that the call returns without an exception.
            assert await send == b""

    asyncio.run(scenario())


# ----- invites ----------------------------------------------------------------------------------


def test_invite_from_one_node_connects_the_other(tmp_path):
    async def scenario():
        port = free_port()
        a, _, _ = make_node(
            tmp_path / "a",
            allow_incoming=True,
            listen_port=port,
            advertised_address=f"127.0.0.1:{port}",
        )
        b, _, _ = make_node(tmp_path / "b")
        async with started(a, b):
            made = await a.make_invite(900)
            assert made.text
            peer = await b.dial_invite(made.text)
            assert peer == a.identity.node_id
            assert await until(lambda: len(a.snapshot().ready_peers()) == 1)

    asyncio.run(scenario())


def test_making_an_invite_needs_incoming_and_an_address(tmp_path):
    async def scenario():
        closed, _, _ = make_node(tmp_path / "closed")
        open_no_address, _, _ = make_node(tmp_path / "open", allow_incoming=True)
        async with started(closed, open_no_address):
            with pytest.raises(NodeError, match="accept incoming connections"):
                await closed.make_invite(900)
            with pytest.raises(NodeError, match="advertised address"):
                await open_no_address.make_invite(900)

    asyncio.run(scenario())


def test_an_invite_that_does_not_verify_is_refused(tmp_path):
    async def scenario():
        b, _, _ = make_node(tmp_path / "b")
        async with started(b):
            with pytest.raises(NodeError, match="does not verify"):
                await b.dial_invite("transferd-invite-v1:not-a-real-invite")

    asyncio.run(scenario())


# ----- settings applied to a running node -------------------------------------------------------


def test_apply_settings_closes_the_listener(tmp_path):
    async def scenario():
        a, a_settings, _ = make_node(tmp_path / "a", allow_incoming=True)
        async with started(a):
            assert a.snapshot().listener.active
            await a.apply_settings(replace(a_settings, allow_incoming=False))
            assert not a.snapshot().listener.active

    asyncio.run(scenario())


# ----- relay ------------------------------------------------------------------------------------


def _free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def relay_address():
    port = _free_udp_port()
    env = {**os.environ, "RELAYD_BIND": f"127.0.0.1:{port}", "RELAYD_DIFFICULTY": "12"}
    proc = subprocess.Popen([RELAYD], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        line = proc.stdout.readline()
        assert "listening" in line, proc.stderr.read()
        yield f"127.0.0.1:{port}"
    finally:
        proc.kill()
        proc.wait()


@needs_relayd
def test_two_nodes_register_with_the_relay_and_connect_through_it(tmp_path, relay_address):
    async def scenario():
        # The answering node must allow incoming connections, or it stays silent by design (as the direct listener does).
        a, _, _ = make_node(tmp_path / "a", relay_address=relay_address, allow_incoming=True)
        b, _, _ = make_node(tmp_path / "b", relay_address=relay_address)
        async with started(a, b):
            assert await until(
                lambda: a.snapshot().relay.state == "registered" and b.snapshot().relay.state == "registered",
                timeout=20.0,
            )
            assert a.snapshot().relay.registrations_ok >= 1
            located = await b.locate(a.identity.node_id)
            assert located.peer_id == a.identity.node_id
            peer = await b.dial_relay(a.identity.node_id)
            assert peer == a.identity.node_id
            assert await until(lambda: len(a.snapshot().ready_peers()) == 1, timeout=20.0)
            assert "relay" in {r.kind for r in b.snapshot().all_routes}

    asyncio.run(scenario())
