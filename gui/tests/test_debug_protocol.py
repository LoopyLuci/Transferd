"""The debug protocol without any network: the wire format, the policy, and the agent and client over in-memory channels.

The operations run for real: shell commands are real processes, forwards reach a real loopback listener, and the log
source is a real process. Nothing is mocked except the device's identity, which is a fixed string.
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import AsyncIterator, List

import pytest

from transferd_gui.debug import agent as agent_mod
from transferd_gui.debug import wire
from transferd_gui.debug.agent import DebugAgent
from transferd_gui.debug.client import DebugClient, DebugError, bridge
from transferd_gui.debug.policy import DebugPolicy, check_modes, decide, parse_clients, parse_ports
from transferd_gui.debug.providers import SystemProviders
from transferd_gui.debug.stream import Channel, pipe_pair

CLIENT = "c" * 64
OTHER = "d" * 64
PY = sys.executable


def run(coro):
    return asyncio.run(coro)


def policy(**changes) -> DebugPolicy:
    base = dict(enabled=True, clients=frozenset({CLIENT}), forward_ports=frozenset({9}))
    base.update(changes)
    return DebugPolicy(**base)


class Connection:
    """A debugging computer and a device joined by in-memory channels. `open` plays the node's part: it makes the
    device's end of a stream and hands the device's agent to serve it."""

    def __init__(self, agent: DebugAgent, client_id: str = CLIENT) -> None:
        self.agent = agent
        self.client_id = client_id
        self.tasks: List[asyncio.Task] = []

    async def open(self, service: str) -> Channel:
        op = wire.op_of(service)
        assert op is not None, service
        decision = self.agent.decide_open(op, self.client_id)
        if decision.outcome == "refuse":
            raise DebugError(decision.code, decision.reason)
        mine, theirs = pipe_pair()
        self.tasks.append(asyncio.create_task(self.agent.serve(op, self.client_id, theirs)))
        return mine

    async def close(self) -> None:
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)


def make(providers=None, **changes):
    audit: List[tuple] = []
    agent = DebugAgent(lambda: policy(**changes), providers or SystemProviders(node_name="device", node_id="e" * 64), lambda level, text: audit.append((level, text)))
    return Connection(agent), audit


# ----- wire ---------------------------------------------------------------------------------------


def test_encode_is_one_line_of_json():
    data = wire.encode({"t": "line", "text": "héllo"})
    assert data.endswith(b"\n") and data.count(b"\n") == 1
    assert json.loads(data) == {"t": "line", "text": "héllo"}


def test_encode_refuses_a_message_over_its_limit():
    with pytest.raises(wire.WireError):
        wire.encode({"t": "line", "text": "x" * 100}, limit=50)


def test_request_carries_version_and_args_and_respects_its_limit():
    line = wire.request({"filter": "x"})
    assert json.loads(line) == {"v": wire.PROTOCOL_VERSION, "args": {"filter": "x"}}
    with pytest.raises(wire.WireError):
        wire.request({"filter": "x" * (wire.MAX_REQUEST + 1)})


def test_decoder_joins_pieces_and_keeps_bytes_after_the_first_line():
    decoder = wire.LineDecoder()
    decoder.push(b'{"t":"ok"}\n{"t":"li')
    assert decoder.take() == {"t": "ok"}
    assert decoder.take() is None
    decoder.push(b'ne"}\nRAW')
    assert decoder.take_all() == [{"t": "line"}]
    assert decoder.rest() == b"RAW"


def test_decoder_refuses_a_line_longer_than_its_limit():
    decoder = wire.LineDecoder(limit=16)
    decoder.push(b"x" * 17)
    with pytest.raises(wire.WireError):
        decoder.take()


def test_decoder_refuses_something_that_is_not_json_or_not_an_object():
    decoder = wire.LineDecoder()
    decoder.push(b"not json\n")
    with pytest.raises(wire.WireError):
        decoder.take()
    decoder.push(b"[1, 2]\n")
    with pytest.raises(wire.WireError):
        decoder.take()


def test_service_names_have_no_dots_and_round_trip():
    for op in wire.OPS:
        name = wire.service_name(op)
        assert "." not in name
        assert wire.op_of(name) == op
    assert wire.op_of("file") is None
    assert wire.op_of("debug-nothing") is None
    with pytest.raises(wire.WireError):
        wire.service_name("reboot")


def test_base64_round_trips_and_refuses_garbage():
    assert wire.unb64(wire.b64(b"\x00\xff bytes")) == b"\x00\xff bytes"
    with pytest.raises(wire.WireError):
        wire.unb64("!!not base64!!")


# ----- policy -------------------------------------------------------------------------------------


def test_disabled_debugging_refuses_everything():
    decision = decide(policy(enabled=False), "info", CLIENT)
    assert decision.outcome == "refuse" and decision.code == "disabled"


def test_a_computer_not_on_the_list_is_refused_whatever_the_mode():
    decision = decide(policy(modes={"info": "allow", "logcat": "allow", "shell": "allow", "forward": "allow"}), "shell", OTHER)
    assert decision.outcome == "refuse" and decision.code == "denied"


def test_each_mode_maps_to_its_outcome():
    modes = {"info": "allow", "logcat": "ask", "shell": "deny", "forward": "ask"}
    p = policy(modes=modes)
    assert decide(p, "info", CLIENT).outcome == "allow"
    assert decide(p, "logcat", CLIENT).outcome == "ask"
    shell = decide(p, "shell", CLIENT)
    assert shell.outcome == "refuse" and shell.code == "denied"


def test_default_policy_asks_before_logs_shell_or_forwards():
    p = DebugPolicy(enabled=True, clients=frozenset({CLIENT}))
    assert decide(p, "info", CLIENT).outcome == "allow"
    for op in ("logcat", "shell", "forward"):
        assert decide(p, op, CLIENT).outcome == "ask", op


def test_ports_parse_and_refuse_bad_values():
    assert parse_ports("") == frozenset()
    assert parse_ports("8080, 5555;9") == frozenset({8080, 5555, 9})
    for bad in ("0", "65536", "http", "80 80x"):
        with pytest.raises(ValueError):
            parse_ports(bad)


def test_clients_parse_and_refuse_bad_ids():
    assert parse_clients("") == frozenset()
    assert parse_clients(CLIENT.upper()) == frozenset({CLIENT})
    with pytest.raises(ValueError):
        parse_clients("abc")
    with pytest.raises(ValueError):
        parse_clients("z" * 64)


def test_check_modes_names_a_bad_mode():
    assert check_modes({"shell": "allow"}) == []
    assert check_modes({"shell": "sometimes"})


# ----- agent and client, over channels ------------------------------------------------------------


class ListLogs:
    """A log source that yields the given lines, then ends."""

    def __init__(self, lines: List[str]) -> None:
        self.lines = lines

    def __call__(self, text_filter: str) -> AsyncIterator[str]:
        async def gen():
            needle = text_filter.lower()
            for line in self.lines:
                if not needle or needle in line.lower():
                    yield line

        return gen()


class NoLogs(SystemProviders):
    def log_lines(self, text_filter: str):
        return None


def test_info_reports_the_device_and_ends():
    async def scenario():
        connection, audit = make()
        result = await DebugClient(connection.open).info()
        await connection.close()
        return result, audit

    result, audit = run(scenario())
    assert result["node"] == "device"
    assert result["protocol"] == wire.PROTOCOL_VERSION
    assert any("info answered" in text for _, text in audit)


def test_logcat_streams_the_lines_that_match_the_filter():
    async def scenario():
        providers = SystemProviders(node_name="device", node_id="e" * 64, log_command="")
        providers.log_lines = ListLogs(["I tag: hello", "E tag: bad thing", "I other: fine"])  # type: ignore[method-assign]
        connection, _audit = make(providers)
        got: List[dict] = []
        await DebugClient(connection.open).logcat("tag", got.append)
        await connection.close()
        return got

    got = run(scenario())
    assert [m["text"] for m in got if m["t"] == "line"] == ["I tag: hello", "E tag: bad thing"]


def test_logcat_without_a_log_source_says_so():
    async def scenario():
        connection, _audit = make(NoLogs(node_name="device", node_id="e" * 64))
        try:
            await DebugClient(connection.open).logcat("", lambda m: None)
        except DebugError as exc:
            return exc
        finally:
            await connection.close()
        return None

    error = run(scenario())
    assert error is not None and error.code == "unavailable"


def test_logcat_drops_lines_the_client_does_not_read_and_says_how_many():
    async def scenario():
        providers = SystemProviders(node_name="device", node_id="e" * 64)
        many = [f"line {i}" for i in range(agent_mod.LOG_QUEUE + 300)]
        providers.log_lines = ListLogs(many)  # type: ignore[method-assign]
        connection, _audit = make(providers)
        got: List[dict] = []

        def slow(message: dict) -> None:
            got.append(message)

        await DebugClient(connection.open).logcat("", slow)
        await connection.close()
        return got

    got = run(scenario())
    lines = [m for m in got if m["t"] == "line"]
    drops = sum(m["count"] for m in got if m["t"] == "drop")
    assert len(lines) + drops == agent_mod.LOG_QUEUE + 300


def test_shell_runs_a_command_and_returns_its_output_and_code():
    async def scenario():
        connection, audit = make()
        command = f'"{PY}" -c "import sys; print(\'hello from the device\'); sys.stderr.write(\'warn\'); sys.exit(3)"'
        result = await DebugClient(connection.open).shell(command, 20.0)
        await connection.close()
        return result, audit

    result, audit = run(scenario())
    assert result["code"] == 3 and result["reason"] == "exited"
    assert b"hello from the device" in result["stdout"]
    assert b"warn" in result["stderr"]
    assert any(level == "warning" and "shell" in text for level, text in audit)


def test_shell_stops_a_command_that_runs_past_its_timeout():
    async def scenario():
        connection, _audit = make()
        command = f'"{PY}" -c "import time; time.sleep(60)"'
        result = await DebugClient(connection.open).shell(command, 1.0)
        await connection.close()
        return result

    result = run(scenario())
    assert result["reason"] == "timeout" and result["code"] is None


def test_shell_stops_at_the_output_limit():
    async def scenario():
        connection, _audit = make(shell_output_limit=4096)
        command = f'"{PY}" -c "import sys; sys.stdout.write(\'x\' * 200000)"'
        result = await DebugClient(connection.open).shell(command, 20.0)
        await connection.close()
        return result

    result = run(scenario())
    assert result["reason"] == "output_limit"
    assert len(result["stdout"]) <= 4096


def test_a_forward_reaches_a_port_on_the_device_only_when_it_is_listed():
    async def scenario():
        async def echo(reader, writer):
            data = await reader.read(100)
            writer.write(data.upper())
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(echo, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            connection, _audit = make(forward_ports=frozenset({port}))
            channel, rest = await DebugClient(connection.open).forward(port)
            await channel.write(b"hello")
            await channel.end()
            received = rest
            while True:
                chunk = await channel.read()
                if chunk is None:
                    break
                received += chunk
            refused = None
            try:
                await DebugClient(connection.open).forward(port + 1)
            except DebugError as exc:
                refused = exc.code
            await connection.close()
            return received, refused
        finally:
            server.close()
            await server.wait_closed()

    received, refused = run(scenario())
    assert received == b"HELLO"
    assert refused == "not_allowed"


def test_a_forward_with_nothing_listening_fails_cleanly():
    async def scenario():
        connection, _audit = make(forward_ports=frozenset({9}))
        try:
            await DebugClient(connection.open).forward(9)
        except DebugError as exc:
            return exc.code
        finally:
            await connection.close()
        return None

    assert run(scenario()) == "failed"


def test_an_unsupported_version_is_refused_with_its_code():
    async def scenario():
        connection, _audit = make()
        channel = await connection.open(wire.service_name("info"))
        await channel.write(b'{"v": 99, "args": {}}\n')
        message = wire.LineDecoder()
        got = None
        while got is None:
            chunk = await channel.read()
            assert chunk is not None
            messages = message.feed(chunk)
            if messages:
                got = messages[0]
        await connection.close()
        return got

    got = run(scenario())
    assert got["t"] == "error" and got["code"] == "unsupported_version"


def test_a_stream_with_no_request_is_refused_as_bad_request():
    async def scenario():
        connection, _audit = make()
        channel = await connection.open(wire.service_name("shell"))
        await channel.end()
        decoder = wire.LineDecoder()
        got = None
        while got is None:
            chunk = await channel.read()
            if chunk is None:
                break
            messages = decoder.feed(chunk)
            if messages:
                got = messages[0]
        await connection.close()
        return got

    got = run(scenario())
    assert got is not None and got["code"] == "bad_request"


def test_too_many_streams_from_one_computer_are_refused():
    async def scenario():
        connection, _audit = make(streams_per_client=1)
        agent = connection.agent
        agent._active[CLIENT] = 1  # one stream already open
        try:
            await DebugClient(connection.open).info()
        except DebugError as exc:
            return exc.code
        finally:
            agent._active.pop(CLIENT, None)
            await connection.close()
        return None

    assert run(scenario()) == "too_many"


def test_bridge_carries_a_local_tcp_connection_over_a_forward():
    async def scenario():
        async def echo(reader, writer):
            while True:
                data = await reader.read(100)
                if not data:
                    break
                writer.write(data)
                await writer.drain()
            writer.close()

        device = await asyncio.start_server(echo, "127.0.0.1", 0)
        port = device.sockets[0].getsockname()[1]
        local_server = None
        try:
            connection, _audit = make(forward_ports=frozenset({port}))
            client = DebugClient(connection.open)
            done = asyncio.get_running_loop().create_future()

            async def accept(reader, writer):
                channel, rest = await client.forward(port)
                await bridge(reader, writer, channel, rest)
                done.set_result(True)

            local_server = await asyncio.start_server(accept, "127.0.0.1", 0)
            local_port = local_server.sockets[0].getsockname()[1]
            reader, writer = await asyncio.open_connection("127.0.0.1", local_port)
            writer.write(b"ping through the forward")
            await writer.drain()
            echoed = await asyncio.wait_for(reader.read(100), 10)
            writer.write_eof()
            await asyncio.wait_for(done, 10)
            writer.close()
            await connection.close()
            return echoed
        finally:
            device.close()
            await device.wait_closed()
            if local_server is not None:
                local_server.close()
                await local_server.wait_closed()

    assert run(scenario()) == b"ping through the forward"
