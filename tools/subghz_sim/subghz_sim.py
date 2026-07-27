#!/usr/bin/env python3
"""Sub-GHz sensor simulator entry point.

Usage: python subghz_sim.py --port <PORT> [--baud 115200] [--interval 5]
"""

import sys
from pathlib import Path

# Make the repo root importable so this file works both when run directly and
# when the package is imported from elsewhere in the project.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.subghz_sim.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
