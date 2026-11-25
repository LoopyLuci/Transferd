"""The service the UI talks to: one object that owns the backend thread, the node, the settings and the event log.

Views call it from the Qt thread and never touch the library. Node work is submitted to the backend loop with `run`.
Results and snapshots come back through the callbacks given to the constructor; the Qt bridge turns those into signals,
which is safe from any thread.
"""

from __future__ import annotations

import concurrent.futures
import threading
from pathlib import Path
from typing import Any, Callable, Coroutine, Mapping, Optional, Tuple

from transferd_gui.backend.ghost import GhostService
from transferd_gui.backend.node import Node, NodeError, describe
from transferd_gui.backend.worker import BackendWorker
from transferd_gui.model.eventlog import EventLogStore, LogEntry
from transferd_gui.model.settings import Settings, SettingsError, load_settings, save_settings, updated
from transferd_gui.model.snapshot import NodeSnapshot

OnSnapshot = Callable[[NodeSnapshot], None]
OnLog = Callable[[LogEntry], None]
OnDone = Callable[[bool, Any], None]


class TransferdService:
    """Owns the backend thread, the node, the settings file and the event log."""

    def __init__(
        self,
        settings_path: Path,
        defaults: Settings,
        *,
        on_snapshot: Optional[OnSnapshot] = None,
        on_log: Optional[OnLog] = None,
    ) -> None:
        self._settings_path = settings_path
        self._on_snapshot = on_snapshot
        self._on_log = on_log
        self._settings, self.settings_problem = self._load(defaults)
        self._log_store = EventLogStore(self._settings.event_log_size)
        self._latest: Optional[NodeSnapshot] = None
        self._lock = threading.Lock()
        self._worker = BackendWorker()
        self._node = Node(self._settings, self._log, self._publish)

    # ----- settings -----------------------------------------------------------------------------

    def _load(self, defaults: Settings) -> Tuple[Settings, str]:
        """The saved settings, or the defaults with the problem named. A file that does not validate is left as it is."""
        try:
            return load_settings(self._settings_path, defaults), ""
        except SettingsError as exc:
            return defaults, f"Saved settings were not used: {'; '.join(exc.problems)}"

    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def node(self) -> Node:
        """The node, for pages to build actions from. Its methods must run through `run`, on the backend loop."""
        return self._node

    @property
    def settings_path(self) -> Path:
        return self._settings_path

    def change_settings(self, changes: Mapping[str, Any]) -> Settings:
        """Validate and save the changes, then apply them. Raises `SettingsError` and changes nothing when they are invalid."""
        new = updated(self._settings, changes)
        save_settings(self._settings_path, new)
        old, self._settings = self._settings, new
        self.settings_problem = ""
        self._log_store.resize(new.event_log_size)
        self._log("info", "settings", "settings saved")
        if old.data_dir != new.data_dir:
            self._log("info", "settings", "the data directory takes effect after a restart")
        if self._worker.running:
            self.run(self._node.apply_settings(new), "apply settings")
        return new

    def ghost(self) -> GhostService:
        return GhostService(Path(self._settings.data_dir))

    # ----- lifecycle ----------------------------------------------------------------------------

    def start(self) -> None:
        self._worker.start()
        self.run(self._node.start(), "start the node")

    def stop(self, timeout: float = 5.0) -> None:
        if not self._worker.running:
            return
        try:
            self._worker.submit(self._node.stop()).result(timeout=timeout)
        except Exception as exc:  # stopping must finish even when a session will not close cleanly
            self._log("warning", "node", f"stopping was not clean: {describe(exc)}")
        self._worker.stop(timeout)

    @property
    def running(self) -> bool:
        return self._worker.running

    # ----- node actions -------------------------------------------------------------------------

    def run(
        self,
        coro: Coroutine[Any, Any, Any],
        what: str,
        on_done: Optional[OnDone] = None,
    ) -> Optional[concurrent.futures.Future]:
        """Run a node action on the backend. A failure is written to the event log in plain words; `on_done(ok, value)`
        is called on the backend thread with the result or the message."""

        async def wrapper() -> None:
            try:
                value = await coro
            except NodeError as exc:
                self._log("error", "node", f"{what}: {exc}")
                self._done(on_done, False, str(exc))
            except Exception as exc:
                message = describe(exc)
                self._log("error", "node", f"{what} failed: {message}")
                self._done(on_done, False, message)
            else:
                self._done(on_done, True, value)

        if not self._worker.running:
            coro.close()
            self._log("error", "node", f"{what}: the backend is not running")
            return None
        return self._worker.submit(wrapper())

    @staticmethod
    def _done(on_done: Optional[OnDone], ok: bool, value: Any) -> None:
        if on_done is not None:
            on_done(ok, value)

    # ----- reading ------------------------------------------------------------------------------

    def latest(self) -> Optional[NodeSnapshot]:
        with self._lock:
            return self._latest

    def log_entries(self, level: Optional[str] = None):
        return self._log_store.entries(level)

    def clear_log(self) -> None:
        self._log_store.clear()

    # ----- callbacks from the backend -----------------------------------------------------------

    def _publish(self, snapshot: NodeSnapshot) -> None:
        with self._lock:
            self._latest = snapshot
        if self._on_snapshot is not None:
            self._on_snapshot(snapshot)

    def _log(self, level: str, source: str, message: str) -> None:
        entry = self._log_store.add(level, source, message)
        if self._on_log is not None:
            self._on_log(entry)
