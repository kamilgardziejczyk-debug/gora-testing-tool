#!/usr/bin/env python3
"""MQTT listener entry point.

Usage:
  python mqtt_listener.py --endpoint <HOST> --client-id <ID> --cert <PEM> \
      --private-key <PEM> --root-ca <PEM> --topic <FILTER> [--timeout <S>]
"""

import sys
from pathlib import Path

# Make the repo root importable so this file works both when run directly and
# when the package is imported from elsewhere in the project.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.mqtt_listener.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
