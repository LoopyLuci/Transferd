"""The bridge from the backend thread to the Qt thread.

The backend calls plain callbacks from its own thread. The bridge turns each call into a signal, and a signal emitted from
another thread is delivered on the thread that owns the receiving object: here, the UI thread. `on_ui` wraps a function so
that a backend callback can run it on the UI thread, which is how an action's result reaches a widget.
"""

from __future__ import annotations

import functools
from typing import Any, Callable

from PySide6.QtCore import QObject, Signal


class Bridge(QObject):
    """Signals for everything the backend reports, and a way to run a function on the UI thread."""

    #: A NodeSnapshot, about once a second.
    snapshot = Signal(object)
    #: A LogEntry, as it is written.
    logged = Signal(object)
    _invoke = Signal(object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._invoke.connect(self._run)

    def _run(self, call: Callable[[], None]) -> None:
        call()

    def on_ui(self, fn: Callable[..., None]) -> Callable[..., None]:
        """A callback that may be called from any thread and runs `fn(*args)` on the UI thread."""

        def callback(*args: Any) -> None:
            self._invoke.emit(functools.partial(fn, *args))

        return callback

    def snapshot_callback(self, snapshot: Any) -> None:
        self.snapshot.emit(snapshot)

    def log_callback(self, entry: Any) -> None:
        self.logged.emit(entry)
