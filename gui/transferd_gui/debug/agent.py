"""The device side of the debug protocol: answers `info`, `logcat`, `shell` and `forward` streams under the device's policy.

The agent does not decide who is asked about; the node does, when a stream opens (`decide_open`). Once a stream is
accepted, `serve` reads the request, checks it against the policy again, and runs the operation. Everything it does is
reported through `audit`, so the device's event log shows every stream and how it ended.

What the device can read, run and reach comes from a `Providers` object. `providers.SystemProviders` is the real one;
tests give their own.
"""

from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator, Callable, Dict, Optional, Protocol, Tuple

from transferd_gui.debug.policy import Decision, DebugPolicy, decide
from transferd_gui.debug.stream import Channel, ChannelReset
from transferd_gui.debug.wire import (
    MAX_REQUEST,
    MAX_SHELL_COMMAND,
    MAX_SHELL_TIMEOUT_S,
    PROTOCOL_VERSION,
    LineDecoder,
    WireError,
    b64,
    clip_text,
    encode,
)

#: Bytes per shell output message.
OUTPUT_CHUNK = 16 * 1024
#: Bytes read from a forwarded connection at a time.
FORWARD_CHUNK = 64 * 1024
#: Log lines held while the client reads more slowly than the log produces them.
LOG_QUEUE = 1024
#: The longest log filter text a client may send.
MAX_FILTER = 256
#: How often a logcat stream reports the lines it dropped.
DROP_REPORT_S = 0.5

Audit = Callable[[str, str], None]


class ShellRun(Protocol):
    """A running shell command: its output as (fd, bytes), its exit code, and a way to stop it."""

    def output(self) -> AsyncIterator[Tuple[int, bytes]]: ...

    async def wait(self) -> Optional[int]: ...

    def kill(self) -> None: ...

    async def close(self) -> None:
        """Stop the command if it is still running, and wait until it has gone."""


class Providers(Protocol):
    """What the device gives the agent. Each method can be replaced in tests."""

    def info(self) -> Dict[str, Any]:
        """Device properties for the `info` operation."""

    def log_lines(self, text_filter: str) -> Optional[AsyncIterator[str]]:
        """Log lines that contain `text_filter`, or None when the device has no log source configured."""

    def run_shell(self, command: str) -> ShellRun:
        """Start a shell command. Nothing runs until its `output` is iterated."""

    async def open_port(self, port: int) -> Tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """Connect to a port on this device's loopback. Raises OSError when nothing listens there."""


class _Refused(Exception):
    """The request cannot be served. The agent answers with this code and text before the operation starts."""

    def __init__(self, code: str, text: str) -> None:
        super().__init__(text)
        self.code = code
        self.text = text


def _quiet(_level: str, _text: str) -> None:
    return None


