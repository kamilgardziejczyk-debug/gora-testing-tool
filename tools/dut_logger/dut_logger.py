#!/usr/bin/env python3
"""DUT console capture entry point.

Usage: python dut_logger.py --port /dev/ttyACM0 [--baud 115200] [--duration 30]
"""

import sys
from pathlib import Path

# Make the repo root importable so this file works both when run directly and
# when the package is imported from elsewhere in the project.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.dut_logger.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
