# Transferd desktop app

A desktop window for a Transferd node. It runs one node in the background, shows what that node is doing, and lets
you send and receive files and streams, make and accept invites, and inspect identity, relay, peers and ghost keys.

It is built on the Python library in `..\python\transferd`. That library is used as it is: nothing under `python\`,
`typescript\`, `crates\` or `vectors\` is changed by this folder.

## Setup

Run these from this folder (`H:\Projects\Transferd\gui`). Packages are installed only into `.venv` here.

```
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## Run

```
.venv\Scripts\python.exe run.py
```

Settings are saved to `%APPDATA%\Transferd\settings.json`. The node's data (identity, received files, ghost key files)
is kept in `%APPDATA%\Transferd\data` unless the data directory is changed in Settings. Set `TRANSFERD_GUI_HOME` to move
both, which is how the tests keep their files out of your profile.

## Tests

```
.venv\Scripts\python.exe -m pytest tests -q
.venv\Scripts\python.exe -m pyflakes transferd_gui tests run.py
```

The tests run offscreen (`QT_QPA_PLATFORM=offscreen` is set by `tests\conftest.py`). They cover:

- the model layer: settings validation and saving, the bounded event log, ring buffers, rate and telemetry recording,
  and formatting (`test_settings.py`, `test_model_basics.py`);
- the backend: the file header, safe file names, the backend thread, the capability list, the ghost key flow with a real
  issuer (`test_files_and_worker.py`, `test_ghost.py`);
- the node adapter against the real library, with two nodes on 127.0.0.1: dialling, sessions, route counters, file accept,
  refuse and auto-accept, streams, invites, disconnect, the transfer limit, settings applied to a live node
  (`test_node_loopback.py`);
- the service facade: start, settings saved and refused, failed actions logged and handed back (`test_service.py`);
- every screen, offscreen, in an empty state and in a populated state where a real peer is connected, a file has moved and
  an invite has been made (`test_qt_screens.py`);
- the debug protocol without a network: the wire format, the device policy, and the agent and client over in-memory
  channels, with real shell commands, a real loopback forward and a real log process (`test_debug_protocol.py`);
- debugging between real nodes on 127.0.0.1: the device answers under its policy, consent for shell commands, forwards,
  the device refusing unlisted computers, and the main claim: a second route joins the session and a log stream survives
  the USB route being cut, with every line arriving once and in order (`test_debug_node.py`). The adb program is a fake
  there, and the test says so;
- the Debug screen and the debugging settings, offscreen, with the real window and service (`test_debug_screen.py`);
- the wormhole (`test_wormhole_*.py`, described in `docs\WORMHOLE.md`): the codes and keys, the mailbox against the real
  server, its limits and refusal codes, TLS and pins, the mailbox list, a hosted mailbox, the message checks, the folder archive and its safe unpacking, whole attempts in memory, the overlay transit on
  loopback, the Wormhole screen with two real windows, and end-to-end runs with a cut connection, a cut route and the relay.

The relay tests need the built relay (`test_node_loopback.py` and the relay case in `test_wormhole_e2e.py`). Point
`TRANSFERD_RELAYD` at it and they run; without it they are skipped. One wormhole relay test is an expected failure for a
known stall above about 1 MiB (see "Known limits" in `docs\WORMHOLE.md`):

```
set TRANSFERD_RELAYD=H:\Projects\Transferd\target\debug\relayd.exe
.venv\Scripts\python.exe -m pytest tests -q
```

Offscreen tests do not show a window on a display, so they do not check how the screens look. See "Not verified" below.

## Screens

Navigation is in the left sidebar, in five sections.

- **Overview**: Dashboard. The node name and id (copyable), the listener, the relay state with its next renewal, the
  peers, the ghost key status, the sessions and routes tables, and two charts (round-trip time and traffic per route).
- **Network**: Identity (node id, fingerprint, public keys, relay token, each copyable); Relay (connect and disconnect,
  registration counts, locate a peer); Peers (sessions, disconnect, accept incoming, dial a peer by address or through the
  relay); Invites (make an invite, copy it, check one, connect to its inviter).
- **Transfer**: Transfer (send a file to a ready peer, open a stream and read its reply, accept or refuse incoming
  requests, history); Wormhole (send a file, a folder or a text with a short code, or type a code to receive it; needs a
  mailbox, listed in Settings or hosted on this computer, and is disabled until there is one); Hole punch (locate, with the start button disabled).