class DebugAgent:
    """Serves debug streams under the policy `policy()` returns now (the user may change it while the agent runs)."""

    def __init__(
        self,
        policy: Callable[[], DebugPolicy],
        providers: Providers,
        audit: Audit = _quiet,
    ) -> None:
        self._policy = policy
        self._providers = providers
        self._audit = audit
        self._active: Dict[str, int] = {}

    def decide_open(self, op: str, client_node_id: str) -> Decision:
        """Allow, ask or refuse a stream the client has just opened, under the policy now in force."""
        return decide(self._policy(), op, client_node_id)

    def active_streams(self, client_node_id: str) -> int:
        return self._active.get(client_node_id, 0)

    async def serve(self, op: str, client_node_id: str, channel: Channel) -> None:
        """Run one accepted stream to its end, then release it. Never raises: every failure is answered on the stream and audited."""
        try:
            await self._serve(op, client_node_id, channel)
        finally:
            try:
                await channel.close()
            except Exception:
                pass  # released already, or the session is gone

    async def _serve(self, op: str, client_node_id: str, channel: Channel) -> None:
        who = _short(client_node_id)
        policy = self._policy()
        decision = decide(policy, op, client_node_id)
        if decision.outcome == "refuse":
            await self._answer_error(channel, decision.code, decision.reason)
            self._audit("warning", f"debug {op} from {who} refused: {decision.reason}")
            return
        if self._active.get(client_node_id, 0) >= policy.streams_per_client:
            await self._answer_error(channel, "too_many", "this computer already has the most debug streams open")
            self._audit("warning", f"debug {op} from {who} refused: too many open streams")
            return
        self._active[client_node_id] = self._active.get(client_node_id, 0) + 1
        try:
            await self._serve_open(op, who, client_node_id, channel, policy)
        except ChannelReset:
            self._audit("info", f"debug {op} from {who} ended: the computer left the stream")
        except Exception as exc:  # the operation failed in a way the agent did not expect; the stream says so
            self._audit("error", f"debug {op} from {who} failed: {exc}")
            await _reset_quietly(channel, "the device failed the operation")
        finally:
            count = self._active.get(client_node_id, 1) - 1
            if count > 0:
                self._active[client_node_id] = count
            else:
                self._active.pop(client_node_id, None)

    async def _serve_open(self, op: str, who: str, client_id: str, channel: Channel, policy: DebugPolicy) -> None:
        decoder = LineDecoder(MAX_REQUEST)
        try:
            args = await _read_request(channel, decoder)
        except _Refused as refused:
            await self._answer_error(channel, refused.code, refused.text)
            self._audit("warning", f"debug {op} from {who} refused: {refused.text}")
            return
        try:
            if op == "info":
                await self._info(channel)
            elif op == "logcat":
                await self._logcat(channel, args, who)
            elif op == "shell":
                await self._shell(channel, args, who, policy)
            elif op == "forward":
                await self._forward(channel, args, decoder, who, policy)
            else:
                raise _Refused("bad_request", f"there is no operation '{op}'")
        except _Refused as refused:
            await self._answer_error(channel, refused.code, refused.text)
            self._audit("warning", f"debug {op} from {who} refused: {refused.text}")

    # ----- operations ---------------------------------------------------------------------------

    async def _info(self, channel: Channel) -> None:
        await channel.write(encode({"t": "ok"}))
        await channel.write(encode({"t": "info", "props": self._providers.info()}, _LIMIT))
        await channel.end()
        self._audit("info", "debug info answered")

    async def _logcat(self, channel: Channel, args: Dict[str, Any], who: str) -> None:
        text_filter = _text_arg(args, "filter", "", MAX_FILTER)
        lines = self._providers.log_lines(text_filter)
        if lines is None:
            raise _Refused("unavailable", "this device has no log source configured")
        await channel.write(encode({"t": "ok"}))
        self._audit("info", f"debug logcat from {who} started" + (f" (filter '{text_filter}')" if text_filter else ""))

        queue: asyncio.Queue = asyncio.Queue(maxsize=LOG_QUEUE)
        counts = {"dropped": 0, "reported": 0}
        stop = asyncio.Event()

        async def produce() -> None:
            try:
                async for line in lines:
                    try:
                        queue.put_nowait(line)
                    except asyncio.QueueFull:
                        counts["dropped"] += 1
            finally:
                stop.set()

        async def watch() -> None:
            # The client sends nothing after its request; its end (or a reset) is the only thing to watch for.
            try:
                while await channel.read() is not None:
                    pass
            except ChannelReset:
                pass
            stop.set()

        producer = asyncio.create_task(produce())
        watcher = asyncio.create_task(watch())
        try:
            while True:
                if counts["dropped"] > counts["reported"]:
                    count = counts["dropped"] - counts["reported"]
                    counts["reported"] = counts["dropped"]
                    await channel.write(encode({"t": "drop", "count": count}))
                if stop.is_set() and queue.empty():
                    break
                try:
                    line = await asyncio.wait_for(queue.get(), DROP_REPORT_S)
                except asyncio.TimeoutError:
                    continue
                text, cut = clip_text(line)
                await channel.write(encode({"t": "line", "text": text, "cut": cut}, _LIMIT))
        finally:
            for task in (producer, watcher):
                task.cancel()
            await asyncio.gather(producer, watcher, return_exceptions=True)
            aclose = getattr(lines, "aclose", None)
            if aclose is not None:
                await aclose()
        await channel.end()
        self._audit("info", f"debug logcat from {who} ended")

    async def _shell(self, channel: Channel, args: Dict[str, Any], who: str, policy: DebugPolicy) -> None:
        command = _text_arg(args, "command", "", MAX_SHELL_COMMAND)
        if not command.strip():
            raise _Refused("bad_request", "the shell command is empty")
        limit = min(policy.shell_timeout_s, MAX_SHELL_TIMEOUT_S)
        requested = args.get("timeout", limit)
        if isinstance(requested, bool) or not isinstance(requested, (int, float)) or requested <= 0:
            raise _Refused("bad_request", "the shell timeout must be a positive number of seconds")
        timeout = min(float(requested), limit)
        run = self._providers.run_shell(command)
        await channel.write(encode({"t": "ok"}))
        self._audit("warning", f"debug shell from {who} started: {command[:200]}")

        reason = "exited"
        sent = 0

        async def pump() -> None:
            nonlocal reason, sent
            output = run.output()
            try:
                async for fd, data in output:
                    for start in range(0, len(data), OUTPUT_CHUNK):
                        piece = data[start : start + OUTPUT_CHUNK]
                        if sent + len(piece) > policy.shell_output_limit:
                            reason = "output_limit"
                            run.kill()
                            return
                        sent += len(piece)
                        await channel.write(encode({"t": "out", "fd": fd, "b64": b64(piece)}, _LIMIT))
            finally:
                aclose = getattr(output, "aclose", None)
                if aclose is not None:
                    await aclose()

        async def watch() -> None:
            try:
                while await channel.read() is not None:
                    pass
            except ChannelReset:
                pass
            run.kill()

        watcher = asyncio.create_task(watch())
        code: Optional[int] = None
        try:
            await asyncio.wait_for(pump(), timeout)
            code = await run.wait()
        except asyncio.TimeoutError:
            reason = "timeout"
        finally:
            # Whatever ended the run (timeout, limit, the computer leaving, a failure), nothing is left running.
            await run.close()
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
        await channel.write(encode({"t": "exit", "code": code, "reason": reason}))
        await channel.end()
        self._audit("warning", f"debug shell from {who} ended: {reason}" + (f", exit {code}" if code is not None else ""))

    async def _forward(self, channel: Channel, args: Dict[str, Any], decoder: LineDecoder, who: str, policy: DebugPolicy) -> None:
        port = args.get("port")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise _Refused("bad_request", "the forward port must be a number from 1 to 65535")
        if port not in policy.forward_ports:
            raise _Refused("not_allowed", f"port {port} is not on this device's forward list")
        try:
            reader, writer = await self._providers.open_port(port)
        except OSError as exc:
            raise _Refused("failed", f"nothing is listening on port {port} on this device ({exc.strerror or exc})") from exc
        await channel.write(encode({"t": "ok"}))
        self._audit("warning", f"debug forward from {who} to port {port} started")
        pending = decoder.rest()
        if pending:
            writer.write(pending)

        async def to_device() -> None:
            try:
                while True:
                    data = await channel.read()
                    if data is None:
                        break
                    writer.write(data)
                    await writer.drain()
            except (ChannelReset, ConnectionError):
                writer.transport.abort()
                return
            if writer.can_write_eof():
                writer.write_eof()

        async def to_client() -> None:
            while True:
                data = await reader.read(FORWARD_CHUNK)
                if not data:
                    break
                await channel.write(data)
            await channel.end()

        try:
            await asyncio.gather(to_device(), to_client())
        finally:
            writer.close()
            await asyncio.gather(writer.wait_closed(), return_exceptions=True)
        self._audit("warning", f"debug forward from {who} to port {port} ended")

    # ----- answers ------------------------------------------------------------------------------

    async def _answer_error(self, channel: Channel, code: str, text: str) -> None:
        try:
            await channel.write(encode({"t": "error", "code": code, "text": text}))
            await channel.end()
        except ChannelReset:
            pass


