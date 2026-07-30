"""8-channel relay board control.

Runnable from the command line (see `relay_board.py`) and importable as an API,
which is how a `!RelayControl` wrapper will drive it.
"""

from .board import (
    DEFAULT_ACTIVE_LOW,
    DEFAULT_PINS,
    RELAY_COUNT,
    RelayBoard,
    parse_pins,
)

__all__ = [
    "DEFAULT_ACTIVE_LOW",
    "DEFAULT_PINS",
    "RELAY_COUNT",
    "RelayBoard",
    "parse_pins",
]
