"""Immutable snapshots: what the node looked like at one moment, as plain values the views can read.

The backend builds a `NodeSnapshot` on its own thread from live library objects. Every number here is either a value the
library reported, or `None` when nothing has been measured yet. Views show `None` as "no data yet"; they never invent a
value to fill a gap.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

#: Session states, in the order a session goes through them.
SESSION_STATES = ("negotiating", "ready", "no route", "closed")
#: Relay states. `off` is not connected, `connecting` is the first registration, `failed` is the last attempt.
RELAY_STATES = ("off", "connecting", "registered", "failed")


@dataclass(frozen=True)
class RouteSnapshot:
    """One route of one session. `key` is stable for the life of the route and names its telemetry series."""

    key: str
    peer_id: str
    link_id: int
    kind: str
    up: bool
    closed: bool
    #: What the route is: its address, or `usb:<serial>` for a route carried over the cable.
    label: str = ""
    #: Library measurements. None until the route has answered a probe. An RTT of 0.0 is real: it is under the
    #: platform's clock resolution.
    rtt_ms: Optional[float] = None
    srtt_ms: Optional[float] = None
    loss: Optional[float] = None
    mbps: Optional[float] = None
    cost_ms: Optional[float] = None
    #: Bytes of every frame the link accepted for sending, and every frame the session took off it.
    sent_bytes: int = 0
    received_bytes: int = 0
    in_flight: Optional[int] = None
    cwnd: Optional[int] = None


@dataclass(frozen=True)
class SessionSnapshot:
    peer_id: str
    peer_name: str
    state: str
    version: int
    services: Tuple[str, ...]
    routes: Tuple[RouteSnapshot, ...]
    established_at: float

    @property
    def routes_up(self) -> int:
        return sum(1 for route in self.routes if route.up)


@dataclass(frozen=True)
class IdentitySnapshot:
    ready: bool
    name: str = ""
    node_id: str = ""
    fingerprint: str = ""
    ed25519_public: str = ""
    mldsa_public: str = ""
    relay_token: str = ""


@dataclass(frozen=True)
class ListenerSnapshot:
    active: bool
    port: Optional[int] = None
    detail: str = ""


@dataclass(frozen=True)
class RelaySnapshot:
    configured: bool
    address: str
    state: str
    detail: str = ""
    registered_at: Optional[float] = None
    last_registered_at: Optional[float] = None
    registrations_ok: int = 0
    registration_failures: int = 0
    next_registration_at: Optional[float] = None
    renew_every_s: float = 30.0


@dataclass(frozen=True)
class IncomingRequest:
    """A stream a peer asked to open, waiting for this node to accept or refuse it."""

    request_id: int
    peer_id: str
    peer_name: str
    service: str
    received_at: float


@dataclass(frozen=True)
class TransferRecord:
    """One file or stream transfer, newest last. `status` is `done`, `handed to the session`, `failed` or a note."""

    when: float
    direction: str
    peer_id: str
    service: str
    name: str
    size: int
    status: str
    detail: str = ""


@dataclass(frozen=True)
class LocateResult:
    peer_id: str
    self_addr: str
    peer_addr: Optional[str]
    when: float
    punch_candidates: Tuple[str, ...] = ()


@dataclass(frozen=True)
class UsbDeviceSnapshot:
    """A device adb reported. `state` is adb's word: `device` (ready), `unauthorized`, `offline` and so on."""

    serial: str
    state: str
    model: str = ""


@dataclass(frozen=True)
class DebugStreamSnapshot:
    """A debug operation this node is running as a debugger: a log stream, a shell command or a port forward."""

    handle: int
    peer_id: str
    kind: str
    detail: str
    started_at: float
    state: str
    note: str = ""


@dataclass(frozen=True)
class DebugSnapshot:
    """Debugging on this node, in both roles, and what the USB bridge can see."""

    agent_enabled: bool = False
    clients: Tuple[str, ...] = ()
    device_id: str = ""
    usb_state: str = "not checked"
    usb_detail: str = ""
    usb_devices: Tuple[UsbDeviceSnapshot, ...] = ()
    streams: Tuple[DebugStreamSnapshot, ...] = ()


@dataclass(frozen=True)
class NodeSnapshot:
    taken_at: float
    identity: IdentitySnapshot
    listener: ListenerSnapshot
    relay: RelaySnapshot
    sessions: Tuple[SessionSnapshot, ...] = ()
    requests: Tuple[IncomingRequest, ...] = ()
    transfers: Tuple[TransferRecord, ...] = ()
    locate: Optional[LocateResult] = None
    debug: DebugSnapshot = DebugSnapshot()

    @property
    def all_routes(self) -> Tuple[RouteSnapshot, ...]:
        return tuple(route for session in self.sessions for route in session.routes)

    @property
    def routes_up(self) -> int:
        return sum(1 for route in self.all_routes if route.up)

    def ready_peers(self) -> Tuple[SessionSnapshot, ...]:
        return tuple(session for session in self.sessions if session.state == "ready")

    def session_for(self, peer_id: str) -> Optional[SessionSnapshot]:
        return next((session for session in self.sessions if session.peer_id == peer_id), None)
