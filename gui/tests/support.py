"""Helpers shared by the GUI tests: settings in a temporary folder, free ports and waiting for a condition."""

import socket
import time
from dataclasses import replace
from pathlib import Path
from typing import Callable

from transferd_gui.model.settings import Settings, default_settings


def make_settings(folder: Path, **changes) -> Settings:
    """Settings whose data and download folders sit under `folder`, with `changes` applied."""
    data = folder / "data"
    return replace(default_settings(str(data), str(data / "received")), **changes)


def free_port() -> int:
    """A TCP port that was free a moment ago. Binding it again right away is the usual test practice."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def wait_until(predicate: Callable[[], bool], timeout: float = 5.0, step: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return predicate()
