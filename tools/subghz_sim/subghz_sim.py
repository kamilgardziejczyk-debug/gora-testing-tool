#!/usr/bin/env python3
"""Sub-GHz sensor simulator entry point.

Usage: python subghz_sim.py --port <PORT> [--baud 115200] [--interval 5]
"""

import sys

from cli import main

if __name__ == "__main__":
    sys.exit(main())
