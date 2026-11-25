"""Telemetry: bounded time series of values the library measured, sampled from snapshots.

Every series is a ring buffer of `(time, value)` pairs. A buffer holds at most `window` samples, so a long run uses a
fixed amount of memory. A missing value is never recorded: the series simply has no point for that moment, and the
charts show a gap or "no data yet".
"""

from __future__ import annotations

import math
from collections import deque
from typing import Deque, Dict, Iterable, List, Optional, Sequence, Tuple

from .snapshot import RouteSnapshot

Point = Tuple[float, float]
#: A series is named by its route key and what it measures, for example ("peer/3", "rtt_ms").
SeriesKey = Tuple[str, str]

RTT = "rtt_ms"
LOSS = "loss"
LIBRARY_MBPS = "library_mbps"
SENT_MBPS = "sent_mbps"
RECEIVED_MBPS = "received_mbps"
METRICS = (RTT, LOSS, LIBRARY_MBPS, SENT_MBPS, RECEIVED_MBPS)


class RingBuffer:
    """The newest `capacity` points of a series, oldest dropped first."""

    def __init__(self, capacity: int) -> None:
        if capacity < 1:
            raise ValueError("a ring buffer holds at least one point")
        self._points: Deque[Point] = deque(maxlen=capacity)

    @property
    def capacity(self) -> int:
        return self._points.maxlen or 0

    def __len__(self) -> int:
        return len(self._points)

    def append(self, when: float, value: float) -> None:
        self._points.append((when, value))

    def points(self) -> List[Point]:
        return list(self._points)

    def latest(self) -> Optional[float]:
        return self._points[-1][1] if self._points else None

    def resize(self, capacity: int) -> None:
        self._points = deque(self._points, maxlen=capacity)


class RateTracker:
    """Turns a growing byte counter into a rate. The first sample gives no rate: there is nothing to compare with."""

    def __init__(self) -> None:
        self._last: Optional[Tuple[float, int]] = None

    def observe(self, when: float, total: int) -> Optional[float]:
        """Bytes per second since the previous sample, or None when it cannot be known (first sample, no time passed,
        or a counter that went backwards)."""
        previous, self._last = self._last, (when, total)
        if previous is None:
            return None
        last_when, last_total = previous
        elapsed = when - last_when
        if elapsed <= 0 or total < last_total:
            return None
        return (total - last_total) / elapsed


class TelemetryStore:
    """All the series the dashboard charts, keyed by `(route key, metric)`."""

    def __init__(self, window: int) -> None:
        self._window = window
        self._series: Dict[SeriesKey, RingBuffer] = {}
        self._rates: Dict[Tuple[str, str], RateTracker] = {}

    @property
    def window(self) -> int:
        return self._window

    def record(self, key: SeriesKey, when: float, value: Optional[float]) -> None:
        """Add a point. None and non-finite values are not recorded."""
        if value is None or not math.isfinite(value):
            return
        buffer = self._series.get(key)
        if buffer is None:
            buffer = self._series[key] = RingBuffer(self._window)
        buffer.append(when, value)

    def points(self, key: SeriesKey) -> List[Point]:
        buffer = self._series.get(key)
        return buffer.points() if buffer is not None else []

    def latest(self, key: SeriesKey) -> Optional[float]:
        buffer = self._series.get(key)
        return buffer.latest() if buffer is not None else None

    def keys(self) -> List[SeriesKey]:
        return list(self._series)

    def rate(self, route: str, counter: str, when: float, total: int) -> Optional[float]:
        """The bytes per second of one counter on one route, from successive totals."""
        tracker = self._rates.get((route, counter))
        if tracker is None:
            tracker = self._rates[(route, counter)] = RateTracker()
        return tracker.observe(when, total)

    def retain_routes(self, keep: Iterable[str]) -> None:
        """Forget the series and rate trackers of routes that no longer exist, so a long run does not grow forever."""
        kept = set(keep)
        for key in [k for k in self._series if k[0] not in kept]:
            del self._series[key]
        for key in [k for k in self._rates if k[0] not in kept]:
            del self._rates[key]

    def resize(self, window: int) -> None:
        self._window = window
        for buffer in self._series.values():
            buffer.resize(window)


def record_routes(store: TelemetryStore, when: float, routes: Sequence[RouteSnapshot]) -> None:
    """Sample every route of a snapshot into the store. Down routes record nothing: a down route has no current value."""
    for route in routes:
        if not route.up:
            continue
        sent = store.rate(route.key, "sent", when, route.sent_bytes)
        received = store.rate(route.key, "received", when, route.received_bytes)
        store.record((route.key, RTT), when, route.rtt_ms)
        store.record((route.key, LOSS), when, route.loss)
        store.record((route.key, LIBRARY_MBPS), when, route.mbps)
        store.record((route.key, SENT_MBPS), when, _mbps(sent))
        store.record((route.key, RECEIVED_MBPS), when, _mbps(received))
    store.retain_routes(route.key for route in routes)


def _mbps(bytes_per_second: Optional[float]) -> Optional[float]:
    return None if bytes_per_second is None else bytes_per_second * 8.0 / 1_000_000.0
