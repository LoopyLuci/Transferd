"""The debugging computer's side: `info`, `logcat`, `shell` and `forward` against a device, over channels.

`open_channel(service)` is how a client gets a channel: the node opens the overlay stream for `service` and returns a
channel once the device has accepted it. A refusal raises `DebugError`, with the device's code and text, so the
caller can say what happened in plain words.
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

from transferd_gui.debug.stream import Channel, ChannelReset
from transferd_gui.debug.wire import LineDecoder, WireError, request, service_name, unb64

OpenChannel = Callable[[str], Awaitable[Channel]]
#: Called with each message the device sends for a logcat stream: `line` or `drop`.
OnLog = Callable[[Dict[str, Any]], None]
#: Called with each shell output piece as (fd, bytes).
OnOutput = Callable[[int, bytes], None]
READ_SIZE = 64 * 1024


class DebugError(Exception):
    """The device refused the operation, failed it, or broke the protocol. `code` is one of the protocol's error codes."""

    def __init__(self, code: str, text: str) -> None:
        super().__init__(text)
        self.code = code
        self.text = text


class DebugClient:
    """The operations a debugging computer runs against one device."""

    def __init__(self, open_channel: OpenChannel) -> None:
        self._open_channel = open_channel

    async def info(self) -> Dict[str, Any]:
        channel = await self._start("info", {})
        try:
            decoder = LineDecoder()
            await _expect_ok(channel, decoder)
            while True:
                message = await _next(channel, decoder)
                if message is None:
                    raise DebugError("failed", "the device ended the stream before its information")
                if message.get("t") == "info":
                    props = message.get("props")
                    return dict(props) if isinstance(props, dict) else {}
                if message.get("t") == "error":
                    raise _device_error(message)
        finally:
            await channel.close()

    async def logcat(self, text_filter: str, on_log: OnLog) -> None:
        """Stream log lines until the device ends the stream. Cancel the task to stop: the stream is ended on the device."""
        channel = await self._start("logcat", {"filter": text_filter} if text_filter else {})
        decoder = LineDecoder()
        try:
            await _expect_ok(channel, decoder)
            while True:
                message = await _next(channel, decoder)
                if message is None:
                    return
                kind = message.get("t")
                if kind in ("line", "drop"):
                    on_log(message)
                elif kind == "error":
                    raise _device_error(message)
        finally:
            await channel.close()

    async def shell(self, command: str, timeout: float, on_output: Optional[OnOutput] = None) -> Dict[str, Any]:
        """Run a command on the device. Returns its exit code, why it ended, and what it printed (stdout and stderr)."""
        channel = await self._start("shell", {"command": command, "timeout": timeout})
        decoder = LineDecoder()
        out = {1: bytearray(), 2: bytearray()}
        result: Dict[str, Any] = {"code": None, "reason": ""}
        try:
            await _expect_ok(channel, decoder)
            while True:
                message = await _next(channel, decoder)
                if message is None:
                    raise DebugError("failed", "the device ended the stream before the command finished")
                kind = message.get("t")
                if kind == "out":
                    fd = message.get("fd")
                    data = unb64(str(message.get("b64", "")))
                    if fd in out:
                        out[fd] += data
                    if on_output is not None:
                        on_output(fd if fd in out else 1, data)
                elif kind == "exit":
                    code = message.get("code")
                    result["code"] = code if isinstance(code, int) else None
                    result["reason"] = str(message.get("reason", ""))
                    break
                elif kind == "error":
                    raise _device_error(message)
        finally:
            await channel.close()
        result["stdout"] = bytes(out[1])
        result["stderr"] = bytes(out[2])
        return result

    async def forward(self, port: int) -> Tuple[Channel, bytes]:
        """Open a raw connection to `port` on the device's loopback. Returns the channel, which carries bytes both ways,
        and any bytes the device sent in the same read as its answer: they belong to the connection."""
        channel = await self._start("forward", {"port": port})
        decoder = LineDecoder()
        try:
            await _expect_ok(channel, decoder)
        except BaseException:
            await channel.reset("the forward did not start")
            raise
        return channel, decoder.rest()

    async def _start(self, op: str, args: Dict[str, Any]) -> Channel:
        channel = await self._open_channel(service_name(op))
        try:
            await channel.write(request(args))
        except BaseException:
            await channel.reset("could not send the request")
            raise
        return channel


async def bridge(local_reader: asyncio.StreamReader, local_writer: asyncio.StreamWriter, channel: Channel, rest: bytes = b"") -> None:
    """Carry bytes between a local TCP connection and a forward channel until both have ended."""

    async def up() -> None:
        try:
            if rest:
                local_writer.write(rest)
                await local_writer.drain()
            while True:
                data = await local_reader.read(READ_SIZE)
                if not data:
                    break
                await channel.write(data)
        except (ChannelReset, ConnectionError):
            await channel.reset("the local connection failed")
            return
        await channel.end()

    async def down() -> None:
        try:
            while True:
                data = await channel.read()
                if data is None:
                    break
                local_writer.write(data)
                await local_writer.drain()
        except (ChannelReset, ConnectionError):
            local_writer.transport.abort()
            return
        if local_writer.can_write_eof():
            local_writer.write_eof()

    try:
        await asyncio.gather(up(), down())
    finally:
        await channel.close()
        local_writer.close()
        await asyncio.gather(local_writer.wait_closed(), return_exceptions=True)


async def _next(channel: Channel, decoder: LineDecoder) -> Optional[Dict[str, Any]]:
    """The next message from the device, or None when the stream has ended."""
    while True:
        try:
            message = decoder.take()
        except WireError as exc:
            raise DebugError("bad_request", f"the device sent something that is not the protocol: {exc}") from exc
        if message is not None:
            return message
        try:
            chunk = await channel.read()
        except ChannelReset as exc:
            raise DebugError("failed", f"the device reset the stream: {exc}") from exc
        if chunk is None:
            return None
        decoder.push(chunk)


async def _expect_ok(channel: Channel, decoder: LineDecoder) -> None:
    """The device's first message must be `ok`. Anything else is its refusal, or a protocol break."""
    message = await _next(channel, decoder)
    if message is None:
        raise DebugError("failed", "the device ended the stream without answering")
    if message.get("t") == "ok":
        return
    if message.get("t") == "error":
        raise _device_error(message)
    raise DebugError("bad_request", f"the device answered with '{message.get('t')}' instead of ok")


def _device_error(message: Dict[str, Any]) -> DebugError:
    code = str(message.get("code", "failed"))
    text = str(message.get("text", "")) or "the device refused the operation"
    return DebugError(code, text)
