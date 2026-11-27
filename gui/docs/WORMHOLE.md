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
- **No mailbox.** Until a mailbox address is set in Settings → Wormhole, the screen says so and every button is disabled.

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

Its address is set in Settings. There is no default server: the wormhole is off until a mailbox is set (see "Open
decisions").

### Messages

Every message is one JSON object in a WebSocket text frame. The client sends these:

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
| `welcome` | `limits`: `max_message_bytes` (65536), `max_messages_per_mailbox` (64); sent on connect |
| `opened` | `mailbox`; sent after the stored messages of an `open` |
| `added` | `id`; a message is stored (also sent again for a retry of a stored message) |
| `allocated`, `claimed`, `released`, `closed` | the answers to `allocate`, `claim`, `release` and `close` |
| `message` | `mailbox`, `id`, `side` (the sender), `phase`, `body` |
| `error` | `error` (a short reason), `orig` (the message type it answers), and `id` when it refuses one `add` |

Rules the server keeps:

- An unclaimed nameplate expires after 5 minutes.
- A nameplate has at most two sides. A third `claim` gets `crowded`.
- A mailbox expires after one hour with no traffic, or when both sides have sent `close`.
- A message the other side has not acknowledged is kept and sent again when that side opens the mailbox again. This is how
  a dropped connection does not lose a message.
- The server never echoes a message to the side that sent it.
- A message over `max_message_bytes`, or past `max_messages_per_mailbox`, is refused with `error`.

### Running a mailbox

```
.venv\Scripts\python.exe -m transferd_gui.wormhole.server --host 0.0.0.0 --port 4470
```

It is meant to run on a server the user controls. The mailbox sees the nameplate, the side ids, the phase names, the size
and timing of each message, the mood of each close, and the IP address of each client. It does not see the code, the keys
or the content.

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
- **Downloads.** A received file has a unique name, is staged in a hidden folder until its hash is checked, and is never
  placed outside the download folder.

## Tests

Run from `gui\`:

```
.venv\Scripts\python.exe -m pytest tests\test_wormhole_codes.py tests\test_wormhole_keys.py tests\test_wormhole_mailbox.py tests\test_wormhole_protocol.py tests\test_wormhole_transfer.py tests\test_wormhole_session.py tests\test_wormhole_endpoint.py tests\test_wormhole_screen.py tests\test_wormhole_e2e.py -q
```

| File | What it covers |
|---|---|
| `test_wormhole_codes.py`, `test_wormhole_keys.py` | the code format, the word parity check, the key schedule, and AES-GCM with the side, phase and id bound |
| `test_wormhole_mailbox.py` | meeting on a nameplate, replay, acknowledgements, reconnection, limits, crowding, expiry (against the real server) |
| `test_wormhole_protocol.py` | every message's fields, types and sizes; the versions that are refused |
| `test_wormhole_transfer.py` | the folder zip, the safe unpack (paths, links, entry and size limits) and the placing of files |
| `test_wormhole_session.py` | whole attempts with an in-memory transit: text, file, folder, decline, wrong code, timeouts, tampering |
| `test_wormhole_endpoint.py` | the overlay transit on 127.0.0.1: direct files, parallel streams with progress pacing, empty files, dial and identity checks |
| `test_wormhole_screen.py` | the Wormhole screen, offscreen, with two real windows and a real mailbox: a text sent and received; decline; stop while an offer waits; a wrong code on both sides; cancel and a new send |
| `test_wormhole_e2e.py` | a route cut mid-transfer (resends and completion); the mailbox connection cut before the other side arrives; a relay transfer (needs `TRANSFERD_RELAYD`) |

The relay tests need the built relay. Set `TRANSFERD_RELAYD` to `relayd.exe` and they run; without it they are skipped.

## Open decisions

- **Mailbox default.** Magic-wormhole's public mailbox is a third party that would see every nameplate. This build ships
  with no default. The app refuses to start a wormhole until a mailbox is set in Settings, and says so. The alternative
  is to default to `wss://mailbox.magic-wormhole.io`; that works out of the box but gives a third party the metadata.
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
- **No default mailbox.** The wormhole does nothing until one is set.
- **Not on the wide area.** Every test runs on 127.0.0.1.

## Not verified

- A real network with loss, reordering and a real cable: tests use loopback, and a proxy that cuts connections on purpose.
- A real wide-area mailbox: tests run the mailbox on 127.0.0.1.
- Relay transfers above about 1 MiB (the known stall above).
- A relay connection with a wrong public key (the check is in the code; no test forces it).
- The window's look, and a person using the screen: offscreen tests build the screen and drive its buttons, but do not show a
  window, and the file pickers are not opened.
- A phone or any other device.
