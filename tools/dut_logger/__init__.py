"""DUT serial console capture, saved alongside a scenario's test results.

Runnable from the command line (see `dut_logger.py`) for bring-up checks, and
importable as an API, which is how `main.py` captures a scenario run.
"""

from .handler import LogSessionHandler, attach, detach
from .reader import DEFAULT_BAUD, DutLogger
from .session import (
    COMBINED_SUFFIX,
    DEVICE_SUFFIX,
    TOOL_SUFFIX,
    LogSession,
    log_paths,
)

__all__ = [
    "COMBINED_SUFFIX",
    "DEFAULT_BAUD",
    "DEVICE_SUFFIX",
    "TOOL_SUFFIX",
    "DutLogger",
    "LogSession",
    "LogSessionHandler",
    "attach",
    "detach",
    "log_paths",
]
