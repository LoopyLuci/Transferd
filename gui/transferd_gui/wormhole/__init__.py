"""Transferd Wormhole: send a file, a folder or a text with a short code. See `gui\\docs\\WORMHOLE.md` for the design.

Importing this package puts the repository's `python` folder on `sys.path`, so `import transferd` works from here on, as it
does in the rest of the GUI.
"""

from __future__ import annotations

from transferd_gui.paths import ensure_library_on_path

ensure_library_on_path()
