"""What this build of the library cannot do, in one place.

A feature whose backend is missing from `python/transferd` stays visible in the GUI, disabled, with the reason below. It is
never hidden and never stubbed to look as if it works. The reasons name the Rust source each one comes from, so the gap
is easy to check.
"""

from __future__ import annotations

from typing import Dict

HOLE_PUNCH_START = "hole_punch_start"
MESH_DNS = "mesh_dns"
GHOST_REGISTER = "ghost_register"
CUSTOM_RELAY_TOKEN = "custom_relay_token"
RELAY_UDP_PORT = "relay_udp_port"
SESSION_TIMEOUTS = "session_timeouts"

UNAVAILABLE: Dict[str, str] = {
    HOLE_PUNCH_START: (
        "The node orchestration that starts a punch (start_punch, on_punch, finish_punch) is not ported to python/transferd. "
        "The probe table and the CANDIDATE frame exist, but nothing drives them from a session."
    ),
    MESH_DNS: (
        "Mesh DNS is in the Rust transferd-dns crate. python/transferd has no resolver, DNS client or DNS service to call."
    ),
    GHOST_REGISTER: (
        "RelayClient.register takes no ghost key certificate, so a registration cannot prove a ghost key in this build."
    ),
    CUSTOM_RELAY_TOKEN: (
        "The protocol derives the relay token from the node id (BLAKE3 over the node id). No custom token is defined."
    ),
    RELAY_UDP_PORT: (
        "RelayClient binds an ephemeral UDP port. The library cannot be told which port to use."
    ),
    SESSION_TIMEOUTS: (
        "The open and handshake timeouts are module constants in the library (OPEN_TIMEOUT, HANDSHAKE_TIMEOUT). "
        "They cannot be changed from the GUI."
    ),
}


def reason(feature: str) -> str:
    """The reason a feature is unavailable. Raises KeyError for a feature the GUI does not know about."""
    return UNAVAILABLE[feature]
