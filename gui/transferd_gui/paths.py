"""Where the GUI finds the transferd library, and where it keeps its files.

The library is the repository's `python` folder. `ensure_library_on_path` puts it on `sys.path` once, so the GUI imports
`transferd` exactly as the library's own tests do. Nothing in `crates` or `typescript` is used at runtime.

The settings file always lives in the anchor folder (APPDATA/Transferd on Windows), because the settings say where the data
directory is. The data directory, anchor/data by default, holds the node identity, received files and ghost key files.
Setting the environment variable TRANSFERD_GUI_HOME moves the anchor, for tests and portable use.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

GUI_ROOT = Path(__file__).resolve().parents[1]
LIBRARY_ROOT = GUI_ROOT.parent / "python"
HOME_ENV = "TRANSFERD_GUI_HOME"
SETTINGS_FILE = "settings.json"


def ensure_library_on_path() -> Path:
    """Make `import transferd` resolve to the repository's Python library. Safe to call more than once."""
    text = str(LIBRARY_ROOT)
    if text not in sys.path:
        sys.path.insert(0, text)
    return LIBRARY_ROOT


def anchor_dir() -> Path:
    """The folder that holds the settings file: the default data directory, or `TRANSFERD_GUI_HOME` when it is set."""
    override = os.environ.get(HOME_ENV)
    if override:
        return Path(override)
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "Transferd"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "transferd"


def settings_path() -> Path:
    return anchor_dir() / SETTINGS_FILE


def default_data_dir() -> Path:
    return anchor_dir() / "data"
