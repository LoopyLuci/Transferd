"""Settings: what the GUI remembers between runs, validated before it is saved or used.

A value that fails validation is rejected with a message that names it. Nothing is replaced silently: a settings file that
does not validate is left exactly as it is on disk, and the app runs on the defaults until the user saves a valid set.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, List, Mapping, Tuple

THEMES: Tuple[str, ...] = ("system", "light", "dark")
NODE_NAME_MAX = 64


class SettingsError(ValueError):
    """Settings that do not validate. `problems` holds one message per bad value."""

    def __init__(self, problems: List[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = list(problems)


@dataclass(frozen=True)
class Settings:
    """Every value the GUI keeps. Times are seconds, sizes are MiB."""

    data_dir: str
    download_dir: str
    node_name: str = ""
    relay_address: str = ""
    advertised_address: str = ""
    listen_port: int = 0
    allow_incoming: bool = False
    auto_accept_files: bool = False
    relay_request_timeout: float = 5.0
    peer_connect_timeout: float = 10.0
    max_transfer_mib: int = 64
    theme: str = "system"
    event_log_size: int = 500
    telemetry_window_s: int = 600


# Each integer or float setting with its inclusive bounds. Bools and strings are checked separately.
_NUMBER_RANGES = {
    "listen_port": (0, 65535, int),
    "relay_request_timeout": (0.5, 60.0, float),
    "peer_connect_timeout": (1.0, 120.0, float),
    "max_transfer_mib": (1, 512, int),
    "event_log_size": (50, 10000, int),
    "telemetry_window_s": (60, 3600, int),
}


def parse_host_port(text: str) -> Tuple[str, int]:
    """Split `host:port` or `[ipv6]:port` into its parts. Raises ValueError with a message a user can act on."""
    value = text.strip()
    if not value:
        raise ValueError("an address is host:port, for example relay.example.net:7000")
    if value.startswith("["):
        host, sep, rest = value[1:].partition("]")
        if not sep or not rest.startswith(":"):
            raise ValueError("an IPv6 address is written [address]:port")
        port_text = rest[1:]
    else:
        host, sep, port_text = value.rpartition(":")
        if not sep:
            raise ValueError("an address needs a port after a colon, for example relay.example.net:7000")
    if not host or any(c.isspace() for c in host):
        raise ValueError("the host part of an address is empty or has spaces")
    if not port_text.isdigit() or not 1 <= int(port_text) <= 65535:
        raise ValueError("the port must be a number from 1 to 65535")
    return host, int(port_text)


def default_settings(data_dir: str, download_dir: str) -> Settings:
    return Settings(data_dir=data_dir, download_dir=download_dir)


def validate(raw: Mapping[str, Any]) -> Settings:
    """Check a mapping of setting names to values and return the settings they make. Raises `SettingsError`."""
    known = set(Settings.__dataclass_fields__)
    problems: List[str] = [f"unknown setting '{key}'" for key in raw if key not in known]
    values = {key: raw[key] for key in raw if key in known}
    defaults = asdict(Settings(data_dir="", download_dir=""))

    for key in ("data_dir", "download_dir"):
        value = values.get(key, "")
        if not isinstance(value, str) or not value.strip():
            problems.append(f"{key} must be a folder path")
        elif not Path(value).is_absolute():
            problems.append(f"{key} must be an absolute path")
    name = values.get("node_name", "")
    if not isinstance(name, str) or len(name) > NODE_NAME_MAX or any(ord(c) < 32 for c in name):
        problems.append(f"node_name must be text of at most {NODE_NAME_MAX} characters without control characters")
    for key in ("relay_address", "advertised_address"):
        value = values.get(key, "")
        if not isinstance(value, str):
            problems.append(f"{key} must be text")
        elif value.strip():
            try:
                parse_host_port(value)
            except ValueError as exc:
                problems.append(f"{key}: {exc}")
    for key in ("allow_incoming", "auto_accept_files"):
        if not isinstance(values.get(key, False), bool):
            problems.append(f"{key} must be on or off")
    if values.get("theme", defaults["theme"]) not in THEMES:
        problems.append(f"theme must be one of: {', '.join(THEMES)}")
    for key, (low, high, kind) in _NUMBER_RANGES.items():
        value = values.get(key, defaults[key])
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            problems.append(f"{key} must be a number")
            continue
        if isinstance(value, float) and not math.isfinite(value):
            problems.append(f"{key} must be a finite number")
            continue
        if kind is int and not float(value).is_integer():
            problems.append(f"{key} must be a whole number")
            continue
        if not low <= value <= high:
            problems.append(f"{key} must be between {low:g} and {high:g}")

    if problems:
        raise SettingsError(problems)
    merged = {**defaults, **values}
    for key, (_low, _high, kind) in _NUMBER_RANGES.items():
        merged[key] = kind(merged[key])
    return Settings(**merged)


def updated(current: Settings, changes: Mapping[str, Any]) -> Settings:
    """The settings with `changes` applied, validated as a whole. Raises `SettingsError` and changes nothing."""
    return validate({**asdict(current), **changes})


def load_settings(path: Path, defaults: Settings) -> Settings:
    """Read the settings file. A missing file gives the defaults. A file that does not validate raises `SettingsError`
    and is not touched."""
    if not path.exists():
        return defaults
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SettingsError([f"the settings file {path} is not readable JSON: {exc}"]) from exc
    if not isinstance(data, dict):
        raise SettingsError([f"the settings file {path} must hold a JSON object"])
    return validate({**asdict(defaults), **data})


def save_settings(path: Path, settings: Settings) -> None:
    """Validate, then write the file atomically: a temporary file in the same folder, renamed over the old one."""
    validate(asdict(settings))
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".settings-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as tmp:
            json.dump(asdict(settings), tmp, indent=2, sort_keys=True)
            tmp.write("\n")
        os.replace(tmp_name, path)
    except BaseException:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise
