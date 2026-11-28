# Transferd Wormhole

Transferd Wormhole sends a file, a folder or a text from one computer to another with a short code. It is modelled on
magic-wormhole (https://github.com/magic-wormhole/magic-wormhole, MIT), and it is a separate protocol. It is not
wire-compatible with magic-wormhole: a code made by one program does not open the other.

It takes the parts of magic-wormhole that work well (a short human code, a password-authenticated key exchange, a
rendezvous mailbox that holds encrypted messages, and a direct or relayed transfer) and builds the transfer on Transferd's
overlay. The overlay gives it what magic-wormhole's transit protocol does not have: post-quantum hybrid keys, several
routes at once with resends when one is cut, a relay that is Transferd's own, flow control, and identity checks on every
connection.

The code lives in `gui\transferd_gui\wormhole\`. It is outside `python\`, so `scripts\sync-core.sh` does not copy it to
TransferDaemon.

## What the user sees

The screen is **Transfer → Wormhole** in the sidebar.

- **Send**: choose a file, a folder or a text. The app shows a code such as `4217-ember-tundra-lattice`, with a copy
  button. The receiver types it in. The screen then shows the state (for example "Waiting for the other computer to type
  this code", "Sending") and the answer.
- **Receive**: type the code and press Look up. The app shows what is offered (name, size, kind, a text's length) and asks
  Accept or Decline. Nothing is sent before Accept. A text is shown in the screen once received.
- **Stop or Cancel.** Cancelling a send ends it and the code stops working. Stopping while an offer waits declines it, so
  the sender is told "stopped by the receiving user".
- **Mailbox.** The card at the top of the screen says where wormholes meet: the mailboxes listed in Settings, or this
  computer's own mailbox when it is hosting one. If neither is set, the screen says so and every button is disabled. When
  this computer hosts a mailbox on the network, the card shows its fingerprint and a **share line** to copy into the other
  computer's Settings.

The code is a secret for one transfer. After the transfer, a cancel, or a failed attempt, the code no longer works.

## The code

```
4217-ember-tundra-lattice
```

- The first part is the **nameplate**, a decimal number from 1 to 65535. The mailbox allocates it at random.
- The rest are **three words** from the PGP word list (`wordlist.py`, copied from magic-wormhole's list, 256 pairs).
  They carry 24 bits of secret. The word at position `i` is taken from the list for `i % 2`: even positions use the even
  list and odd positions the odd list. A word that is missing, repeated or out of order fails the parity check, so a
  dropped word is caught before any network is used.
- The code is normalised before use: lower case, spaces and underscores become `-`, and the nameplate must be digits.
- The password given to SPAKE2 is the normalised code as bytes, `b"4217-ember-tundra-lattice"`, so the nameplate is
  inside the password. A mailbox cannot swap the nameplate without failing the exchange.

Why three words and not two: magic-wormhole uses two. The mailbox cannot see right guesses from wrong ones, so the only
protection against guessing is the size of the secret. Three words make a guess 256 times harder, for one more word to
read out.

## The mailbox

The mailbox is a WebSocket server. It holds encrypted messages for a nameplate until both sides have read them. It never
sees a code, a key or a file.

Settings → Wormhole holds the **mailbox list**: one or more addresses separated by commas, tried in the order given. There
is no public default. A wormhole needs a mailbox. It meets on this computer's own mailbox when the list is empty and
hosting is on (see "Hosting a mailbox on this computer" below). The two sides of a wormhole must reach the same mailbox.

### Messages

Every message is one JSON object in a WebSocket text frame. The mailbox protocol is version 2, which the `welcome` message
names. The wormhole's own protocol, the `version` phase below, is still version 1. The client sends these:

| Message | Fields | Reply |
|---|---|---|
| `bind` | `appid` = `"transferd-wormhole/1"`, `side` (16 hex characters, random per attempt) | none |
| `allocate` | | `allocated` with `nameplate` |
| `claim` | `nameplate` | `claimed` with `mailbox` (32 hex characters), or `error` with `crowded` |
| `release` | `nameplate` | `released` |
| `open` | `mailbox` | the stored messages from the other side, then `opened`; new ones arrive as they are sent |
| `add` | `mailbox`, `id` (4 hex characters), `phase`, `body` (hex) | `added` with `id`, once stored; delivered to the other side as `message` |
| `ack` | `mailbox`, `id` | none; the message is then not replayed |
| `close` | `mailbox`, `mood` (`happy`, `lonely`, `errory`, `scary`) | `closed` |

The server sends these:

| Message | Fields |
|---|---|
| `welcome` | `protocol` (2), `limits`: `max_message_bytes` (65536), `max_messages_per_mailbox` (64), `bind_timeout_s` (30); sent first on every connection |
| `opened` | `mailbox`; sent after the stored messages of an `open` |
| `added` | `id`; a message is stored (also sent again for a retry of a stored message) |
| `allocated`, `claimed`, `released`, `closed` | the answers to `allocate`, `claim`, `release` and `close` |
| `message` | `mailbox`, `id`, `side` (the sender), `phase`, `body` |
| `error` | `code` (a machine-readable reason, see the table below), `error` (a short reason), `orig` (the message type it answers), `id` when it refuses one `add`, and `retry_after` (seconds) when trying again may succeed |

Every refusal carries a `code`:

| Code | Meaning | Retry |
|---|---|---|
| `bad_request` | a message is not a JSON object | no |
| `not_bound`, `already_bound`, `wrong_app`, `bad_side` | the bind was missing, repeated, for another app, or had a bad side id | no |
| `unknown_message` | a message type the mailbox does not know | no |
| `bad_nameplate`, `bad_id`, `bad_phase`, `bad_body`, `too_big` | a field has the wrong form or size | no |
| `no_mailbox` | the mailbox id is unknown, or this side is not in it | no |
| `crowded` | a third computer tried to claim a code | no |
| `full` | the mailbox holds as many messages as it allows | no |
| `no_nameplate` | no free nameplate could be found | after `retry_after` |
| `rate_limited` | this computer asked for too much, too fast | after `retry_after` |
| `busy` | the mailbox is at a cap, or the connection is over its limit | after `retry_after` |

Rules the server keeps:

- An unclaimed nameplate expires after 5 minutes.
- A nameplate has at most two sides. A third `claim` gets `crowded`.
- A mailbox expires after one hour with no traffic, or when both sides have sent `close`.
- A message the other side has not acknowledged is kept and sent again when that side opens the mailbox again. This is how
  a dropped connection does not lose a message.
- The server never echoes a message to the side that sent it.
- A message over `max_message_bytes` is refused with `too_big`, and one past `max_messages_per_mailbox` with `full`.
- **Rate limits, per address.** Allocating a code: a burst of 30, then 30 a minute. Claiming: a burst of 120, then 2 a
  second. Adding a message: a burst of 200, then 50 a second. A refusal says how long to wait. The limits are per address,
  so computers behind one router share one allowance.
- **Caps.** At most 4096 nameplates, 1024 connections, and 64 connections from one address. Beyond these the mailbox
  answers `busy` and closes the connection.
- **Idle connections.** A connection that has not bound within 30 seconds is closed. A connection that stops answering
  pings is closed, so its nameplate is not held by a dead socket.

### Running a mailbox

```
.venv\Scripts\python.exe -m transferd_gui.wormhole.server --host 127.0.0.1 --port 4470
.venv\Scripts\python.exe -m transferd_gui.wormhole.server --host 0.0.0.0 --port 4470 --identity C:\path\to\folder
```

A mailbox that other computers can reach must be served over TLS. The second line does that: `--identity` names the folder
that holds its certificate and key, made on the first run. Without `--identity`, the server refuses any address other than
this computer's own. On start it prints the fingerprint to pin in the other computers' settings.

It is meant to run on a server the user controls, or on this computer. The mailbox sees the nameplate, the side ids, the
phase names, the size and timing of each message, the mood of each close, and the IP address of each client. It does not
see the code, the keys or the content.

### Addresses and the list

An address is `wss://host:port#fingerprint` for another computer, or `ws://127.0.0.1:port` for this one. The port defaults
to 4470.

- `wss://` is the only way to reach a mailbox on another computer. `ws://` is refused for any address other than
  `127.0.0.1`, `localhost` or `::1`, so a plain connection never leaves the computer.
- The `#fingerprint` is the SHA-256 of the mailbox's certificate, 64 hex characters. It may be copied with spaces, colons
  or dashes. It is never sent to the server: the client keeps it.
- A `wss://` address without a fingerprint is checked against the system's certificates, as a browser would check it. Use
  this for a mailbox run with a certificate from a public authority.
- The list holds up to four addresses. It is tried in order, and the first that answers is used.

### Certificates and pins

A mailbox on the network serves `wss://` with a certificate this computer made for itself: an ECDSA P-256 key and a
certificate valid for ten years, kept in the wormhole folder. The certificate's fingerprint is the pin. Other computers are
given the pin with the address, and accept that certificate only.

The check is made after the TLS handshake and before anything is sent. A different certificate is refused with a message
that names the mailbox and says to check the fingerprint. No nameplate, code or message leaves the computer. The handshake
still proves that the server holds the matching private key, so copying the certificate is not enough.

The certificate is renewed when it is within 30 days of its end, or when its key does not match it. A renewal changes the
fingerprint, so the other computers must be given the new one. Keep the folder to keep the fingerprint.

TLS keeps the nameplates, the timing, the sizes and the addresses from anyone on the network between the computers. The
messages are sealed by the two clients whatever the transport does. So a stranger who stood in for the mailbox could stop a
meeting, but could not read or change one.

### Hosting a mailbox on this computer

Settings → Wormhole → *Host a mailbox on this computer* starts a mailbox in the app, on the backend loop, on the port in
Settings (4470 by default). With it on:

- With no list in Settings, wormholes on this computer meet on its own mailbox, at `ws://127.0.0.1:port`.
- With *Let other computers reach it* also on, the mailbox listens on every interface over `wss://`, pinned as above. The
  Wormhole screen then shows the **share line**, `wss://<this computer's network address>:port#fingerprint`, with a button
  to copy it. Other computers paste that line into their list in Settings.
- A port already in use is reported on the screen and in the event log. The app keeps running.
- Turning hosting off, or changing its port or its network setting, stops the server and starts it again with the new values.

The first time the mailbox listens on the network, Windows Firewall may ask whether Python may accept connections. The
mailbox needs that allowed on the networks the other computers are on.

### Connecting: retries and fallback

- A connection is tried for each address in the list, in order, in up to three rounds. Between rounds the client waits a
  growing time: half a second, doubling up to 8 seconds, randomised. If the mailbox asked for a longer wait (a busy mailbox
  does), it waits up to 10 seconds.
- Once a mailbox has been reached, every later connection goes to that same mailbox. A reconnect never moves to another
  one, because the two sides are meeting on the first.
- A request refused with `rate_limited` is sent again after the time the mailbox asked for, up to four times. A message
  refused that way is sent again, rather than failing its caller.
- A connection the mailbox refuses as `busy` is tried again in the next round.

The list helps when one mailbox is down for both computers. It cannot help when the two computers can reach different
mailboxes: each takes the first one it can reach, and they then wait on different servers. Keep the lists the same.

## The key exchange

Each wormhole attempt uses the same steps. Every step after `pake` is encrypted under keys that only the two sides can
make.

1. **pake.** Both sides run SPAKE2 (`spake2` 0.9, symmetric, `idSymmetric = b"transferd-wormhole/1"`) with the code as
   the password. Each sends its SPAKE2 message in plain: the message is public by design. Each side computes the same
   shared secret `K`.
2. **Root key.** `root = blake3_derive_key("transferd-wormhole/1 root", K)`.
3. **Phase keys.** Each side has a random 16-hex-character `side`. The key a side uses to send in phase `P` is
   `blake3_derive_key("transferd-wormhole/1 phase " + P, root + side)`. A side therefore has a different key from the other
   side in every phase, so a message cannot be sent back to its sender and accepted there.
4. **Encryption.** AES-256-GCM (`cryptography`). The nonce is 12 random bytes and is sent with the message. The additional
   authenticated data is `"transferd-wormhole/1" 0x00 side 0x00 phase 0x00 id`. A change to the side, the phase, or the
   message id makes decryption fail.

Every encrypted phase after `pake` is a JSON object, sealed as `nonce || ciphertext || tag`, in hex.

**Key confirmation.** There is no separate verifier to read out. The `version` phase is the confirmation: if the codes
differ, the peer's `version` message will not decrypt, and the app reports "the code is wrong, or the message was
changed". This is the same result as an out-of-band check, with nothing extra for the user to do.

### The phases

Each phase is one message from each side, in this order:

| Phase | Encrypted | Body |
|---|---|---|
| `pake` | no | the SPAKE2 message |
| `version` | yes | `{"protocol": 1, "app": "transferd-wormhole/1", "overlay": <int>, "features": [...]}` |
| `identity` | yes | the sender's ephemeral identity and its places to be reached (below) |
| `offer` | yes | what is being sent (below). Only the sender sends it |
| `answer` | yes | `{"accept": true}` or `{"accept": false, "reason": "..."}`. Only the receiver sends it |
| `done` | yes | `{"ok": true, "sha256": "..."}` or `{"ok": false, "reason": "..."}`, sent after the transfer is checked |

Both sides send `version` and `identity`. Each side refuses a `protocol` it does not speak, and refuses an `overlay` version
that does not match its own.

### `identity`

```json
{
  "node_id": "<64 hex characters>",
  "public_key": "<hybrid public key, hex>",
  "name": "<display name, at most 64 characters>",
  "relay": "<host:port of the relay both sides use, or empty>",
  "hints": [{"kind": "direct", "host": "192.168.1.5", "port": 53123}]
}
```

The identity is generated for this attempt only. The long-term node id is never sent, so a wormhole cannot be linked to
the computer's usual Transferd identity. The public key is sent under the phase keys, so the overlay can check that the
peer it connects to is the one that was named here (`expect_node_id`).

### `offer` and `answer`

```json
{"kind": "text", "text": "..."}
{"kind": "file", "name": "report.pdf", "size": 1234567, "sha256": "<hex>"}
{"kind": "directory", "name": "photos", "size": 9999, "sha256": "<hex of the zip>", "entries": 42}
```

A text is limited to 16 KiB of UTF-8, so it fits in one mailbox message and is never sent over the overlay. A folder is
sent as one zip archive, and the receiver unpacks it under a safe-name check described below.

The receiver shows the offer and sends `answer`. Nothing is sent over the overlay before `accept`. A decline closes the
attempt, and the sender is told the reason.

## Transit over the overlay

Transit runs only for a file or a folder, after `accept`. The sender dials; the receiver serves.

1. **Listen.** The receiver opens a TCP listener on an ephemeral port and lists its addresses as `hints`. If a relay is
   set, it also registers its ephemeral identity there with `RelayHub` (token from `relay_token_for(node_id)`). Both
   happen before `identity` is sent.
2. **Dial.** The sender dials every direct hint and, if the receiver names a relay, the relay, all at once. Each attempt
   has 10 seconds. Each route that comes up is kept. A direct attempt completes the hybrid handshake with
   `handshake_initiator(..., expect_node_id=...)` and checks the public key against the one in `identity`.
3. **Session.** Each connection becomes a `Link` (`TcpLink` for direct, `RelayLink` for the relay) and all of them go into one
   `OverlaySession` (`spawn`). The session's services are `wormhole.control` and `wormhole.data`. The session must be up
   within 15 seconds.
4. **Control stream.** The sender opens `wormhole.control` and writes one JSON line:
   `{"kind", "name", "size", "sha256", "streams": N, "chunk": 262144}`. It must match the `offer` exactly, so the
   receiver rejects a mismatch.
5. **Data streams.** The sender opens `N` streams of `wormhole.data` (default 4, at most 8). Chunk `i` of `chunk` bytes goes
   to stream `i % N`. Each frame on a data stream is `offset (8 bytes, big-endian) | length (4 bytes) | payload`. Each
   stream ends with the overlay's end-of-stream (FIN). The sender stays within 8 MiB of the progress the receiver reports.
6. **Check.** The receiver writes each frame at its offset into a file in a hidden staging folder inside the download
   folder, and tracks which chunks have arrived. When all have arrived, it computes the SHA-256 of the whole file and
   compares it with the `offer`. A file that does not match is removed and the transfer reports `ok: false`. A file that
   matches is moved to a unique name in the download folder; a folder is unpacked from its checked archive.
7. **Done.** The receiver sends `done` with `ok` and the hash. The sender waits for it, then closes the session and the
   mailbox (`close`, mood `happy`).

### Using the overlay's features

- **Several routes at once.** Every route that comes up joins one session, and the overlay spreads the data over them. The
  overlay keeps counters per route (bytes sent, bytes sent again, round-trip time), exposed as
  `OverlayTransit.route_reports()`. The Wormhole screen does not show them yet.
- **Failover.** If a route is cut in the middle of a transfer, the chunks still in flight on that route are sent again on
  another one, and the transfer carries on from where it was. It does not start again from the beginning. This is tested
  (see "Tests" below): a route is cut after both routes have carried data, the file arrives intact, and the counters show
  bytes sent again.
- **No re-dial.** The sender dials once, when the transfer starts. A route that comes back later is not dialled again by
  the sender. The receiver does accept new inbound routes into the same session.
- **Parallel streams.** Each stream has its own flow control, so one slow stream does not hold up the others.
- **Identity pinning.** A direct connection is refused unless the handshake's node id and public key are the ones named in
  `identity` (tested with a stranger and a wrong node id). The relay path checks the same public key in `_dial_relay`; no
  test yet forces a relay connection with a wrong key.
- **Post-quantum keys.** The overlay's keys come from its own hybrid handshake, not from the PAKE. So a future break of the
  PAKE's discrete-log group would not expose the file contents recorded on the wire, although it could expose the
  identities that were exchanged.

### Limits

| Limit | Value |
|---|---|
| Text | 16 KiB of UTF-8 |
| Mailbox message | 64 KiB (`max_message_bytes`); 64 messages per mailbox |
| Mailbox requests per address | allocate: 30 at once, then 30 a minute; claim: 120 at once, then 2 a second; add: 200 at once, then 50 a second |
| Mailbox caps | 4096 nameplates; 1024 connections; 64 connections from one address |
| Connection not bound | closed after 30 seconds |
| Mailbox list | up to 4 addresses, tried in order |
| Unclaimed nameplate | expires after 5 minutes |
| Idle mailbox | expires after 1 hour |
| Chunk | 256 KiB |
| Streams | 1 to 8 (default 4; set in Settings) |
| Data in flight | the sender stays within 8 MiB of the receiver's reported progress |
| Wait for the other side's `pake` | 60 s |
| Wait for `version`, `identity` and `offer` | 120 s each |
| Wait for accept or decline | 600 s |
| Dial each direct address, and the relay | 10 s each |
| Session up | 15 s |
| Receiver waits for the sender's connection after accepting | 300 s |
| Transfer with no data arriving | 120 s |
| Folder | 10,000 entries; 10 GiB unpacked; a compression ratio over 100 refused after the first MiB |

## Folders and safe unpacking

A folder is zipped to a temporary file before the offer. The receiver unpacks it only after the hash is checked, and it
refuses any archive that:

- has an entry whose path is absolute, has a drive letter, or contains `..` after normalising;
- has a symbolic link, a device, or any entry that is not a file or a directory;
- has more entries than the offer named, more than 10,000 entries, or a total unpacked size over 10 GiB, or whose unpacked
  size is more than 100 times its compressed size once the first MiB has been read.

Unpacking writes into a new folder under the download folder, with a unique name. A failed unpack leaves nothing behind.

## Deviations from magic-wormhole

| Area | magic-wormhole | Transferd Wormhole |
|---|---|---|
| Wire compatibility | the reference | not compatible; its own appid and mailbox name |
| Code | 2 words | 3 words |
| Key derivation | HKDF over SHA-256 | BLAKE3 derive-key |
| Message encryption | NaCl SecretBox | AES-256-GCM with the side and phase in the additional data |
| Per-direction keys | one key per phase | one key per side per phase, so a message cannot be reflected |
| Verifier | a readable code to compare | none; the `version` phase is the confirmation |
| Identity | none (anonymous) | an ephemeral hybrid identity, sent under the phase keys |
| Transit | magic-wormhole's own relay and direct protocol | the Transferd overlay: hybrid handshake, several routes, resends, streams |
| Consent | the offer is shown, then accepted | the offer is shown, and nothing is sent before `accept` |
| Text | sent over the transit | sent in the mailbox, never over the transit |

## Security

- **Guessing the code.** The secret is 24 bits (three words). A guess is a live exchange on the nameplate. The mailbox can
  count how many attempts ended with the mood `scary`, but it cannot tell which guess was wrong from the messages. A
  nameplate takes at most two sides, and an unclaimed one expires after 5 minutes. A code is for one transfer.
- **Offline attacks.** SPAKE2 means a recorded exchange gives nothing to check guesses against.
- **The mailbox.** It can refuse, delay, drop or replay messages, but it cannot read or change them, because every message
  is sealed with keys it does not have. A replay is caught by the message id and by the phase order.
- **The relay.** It sees encrypted overlay traffic and the node ids it registers. The relay token is derived from the node
  id, as in the rest of Transferd.
- **Man in the middle.** Someone who sits between the two sides and does not know the code cannot pass `version`. Someone
  who knows the code can be in the middle of the PAKE, but the code is then shared, and the attempt is the one the user
  made.
- **Keys left behind.** Ephemeral identities and phase keys are kept in memory for one attempt. The attempt's identity is
  not written to disk.
- **The mailbox's certificate.** Its private key sits in the wormhole folder, made owner-only where Windows allows it, and
  otherwise under the folder's own permissions. The fingerprint is not secret: it is shown and shared on purpose.
- **Denial of service.** Rate limits and caps stop one computer, or a few, from filling a mailbox. They do not stop a large
  number of addresses. A mailbox on the open internet should sit behind a firewall that limits who can reach it.
- **Plain connections.** The app never sends a message over a plain connection to another computer. A `ws://` address
  other than this computer is refused when it is read, and again before connecting.
- **Downloads.** A received file has a unique name, is staged in a hidden folder until its hash is checked, and is never
  placed outside the download folder.

## Tests

Run from `gui\`:

```
.venv\Scripts\python.exe -m pytest tests\test_wormhole_*.py -q
```

| File | What it covers |
|---|---|
| `test_wormhole_codes.py`, `test_wormhole_keys.py` | the code format, the word parity check, the key schedule, and AES-GCM with the side, phase and id bound |
| `test_wormhole_mailbox.py` | meeting on a nameplate, replay, acknowledgements, reconnection, limits, crowding, expiry (against the real server) |
| `test_wormhole_limits.py` | every refusal has its code; the rate limit and the time to wait; a client waits out a limit and carries on; a refused add is sent again; the connection, address and nameplate caps; the bind timeout |
| `test_wormhole_tls.py` | a pinned mailbox meets over `wss://`; a wrong pin is refused before anything is sent; a mailbox with no pin must have a certificate the system trusts; plain `ws://` to another computer is refused; the identity is kept, or made again when it no longer matches or is near its end; retries while a mailbox starts; a mailbox that is down is passed over; a reconnect stays on one mailbox; the command refuses the network without TLS |
| `test_wormhole_addresses.py` | the address and list rules, the defaults, the fingerprint forms, and each refusal with its reason |
| `test_wormhole_host.py` | the settings for the list and hosting; the hosted mailbox starts, stops, reports its state, serves `wss://` pinned to its own certificate, keeps its identity across restarts, and reports a port in use; a wormhole with no list meets on the hosted mailbox, end to end |
| `test_wormhole_protocol.py` | every message's fields, types and sizes; the versions that are refused |
| `test_wormhole_transfer.py` | the folder zip, the safe unpack (paths, links, entry and size limits) and the placing of files |
| `test_wormhole_session.py` | whole attempts with an in-memory transit: text, file, folder, decline, wrong code, timeouts, tampering |
| `test_wormhole_endpoint.py` | the overlay transit on 127.0.0.1: direct files, parallel streams with progress pacing, empty files, dial and identity checks |
| `test_wormhole_screen.py` | the Wormhole screen, offscreen, with two real windows and a real mailbox: a text sent and received; decline; stop while an offer waits; a wrong code on both sides; cancel and a new send; the mailbox card while hosting on this computer, on the network (fingerprint and share line), and after turning hosting off; a port in use; saving a bad mailbox list and the hosting choice in Settings |
| `test_wormhole_e2e.py` | a route cut mid-transfer (resends and completion); the mailbox connection cut before the other side arrives; a relay transfer (needs `TRANSFERD_RELAYD`) |

The relay tests need the built relay. Set `TRANSFERD_RELAYD` to `relayd.exe` and they run; without it they are skipped.

## Open decisions

- **Mailbox default.** Magic-wormhole's public mailbox is a third party that would see every nameplate, so this build ships
  with no public default. A wormhole needs a mailbox: one listed in Settings, or this computer's own when it hosts one.
  Hosting in the app is the simple route for computers under one owner. A mailbox on a server the user controls is the
  route for people who meet elsewhere. Defaulting to `wss://mailbox.magic-wormhole.io` would work out of the box, but it
  gives a third party the metadata.
- **Relay.** The relay used for transit is the one in Settings, the same relay as the rest of the app. Files above about
  1 MiB through the relay do not complete yet; see "Known limits".

## Known limits

- **Large files over the relay stall.** With the receiver offering no direct address, a file through the relay completes
  at 16 KiB, 65 KiB, 500 KiB and 1 MiB in about a second. At 1.5 MiB and 2 MiB it stops: the receiver gets no data after
  the first burst, and the sender's route reports a loss of 1.0 and about 6 MB sent again. Cutting the relay's forwards in
  flight from 128 to 8 did not change it. The cause is not found yet. The stall is on the relay path, in the library's
  relay client or in `relayd`, not in the wormhole code. The reproduction is
  `test_wormhole_e2e.py::test_a_file_of_two_mib_moves_through_the_relay`. It is marked as an expected failure with
  `strict=True`, so when it is fixed the test fails until the mark is removed.
- **No route re-dial.** A route that is cut is not dialled again by the sender (see "Using the overlay's features").
- **No progress bar or route table in the screen.** The screen shows the state, not the number of bytes sent.
- **No public default mailbox.** A wormhole does nothing until a mailbox is listed, or this computer hosts one.
- **The fallback needs the same reachability.** The two sides meet on the first mailbox each can reach. If one side can
  reach a mailbox the other cannot, they wait on different servers and the meeting times out.
- **Shared rate limits.** Rate limits are per IP address, so computers behind one router share one allowance. On a busy
  network a client can see `rate_limited`; it waits and carries on.
- **Hosting behind a router.** A hosted mailbox is reachable from the local network. Reaching it from the internet needs a
  port forward on the router, which the app does not set up.
- **A renewed certificate changes the fingerprint.** The other computers must then be given the new one. The certificate is
  renewed only near its end, or when its files are missing or do not match.
- **Not on the wide area.** Every test runs on 127.0.0.1.

## Not verified

- A real network with loss, reordering and a real cable: tests use loopback, and a proxy that cuts connections on purpose.
- A real wide-area mailbox: tests run the mailbox on 127.0.0.1.
- Relay transfers above about 1 MiB (the known stall above).
- A relay connection with a wrong public key (the check is in the code; no test forces it).
- The window's look, and a person using the screen: offscreen tests build the screen and drive its buttons, but do not show a
  window, and the file pickers are not opened.
- The hosted mailbox across a real network: its tests bind to 127.0.0.1 through an injected address, so the bind to all
  interfaces, the address in the share line, and the Windows Firewall prompt were not exercised.
- A `wss://` mailbox with a certificate from a public authority. The path is in the code, and a self-made certificate is
  refused without a pin, but no test serves a publicly trusted certificate.
- A man in the middle. The pin check is tested with a fingerprint that the certificate does not match, not with an
  attacker who intercepts the connection.
- The rate limits and caps under load. Tests set small limits and use one or two clients, not a crowd.
- A phone or any other device.
