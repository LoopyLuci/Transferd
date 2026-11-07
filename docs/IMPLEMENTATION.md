# Implementing Transferd

## Reference Implementation

The production reference implementation is [TransferDaemon](https://github.com/LoopyLuci/TransferDaemon).

The crates of the reference implementation that carry this specification:

| Crate | Role |
|-------|------|
| `transferd-crypto` | Hybrid identity, handshake, key derivation, AES-GCM |
| `transferd-overlay` | Session, streams, routes, congestion control, hole punching, invites |
| `relayd` | Blind relay |
| `transferd-core` | Adaptive multi-path scheduling and the transport lane trait |

The wire protocol defined in this repository is independent of the gRPC control plane (`transferd-api` in TransferDaemon, an application interface) and of any particular user interface. The crate names above describe one codebase's internal layout. They are informative and may change; the specification is what matters for interoperability.

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

The reference implementation's overlay tests cover the behavior in this specification. They are in [`transferd-overlay/tests`](https://github.com/LoopyLuci/TransferDaemon/tree/main/transferdaemon/crates/transferd-overlay/tests) of the TransferDaemon repository:

- `paths.rs`: a current peer, a peer limited to version 1 (`max_version = 1`) and a simulated newer peer each agree on the highest common version.
- `punch.rs`: two cone NATs get a punched route beside the relay, and a stream crosses it. A symmetric NAT cannot be punched and the relay keeps working. A stranger's probe is ignored. A peer that agreed on an older version is never sent a punch frame.
- `lossy.rs`: the relayed path loses a fifth of its datagrams in both directions, and everything still arrives.

From the `transferdaemon/` directory of that repository, `cargo test -p transferd-overlay` runs them. An independent implementation can use the same scenarios as its acceptance tests.

## Protocol Evolution

All changes to frame formats, key derivation labels, or version ranges are made in this repository first, then implemented. The documents here are the source of truth for the wire protocol. If an implementation behaves differently from them, the difference is a bug in one or the other, and should be reported as an issue in this repository.
