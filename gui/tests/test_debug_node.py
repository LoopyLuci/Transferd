"""Debugging between real nodes. GUI nodes run in one process and talk over 127.0.0.1 through the library's own handshake,
sessions and streams. The device answers under its policy; the computer runs the operations.

The USB route is a second loopback connection to the device's listener. That is what `adb forward` provides: the node
dials 127.0.0.1 and attaches the connection to the same session. The adb program itself is replaced by a fake in the adb
tests, which say so. Nothing here needs a phone or a cable.
"""

from __future__ import annotations

import asyncio
import re
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Callable, Dict, List, Tuple

import pytest

from support import free_port, make_settings
from transferd_gui.backend.debugnode import policy_from_settings
from transferd_gui.backend.errors import NodeError
from transferd_gui.backend.node import Node
from transferd_gui.model.settings import SettingsError, updated

PY = sys.executable
COMPUTER = "a" * 64  # a computer id that the device lists as a debugger
STRANGER = "f" * 64  # a valid id the device does not list
PORT_IN_DETAIL = re.compile(r"127\.0\.0\.1:(\d+)")


class EventLog:
    """What a node wrote to its event log: a (level, source, text) entry per line."""

    def __init__(self) -> None:
        self.lines: List[Tuple[str, str, str]] = []

    def __call__(self, level: str, source: str, text: str) -> None:
        self.lines.append((level, source, text))

    def has(self, level: str, fragment: str) -> bool:
        return any(lvl == level and fragment in text for lvl, _, text in self.lines)


def make_node(folder: Path, **changes) -> Tuple[Node, EventLog]:
    log = EventLog()
    return Node(make_settings(folder, **changes), log), log


def computer_settings() -> Dict[str, object]:
    """A computer dials out; it does not listen and does not use the relay."""
    return dict(allow_incoming=False, relay_address="")


def device_settings(computer_id: str, **changes) -> Dict[str, object]:
    """A device that listens on a free port and answers `computer_id` for every debug operation."""
    base: Dict[str, object] = dict(
        allow_incoming=True,
        relay_address="",
        listen_port=free_port(),
        debug_enabled=True,
        debug_clients=computer_id,
        debug_info_mode="allow",
        debug_logcat_mode="allow",
        debug_shell_mode="ask",
        debug_forward_mode="allow",
    )
    base.update(changes)
    return base


def script(folder: Path, name: str, body: str) -> str:
    """Write a Python script and return the shell command that runs it with this interpreter."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text(body, encoding="utf-8")
    return f'"{PY}" "{path}"'


def printing_lines(folder: Path, count: int, delay: float = 0.0) -> str:
    """A command that prints `line 0` … `line N-1`, one every `delay` seconds."""
    body = f"import time\nfor i in range({count}):\n    print('line', i, flush=True)\n    time.sleep({delay})\n"
    return script(folder, f"lines_{count}_{int(delay * 1000)}.py", body)


def line_texts(messages: List[dict]) -> List[str]:
    return [m["text"] for m in messages if m.get("t") == "line"]


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
    for node in nodes:
        await node.start()
    try:
        yield
    finally:
        for node in reversed(nodes):
            await node.stop()


async def connect(computer: Node, device: Node) -> str:
    """The computer dials the device's listener; returns once both ends report a ready session."""
    port = device.snapshot().listener.port
    assert port is not None, "the device is not listening"
    peer_id = await computer.dial_direct("127.0.0.1", port, expect_node_id=device.identity.node_id)
    assert await until(lambda: bool(device.snapshot().ready_peers()))
    return peer_id


def stream_for(node: Node, handle: int):
    return next(s for s in node.snapshot().debug.streams if s.handle == handle)


# ----- the device answers, under its policy -------------------------------------------------------


def test_a_listed_computer_reads_the_device_information(tmp_path):
    async def scenario():
        computer, _ = make_node(tmp_path / "computer", **computer_settings())
        async with started(computer):
            device, _ = make_node(tmp_path / "device", **device_settings(computer.identity.node_id))
            async with started(device):
                await connect(computer, device)
                info = await computer.debug_info(device.identity.node_id)
                return info, device.identity.name

    info, device_name = asyncio.run(scenario())
    assert info["node"] == device_name
    assert info["protocol"] == 1
    assert info["platform"] == sys.platform


def test_a_computer_not_on_the_list_is_refused_and_the_device_logs_why(tmp_path):
    async def scenario():
        computer, _ = make_node(tmp_path / "computer", **computer_settings())
        async with started(computer):
            device, device_log = make_node(tmp_path / "device", **device_settings(STRANGER))
            async with started(device):
                await connect(computer, device)
                with pytest.raises(NodeError) as refused:
                    await computer.debug_info(device.identity.node_id)
                return str(refused.value), device_log

    message, device_log = asyncio.run(scenario())
    assert "did not allow" in message
    assert device_log.has("warning", "not on the device's list")


