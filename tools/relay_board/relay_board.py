#!/usr/bin/env python3
"""8-channel relay board entry point.

Usage: python relay_board.py [--pins 5,6,13,19,16,26,20,21] [--active-high] <command>
"""

import sys
from pathlib import Path

# Make the repo root importable so this file works both when run directly and
# when the package is imported from elsewhere in the project.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.relay_board.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
