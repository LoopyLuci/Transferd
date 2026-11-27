"""The device's debugging policy: whether debugging is on, who may debug, and what each operation may do.

`decide` is the one place the rule is written down. The agent calls it for every stream a debugger opens, and the
settings page shows the same values, so what the user sees is what the device enforces.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import FrozenSet, Iterable, Tuple

from transferd_gui.debug.wire import OPS

#: The modes an operation can have. `ask` means the device's user decides each stream.
MODES: Tuple[str, ...] = ("allow", "ask", "deny")
#: The defaults: reading device information is open; logs, shells and forwards need a yes from the device's user.
DEFAULT_MODES = {"info": "allow", "logcat": "ask", "shell": "ask", "forward": "ask"}
NODE_ID_HEX_LEN = 64
PORT_MAX = 65535


@dataclass(frozen=True)
class DebugPolicy:
    """Every debugging setting the device enforces. The defaults refuse everything except device information, and only
    after the user has turned debugging on and named a debugger."""

    enabled: bool = False
    modes: dict = field(default_factory=lambda: dict(DEFAULT_MODES))
    clients: FrozenSet[str] = frozenset()
    forward_ports: FrozenSet[int] = frozenset()
    shell_timeout_s: float = 60.0
    shell_output_limit: int = 256 * 1024
    streams_per_client: int = 8

    def mode(self, op: str) -> str:
        return self.modes.get(op, "deny")

    def allows_client(self, node_id: str) -> bool:
        return node_id in self.clients


@dataclass(frozen=True)
class Decision:
    """What the device does with one stream. `code` and `reason` are set when it is refused."""

    outcome: str  # "allow", "ask" or "refuse"
    code: str = ""
    reason: str = ""


def decide(policy: DebugPolicy, op: str, client_node_id: str) -> Decision:
    """Allow, ask, or refuse a stream from `client_node_id` for `op`. The checks run in the order a user would expect:
    the master switch, then the allowlist, then the operation's own mode."""
    if not policy.enabled:
        return Decision("refuse", "disabled", "debugging is turned off on this device")
    if not policy.allows_client(client_node_id):
        return Decision("refuse", "denied", "this computer is not on the device's list of debuggers")
    mode = policy.mode(op)
    if mode == "deny":
        return Decision("refuse", "denied", f"'{op}' is turned off on this device")
    if mode == "ask":
        return Decision("ask")
    return Decision("allow")


def parse_ports(text: str) -> FrozenSet[int]:
    """`8080, 5555` as a set of ports. Empty text is the empty set. Raises ValueError naming the bad part."""
    ports = set()
    for part in _split(text):
        if not part.isdigit() or not 1 <= int(part) <= PORT_MAX:
            raise ValueError(f"'{part}' is not a port from 1 to {PORT_MAX}")
        ports.add(int(part))
    return frozenset(ports)


def parse_clients(text: str) -> FrozenSet[str]:
    """Node ids, comma separated, each 64 hexadecimal characters. Empty text is the empty set."""
    ids = []
    for part in _split(text):
        value = part.lower()
        if len(value) != NODE_ID_HEX_LEN or any(c not in "0123456789abcdef" for c in value):
            raise ValueError(f"'{part}' is not a node id (64 hexadecimal characters)")
        ids.append(value)
    return frozenset(ids)


def format_ports(ports: Iterable[int]) -> str:
    return ", ".join(str(p) for p in sorted(ports))


def format_clients(ids: Iterable[str]) -> str:
    return ", ".join(sorted(ids))


def _split(text: str) -> list:
    return [part.strip() for part in text.replace(";", ",").split(",") if part.strip()]


def check_modes(modes: dict) -> list:
    """Problems with a set of operation modes, one message per problem. An empty list means they are valid."""
    problems = []
    for op in OPS:
        mode = modes.get(op, DEFAULT_MODES[op])
        if mode not in MODES:
            problems.append(f"the {op} mode must be one of: {', '.join(MODES)}")
    return problems
