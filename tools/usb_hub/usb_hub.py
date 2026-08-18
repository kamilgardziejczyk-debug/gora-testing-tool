#!/usr/bin/env python3
"""MEGA4 USB hub entry point.

Usage: python usb_hub.py [-l 1-1.2] <command>
"""

import sys
from pathlib import Path

# Make the repo root importable so this file works both when run directly and
# when the package is imported from elsewhere in the project.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.usb_hub.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
