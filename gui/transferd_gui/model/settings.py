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

from transferd_gui.debug.policy import MODES, parse_clients, parse_ports
from transferd_gui.wormhole.addresses import parse_list

THEMES: Tuple[str, ...] = ("system", "light", "dark")
#: How the debugging computer reaches a device that is also paired over Wi-Fi or the relay.
DEBUG_TRANSPORTS: Tuple[str, ...] = ("auto", "usb_only", "wifi_only")
NODE_NAME_MAX = 64
#: The longest path or command text the debugging settings accept.
TEXT_MAX = 512


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
    # Debugging, as a device (the agent): whether this node answers debug streams, who may ask, and what each may do.
    debug_enabled: bool = False
    debug_clients: str = ""
    debug_info_mode: str = "allow"
    debug_logcat_mode: str = "ask"
    debug_shell_mode: str = "ask"
    debug_forward_mode: str = "ask"
    debug_forward_ports: str = ""
    debug_log_command: str = ""
    debug_shell_timeout_s: float = 60.0
    debug_shell_output_kib: int = 256
    debug_streams_per_client: int = 8
    # Debugging, as a computer (the client): which device it debugs, how it reaches it, and the USB bridge.
    debug_device_id: str = ""
    debug_transport: str = "auto"
    debug_wifi_address: str = ""
    usb_adb_path: str = "adb"
    usb_device_port: int = 0

    #: The mailboxes a wormhole may meet on, a comma-separated list tried in order: wss://host:port#fingerprint for another
    #: computer (the fingerprint pins its certificate), or ws://127.0.0.1:port for this one. Empty means none is set.
    wormhole_mailbox: str = ""
    #: How many data streams a wormhole file is spread over.
    wormhole_streams: int = 4
    #: Host a mailbox on this computer. Off until it is turned on; nothing listens while it is off.
    wormhole_host: bool = False
    #: The port the hosted mailbox listens on.
    wormhole_host_port: int = 4470
    #: Whether the hosted mailbox is reachable from other computers: wss:// with its own pinned certificate. Off means only
    #: this computer, over ws://127.0.0.1.
    wormhole_host_network: bool = False


# Each integer or float setting with its inclusive bounds. Bools and strings are checked separately.
_NUMBER_RANGES = {
    "listen_port": (0, 65535, int),
    "relay_request_timeout": (0.5, 60.0, float),
    "peer_connect_timeout": (1.0, 120.0, float),
    "max_transfer_mib": (1, 512, int),
    "event_log_size": (50, 10000, int),
    "telemetry_window_s": (60, 3600, int),
    "debug_shell_timeout_s": (1.0, 600.0, float),
    "debug_shell_output_kib": (16, 16384, int),
    "debug_streams_per_client": (1, 64, int),
    "usb_device_port": (0, 65535, int),
    "wormhole_streams": (1, 8, int),
    "wormhole_host_port": (1, 65535, int),
}

#: The longest mailbox list Settings accepts: up to four addresses, each with its pin.
WORMHOLE_MAILBOX_MAX = 600


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
    for key in ("allow_incoming", "auto_accept_files", "debug_enabled", "wormhole_host", "wormhole_host_network"):
        if not isinstance(values.get(key, False), bool):
            problems.append(f"{key} must be on or off")
    mailbox = values.get("wormhole_mailbox", defaults["wormhole_mailbox"])
    if not isinstance(mailbox, str):
        problems.append("wormhole_mailbox must be text")
    elif len(mailbox) > WORMHOLE_MAILBOX_MAX:
        problems.append(f"wormhole_mailbox is longer than {WORMHOLE_MAILBOX_MAX} characters")
    else:
        try:
            parse_list(mailbox)
        except ValueError as exc:
            problems.append(f"wormhole_mailbox: {exc}")
    problems.extend(_debug_problems(values, defaults))
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


def _debug_problems(values: Mapping[str, Any], defaults: Mapping[str, Any]) -> List[str]:
    """Problems with the debugging settings. Each text value is checked as the protocol will read it."""
    problems: List[str] = []

    def text(key: str) -> str:
        value = values.get(key, defaults[key])
        return value if isinstance(value, str) else ""

    for key in ("debug_clients", "debug_forward_ports", "debug_device_id", "debug_wifi_address", "debug_log_command", "usb_adb_path", "debug_transport"):
        if not isinstance(values.get(key, defaults[key]), str):
            problems.append(f"{key} must be text")
    if problems:
        return problems
    try:
        parse_clients(text("debug_clients"))
    except ValueError as exc:
        problems.append(f"debug_clients: {exc}")
    try:
        parse_ports(text("debug_forward_ports"))
    except ValueError as exc:
        problems.append(f"debug_forward_ports: {exc}")
    if text("debug_device_id").strip():
        try:
            parse_clients(text("debug_device_id"))
        except ValueError as exc:
            problems.append(f"debug_device_id: {exc}")
    if text("debug_wifi_address").strip():
        try:
            parse_host_port(text("debug_wifi_address"))
        except ValueError as exc:
            problems.append(f"debug_wifi_address: {exc}")
    for key in ("debug_log_command", "usb_adb_path"):
        value = text(key)
        if len(value) > TEXT_MAX or any(ord(c) < 32 for c in value):
            problems.append(f"{key} must be at most {TEXT_MAX} characters without control characters")
    if not text("usb_adb_path").strip():
        problems.append("usb_adb_path must name the adb program, or be 'adb' to use the one on PATH")
    if text("debug_transport") not in DEBUG_TRANSPORTS:
        problems.append(f"debug_transport must be one of: {', '.join(DEBUG_TRANSPORTS)}")
    modes = {op: values.get(f"debug_{op}_mode", defaults[f"debug_{op}_mode"]) for op in ("info", "logcat", "shell", "forward")}
    for key, mode in modes.items():
        if mode not in MODES:
            problems.append(f"debug_{key}_mode must be one of: {', '.join(MODES)}")
    return problems


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