def test_a_device_with_debugging_off_does_not_offer_the_operations(tmp_path):
    async def scenario():
        computer, _ = make_node(tmp_path / "computer", **computer_settings())
        async with started(computer):
            device, _ = make_node(tmp_path / "device", **device_settings(computer.identity.node_id, debug_enabled=False))
            async with started(device):
                await connect(computer, device)
                with pytest.raises(NodeError) as off:
                    await computer.debug_info(device.identity.node_id)
                return str(off.value)

    assert "does not offer debugging" in asyncio.run(scenario())


def test_logcat_streams_the_device_log_to_the_computer_in_order(tmp_path):
    async def scenario():
        computer, _ = make_node(tmp_path / "computer", **computer_settings())
        async with started(computer):
            command = printing_lines(tmp_path / "scripts", 5)
            device, _ = make_node(
                tmp_path / "device",
                **device_settings(computer.identity.node_id, debug_log_command=command),
            )
            async with started(device):
                await connect(computer, device)
                got: List[dict] = []
                handle = await computer.debug_logcat_start(device.identity.node_id, "line", got.append)
                assert await until(lambda: len(line_texts(got)) >= 5, timeout=20)
                await computer.debug_stop(handle)
                return line_texts(got)

    assert asyncio.run(scenario()) == [f"line {i}" for i in range(5)]


def test_a_shell_command_runs_only_after_the_device_user_accepts_it(tmp_path):
    async def scenario():
        computer, _ = make_node(tmp_path / "computer", **computer_settings())
        async with started(computer):
            device, _ = make_node(tmp_path / "device", **device_settings(computer.identity.node_id))
            async with started(device):
                await connect(computer, device)
                command = script(tmp_path / "scripts", "hi.py", "print('hi', 0)\n")
                task = asyncio.create_task(computer.debug_shell(device.identity.node_id, command, 20.0))
                assert await until(lambda: any(r.service == "debug-shell" for r in device.snapshot().requests))
                await asyncio.sleep(0.3)
                assert not task.done(), "the command ran before the device user accepted it"
                request = next(r for r in device.snapshot().requests if r.service == "debug-shell")
                await device.accept_request(request.request_id)
                return await asyncio.wait_for(task, 20)

    result = asyncio.run(scenario())
    assert result["code"] == 0
    assert b"hi 0" in result["stdout"]


def test_a_shell_command_the_device_user_refuses_never_runs_and_is_reported_failed(tmp_path):
    async def scenario():
        computer, _ = make_node(tmp_path / "computer", **computer_settings())
        async with started(computer):
            device, _ = make_node(tmp_path / "device", **device_settings(computer.identity.node_id))
            async with started(device):
                await connect(computer, device)
                marker = tmp_path / "ran.txt"
                command = script(tmp_path / "scripts", "mark.py", f"open(r'{marker}', 'w').write('ran')\n")
                task = asyncio.create_task(computer.debug_shell(device.identity.node_id, command, 20.0))
                assert await until(lambda: any(r.service == "debug-shell" for r in device.snapshot().requests))
                request = next(r for r in device.snapshot().requests if r.service == "debug-shell")
                await device.refuse_request(request.request_id)
                with pytest.raises(NodeError):
                    await asyncio.wait_for(task, 20)
                shells = [s for s in computer.snapshot().debug.streams if s.kind == "shell"]
                return shells, marker.exists(), device.snapshot().requests

    shells, marker_exists, pending = asyncio.run(scenario())
    assert not marker_exists, "the refused command ran"
    assert pending == ()
    assert shells and shells[0].state == "failed", "the computer's Debug screen must not show a refused command as running"


def test_a_forward_carries_a_connection_to_a_port_on_the_device(tmp_path):
    async def scenario():
        async def echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            data = await reader.read(100)
            writer.write(b"echo:" + data)
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(echo, "127.0.0.1", 0)
        target = int(server.sockets[0].getsockname()[1])
        try:
            computer, _ = make_node(tmp_path / "computer", **computer_settings())
            async with started(computer):
                device, _ = make_node(
                    tmp_path / "device",
                    **device_settings(computer.identity.node_id, debug_forward_ports=str(target)),
                )
                async with started(device):
                    await connect(computer, device)
                    handle = await computer.debug_forward_start(device.identity.node_id, 0, target)
                    local = int(PORT_IN_DETAIL.search(stream_for(computer, handle).detail).group(1))
                    reader, writer = await asyncio.open_connection("127.0.0.1", local)
                    writer.write(b"hello")
                    await writer.drain()
                    writer.write_eof()
                    reply = await asyncio.wait_for(reader.read(), 20)
                    writer.close()
                    await computer.debug_stop(handle)
                    return reply
        finally:
            server.close()
            await server.wait_closed()

    assert asyncio.run(scenario()) == b"echo:hello"


