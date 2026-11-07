# Implementing Transferd

## Reference Implementation

The production reference implementation is [TransferDaemon](https://github.com/LoopyLuci/TransferDaemon).

Key crates that implement this specification:

| Crate | Role |
|-------|------|
| `transferd-crypto` | Hybrid identity, handshake, key derivation, AES-GCM |
| `transferd-overlay` | Session, streams, routes, hole punching, invites |
| `relayd` | Blind relay |
| `transferd-core` | Adaptive multi-path scheduling used by the application layer |
| `transferd-api` | gRPC control plane used by TransferDaemon (not part of the wire protocol) |

The wire protocol defined in this repository is independent of the gRPC control plane and of any particular user interface.

## Building a Conforming Node

A minimal conforming node must:

1. Implement the hybrid handshake and tunnel framing exactly as specified in [PROTOCOL.md](PROTOCOL.md).
2. Speak the frame kinds it claims to support, and correctly ignore unknown kinds and trailing fields.
3. Honor the version negotiation rules.
4. When acting as a relay, implement the token registration and blind forwarding rules in [RELAY.md](RELAY.md).
5. Protect long-term identity material.

Optional but recommended:

- Hole punching (version ≥ 3)
- Congestion control and pacing
- Constant-rate padding for additional metadata protection

## Testing Interoperability

TransferDaemon contains extensive interoperability tests, including:

- Current peer talking to an older peer (`max_version = 1`)
- Current peer talking to a simulated newer peer
- Hole punching through userspace NAT emulators
- Lossy relay paths

Implementers are encouraged to test against TransferDaemon’s overlay crates.

## Protocol Evolution

All changes to frame formats, key derivation labels, or version ranges must be reflected in this repository first. The TransferDaemon maintainers treat the documents here as the source of truth for the wire protocol.
