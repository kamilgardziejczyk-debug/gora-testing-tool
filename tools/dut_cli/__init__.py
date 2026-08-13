"""Send commands to a DUT's Zephyr shell over UART and read the replies back.

Runnable from the command line (see `dut_cli.py`) for poking at a device by
hand, and importable as an API, which is how a scenario wrapper drives it.

The DUT's shell is expected on its own port, separate from the console that
`dut_logger` captures - one process reading a port is a precondition for
framing responses at all.
"""

from .parsing import Response, is_log_line, strip_ansi
from .shell import (
    DEFAULT_PROMPT,
    DEFAULT_SYNC_TIMEOUT_S,
    DEFAULT_TIMEOUT_S,
    DutShell,
)
from .transport import DEFAULT_BAUD, SerialTransport

__all__ = [
    "DEFAULT_BAUD",
    "DEFAULT_PROMPT",
    "DEFAULT_SYNC_TIMEOUT_S",
    "DEFAULT_TIMEOUT_S",
    "DutShell",
    "Response",
    "SerialTransport",
    "is_log_line",
    "strip_ansi",
]
