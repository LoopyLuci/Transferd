"""Observing library objects without changing the library.

The GUI needs two numbers the library does not keep: the bytes received on each route, and the outcome of each relay
registration. Both are read by wrapping the one method that produces them, on that one instance. The wrapped method
still runs and returns what it returned; the wrapper only looks at the result.
"""

from __future__ import annotations

from typing import Any, Callable

from transferd.frames import Frame
from transferd.link import Link


def count_received(link: Link, on_frame: Callable[[Frame], None]) -> None:
    """Call `on_frame` for every frame the session takes off this link."""
    original = link.try_recv

    def observed() -> Frame | None:
        frame = original()
        if frame is not None:
            on_frame(frame)
        return frame

    link.try_recv = observed  # type: ignore[method-assign]


def count_sent(link: Link, on_frame: Callable[[Frame], None]) -> None:
    """Call `on_frame` for every frame the link accepts for sending. A frame it refuses is not counted."""
    original = link.send

    def observed(frame: Frame) -> bool:
        accepted = original(frame)
        if accepted:
            on_frame(frame)
        return accepted

    link.send = observed  # type: ignore[method-assign]


def count_probe_answers(link: Link, on_answer: Callable[[float], None]) -> None:
    """Call `on_answer` with the round trip of each probe the route answered. The library records a round trip of 0.0
    when it is under the platform's clock resolution (about 16 ms on Windows), so the answer count is what says a route
    was measured."""
    stats = link.stats
    original = stats.probe_ok

    def observed(rtt_ms: float) -> None:
        on_answer(rtt_ms)
        original(rtt_ms)

    stats.probe_ok = observed  # type: ignore[method-assign]


def observe_async(obj: Any, name: str, on_ok: Callable[[], None], on_error: Callable[[BaseException], None]) -> None:
    """Call `on_ok` or `on_error` after each awaited call to `obj.<name>`, then pass its result or error on unchanged."""
    original = getattr(obj, name)

    async def observed(*args: Any, **kwargs: Any) -> Any:
        try:
            result = await original(*args, **kwargs)
        except Exception as exc:
            on_error(exc)
            raise
        on_ok()
        return result

    setattr(obj, name, observed)