- **Tools**: DNS lookup (disabled); Ghost keys (create the key, load the issuer's key, make the request, finish with the
  answer, reset).
- **Debug**: Debugging. Choose the device (a connected peer, or paste its node id) and see its state. Connect a route over
  Wi-Fi or USB; the routes it has are listed with their bytes. Ask the device for its information; start and stop its log,
  with an optional filter; run a shell command with a time limit and see its output and exit code; forward a port to the
  device; list every operation running now and stop one. Requests from computers wait here to be accepted or refused.
- **System**: Events (the bounded log, filtered by level, clearable); Settings (every setting, validated; a value that
  does not check out is refused with a message and nothing is saved). Settings has two debugging groups: what listed
  computers may do with this device (on/off, the list of computer ids, allow/ask/deny for each operation, forward ports,
  the log command, the command limits), and this computer's side (the device to debug, transport, Wi-Fi address, adb
  program, USB device port). A Wormhole group sets the mailboxes a wormhole meets on (a list, tried in order), whether this
  computer hosts a mailbox and on which port, whether other computers may reach it, and how many data streams a wormhole
  file uses (1 to 8).

The debug protocol is specified in `docs\DEBUG_PROTOCOL.md`. It runs over the overlay's services `debug-info`,
`debug-logcat`, `debug-shell` and `debug-forward`, so it needs nothing from the relay or the library beyond sessions and
streams.

Empty states say what is missing and what to do. Telemetry that has not been measured shows "no data yet"; no value is
invented.

## Not available in this build

These features are visible and disabled, each with the reason. The reasons are in
`transferd_gui\backend\capabilities.py`.

- **Start a hole punch.** The node orchestration that starts a punch (`start_punch`, `on_punch`, `finish_punch`) is not
  ported to `python\transferd`. The probe table and the CANDIDATE frame exist, but nothing drives them from a session.
- **Mesh DNS.** Mesh DNS is in the Rust `transferd-dns` crate. `python\transferd` has no resolver, DNS client or DNS
  service to call.
- **Register with a ghost key.** `RelayClient.register` takes no ghost key certificate, so a registration cannot prove a
  ghost key in this build.
- **A custom relay token.** The protocol derives the relay token from the node id. No custom token is defined.
- **A chosen relay UDP port.** `RelayClient` binds an ephemeral UDP port, and the library cannot be told which one.
- **Session timeouts.** `OPEN_TIMEOUT` and `HANDSHAKE_TIMEOUT` are module constants in the library and cannot be changed
  from the GUI.

## Notes

- Charts are drawn with QPainter, not pyqtgraph, to keep one dependency fewer; there are three simple time series.
- The file and stream conventions are this GUI's own, documented in `transferd_gui\backend\files.py`. A file is one JSON
  header line (`name`, `size`), then the bytes, then the end of the stream, sent on the service `file`. Other streams use the
  service `stream`. A peer that does not speak this convention will receive a stream it cannot read.
- An incoming file or stream waits for the user to accept it, unless "accept incoming files without asking" is set.
- A file being sent is read from disk in chunks. The GUI records that the bytes were handed to the session; it does not
  get a delivery confirmation from the library.
- The identity seed is stored in plain form in `identity.seed` under the data directory. It is written owner-only where
  Windows allows, which on Windows means the folder's own permissions apply.
- Received files are written to the download folder under a unique name; a file that does not finish is removed.

## Not verified

- The window was not inspected visually. Offscreen tests build every screen and check its state, but they do not show a
  window, so layout, colours and the look of the charts are unchecked.
- The window was not driven by a person. Button clicks in the tests are programmatic.
- The relay tests run only when `TRANSFERD_RELAYD` is set to a built `relayd.exe`.
- Wormhole: a relay transfer above about 1 MiB stalls (a known issue, see `docs\WORMHOLE.md`); the mailbox, its TLS and its hosting were tested only
  on 127.0.0.1, with a self-made certificate; the Wormhole screen was driven by its buttons in tests, not by a person, and its file pickers were not
  opened.
- Debugging has not been run with a real phone. No phone, cable or Android device was available. The Android agent
  (an app that runs the device side on Android) is not built; the device side is this Python app, which runs on any
  machine with Transferd. The USB route is tested with a fake adb program and with a second loopback connection that
  stands in for `adb forward`. The real `adb forward` and the real cable were not exercised.
- The debug log and shell output were checked with commands on this computer, not on a device's real `logcat`.
