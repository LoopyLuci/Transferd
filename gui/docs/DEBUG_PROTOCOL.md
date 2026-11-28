# Hybrid debugging over Transferd

Status: version 1, implemented in `gui/transferd_gui/debug` (Python). The Android agent is not part of this build.

## 1. What this is

A way to debug a mobile device from a computer, built on Transferd's overlay. Debugging runs over authenticated,
encrypted Transferd sessions, and the same session can use a USB cable and Wi-Fi (or the relay) at once. When one
route goes away, the session carries on over the others, and the debugging streams keep running.

It is not the Android Debug Bridge (ADB), and it does not speak ADB's wire format. It uses ADB only as one way to carry
a TCP connection over USB (`adb forward`). The rest is Transferd:

| Concern | ADB | This protocol |
|---|---|---|
| Who may connect | An RSA key confirmed once on the device | A Transferd pairing (invite) plus an explicit allowlist of debugger node ids on the device |
| Encryption | None over TCP or USB | Every route is encrypted and authenticated (hybrid X25519 + ML-KEM, Ed25519 + ML-DSA) |
| Cable pulled out | The session drops and must be rebuilt by hand | Streams move to the Wi-Fi or relay route and continue |
| Per-operation control | All-or-nothing | Each operation (info, logcat, shell, forward) is allowed, asked for, or denied |
| Audit | None | Every operation is written to the device's event log, with the debugger's node id |

## 2. Roles

- **Client**: the debugging computer. It opens debug streams to a device and shows what comes back.
- **Agent**: the device. It answers debug streams, and enforces the policy below. Any Transferd node can be an agent; the
  GUI node is one when "Allow debugging" is on in Settings.

The roles are per node, so one node can be both.

## 3. Routes and transports

A debug session is one Transferd session to the device. Its routes are:

- **Wi-Fi**: a direct connection to the device's listener, or the relay. Labelled by its address.
- **USB**: `adb forward tcp:L tcp:P` makes the device's listener (port `P`, on the device) reachable at `127.0.0.1:L`
  on the computer. The client dials that address with the same handshake as any direct route, and adds it to the
  session as a second route. Labelled `usb:<serial>`.

Routes are attached to one session, so the overlay chooses between them by measured cost, and it resends unacknowledged
frames over a surviving route when one fails. A debug stream is an overlay stream, so it survives a route change without
the application noticing.

The **transport mode** (Settings) decides which routes the client attaches:

- `auto` (default): attach USB when the device is connected over USB, and keep Wi-Fi as well. Reattach USB after the
  cable is plugged back in.
- `usb_only`: attach only USB.
- `wifi_only`: never run `adb`.

## 4. Trust and consent

Nothing is reachable by default.

1. **Pairing.** The device and the computer must be paired by Transferd invite first. The handshake verifies the peer's
   keys, so a debugger is always a known node.
2. **Debugger allowlist (device).** The device answers debug streams only from node ids in its `debug clients` list.
   Any other node is refused with `denied`, whatever the per-operation modes say.
3. **Per-operation mode (device).** Each operation is one of:
   - `allow`: run without asking;
   - `ask`: the device's user accepts or refuses each stream in the app. A request nobody answers is refused when the
     opener gives up (the overlay's open timeout);
   - `deny`: always refused.
   Defaults: `info` allow; `logcat`, `shell` and `forward` ask.
4. **Forward ports (device).** A forward is allowed only to a port in the device's `forward ports` list, and only to
   `127.0.0.1` on the device. Empty list: no forwards.
5. **Shell.** Off unless `shell` is set to `allow` or `ask`. It runs the configured shell with the agent's own rights,
   with a timeout and an output limit. There is no stdin.
6. **Audit.** Every stream the device gets, and its outcome, is written to the event log: who asked, which operation,
   and whether it was allowed, asked for, refused or ended.

## 5. Streams and messages

Each operation is an overlay stream to a service named `debug-<op>`, where `<op>` is `info`, `logcat`, `shell` or
`forward`. The service names have no dots, so a Rust node can expose them too (its `expose` rejects dots).

### 5.1 Request

The client's first bytes on the stream are one JSON object and a newline:

```json
{"v": 1, "args": {...}}
```

The request is at most 4096 bytes. `args` depends on the operation (section 5.2). Unknown keys in `args` are ignored.

### 5.2 Operations

| Operation | `args` | After `ok` |
|---|---|---|
| `info` | none | one `info` message, then the end |
| `logcat` | `filter` (text, optional) | `line` and `drop` messages until the client ends the stream |
| `shell` | `command` (text, required), `timeout` (seconds, optional, at most 600) | `out` messages, then one `exit` message, then the end |
| `forward` | `port` (1 to 65535) | raw bytes in both directions until either side ends |

### 5.3 Responses

Every operation begins with the agent's first message, which is either:

- `{"t": "ok"}`: the operation was allowed and started; or
- `{"t": "error", "code": "<code>", "text": "<what happened>"}`, followed by the end of the stream.

Later messages are one JSON object per line, with a `t` field:

| `t` | Fields | Meaning |
|---|---|---|
| `info` | `props` (object) | Device properties: node name, node id, platform, Python version, protocol version, policy summary |
| `line` | `text` (string, at most 4096 characters; longer lines are cut) | One line of log output |
| `drop` | `count` (integer) | `count` lines were not sent because the client was not reading fast enough |
| `out` | `fd` (1 or 2), `b64` (base64 bytes) | Shell output |
| `exit` | `code` (integer or null), `reason` (`exited`, `timeout` or `output_limit`) | End of a shell command |
| `error` | `code`, `text` | The operation failed after it started |

Receivers ignore messages with a `t` they do not know, and fields they do not know. That is how later versions add
things without breaking version 1.

Error codes: `denied` (refused by the device's allowlist or by its user), `disabled` (debugging is off on the device),
`not_allowed` (a port or command outside the policy), `bad_request`, `unsupported_version`, `unavailable` (for example,
no log command is set on the device), `failed` (it could not start, for example the port refused the connection),
`too_many` (the client has too many streams open), `timeout`.

### 5.4 Limits

| Limit | Value |
|---|---|
| Request | 4096 bytes |
| One message line | 64 KiB |
| Shell command | 2048 bytes |
| Shell output | the device's `shell output limit` (default 256 KiB), then `exit` with `output_limit` |
| Shell time | the device's `shell timeout` (default 60 s), at most 600 s |
| Logcat queue | 1024 lines; more are dropped and counted in `drop` |
| Open streams per client | the device's `streams per client` (default 8) |

## 6. Lifecycle

1. The client opens `debug-<op>` on the session. The device checks the client's node id, the operation's mode and
   the arguments. It answers the open with allow, ask (pending in the device's app), or refuse.
2. If allowed, the agent sends `ok` and runs the operation.
3. The client may end the stream at any time. The agent stops the operation: a logcat reader stops, a shell command is
   killed, and a forward closes its connection.
4. If the session loses every route, the streams end with an error, as any overlay stream does. Losing one route does
   not end them.

## 7. Versioning

The request carries `v`. Version 1 is the only version. An agent that does not speak the version answers
`unsupported_version`. New operations are new services; new message types and fields are additive (section 5.3).

## 8. Not in this build

- The Android agent (an app that runs the device side). The agent in `gui/transferd_gui/debug/agent.py` runs on any
  machine with Python and Transferd, which is enough to test the protocol and to act as a device on a computer.
- File sync, screenshots, and process lists. They fit the same pattern and are not built yet.
- Any use of the ADB server other than `adb devices` and `adb forward`.
