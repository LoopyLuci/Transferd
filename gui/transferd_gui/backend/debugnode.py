"""The debugging part of the node: the device's agent, the computer's client operations, and the routes that carry them.

`Node` inherits from `DebugNodeMixin`, so everything here runs on the node's asyncio loop and reads the node's own state
(peers, settings, the event log). The protocol lives in `transferd_gui.debug`; this module connects it to overlay
sessions and to the USB bridge.

Two roles, one node:

* as a device (the agent), it answers debug streams from the computers on its debugger list, under its policy;
* as a computer (the client), it runs operations against the device it has chosen, over Wi-Fi, the relay, and USB.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from transferd.errors import Denied, NoSuchService, TransferdError, Unreachable
from transferd.frames import ServiceInfo
from transferd.handshake import handshake_initiator
from transferd.link import Link, RouteKind, TcpLink
from transferd.overlay import OverlaySession

from transferd_gui.backend.errors import NodeError, describe
from transferd_gui.backend.files import FILE_SERVICE, STREAM_SERVICE
from transferd_gui.debug import wire
from transferd_gui.debug.agent import DebugAgent
from transferd_gui.debug.client import DebugClient, DebugError, bridge
from transferd_gui.debug.policy import DebugPolicy, parse_clients, parse_ports
from transferd_gui.debug.providers import SystemProviders
from transferd_gui.debug.stream import Channel, ChannelReset
from transferd_gui.debug.usb import Adb, AdbDevice, AdbError, free_local_port
from transferd_gui.model.settings import Settings, parse_host_port
from transferd_gui.model.snapshot import DebugSnapshot, DebugStreamSnapshot, UsbDeviceSnapshot

SOURCE_DEBUG = "debug"
#: How often the USB bridge looks for the chosen device.
USB_SCAN_S = 5.0
#: How long the USB bridge waits after a failed attempt before it tries again.
USB_RETRY_S = 30.0
#: Finished debug streams kept for the Debug screen.
FINISHED_KEPT = 20


class OverlayChannel:
    """A debug channel over one overlay stream of a session.

    `end` is a half-close: an empty send with FIN, so the other side still reads what it is sending back (a forward's
    reply, for example). `close` releases the stream fully. The overlay closes a stream by flushing it first, so `reset`
    cannot skip the flush; every debug error is therefore sent in-band, as an `error` message before the end.
    """

    def __init__(self, session: OverlaySession, stream: int) -> None:
        self._session = session
        self._stream = stream
        self._sent_end = False
        self._closed = False

    async def read(self) -> Optional[bytes]:
        if self._closed:
            return None
        try:
            return await self._session.recv(self._stream)
        except Exception as exc:
            raise ChannelReset(describe(exc)) from exc

    async def write(self, data: bytes) -> None:
        try:
            await self._session.send(self._stream, data)
        except Exception as exc:
            raise ChannelReset(describe(exc)) from exc

    async def end(self) -> None:
        if self._sent_end:
            return
        self._sent_end = True
        try:
            await self._session.send(self._stream, b"", fin=True)
        except Exception:
            pass  # the session is gone; there is nothing left to end

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._session.close(self._stream)
        except Exception:
            pass  # already gone; closing a finished stream is not an error

    async def reset(self, reason: str) -> None:
        await self.close()


@dataclass
class _Handle:
    """One debug operation this node is running as a computer."""

    handle: int
    peer_id: str
    kind: str
    detail: str
    started_at: float
    state: str = "running"
    note: str = ""
    task: Optional[asyncio.Task] = None
    server: Optional[asyncio.base_events.Server] = None
    tasks: Set[asyncio.Task] = field(default_factory=set)


def policy_from_settings(settings: Settings) -> DebugPolicy:
    """The device policy the settings describe. Settings are validated before they reach here."""
    return DebugPolicy(
        enabled=settings.debug_enabled,
        modes={
            "info": settings.debug_info_mode,
            "logcat": settings.debug_logcat_mode,
            "shell": settings.debug_shell_mode,
            "forward": settings.debug_forward_mode,
        },
        clients=parse_clients(settings.debug_clients),
        forward_ports=parse_ports(settings.debug_forward_ports),
        shell_timeout_s=settings.debug_shell_timeout_s,
        shell_output_limit=settings.debug_shell_output_kib * 1024,
        streams_per_client=settings.debug_streams_per_client,
    )


class DebugNodeMixin:
    """The debugging methods of `Node`. Relies on the node's `_settings`, `_peers`, `_pending`, `_log`, `_spawn`,
    `_start_peer`, `_observe_link`, `_require_ready_peer`, `_await_ready` and `identity`."""

    # ----- setup and state ----------------------------------------------------------------------

    def _debug_setup(self) -> None:
        self._agent: Optional[DebugAgent] = None
        self._debug_handles: Dict[int, _Handle] = {}
        self._next_handle = 1
        self._usb_state: Tuple[str, str, Tuple[AdbDevice, ...]] = ("not checked", "", ())
        self._usb_retry_at = 0.0

    def _debug_start(self) -> None:
        """Start the USB bridge. Called when the node starts."""
        self._spawn(self._usb_loop())

    async def _debug_stop(self) -> None:
        """Stop every debug operation this node runs as a computer, and close their listeners."""
        for handle in self._debug_handles.values():
            if handle.server is not None:
                handle.server.close()
            for task in list(handle.tasks):
                task.cancel()
            if handle.task is not None:
                handle.task.cancel()
            if handle.state == "running":
                handle.state = "stopped"

    def _debug_policy(self) -> DebugPolicy:
        return policy_from_settings(self._settings)

    def _debug_audit(self, level: str, text: str) -> None:
        self._log(level, SOURCE_DEBUG, text)

    def _agent_instance(self) -> DebugAgent:
        if self._agent is None:
            identity = self.identity
            providers = SystemProviders(
                node_name=identity.name,
                node_id=identity.node_id,
                log_command=self._settings.debug_log_command,
            )
            self._agent = DebugAgent(self._debug_policy, providers, self._debug_audit)
        return self._agent

    def _advertised_services(self) -> List[ServiceInfo]:
        """What this node offers peers: file and stream transfer, and the debug operations while debugging is on."""
        services = [ServiceInfo(name=FILE_SERVICE, description="files"), ServiceInfo(name=STREAM_SERVICE, description="streams")]
        if self._settings.debug_enabled:
            services += [ServiceInfo(name=wire.service_name(op), description=f"debugging: {op}") for op in wire.OPS]
        return services

    def _debug_settings_changed(self, old: Settings, new: Settings) -> None:
        """Called from `apply_settings`: a changed log command needs a new agent; the rest is read at each stream."""
        if old.debug_log_command != new.debug_log_command or old.debug_shell_timeout_s != new.debug_shell_timeout_s:
            self._agent = None

    async def _debug_readvertise(self) -> None:
        """Tell every live peer what this node now offers, after debugging was turned on or off."""
        for peer in list(self._peers.values()):
            await peer.session.advertise(self._advertised_services())

    # ----- the device side ----------------------------------------------------------------------

    def _debug_incoming(self, request_id: int) -> None:
        """A debug stream was asked for: allow it, refuse it, or leave it waiting for the user's answer."""
        pending = self._pending[request_id]
        op = wire.op_of(pending.service)
        decision = self._agent_instance().decide_open(op, pending.peer_id)
        if decision.outcome == "refuse":
            self._pending.pop(request_id, None)
            self._spawn(pending.session.authorize(pending.stream, pending.service, False, decision.reason))
            self._debug_audit("warning", f"refused debug {op} from {pending.peer_id[:12]}: {decision.reason}")
        elif decision.outcome == "allow":
            self._spawn(self.accept_request(request_id))
        else:
            self._debug_audit("info", f"{pending.peer_id[:12]} asks for debug {op}; accept or refuse it on the Debug screen")

    async def _serve_debug(self, pending) -> None:
        """Run an accepted debug stream on the agent. The agent answers every outcome on the stream itself."""
        channel = OverlayChannel(pending.session, pending.stream)
        await self._agent_instance().serve(wire.op_of(pending.service), pending.peer_id, channel)

    # ----- the computer side: opening streams ---------------------------------------------------

    async def _open_debug_channel(self, peer_id: str, service: str) -> Channel:
        peer = self._require_ready_peer(peer_id)
        if service not in [s.name for s in peer.session.services]:
            raise NodeError("the device does not offer debugging; turn debugging on in its Settings (Debugging)")
        try:
            stream = await peer.session.open(service)
        except Denied as exc:
            raise NodeError("the device did not allow it: its debugging is off, this computer is not on its list, or its user refused") from exc
        except NoSuchService as exc:
            raise NodeError("the device does not offer this debug operation") from exc
        except Unreachable as exc:
            raise NodeError("the device did not answer in time; if it asked its user, the request was not accepted") from exc
        except TransferdError as exc:
            raise NodeError(f"could not open the debug stream: {describe(exc)}") from exc
        return OverlayChannel(peer.session, stream)

    def _client(self, peer_id: str) -> DebugClient:
        return DebugClient(lambda service: self._open_debug_channel(peer_id, service))

    # ----- the computer side: operations --------------------------------------------------------

    async def debug_info(self, peer_id: str) -> Dict[str, Any]:
        try:
            return await self._client(peer_id).info()
        except DebugError as exc:
            raise NodeError(exc.text) from exc

    async def debug_shell(
        self,
        peer_id: str,
        command: str,
        timeout: float,
        on_output: Optional[Callable[[int, bytes], None]] = None,
    ) -> Dict[str, Any]:
        self._require_ready_peer(peer_id)
        handle = self._new_handle(peer_id, "shell", command if len(command) <= 80 else command[:77] + "...")
        try:
            result = await self._client(peer_id).shell(command, timeout, on_output)
        except (DebugError, NodeError) as exc:
            # A refusal raises NodeError before any channel exists; both kinds must close the handle, or it stays "running".
            text = exc.text if isinstance(exc, DebugError) else str(exc)
            self._finish(handle, "failed", text)
            raise NodeError(text) from exc
        except asyncio.CancelledError:
            self._finish(handle, "stopped")
            raise
        code = result.get("code")
        self._finish(handle, "ended", f"{result.get('reason') or 'ended'}" + (f", exit {code}" if code is not None else ""))
        return result

    async def debug_logcat_start(self, peer_id: str, text_filter: str, sink: Callable[[Dict[str, Any]], None]) -> int:
        """Start streaming a device's log. Each message goes to `sink` (`line`, `drop`, or `error`). Returns the handle."""
        self._require_ready_peer(peer_id)
        detail = f"lines containing '{text_filter}'" if text_filter else "all lines"
        handle = self._new_handle(peer_id, "logcat", detail)
        client = self._client(peer_id)

        async def run() -> None:
            try:
                await client.logcat(text_filter, sink)
            except asyncio.CancelledError:
                self._finish(handle, "stopped")
                raise
            except (DebugError, NodeError) as exc:
                message = exc.text if isinstance(exc, DebugError) else str(exc)
                self._finish(handle, "failed", message)
                sink({"t": "error", "text": message})
                return
            self._finish(handle, "ended")

        handle.task = self._spawn(run())
        return handle.handle

    async def debug_forward_start(self, peer_id: str, local_port: int, device_port: int) -> int:
        """Listen on 127.0.0.1:`local_port` (0 chooses one) and carry each connection to `device_port` on the device."""
        self._require_ready_peer(peer_id)
        handle = self._new_handle(peer_id, "forward", f"device port {device_port}")
        client = self._client(peer_id)

        async def connect(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            task = asyncio.current_task()
            if task is not None:
                handle.tasks.add(task)
            try:
                try:
                    channel, rest = await client.forward(device_port)
                except (DebugError, NodeError) as exc:
                    handle.note = exc.text if isinstance(exc, DebugError) else str(exc)
                    writer.close()
                    return
                await bridge(reader, writer, channel, rest)
            finally:
                if task is not None:
                    handle.tasks.discard(task)

        try:
            server = await asyncio.start_server(connect, "127.0.0.1", local_port)
        except OSError as exc:
            self._finish(handle, "failed", f"could not listen on 127.0.0.1:{local_port}: {describe(exc)}")
            raise NodeError(f"could not listen on 127.0.0.1:{local_port}: {describe(exc)}") from exc
        handle.server = server
        port = int(server.sockets[0].getsockname()[1])
        handle.detail = f"127.0.0.1:{port} -> device port {device_port}"
        self._log("info", SOURCE_DEBUG, f"forwarding 127.0.0.1:{port} to port {device_port} on {peer_id[:12]}")
        return handle.handle

    async def debug_stop(self, handle_id: int) -> None:
        handle = self._debug_handles.get(handle_id)
        if handle is None:
            raise NodeError("that debug stream is no longer known")
        if handle.server is not None:
            handle.server.close()
        for task in list(handle.tasks):
            task.cancel()
        if handle.task is not None and not handle.task.done():
            handle.task.cancel()
        if handle.state == "running":
            self._finish(handle, "stopped")

    # ----- routes -------------------------------------------------------------------------------

    async def attach_route(self, peer_id: str, host: str, port: int, label: str) -> None:
        """Connect to `peer_id` at host:port and use that connection as a route. When there is no session with the peer
        yet, this starts one; when there is, the connection becomes a second route of the same session."""
        timeout = self._settings.peer_connect_timeout
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
        except (OSError, asyncio.TimeoutError) as exc:
            raise NodeError(f"could not reach {label}: {describe(exc)}") from exc
        try:
            result = await asyncio.wait_for(handshake_initiator(reader, writer, self.identity, peer_id), timeout)
        except Exception as exc:
            writer.close()
            raise NodeError(f"the handshake through {label} failed: {describe(exc)}") from exc
        link = TcpLink(reader, writer, result.keys, kind=RouteKind.DIRECT, describe=label)
        link.start()
        existing = self._peers.get(result.peer_node_id)
        if existing is None:
            peer = self._start_peer(result.peer_node_id, [link], via="usb" if label.startswith("usb:") else "direct", relay_token=None)
            await self._await_ready(peer, timeout)
        else:
            await self._add_route(existing, link)

    async def _add_route(self, peer, link: Link) -> None:
        """Make `link` a route of a live session, and say so in the log."""
        self._observe_link(link)
        peer.links.append(link)
        await peer.session.add_link(link)
        self._log("info", SOURCE_DEBUG, f"a {link.describe} route to {peer.node_id[:12]} was added")

    def _usb_route_up(self, peer_id: str) -> bool:
        peer = self._peers.get(peer_id)
        return peer is not None and any(link.describe.startswith("usb:") and link.is_up() for link in peer.links)

    async def debug_connect_wifi(self) -> None:
        """Attach the Wi-Fi route to the chosen device, at the address in Settings."""
        settings = self._settings
        if not settings.debug_device_id:
            raise NodeError("choose the device to debug first")
        if settings.debug_transport == "usb_only":
            raise NodeError("the transport is set to USB only; change it in Settings to use Wi-Fi")
        if not settings.debug_wifi_address.strip():
            raise NodeError("set the device's Wi-Fi address in Settings (host:port)")
        host, port = parse_host_port(settings.debug_wifi_address)
        await self.attach_route(settings.debug_device_id, host, port, label=f"wifi://{host}:{port}")

    async def debug_scan_usb(self) -> Tuple[AdbDevice, ...]:
        """Ask adb which devices are connected. The answer is kept for the Debug screen."""
        try:
            devices = tuple(await asyncio.to_thread(self._adb().devices))
        except AdbError as exc:
            self._usb_state = ("unavailable", str(exc), ())
            raise NodeError(str(exc)) from exc
        ready = [d for d in devices if d.ready]
        if not devices:
            self._usb_state = ("no device", "no device is connected over USB; plug the phone in and allow USB debugging", devices)
        elif ready:
            self._usb_state = ("ready", f"{len(ready)} device(s) ready over USB", devices)
        else:
            self._usb_state = ("needs attention", devices[0].help, devices)
        return devices

    async def debug_connect_usb(self, serial: str) -> None:
        """Forward the device's listener through `serial` and attach that route to the chosen device."""
        settings = self._settings
        if not settings.debug_device_id:
            raise NodeError("choose the device to debug first")
        if settings.debug_transport == "wifi_only":
            raise NodeError("the transport is set to Wi-Fi only; change it in Settings to use USB")
        if settings.usb_device_port == 0:
            raise NodeError("set the USB device port in Settings: the port the device's Transferd node listens on")
        adb = self._adb()
        local = await asyncio.to_thread(free_local_port)
        try:
            await asyncio.to_thread(adb.forward, serial, local, settings.usb_device_port)
        except AdbError as exc:
            raise NodeError(str(exc)) from exc
        try:
            await self.attach_route(settings.debug_device_id, "127.0.0.1", local, label=f"usb:{serial}")
        except NodeError:
            await self._remove_forward(adb, local)
            raise
        self._log("info", SOURCE_DEBUG, f"the USB route to {settings.debug_device_id[:12]} is up through {serial}")

    async def _remove_forward(self, adb: Adb, local: int) -> None:
        try:
            await asyncio.to_thread(adb.remove_forward, local)
        except AdbError:
            pass

    def _adb(self) -> Adb:
        return Adb(self._settings.usb_adb_path or "adb")

    async def _usb_loop(self) -> None:
        """Keep the USB route to the chosen device: when the cable is plugged in, attach it; when it is missing, the
        session carries on over Wi-Fi or the relay, and the route is attached again when the cable comes back."""
        while True:
            await asyncio.sleep(USB_SCAN_S)
            try:
                await self._usb_tick()
            except Exception as exc:  # the bridge must keep running whatever adb does
                self._log("warning", SOURCE_DEBUG, f"the USB bridge hit an error: {describe(exc)}")

    async def _usb_tick(self) -> None:
        settings = self._settings
        if not settings.debug_device_id or settings.debug_transport == "wifi_only" or settings.usb_device_port == 0:
            return
        if self._usb_route_up(settings.debug_device_id) or time.time() < self._usb_retry_at:
            return
        try:
            devices = await self.debug_scan_usb()
        except NodeError:
            self._usb_retry_at = time.time() + USB_RETRY_S
            return
        ready = [d for d in devices if d.ready]
        if not ready:
            return
        try:
            await self.debug_connect_usb(ready[0].serial)
        except NodeError as exc:
            self._log("warning", SOURCE_DEBUG, f"could not attach USB: {exc}")
            self._usb_retry_at = time.time() + USB_RETRY_S

    # ----- handles and the snapshot -------------------------------------------------------------

    def _new_handle(self, peer_id: str, kind: str, detail: str) -> _Handle:
        ended = [h for h in self._debug_handles.values() if h.state != "running"]
        for old in ended[: max(0, len(ended) - FINISHED_KEPT)]:
            self._debug_handles.pop(old.handle, None)
        handle = _Handle(self._next_handle, peer_id, kind, detail, time.time())
        self._next_handle += 1
        self._debug_handles[handle.handle] = handle
        self._log("info", SOURCE_DEBUG, f"{kind} started on {peer_id[:12]}: {detail}")
        return handle

    def _finish(self, handle: _Handle, state: str, note: str = "") -> None:
        if handle.state != "running":
            return
        handle.state = state
        handle.note = note
        level = "error" if state == "failed" else "info"
        self._log(level, SOURCE_DEBUG, f"{handle.kind} on {handle.peer_id[:12]} {state}" + (f": {note}" if note else ""))

    def debug_snapshot(self) -> DebugSnapshot:
        settings = self._settings
        state, detail, devices = self._usb_state
        return DebugSnapshot(
            agent_enabled=settings.debug_enabled,
            clients=tuple(sorted(parse_clients(settings.debug_clients))),
            device_id=settings.debug_device_id,
            usb_state=state,
            usb_detail=detail,
            usb_devices=tuple(UsbDeviceSnapshot(serial=d.serial, state=d.state, model=d.model) for d in devices),
            streams=tuple(
                DebugStreamSnapshot(h.handle, h.peer_id, h.kind, h.detail, h.started_at, h.state, h.note)
                for h in self._debug_handles.values()
            ),
        )
