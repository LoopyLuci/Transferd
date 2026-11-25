"""Test setup: put the GUI package on the path, run Qt offscreen, and give every test its own settings home.

The library is found through the `transferd_gui.backend` package, which puts the repository's `python` folder on the
path. The relay tests need a built relayd: set TRANSFERD_RELAYD to its path, or they are skipped with that reason.
"""

import os
import sys
from pathlib import Path

GUI_ROOT = Path(__file__).resolve().parents[1]
if str(GUI_ROOT) not in sys.path:
    sys.path.insert(0, str(GUI_ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402

from transferd_gui.backend import ensure_library_on_path  # noqa: E402

ensure_library_on_path()  # puts the repository's python/ folder on sys.path, so `import transferd` works


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A fresh settings home for this test: the anchor folder, with the data directory under it."""
    folder = tmp_path / "home"
    monkeypatch.setenv("TRANSFERD_GUI_HOME", str(folder))
    return folder


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app
