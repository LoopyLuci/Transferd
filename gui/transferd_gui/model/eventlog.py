"""The event log: a bounded, newest-last buffer of what the node did and what went wrong.

Each entry has a level (`info`, `warning` or `error`), the part of the app that wrote it, and a message. When the buffer
is full the oldest entries go. Thread safety is the caller's job: the backend and the views share one instance through
a lock held by `EventLogStore` (see below).
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, List, Optional, Tuple

LEVELS: Tuple[str, ...] = ("info", "warning", "error")


@dataclass(frozen=True)
class LogEntry:
    when: float
    level: str
    source: str
    message: str


class EventLog:
    """A bounded buffer of `LogEntry`. Adding past capacity drops the oldest entry."""

    def __init__(self, capacity: int) -> None:
        if capacity < 1:
            raise ValueError("an event log holds at least one entry")
        self._entries: Deque[LogEntry] = deque(maxlen=capacity)

    @property
    def capacity(self) -> int:
        return self._entries.maxlen or 0

    def __len__(self) -> int:
        return len(self._entries)

    def add(self, level: str, source: str, message: str, when: Optional[float] = None) -> LogEntry:
        if level not in LEVELS:
            raise ValueError(f"a log level is one of {', '.join(LEVELS)}, not {level!r}")
        entry = LogEntry(time.time() if when is None else when, level, source, message)
        self._entries.append(entry)
        return entry

    def entries(self, level: Optional[str] = None) -> List[LogEntry]:
        """All entries, oldest first, optionally only those at one level or above it."""
        if level is None:
            return list(self._entries)
        rank = LEVELS.index(level)
        return [e for e in self._entries if LEVELS.index(e.level) >= rank]

    def resize(self, capacity: int) -> None:
        """Change the capacity, keeping the newest entries that still fit."""
        if capacity < 1:
            raise ValueError("an event log holds at least one entry")
        self._entries = deque(self._entries, maxlen=capacity)

    def clear(self) -> None:
        self._entries.clear()


class EventLogStore:
    """An `EventLog` behind a lock, for the backend thread to write and the UI thread to read."""

    def __init__(self, capacity: int) -> None:
        self._log = EventLog(capacity)
        self._lock = threading.Lock()

    def add(self, level: str, source: str, message: str) -> LogEntry:
        with self._lock:
            return self._log.add(level, source, message)

    def entries(self, level: Optional[str] = None) -> List[LogEntry]:
        with self._lock:
            return self._log.entries(level)

    def resize(self, capacity: int) -> None:
        with self._lock:
            self._log.resize(capacity)

    def clear(self) -> None:
        with self._lock:
            self._log.clear()
