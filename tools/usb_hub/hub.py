"""UUGear MEGA4 4-port USB hub, addressed through `uhubctl`.

The MEGA4 implements the standard USB per-port power switching (PPPS) feature
with real AP2511 load switches, so cutting a port genuinely drops VBUS rather
than only gating the data lines. That makes it usable for hard power-cycling a
DUT, which is the reason this module exists.

Everything UUGear ships for the board (mega4.sh, the UWI web page) is a wrapper
around `uhubctl`, so this module wraps `uhubctl` directly and skips the vendor
scripts. `uhubctl` is what handles the two awkward parts of the hardware: a
USB 3 hub enumerates as a *pair* of virtual hubs, and both halves must be
switched for the change to take effect; and the Linux kernel re-powers a port
the moment a device disappears from it, which has to be retried out.

Ports are addressed 1..4 to match the silkscreen on the board.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import Sequence


LOGGER = logging.getLogger(__name__)

PORT_COUNT = 4

UHUBCTL_BIN = "uhubctl"

# The VL817 controller enumerates twice, once per USB generation. Power state is
# read from the USB2 half (which every MEGA4 always presents, whether or not the
# upstream link negotiated SuperSpeed); the USB3 half is only consulted to spot
# a SuperSpeed device that is invisible to the USB2 half.
USB2_HUB_ID = "2109:2817"
USB3_HUB_ID = "2109:0817"

# Powering a port off races the kernel, which sees the device vanish and
# immediately powers the port back on. `uhubctl -r` keeps re-issuing the request
# until the kernel gives up; 200 is what UUGear's own script uses. The knock-on
# effect is that off is *much* slower than on - seconds, not milliseconds - so
# callers must not assume the two directions cost the same.
DEFAULT_OFF_RETRIES = 200

# Generous enough to cover a full retry storm on a busy Pi.
DEFAULT_TIMEOUT_S = 30.0

_HUB_HEADER_RE = re.compile(r"^Current status for hub (\S+) \[([0-9a-fA-F]{4}:[0-9a-fA-F]{4})")
_PORT_LINE_RE = re.compile(r"^\s*Port (\d+):\s+[0-9a-fA-F]{4}\s*(.*)$")


class UsbHubError(Exception):
    """Base class for every failure this module raises."""


class UhubctlNotFoundError(UsbHubError):
    """The `uhubctl` binary is not installed or not on PATH."""


class HubNotFoundError(UsbHubError):
    """No MEGA4 hub matched - none attached, or the given location is stale."""


class UhubctlCommandError(UsbHubError):
    """`uhubctl` ran but exited non-zero, or did not finish in time."""


@dataclass(frozen=True)
class PortStatus:
    """Power and occupancy of one downstream port."""

    port: int
    powered: bool
    connected: bool

    def __str__(self) -> str:
        """One aligned line: port number, power state, and whether a device is on it."""
        power = "ON " if self.powered else "off"
        return f"port {self.port}  {power}  {'device' if self.connected else '-'}"


@dataclass(frozen=True)
class _HubBlock:
    """One `Current status for hub ...` block of `uhubctl` output."""

    location: str
    usb_id: str
    # Port number -> the flag words `uhubctl` printed for it.
    ports: dict[int, frozenset[str]]


class UsbHub:
    """A MEGA4 hub, addressed by its USB2 location (e.g. `1-1.2`).

    Location strings describe *topology*, not identity: re-plugging the hub into
    a different upstream port renames it. So `location` is optional, and a hub
    constructed without one discovers the single attached MEGA4 on first use.
    Pin a location explicitly once more than one MEGA4 is in the rig, since
    which one gets discovered is otherwise arbitrary.

    No state is cached across calls beyond the resolved location: every query
    re-reads the hardware, which keeps one-shot CLI runs and long-lived scenario
    runs agreeing with each other.
    """

    def __init__(
        self,
        location: str | None = None,
        uhubctl_bin: str = UHUBCTL_BIN,
        off_retries: int = DEFAULT_OFF_RETRIES,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        """Bind to the MEGA4 at `location`, or to the only one attached."""
        if off_retries < 0:
            raise ValueError(f"off_retries must be >= 0, got {off_retries}")
        if timeout_s <= 0:
            raise ValueError(f"timeout_s must be > 0, got {timeout_s}")

        self.uhubctl_bin = uhubctl_bin
        self.off_retries = off_retries
        self.timeout_s = timeout_s
        self._location = location
        # Simulated power state, used only when `uhubctl` is unavailable.
        self._simulated: dict[int, bool] = {}

    @property
    def simulated(self) -> bool:
        """Whether this hub is simulating instead of switching real ports.

        True only when the `uhubctl` binary is missing, which is a property of
        the machine rather than of the rig - it is what lets the CLI and
        scenarios stay runnable on a developer PC. A *present* binary that finds
        no MEGA4 raises `HubNotFoundError` instead of quietly simulating, so a
        rig with an unplugged hub fails the run rather than passing it.
        """
        return shutil.which(self.uhubctl_bin) is None

    @property
    def location(self) -> str:
        """USB2 location of this hub, discovering it on first access."""
        if self._location is None:
            self._location = self._discover_one()
        return self._location

    def set(self, port: int, powered: bool) -> None:
        """Power `port` on or off, and wait for the change to stick."""
        self.set_ports((port,), powered)

    def set_ports(self, ports: str | Sequence[int] | int, powered: bool) -> None:
        """Power several ports at once, given a spec (`1,3`, `1-3`) or numbers.

        One `uhubctl` call for the whole set rather than one per port, which
        matters on the off path: each call pays its own retry storm (see
        DEFAULT_OFF_RETRIES), so batching four ports is seconds rather than
        tens of seconds.
        """
        spec = _to_spec(ports)
        parse_ports(spec)  # Validate before anything reaches the hardware.
        self._set_ports(spec, powered)

    def on(self, port: int) -> None:
        """Power `port` on."""
        self.set(port, True)

    def off(self, port: int) -> None:
        """Power `port` off, dropping VBUS to whatever is plugged in."""
        self.set(port, False)

    def toggle(self, port: int) -> bool:
        """Invert `port`, returning its new powered state."""
        new_state = not self.state(port)
        self.set(port, new_state)
        return new_state

    def cycle(self, ports: str | Sequence[int] | int, delay_s: float = 1.0) -> None:
        """Power `ports` off, wait `delay_s`, then power them back on.

        Powers back on even if interrupted part-way through, so a Ctrl-C or a
        failing scenario does not leave a DUT dark. Note that `uhubctl` has its
        own `-a cycle`; this does it in two steps instead so `delay_s` is not
        capped by that option's own timing and so the on-leg still runs when
        the off-leg's retry storm eats the whole cycle budget.
        """
        if delay_s < 0:
            raise ValueError(f"delay_s must be >= 0, got {delay_s}")
        self.set_ports(ports, False)
        try:
            time.sleep(delay_s)
        finally:
            self.set_ports(ports, True)

    def set_all(self, powered: bool) -> None:
        """Power all four ports on or off in a single `uhubctl` call."""
        self.set_ports(f"1-{PORT_COUNT}", powered)

    def state(self, port: int) -> bool:
        """Whether `port` currently has power."""
        self._check_port(port)
        return self.status()[port - 1].powered

    def status(self) -> list[PortStatus]:
        """Power and occupancy of ports 1..4, in order."""
        if self.simulated:
            return [
                PortStatus(port, self._simulated.get(port, True), False)
                for port in range(1, PORT_COUNT + 1)
            ]

        blocks = self._list_blocks()
        usb2 = self._find_block(blocks, self.location)
        usb3_ports = self._companion_ports(blocks, self.location)

        statuses = []
        for port in range(1, PORT_COUNT + 1):
            flags = usb2.ports.get(port, frozenset())
            connected = "connect" in flags or "connect" in usb3_ports.get(port, frozenset())
            statuses.append(PortStatus(port, "power" in flags, connected))
        return statuses

    def describe(self) -> str:
        """One line per port: number, power state, and whether a device is on it."""
        header = f"MEGA4 at {self.location}" + (" (simulated)" if self.simulated else "")
        return "\n".join([header, *(str(status) for status in self.status())])

    def _set_ports(self, ports: str, powered: bool) -> None:
        """Apply `powered` to a `uhubctl` port spec such as `3` or `1-4`."""
        if self.simulated:
            self._simulate(ports, powered)
            return

        args = ["-l", self.location, "-p", ports, "-a", "on" if powered else "off"]
        if not powered and self.off_retries:
            # See DEFAULT_OFF_RETRIES: without this the kernel undoes the cut.
            args += ["-r", str(self.off_retries)]
        self._run(args)
        LOGGER.info(
            "MEGA4 %s port(s) %s powered %s", self.location, ports, "on" if powered else "off"
        )

    def _simulate(self, ports: str, powered: bool) -> None:
        """Record a state change that no hardware will see, and say so loudly."""
        for port in parse_ports(ports):
            self._simulated[port] = powered
        LOGGER.warning(
            "%s is not installed on this platform. Simulating: port(s) %s powered %s",
            self.uhubctl_bin,
            ports,
            "on" if powered else "off",
        )

    def _discover_one(self) -> str:
        """Location of the only attached MEGA4, or a clear error saying why not."""
        if self.simulated:
            return "simulated"

        locations = self.discover(self.uhubctl_bin, self.timeout_s)
        if not locations:
            raise HubNotFoundError(
                f"no MEGA4 hub ({USB2_HUB_ID}) found by {self.uhubctl_bin}. Check the hub is "
                "plugged in, and that this process can reach it (root, a udev rule for "
                "idVendor 2109, or --privileged -v /dev/bus/usb:/dev/bus/usb in Docker)."
            )
        if len(locations) > 1:
            raise HubNotFoundError(
                f"{len(locations)} MEGA4 hubs found ({', '.join(locations)}). Pass an explicit "
                "location to say which one to drive."
            )
        LOGGER.info("Discovered MEGA4 at %s", locations[0])
        return locations[0]

    def _companion_ports(
        self, blocks: Sequence[_HubBlock], location: str
    ) -> dict[int, frozenset[str]]:
        """Port flags of the USB3 half paired with the USB2 hub at `location`.

        The two halves have unrelated location strings, and `uhubctl` does not
        print the link between them, so they are paired by position: the Nth
        MEGA4 USB2 hub in the listing belongs with the Nth MEGA4 USB3 hub. That
        is how UUGear's own script does it. Returns empty when the upstream link
        is USB2-only, in which case there is no USB3 half to pair with.
        """
        usb2 = [block for block in blocks if block.usb_id == USB2_HUB_ID]
        usb3 = [block for block in blocks if block.usb_id == USB3_HUB_ID]
        index = next(i for i, block in enumerate(usb2) if block.location == location)
        return usb3[index].ports if index < len(usb3) else {}

    def _list_blocks(self) -> list[_HubBlock]:
        """Every hub `uhubctl` can see, parsed."""
        return _parse_listing(self._run([]))

    def _run(self, args: Sequence[str]) -> str:
        """Run `uhubctl` with `args` and return its stdout."""
        command = [self.uhubctl_bin, *args]
        LOGGER.debug("Running %s", " ".join(command))
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
                check=False,
            )
        except FileNotFoundError:
            raise UhubctlNotFoundError(
                f"{self.uhubctl_bin} is not installed or not on PATH"
            ) from None
        except subprocess.TimeoutExpired:
            raise UhubctlCommandError(
                f"{' '.join(command)} did not finish within {self.timeout_s}s"
            ) from None

        if result.returncode != 0:
            raise UhubctlCommandError(
                f"{' '.join(command)} exited {result.returncode}: "
                f"{(result.stderr or result.stdout).strip()}"
            )
        return result.stdout

    @staticmethod
    def _find_block(blocks: Sequence[_HubBlock], location: str) -> _HubBlock:
        """The MEGA4 USB2 block at `location`, or a clear error."""
        for block in blocks:
            if block.location == location and block.usb_id == USB2_HUB_ID:
                return block
        raise HubNotFoundError(
            f"no MEGA4 hub at location {location}. Locations change when the hub is re-plugged "
            f"into a different upstream port; run discovery to find its current one."
        )

    @staticmethod
    def _check_port(port: int) -> None:
        """Reject a port number outside the silkscreened 1..4."""
        if not 1 <= port <= PORT_COUNT:
            raise ValueError(f"port must be in 1..{PORT_COUNT}, got {port}")

    @staticmethod
    def discover(uhubctl_bin: str = UHUBCTL_BIN, timeout_s: float = DEFAULT_TIMEOUT_S) -> list[str]:
        """USB2 locations of every attached MEGA4, in the order `uhubctl` lists them."""
        probe = UsbHub(location="", uhubctl_bin=uhubctl_bin, timeout_s=timeout_s)
        return [block.location for block in probe._list_blocks() if block.usb_id == USB2_HUB_ID]


def _parse_listing(text: str) -> list[_HubBlock]:
    """Parse `uhubctl` output into one entry per hub it reported.

    Output looks like::

        Current status for hub 1-1.2 [2109:2817 VIA Labs, Inc. USB2.0 Hub, ...]
          Port 1: 0100 power
          Port 2: 0503 power highspeed enable connect
    """
    blocks: list[_HubBlock] = []
    ports: dict[int, frozenset[str]] = {}

    for line in text.splitlines():
        header = _HUB_HEADER_RE.match(line)
        if header:
            ports = {}
            blocks.append(_HubBlock(header.group(1), header.group(2).lower(), ports))
            continue

        port_line = _PORT_LINE_RE.match(line)
        if port_line and blocks:
            ports[int(port_line.group(1))] = frozenset(port_line.group(2).split())

    return blocks


def _to_spec(ports: str | Sequence[int] | int) -> str:
    """Normalize a port argument into a `uhubctl` port spec string."""
    if isinstance(ports, str):
        return ports
    if isinstance(ports, int):
        return str(ports)
    return ",".join(str(port) for port in ports)


def parse_ports(value: str) -> tuple[int, ...]:
    """Parse a `uhubctl` port spec - `3`, `1,3`, or `1-3` - into port numbers.

    Returns them sorted and de-duplicated, so `3,1-2,1` and `1-3` agree.
    """
    ports: set[int] = set()
    for part in value.split(","):
        part = part.strip()
        if not part:
            raise ValueError(f"empty port in '{value}'")
        try:
            if "-" in part:
                low, high = (int(bound) for bound in part.split("-", 1))
                ports.update(range(low, high + 1))
            else:
                ports.add(int(part))
        except ValueError:
            raise ValueError(
                f"ports must be numbers, ranges or a comma-separated mix, got '{value}'"
            ) from None

    if not ports:
        raise ValueError(f"no ports in '{value}'")
    for port in sorted(ports):
        UsbHub._check_port(port)
    return tuple(sorted(ports))
