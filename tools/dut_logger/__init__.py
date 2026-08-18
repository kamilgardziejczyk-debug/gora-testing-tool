"""DUT serial console capture, saved alongside a scenario's test results.

Runnable from the command line (see `dut_logger.py`) for bring-up checks, and
importable as an API, which is how `main.py` captures a scenario run.
"""

from .handler import LogSessionHandler, attach, detach
from .reader import DEFAULT_BAUD, DutLogger
from .session import (
    CLI_LOG,
    CLI_SUFFIX,
    COMBINED_LOG,
    COMBINED_SUFFIX,
    DEVICE_BUFFER_LINES,
    DEVICE_LOG,
    DEVICE_SUFFIX,
    LOG_KINDS,
    MQTT_LOG,
    MQTT_SUFFIX,
    TOOL_LOG,
    TOOL_SUFFIX,
    DeviceLine,
    LogSession,
    log_paths,
)

__all__ = [
    "CLI_LOG",
    "CLI_SUFFIX",
    "COMBINED_LOG",
    "COMBINED_SUFFIX",
    "DEFAULT_BAUD",
    "DEVICE_BUFFER_LINES",
    "DEVICE_LOG",
    "DEVICE_SUFFIX",
    "LOG_KINDS",
    "MQTT_LOG",
    "MQTT_SUFFIX",
    "TOOL_LOG",
    "TOOL_SUFFIX",
    "DeviceLine",
    "DutLogger",
    "LogSession",
    "LogSessionHandler",
    "attach",
    "detach",
    "log_paths",
]
