#!/usr/bin/env python3
"""BLE GATT tool entry point.

Usage:
  python ble_gatt.py --scan
  python ble_gatt.py --connect <NAME|ADDRESS>
  python ble_gatt.py [--adapter hci0] [--scan-timeout 8] [--connect-timeout 15]
"""

import sys
from pathlib import Path

# Make the repo root importable so this file works both when run directly and
# when the package is imported from elsewhere in the project.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.ble_gatt.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
