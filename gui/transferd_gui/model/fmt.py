"""Formatting: turning measured values into short text for the views. Missing values always read "no data yet"."""

from __future__ import annotations

import time
from typing import Callable, Optional

NO_DATA = "no data yet"


def or_no_data(value: Optional[float], render: Callable[[float], str]) -> str:
    """`render(value)`, or the "no data yet" text when there is no value."""
    if value is None:
        return NO_DATA
    return render(value)


def human_bytes(count: int) -> str:
    """A byte count in binary units, for example 1.5 MiB."""
    if count < 1024:
        return f"{count} B"
    size = float(count)
    for unit in ("KiB", "MiB", "GiB", "TiB"):
        size /= 1024.0
        if size < 1024.0 or unit == "TiB":
            return f"{size:.1f} {unit}"
    return f"{count} B"


def human_mbps(mbps: float) -> str:
    return f"{mbps:.2f} Mbit/s"


def human_ms(ms: float) -> str:
    return f"{ms:.1f} ms"


def human_percent(fraction: float) -> str:
    return f"{fraction * 100.0:.1f} %"


def human_age(seconds: float) -> str:
    """A duration as "5 s", "3 min 10 s" or "2 h 5 min"."""
    seconds = max(0, int(round(seconds)))
    if seconds < 60:
        return f"{seconds} s"
    minutes, rest = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} min {rest} s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min"


def clock(when: float) -> str:
    """Local time of day, to the second."""
    return time.strftime("%H:%M:%S", time.localtime(when))


def short_hex(text: str, head: int = 10, tail: int = 6) -> str:
    """A long hex string shortened for display: the first and last characters with an ellipsis between. The full value
    stays available for copying."""
    if len(text) <= head + tail + 1:
        return text
    return f"{text[:head]}...{text[-tail:]}"
