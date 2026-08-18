"""A `UsbHub` paired with the scenario's port names.

Scenario authors should be able to say *what* they are switching ("dut_power")
rather than where it happens to be plugged in this month, because port numbers
are a property of the bench's wiring and names are a property of the test. This
class holds that mapping and resolves either form, so `!UsbSwitch` never has to
know whether it was handed a name or a number.
"""

from __future__ import annotations

import logging
from typing import Mapping

from .hub import PORT_COUNT, UsbHub


LOGGER = logging.getLogger(__name__)


class Switchboard:
    """Port aliases over one MEGA4 hub.

    Aliases are optional: a scenario with no `usb_hub.ports` block can still
    address ports by number, and gets an error naming the alternatives if it
    tries to use a name.
    """

    def __init__(self, hub: UsbHub, ports: Mapping[str, int] | None = None) -> None:
        """Wrap `hub`, resolving names through `ports`."""
        self.hub = hub
        self.ports: dict[str, int] = dict(ports or {})
        for name, port in self.ports.items():
            if not 1 <= port <= PORT_COUNT:
                raise ValueError(
                    f"usb_hub.ports: '{name}' is port {port}, outside 1..{PORT_COUNT}"
                )

    def resolve(self, target: str | int) -> int:
        """Port number for `target`, which may be a number or an alias.

        A numeric string resolves as a number, so a scenario writing `port: 3`
        and one writing `port: "3"` mean the same thing.
        """
        if isinstance(target, int):
            return self._checked(target)

        name = target.strip()
        if name in self.ports:
            return self.ports[name]

        try:
            number = int(name)
        except ValueError:
            # Not a number and not a known alias - the two are reported
            # differently because "you meant a port that does not exist" and
            # "you meant a name nobody defined" need different fixes.
            known = ", ".join(sorted(self.ports)) if self.ports else "none defined"
            raise ValueError(
                f"unknown USB port '{name}'. Use a number 1..{PORT_COUNT}, or one of the "
                f"names in the scenario's usb_hub.ports block ({known})."
            ) from None
        return self._checked(number)

    def set(self, target: str | int, powered: bool) -> None:
        """Power the port named by `target` on or off."""
        port = self.resolve(target)
        LOGGER.info("Powering USB %s %s", self.describe_target(target), "on" if powered else "off")
        self.hub.set(port, powered)

    def cycle(self, target: str | int, delay_s: float) -> None:
        """Power the port named by `target` off, wait, then power it back on."""
        port = self.resolve(target)
        LOGGER.info("Power-cycling USB %s with a %ss gap", self.describe_target(target), delay_s)
        self.hub.cycle(port, delay_s)

    def state(self, target: str | int) -> bool:
        """Whether the port named by `target` currently has power."""
        return self.hub.state(self.resolve(target))

    def describe_target(self, target: str | int) -> str:
        """`target` as it should appear in a log line, e.g. `dut_power (port 3)`."""
        port = self.resolve(target)
        return f"port {port}" if str(target).strip() == str(port) else f"{target} (port {port})"

    @staticmethod
    def _checked(port: int) -> int:
        """Return `port` if it is a real port number, else raise."""
        if not 1 <= port <= PORT_COUNT:
            raise ValueError(f"USB port must be in 1..{PORT_COUNT}, got {port}")
        return port