def _text_arg(args: Dict[str, Any], key: str, default: str, limit: int) -> str:
    value = args.get(key, default)
    if not isinstance(value, str):
        raise _Refused("bad_request", f"'{key}' must be text")
    if len(value) > limit:
        raise _Refused("bad_request", f"'{key}' is longer than {limit} characters")
    return value


async def _read_request(channel: Channel, decoder: LineDecoder) -> Dict[str, Any]:
    """The request line, and its `args`. Bytes after the line stay in `decoder` for a raw stream."""
    while True:
        try:
            message = decoder.take()
        except WireError as exc:
            raise _Refused("bad_request", str(exc)) from exc
        if message is not None:
            break
        chunk = await channel.read()
        if chunk is None:
            raise _Refused("bad_request", "the stream ended before the request")
        decoder.push(chunk)
    if message.get("v") != PROTOCOL_VERSION:
        raise _Refused("unsupported_version", f"this device speaks debug protocol version {PROTOCOL_VERSION}")
    args = message.get("args", {})
    if not isinstance(args, dict):
        raise _Refused("bad_request", "'args' must be an object")
    return args


async def _reset_quietly(channel: Channel, reason: str) -> None:
    try:
        await channel.reset(reason)
    except ChannelReset:
        pass


def _short(node_id: str) -> str:
    return node_id[:12]


_LIMIT = 64 * 1024
