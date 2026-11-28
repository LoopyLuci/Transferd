"""The device's real providers: its log command, its shell, its properties, and its loopback ports.

`log_command` is set by the user in Settings. On Android it is typically `logcat -v time`; on a computer it can be any
command that writes lines. Nothing is read from the device unless the user set it.
"""

from __future__ import annotations

import asyncio
import os
import platform
import signal
import subprocess
import sys
from typing import Any, AsyncIterator, Dict, Optional, Tuple

from transferd_gui.debug.wire import PROTOCOL_VERSION

#: The longest single line a log command may print. Longer lines end the reader, so the limit is generous.
LOG_LINE_LIMIT = 1 << 20
#: Bytes read from a process pipe at a time.
PIPE_CHUNK = 16 * 1024


class SystemProviders:
    """The providers a real device uses. `node_name` and `node_id` describe the device in `info`."""

    def __init__(self, *, node_name: str, node_id: str, log_command: str = "") -> None:
        self._node_name = node_name
        self._node_id = node_id
        self._log_command = log_command.strip()

    def info(self) -> Dict[str, Any]:
        return {
            "node": self._node_name,
            "node_id": self._node_id,
            "system": f"{platform.system()} {platform.release()}".strip(),
            "machine": platform.machine(),
            "python": platform.python_version(),
            "platform": sys.platform,
            "protocol": PROTOCOL_VERSION,
            "log_source": "configured" if self._log_command else "not configured",
        }

    def log_lines(self, text_filter: str) -> Optional[AsyncIterator[str]]:
        if not self._log_command:
            return None
        return _log_lines(self._log_command, text_filter)

    def run_shell(self, command: str) -> "_ProcessRun":
        return _ProcessRun(command)

    async def open_port(self, port: int) -> Tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        return await asyncio.open_connection("127.0.0.1", port)


def _kill_tree(process: asyncio.subprocess.Process) -> None:
    """Kill a shell command and everything it started. Killing only the shell leaves its children holding the output
    pipes open, and the pipes are what says the command has finished."""
    if process.returncode is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        process.kill()


async def _log_lines(command: str, text_filter: str) -> AsyncIterator[str]:
    """The lines `command` prints that contain `text_filter` (case-insensitive). The command is stopped when the
    iterator is closed or dropped."""
    process = await _spawn(command, stderr=asyncio.subprocess.STDOUT)
    needle = text_filter.lower()
    try:
        assert process.stdout is not None
        while True:
            raw = await process.stdout.readline()
            if not raw:
                break
            text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if needle and needle not in text.lower():
                continue
            yield text
    finally:
        _kill_tree(process)
        await process.wait()


async def _spawn(command: str, stderr: int) -> asyncio.subprocess.Process:
    """Start `command` through the platform's shell, in its own process group where the platform has one."""
    return await asyncio.create_subprocess_shell(
        command,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=stderr,
        limit=LOG_LINE_LIMIT,
        start_new_session=os.name != "nt",
    )


class _ProcessRun:
    """A shell command run through the platform's shell. Its output is read as it comes, from both pipes."""

    def __init__(self, command: str) -> None:
        self._command = command
        self._process: Optional[asyncio.subprocess.Process] = None

    async def _start(self) -> asyncio.subprocess.Process:
        if self._process is None:
            self._process = await _spawn(self._command, stderr=asyncio.subprocess.PIPE)
        return self._process

    async def output(self) -> AsyncIterator[Tuple[int, bytes]]:
        process = await self._start()
        assert process.stdout is not None and process.stderr is not None
        queue: asyncio.Queue = asyncio.Queue()

        async def pipe(fd: int, stream: asyncio.StreamReader) -> None:
            try:
                while True:
                    data = await stream.read(PIPE_CHUNK)
                    if not data:
                        break
                    await queue.put((fd, data))
            finally:
                await queue.put(None)

        tasks = [asyncio.create_task(pipe(1, process.stdout)), asyncio.create_task(pipe(2, process.stderr))]
        open_pipes = 2
        try:
            while open_pipes:
                item = await queue.get()
                if item is None:
                    open_pipes -= 1
                    continue
                yield item
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def wait(self) -> Optional[int]:
        process = await self._start()
        return await process.wait()

    def kill(self) -> None:
        if self._process is not None:
            _kill_tree(self._process)

    async def close(self) -> None:
        if self._process is None:
            return
        self.kill()
        await self._process.wait()
