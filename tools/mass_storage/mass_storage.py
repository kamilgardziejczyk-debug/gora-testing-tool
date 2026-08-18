#!/usr/bin/env python3
"""DUT SD card over USB mass storage entry point.

Usage: python mass_storage.py -l 1-1.2 -p 1 <command>
"""

import sys
from pathlib import Path

# Make the repo root importable so this file works both when run directly and
# when the package is imported from elsewhere in the project.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.mass_storage.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
