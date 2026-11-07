# Transferd Protocol

[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Protocol Version](https://img.shields.io/badge/protocol-v1--v3-blue)](docs/PROTOCOL.md)
[![Reference Implementation](https://img.shields.io/badge/reference-TransferDaemon-orange)](https://github.com/LoopyLuci/TransferDaemon)

**Transferd** is a protocol for end-to-end encrypted, post-quantum secure data transfer between paired nodes. Nodes can reach each other directly, through blind relays, or both at once, and applications see reliable, ordered, flow-controlled streams.

This repository is the **specification**. It contains no application code. Anyone can read it, implement it, or run a relay that speaks it.

[TransferDaemon](https://github.com/LoopyLuci/TransferDaemon) is the production application and reference implementation. It is a separate project with its own installers, user interfaces and release process.

---

## What the protocol provides

- **Authenticated, hybrid key exchange on every link.** Identity is Ed25519 + ML-DSA-87. Key exchange is X25519 + ML-KEM-768. Breaking a session requires breaking both the classical and the post-quantum parts.
- **Encrypted tunnels with replay protection.** Directional AES-256-GCM keys, counter nonces, and a 4096-wide replay window.
- **Multiplexed streams.** Per-stream flow control, cumulative and selective acknowledgements, and resending on another route if one fails.
- **Multiple routes per peer.** A session can use direct TCP, relayed datagrams, and a hole-punched UDP path at the same time. Routes are probed, and the session fails over between them.
- **Blind relays.** A relay forwards random 32-byte tokens and size-padded ciphertext. It does not learn identities or plaintext.
- **Hole punching** (protocol version 3 and later) for direct UDP paths through cone NATs.
- **Pairing by signed invite**, with one-time nonces and trust on first use.
- **Compatibility by design.** Protocol versions are negotiated at connection time. Unknown frame kinds and trailing fields are ignored, so newer builds do not break older ones.
- **No central servers.** Every relay and resolver is configuration. Any conforming relay can be self-hosted.

---

## Read the specification

| Document | What it covers |
|----------|----------------|
| [docs/PROTOCOL.md](docs/PROTOCOL.md) | **The wire specification.** Handshake, tunnel, relay encapsulation, frames, sessions, invites, versioning, hole punching. |
| [docs/OVERLAY.md](docs/OVERLAY.md) | **Overlay semantics.** Pairing, services and access control, path selection, streams and congestion control, mesh DNS. |
| [docs/CRYPTO.md](docs/CRYPTO.md) | Cryptographic constructions, key derivation labels, and informal security arguments. |
| [docs/RELAY.md](docs/RELAY.md) | The blind relay: token registration, forwarding, and `Locate`. |
| [docs/IMPLEMENTATION.md](docs/IMPLEMENTATION.md) | How to build a conforming node, which requirements are mandatory, and how to test interoperability. |

Start with `PROTOCOL.md` if you are implementing the wire format. Start with `OVERLAY.md` if you want to know what the network does.

---

## Layers

```
 application stream bytes
 ── overlay ────────────  pairing, services, routes, streams, mesh DNS
 ── frames ─────────────  session: hello, streams, pings, receipts
 ── tunnel ─────────────  per link: AES-256-GCM, counter, replay window
 ── link ───────────────  TCP, relay datagrams, or any LinkTransport
```

---

## Implementations

| Implementation | Status | Notes |
|----------------|--------|-------|
| [TransferDaemon](https://github.com/LoopyLuci/TransferDaemon) | Reference implementation | Speaks protocol versions 1–3. Includes the `relayd` blind relay. |

If you build an implementation, please tell us, and we will list it here. Interoperability tests against the reference implementation are described in [docs/IMPLEMENTATION.md](docs/IMPLEMENTATION.md).

---

## Versioning and change process

- The current protocol version is **3**. The lowest version still supported is **1**. The versions a build speaks are described in [PROTOCOL.md §7](docs/PROTOCOL.md#7-compatibility-and-evolution).
- **This repository is the source of truth for wire behavior.** A change to frame formats, key derivation labels, or version ranges is made here first, and is then implemented.
- If this specification and an implementation disagree, one of them has a bug. Open an issue here so the disagreement can be settled and the specification corrected if needed.
- New ciphersuites and frame kinds are additive. An existing algorithm or identifier is never changed.

---

## Contributing

Propose protocol changes (new frame kinds, ciphersuites, version bumps, or clarifications) by opening an issue or pull request in this repository, before writing implementation code. Changes that alter wire behavior need a version or a new identifier, and need a note in the relevant document about how older builds behave.

---

## License

MIT License. See [LICENSE](LICENSE).