def test_a_port_not_on_the_device_list_is_refused_and_the_reason_is_kept(tmp_path):
    async def scenario():
        computer, _ = make_node(tmp_path / "computer", **computer_settings())
        async with started(computer):
            device, _ = make_node(tmp_path / "device", **device_settings(computer.identity.node_id, debug_forward_ports="9"))
            async with started(device):
                await connect(computer, device)
                handle = await computer.debug_forward_start(device.identity.node_id, 0, 4444)
                local = int(PORT_IN_DETAIL.search(stream_for(computer, handle).detail).group(1))
                reader, writer = await asyncio.open_connection("127.0.0.1", local)
                reply = await asyncio.wait_for(reader.read(), 20)
                writer.close()
                note = stream_for(computer, handle).note
                await computer.debug_stop(handle)
                return reply, note

    reply, note = asyncio.run(scenario())
    assert reply == b"", "no bytes reach a port the device does not allow"
    assert "forward list" in note


# ----- routes: the hybrid part --------------------------------------------------------------------


def test_a_second_route_joins_the_session_and_a_log_stream_survives_losing_the_usb_route(tmp_path):
    """The main claim: one session, two routes. A log stream runs over both; the USB route is cut in the middle; every line
    still arrives, once and in order, over the route that is left."""

    async def scenario():
        computer, _ = make_node(tmp_path / "computer", **computer_settings())
        async with started(computer):
            command = printing_lines(tmp_path / "scripts", 160, delay=0.03)
            device, _ = make_node(
                tmp_path / "device",
                **device_settings(computer.identity.node_id, debug_log_command=command),
            )
            async with started(device):
                await connect(computer, device)
                port = device.snapshot().listener.port
                assert port is not None
                # The USB route: a second loopback connection to the device's listener, as `adb forward` provides.
                await computer.attach_route(device.identity.node_id, "127.0.0.1", port, label="usb:TESTSERIAL")
                assert await until(lambda: any(len(s.routes) == 2 for s in computer.snapshot().sessions))
                labels = {r.label for r in computer.snapshot().sessions[0].routes}

                got: List[dict] = []
                handle = await computer.debug_logcat_start(device.identity.node_id, "line", got.append)
                assert await until(lambda: len(line_texts(got)) >= 20, timeout=20)

                # Pull the cable. The test closes the USB link object directly; nothing else is touched.
                usb = [link for link in computer._peers[device.identity.node_id].links if link.describe.startswith("usb:")]
                assert len(usb) == 1
                usb[0].close()

                assert await until(lambda: len(line_texts(got)) >= 160, timeout=60)
                await computer.debug_stop(handle)
                states = [s.state for s in computer.snapshot().sessions]
                return labels, line_texts(got), states

    labels, texts, states = asyncio.run(scenario())
    assert "usb:TESTSERIAL" in labels
    assert texts == [f"line {i}" for i in range(160)], "every line arrived once, in order, across the route change"
    assert states == ["ready"], "the session stayed up on the route that is left"


def test_a_device_takes_a_second_route_from_a_peer_it_already_has_a_session_with(tmp_path):
    async def scenario():
        computer, _ = make_node(tmp_path / "computer", **computer_settings())
        async with started(computer):
            device, _ = make_node(tmp_path / "device", **device_settings(computer.identity.node_id))
            async with started(device):
                await connect(computer, device)
                port = device.snapshot().listener.port
                await computer.attach_route(device.identity.node_id, "127.0.0.1", port, label="wifi://second")
                assert await until(lambda: any(len(s.routes) == 2 for s in device.snapshot().sessions))
                return [len(s.routes) for s in device.snapshot().sessions], len(device.snapshot().ready_peers())

    route_counts, ready = asyncio.run(scenario())
    assert route_counts == [2], "the second connection became a route of the existing session, not a refusal"
    assert ready == 1


# ----- USB bridge, with a fake adb ----------------------------------------------------------------


