"""The backend thread: one asyncio event loop that owns every library object, run in a daemon thread.

The UI never touches a library object. It hands coroutines to `submit`, gets a concurrent future back, and reads the
snapshots the backend publishes. Anything passed to `call_soon` runs on the backend thread, so callbacks the backend
calls must be thread-safe; the Qt bridge is, because emitting a signal is.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from typing import Any, Callable, Coroutine, Optional


class BackendWorker:
    """Runs one asyncio loop in a background thread until `stop`."""

    def __init__(self, name: str = "transferd-backend") -> None:
        self._name = name
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()

    @property
    def running(self) -> bool:
        return self._loop is not None and self._loop.is_running()

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("the backend thread already started")
        self._thread = threading.Thread(target=self._run, name=self._name, daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=10.0):
            raise RuntimeError("the backend thread did not start")

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            _cancel_pending(loop)
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()

    def submit(self, coro: Coroutine[Any, Any, Any]) -> concurrent.futures.Future:
        """Run `coro` on the backend loop. The returned future resolves with its result or exception."""
        if not self.running:
            coro.close()
            raise RuntimeError("the backend is not running")
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def call_soon(self, fn: Callable[..., None], *args: Any) -> None:
        """Run a plain function on the backend thread, soon."""
        if not self.running:
            raise RuntimeError("the backend is not running")
        self._loop.call_soon_threadsafe(fn, *args)

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the loop and join its thread. Safe to call twice."""
        loop = self._loop
        if loop is None or not loop.is_running():
            return
        loop.call_soon_threadsafe(loop.stop)
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout)


def _cancel_pending(loop: asyncio.AbstractEventLoop) -> None:
    pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
    for task in pending:
        task.cancel()
    if pending:
        loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
