"""Start the Transferd desktop app: `python run.py` from the gui folder, using the gui virtual environment.

The app reads the transferd library from the repository's `python` folder. Nothing is installed globally.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from transferd_gui.app import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
