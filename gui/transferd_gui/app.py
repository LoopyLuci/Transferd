"""Application entry: builds the service, the bridge, the telemetry store and the window, then runs the Qt event loop."""

from __future__ import annotations

import sys
from typing import List, Optional

from transferd_gui.backend.service import TransferdService
from transferd_gui.model.settings import Settings, default_settings
from transferd_gui.model.telemetry import TelemetryStore
from transferd_gui.paths import default_data_dir, settings_path


def initial_settings() -> Settings:
    """The defaults on a first run: the data directory's folder, and received files in its `received` subfolder."""
    data = default_data_dir()
    return default_settings(str(data), str(data / "received"))


def main(argv: Optional[List[str]] = None) -> int:
    from PySide6.QtWidgets import QApplication

    from transferd_gui.ui.bridge import Bridge
    from transferd_gui.ui.main_window import MainWindow

    app = QApplication.instance() or QApplication(list(sys.argv if argv is None else argv))
    app.setApplicationName("Transferd")
    bridge = Bridge()
    service = TransferdService(
        settings_path(),
        initial_settings(),
        on_snapshot=bridge.snapshot_callback,
        on_log=bridge.log_callback,
    )
    telemetry = TelemetryStore(service.settings.telemetry_window_s)
    window = MainWindow(service, bridge, telemetry)
    window.show()
    service.start()
    return app.exec()
