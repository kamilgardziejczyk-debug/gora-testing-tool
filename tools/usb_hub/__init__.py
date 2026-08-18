"""UUGear MEGA4 per-port USB power control.

Runnable from the command line (see `cli.py`) and importable as an API, which
is how the `!UsbSwitch` wrapper drives it.
"""

from .hub import (
    DEFAULT_OFF_RETRIES,
    DEFAULT_TIMEOUT_S,
    PORT_COUNT,
    USB2_HUB_ID,
    USB3_HUB_ID,
    HubNotFoundError,
    PortStatus,
    UhubctlCommandError,
    UhubctlNotFoundError,
    UsbHub,
    UsbHubError,
    parse_ports,
)
from .switchboard import Switchboard

__all__ = [
    "DEFAULT_OFF_RETRIES",
    "DEFAULT_TIMEOUT_S",
    "PORT_COUNT",
    "USB2_HUB_ID",
    "USB3_HUB_ID",
    "HubNotFoundError",
    "PortStatus",
    "Switchboard",
    "UhubctlCommandError",
    "UhubctlNotFoundError",
    "UsbHub",
    "UsbHubError",
    "parse_ports",
]
