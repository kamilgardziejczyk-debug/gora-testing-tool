#!/usr/bin/env python3
"""DUT shell client entry point.

Usage: python dut_cli.py --port /dev/ttyACM1 "kernel version" [-c "gora status"]
"""

import sys
from pathlib import Path

# Make the repo root importable so this file works both when run directly and
# when the package is imported from elsewhere in the project.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.dut_cli.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
