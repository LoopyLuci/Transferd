"""The backend: the adapter over the transferd library, and the thread that runs it.

Importing this package puts the repository's `python` folder on `sys.path`, so `import transferd` works from here on.
"""

from transferd_gui.paths import ensure_library_on_path

ensure_library_on_path()