def fake_adb(folder: Path) -> Path:
    """A stand-in for `adb`. It answers `devices`, `version` and `forward` (with or without `--remove`), and writes every
    call to `calls.txt` in its folder. It forwards nothing: the real adb is not available to the tests, and the forward's
    effect is covered by the loopback route tests."""
    folder.mkdir(parents=True, exist_ok=True)
    calls = folder / "calls.txt"
    body = (
        "import sys\n"
        f"open({str(calls)!r}, 'a', encoding='utf-8').write(' '.join(sys.argv[1:]) + '\\n')\n"
        "args = sys.argv[1:]\n"
        "if args[:1] == ['devices']:\n"
        "    print('List of devices attached')\n"
        "    print('PHONE1234\\tdevice usb:1-1 product:x model:Pixel_8 device:x transport_id:1')\n"
        "    print('OLD9999\\tunauthorized usb:1-2 transport_id:2')\n"
        "elif args[:1] == ['version']:\n"
        "    print('Android Debug Bridge version 1.0.41')\n"
        "elif 'forward' in args:\n"
        "    print('')\n"
        "else:\n"
        "    print('unknown command', file=sys.stderr)\n"
        "    sys.exit(1)\n"
    )
    program = folder / "fake_adb.py"
    program.write_text(body, encoding="utf-8")
    if sys.platform == "win32":
        launcher = folder / "adb.cmd"
        launcher.write_text(f'@"{PY}" "{program}" %*\r\n', encoding="ascii")
    else:
        launcher = folder / "adb"
        launcher.write_text(f'#!/bin/sh\nexec "{PY}" "{program}" "$@"\n', encoding="utf-8")
        launcher.chmod(0o755)
    return launcher


def test_usb_scan_reads_adb_devices_and_says_what_to_do_about_each(tmp_path):
    adb = fake_adb(tmp_path / "adb")

    async def scenario():
        node, _ = make_node(tmp_path / "computer", **computer_settings(), usb_adb_path=str(adb))
        await node.start()
        try:
            devices = await node.debug_scan_usb()
            return devices, node.debug_snapshot()
        finally:
            await node.stop()

    devices, snapshot = asyncio.run(scenario())
    assert [(d.serial, d.state, d.model) for d in devices] == [
        ("PHONE1234", "device", "Pixel 8"),
        ("OLD9999", "unauthorized", ""),
    ]
    assert snapshot.usb_state == "ready", "one device is ready, so the bridge can use it"
    assert "1 device(s) ready" in snapshot.usb_detail
    assert len(snapshot.usb_devices) == 2
    assert "allow USB debugging" in devices[1].help, "the unauthorized phone says what to do"


def test_a_usb_forward_that_cannot_be_attached_is_removed_again(tmp_path):
    adb = fake_adb(tmp_path / "adb")

    async def scenario():
        node, _ = make_node(
            tmp_path / "computer",
            **computer_settings(),
            debug_device_id=COMPUTER,
            usb_adb_path=str(adb),
            usb_device_port=8022,
        )
        await node.start()
        try:
            with pytest.raises(NodeError):
                await node.debug_connect_usb("PHONE1234")  # nothing listens on the forwarded port, so the attach fails
        finally:
            await node.stop()

    asyncio.run(scenario())
    calls = (tmp_path / "adb" / "calls.txt").read_text(encoding="utf-8").splitlines()
    assert any(c.startswith("-s PHONE1234 forward tcp:") for c in calls)
    assert any(c.startswith("forward --remove tcp:") for c in calls), "the forward was left behind"


def test_the_usb_bridge_waits_before_it_tries_again_after_a_failed_attach(tmp_path):
    adb = fake_adb(tmp_path / "adb")

    async def scenario():
        node, log = make_node(
            tmp_path / "computer",
            **computer_settings(),
            debug_device_id=COMPUTER,
            usb_adb_path=str(adb),
            usb_device_port=8022,
        )
        await node.start()
        try:
            await node._usb_tick()
            return node._usb_retry_at, log
        finally:
            await node.stop()

    retry_at, log = asyncio.run(scenario())
    assert retry_at > time.time(), "the bridge must not retry at once"
    assert log.has("warning", "could not attach USB")


# ----- settings and policy ------------------------------------------------------------------------


def test_debugging_settings_are_checked_before_they_are_used(tmp_path):
    base = make_settings(tmp_path)
    good = updated(base, {"debug_forward_ports": "8080, 5555", "debug_clients": COMPUTER, "debug_shell_mode": "allow"})
    assert good.debug_clients == COMPUTER
    for bad in (
        {"debug_forward_ports": "99999"},
        {"debug_clients": "xyz"},
        {"debug_device_id": "short"},
        {"debug_transport": "satellite"},
        {"debug_shell_mode": "sometimes"},
        {"debug_wifi_address": "no-port"},
        {"usb_device_port": 70000},
        {"usb_adb_path": "  "},
        {"debug_log_command": "bad\x01text"},
    ):
        with pytest.raises(SettingsError):
            updated(base, bad)


def test_the_device_policy_follows_the_settings(tmp_path):
    settings = updated(
        make_settings(tmp_path),
        {"debug_enabled": True, "debug_clients": "b" * 64, "debug_forward_ports": "8080", "debug_shell_mode": "deny"},
    )
    policy = policy_from_settings(settings)
    assert policy.enabled
    assert policy.clients == frozenset({"b" * 64})
    assert policy.forward_ports == frozenset({8080})
    assert policy.mode("shell") == "deny"
    assert policy.mode("logcat") == settings.debug_logcat_mode
