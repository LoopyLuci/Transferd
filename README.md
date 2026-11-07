# Transferd Protocol

[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Protocol Version](https://img.shields.io/badge/protocol-v3-blue)](docs/PROTOCOL.md)
[![Reference Implementation](https://img.shields.io/badge/reference-TransferDaemon-orange)](https://github.com/LoopyLuci/TransferDaemon)

**Transferd** is a universal, zero-knowledge, post-quantum secure data transfer and messaging protocol. It unifies arbitrary underlying transports (direct TCP, blind relays, DMI/Thunderbolt, Wi-Fi Direct, Bluetooth, and more) into a single, consistent, end-to-end encrypted abstraction.

Transferd is the **protocol**.  
[TransferDaemon](https://github.com/LoopyLuci/TransferDaemon) is the production-grade **application** and reference implementation that speaks Transferd.

This repository contains the canonical protocol specification, wire formats, security model, and architectural documentation. It is intentionally independent of any particular application so that other implementers can build compatible clients, relays, or specialized nodes.

---

## What Transferd Provides

- **Universal transport abstraction** — Applications see one reliable, ordered, flow-controlled stream interface. The protocol automatically selects, stripes, and fails over across available lanes.
- **Zero-knowledge relays** — Blind relays forward only random tokens and padded ciphertext. They never learn identities, endpoints, or plaintext.
- **Post-quantum security** — Hybrid identity (Ed25519 + ML-DSA-87) and hybrid key exchange (X25519 + ML-KEM-768).
- **Authenticated hybrid handshake** on every link.
- **Multiplexed streams** with cumulative ACKs, SACK ranges, per-stream windows, and per-route congestion control.
- **Hole punching** (protocol version 3+) for direct UDP paths behind cone NATs.
- **Invites and mutual pairing** with one-time nonces.
- **Crypto-agility** — new ciphersuites and frame kinds are additive; older peers ignore what they do not understand.
- **No central servers required**. Any number of self-hosted relays can be used.

See [docs/PROTOCOL.md](docs/PROTOCOL.md) for the complete wire specification and [docs/OVERLAY.md](docs/OVERLAY.md) for the overlay semantics.

---

## Relationship to TransferDaemon

| Aspect | Transferd (this repo) | TransferDaemon |
|--------|-----------------------|----------------|
| Purpose | Protocol specification & wire formats | Full messaging + file-transfer application |
| Contents | Specs, security model, compatibility rules | Daemon, GUI, TUI, CLI, mobile shells, installers |
| Audience | Protocol implementers, security reviewers, alternative clients | End users and application developers |
| Canonical source | Wire protocol, handshake, frame formats, versioning rules | Reference implementation of those specs |

TransferDaemon’s crates (`transferd-crypto`, `transferd-overlay`, `relayd`, etc.) implement this protocol. Changes to the wire format or security properties should be reflected first in this repository, then in the application.

---

## Documentation Map

| Document | Description |
|----------|-------------|
| [docs/PROTOCOL.md](docs/PROTOCOL.md) | Complete wire protocol: handshake, tunnel, frames, sessions, hole punching, compatibility |
| [docs/OVERLAY.md](docs/OVERLAY.md) | Overlay semantics: pairing, services, routes, mesh DNS, congestion control |
| [docs/CRYPTO.md](docs/CRYPTO.md) | Cryptographic primitives, key derivation, and security arguments |
| [docs/RELAY.md](docs/RELAY.md) | Blind relay protocol and registration |
| [docs/IMPLEMENTATION.md](docs/IMPLEMENTATION.md) | Mapping to TransferDaemon crates and guidance for other implementers |

---

## Security Model (Summary)

- **Identity**: long-term hybrid signing key (Ed25519 + ML-DSA-87). Node ID = hex of the Ed25519 public key.
- **Link security**: authenticated hybrid handshake → directional AES-256-GCM keys with counter nonces and a 4096-wide replay window.
- **Metadata protection**: relays see only tokens and size-padded ciphertext. Optional constant-rate padding is available in implementations.
- **Forward secrecy**: ephemeral keys per session/handshake.
- **Compatibility**: protocol versions are negotiated; unknown frame kinds and trailing fields are ignored.

Full details are in the protocol documents.

---

## Status

This is the extracted and canonicalized protocol documentation from the TransferDaemon project. The wire formats and versioning rules described here match the current TransferDaemon implementation (protocol versions 1–3).

---

## License

MIT License. See [LICENSE](LICENSE).

---

## Contributing

Protocol changes (new frame kinds, ciphersuites, version bumps) should be proposed here first so that all implementations can stay interoperable. The reference implementation lives in [TransferDaemon](https://github.com/LoopyLuci/TransferDaemon).
