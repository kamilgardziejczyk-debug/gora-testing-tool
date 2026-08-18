"""Command line and REPL for the MEGA4 USB hub.

Only parses input and prints results - every state change goes through
`UsbHub`, the same API the `!UsbSwitch` wrapper will use.

Port arguments accept anything `uhubctl` does: `3`, `1,3` or `1-3`. Multi-port
commands are issued as one call rather than one per port, which is what keeps
powering several ports off from costing one retry storm each.
"""

from __future__ import annotations

import argparse
import cmd
import logging
from typing import Callable

from .hub import (
    DEFAULT_OFF_RETRIES,
    DEFAULT_TIMEOUT_S,
    PORT_COUNT,
    UHUBCTL_BIN,
    UsbHub,
    UsbHubError,
    parse_ports,
)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2

DEFAULT_CYCLE_S = 1.0

PORTS_HELP = f"Port spec: a number, list or range within 1-{PORT_COUNT}, e.g. 3, 1,3 or 1-3"


class UsbHubShell(cmd.Cmd):
    """Interactive shell, for poking ports by hand during bring-up."""

    intro = (
        "MEGA4 USB hub. Type 'help' for commands, 'quit' to exit.\n"
        "Ports keep whatever power state you leave them in on exit.\n"
        "Powering a port off takes a few seconds - the kernel has to be retried out."
    )
    prompt = "usb> "

    def __init__(self, hub: UsbHub):
        super().__init__()
        self.hub = hub

    def do_on(self, arg: str) -> None:
        """on <ports> - power ports on, e.g. 'on 3' or 'on 1-3'."""
        self._set(arg, True)

    def do_off(self, arg: str) -> None:
        """off <ports> - power ports off. Takes a few seconds."""
        self._set(arg, False)

    def do_toggle(self, arg: str) -> None:
        """toggle <ports> - invert each port's power."""
        ports = self._parse(arg, "toggle")
        if ports is None:
            return
        for port in ports:
            powered = self.hub.toggle(port)
            print(f"port {port} {'ON' if powered else 'off'}")

    def do_cycle(self, arg: str) -> None:
        """cycle <ports> [seconds] - power off, wait, power back on."""
        parts = arg.split()
        if not parts:
            print("usage: cycle <ports> [seconds]")
            return
        ports = self._parse(parts[0], "cycle")
        if ports is None:
            return
        delay_s = DEFAULT_CYCLE_S
        if len(parts) > 1:
            try:
                delay_s = float(parts[1])
            except ValueError:
                print(f"invalid duration '{parts[1]}'")
                return
        if self._run(lambda: self.hub.cycle(ports, delay_s)):
            print(f"port(s) {parts[0]} cycled with a {delay_s}s gap")

    def do_all(self, arg: str) -> None:
        """all <on|off> - drive every port at once."""
        choice = arg.strip().lower()
        if choice not in {"on", "off"}:
            print("usage: all <on|off>")
            return
        if self._run(lambda: self.hub.set_all(choice == "on")):
            print(f"all ports {'ON' if choice == 'on' else 'off'}")

    def do_status(self, arg: str) -> None:
        """status - show every port's power state and whether a device is on it."""
        self._run(lambda: print(self.hub.describe()))

    def do_quit(self, arg: str) -> bool:
        """quit - exit, leaving ports as they are."""
        return True

    do_EOF = do_quit

    def _set(self, arg: str, powered: bool) -> None:
        """Apply one power state to a port spec, then report it."""
        ports = self._parse(arg, "on" if powered else "off")
        if ports is None:
            return
        if self._run(lambda: self.hub.set_ports(ports, powered)):
            print(f"port(s) {arg.strip()} {'ON' if powered else 'off'}")

    def _parse(self, token: str, command: str) -> tuple[int, ...] | None:
        """Parse a port spec, printing a message and returning None if bad."""
        token = token.strip()
        if not token:
            print(f"usage: {command} <ports>")
            return None
        try:
            return parse_ports(token)
        except ValueError as error:
            print(f"error: {error}")
            return None

    @staticmethod
    def _run(action: Callable[[], object]) -> bool:
        """Run a hub action, reporting a hardware failure instead of raising."""
        try:
            action()
        except UsbHubError as error:
            print(f"error: {error}")
            return False
        return True


def build_arg_parser() -> argparse.ArgumentParser:
    """Build the top-level parser, with one subcommand per hub action."""
    parser = argparse.ArgumentParser(description="MEGA4 per-port USB power control")
    parser.add_argument(
        "-l",
        "--location",
        help="Hub location as reported by uhubctl, e.g. 1-1.2 (default: discover "
        "the only attached MEGA4). Required once more than one is attached",
    )
    parser.add_argument(
        "--uhubctl",
        default=UHUBCTL_BIN,
        help=f"uhubctl binary to drive the hub with (default: {UHUBCTL_BIN}). "
        "When it is not installed, port changes are simulated",
    )
    parser.add_argument(
        "--off-retries",
        type=int,
        default=DEFAULT_OFF_RETRIES,
        help=f"uhubctl -r value used when powering off, which the kernel otherwise "
        f"undoes (default: {DEFAULT_OFF_RETRIES})",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_S,
        help=f"Seconds to allow each uhubctl call (default: {DEFAULT_TIMEOUT_S})",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Log each hub call")

    _add_subparsers(parser)
    return parser


def _add_subparsers(parser: argparse.ArgumentParser) -> None:
    """Attach every subcommand to `parser`."""
    subparsers = parser.add_subparsers(dest="command")
    for name, help_text in (
        ("on", "Power ports on"),
        ("off", "Power ports off"),
        ("toggle", "Invert each port's power"),
    ):
        sub = subparsers.add_parser(name, help=help_text)
        sub.add_argument("ports", help=PORTS_HELP)

    cycle = subparsers.add_parser("cycle", help="Power ports off, wait, power back on")
    cycle.add_argument("ports", help=PORTS_HELP)
    cycle.add_argument(
        "--delay",
        type=float,
        default=DEFAULT_CYCLE_S,
        help=f"Seconds to stay unpowered (default: {DEFAULT_CYCLE_S})",
    )

    subparsers.add_parser("all-on", help="Power every port on")
    subparsers.add_parser("all-off", help="Power every port off")
    subparsers.add_parser("status", help="Show every port's power state")
    subparsers.add_parser("discover", help="List the locations of all attached MEGA4 hubs")
    subparsers.add_parser("repl", help="Interactive shell (default if no command)")


def run_command(hub: UsbHub, args: argparse.Namespace) -> int:
    """Run a single non-interactive subcommand, returning an exit code."""
    if args.command == "on":
        hub.set_ports(args.ports, True)
    elif args.command == "off":
        hub.set_ports(args.ports, False)
    elif args.command == "toggle":
        for port in parse_ports(args.ports):
            hub.toggle(port)
    elif args.command == "cycle":
        hub.cycle(args.ports, args.delay)
    elif args.command == "all-on":
        hub.set_all(True)
    elif args.command == "all-off":
        hub.set_all(False)

    print(hub.describe())
    return EXIT_OK


def run_discover(uhubctl_bin: str, timeout_s: float) -> int:
    """Print every attached MEGA4's location, one per line."""
    locations = UsbHub.discover(uhubctl_bin, timeout_s)
    if not locations:
        print("no MEGA4 hub found")
        return EXIT_ERROR
    for location in locations:
        print(location)
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    """Entry point: build a hub from the flags, then dispatch."""
    args = build_arg_parser().parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        hub = UsbHub(
            location=args.location,
            uhubctl_bin=args.uhubctl,
            off_retries=args.off_retries,
            timeout_s=args.timeout,
        )
    except ValueError as error:
        print(f"error: {error}")
        return EXIT_USAGE

    if hub.simulated:
        print(f"warning: {args.uhubctl} not found - simulating, no ports are switched")

    if args.command == "discover":
        return _guard(lambda: run_discover(args.uhubctl, args.timeout))

    if args.command in (None, "repl"):
        try:
            UsbHubShell(hub).cmdloop()
        except KeyboardInterrupt:
            print()
        return EXIT_OK

    if args.command == "status":
        return _guard(lambda: (print(hub.describe()), EXIT_OK)[1])

    return _guard(lambda: run_command(hub, args))


def _guard(action: Callable[[], int]) -> int:
    """Run `action`, turning a bad argument or a hardware failure into an exit code.

    Hardware trouble (no hub, no permission, a timeout) is a different kind of
    failure from a mistyped port, so the two get different exit codes - a
    scenario driving this from a shell can tell "you asked wrong" apart from
    "the rig is broken".
    """
    try:
        return action()
    except ValueError as error:
        print(f"error: {error}")
        return EXIT_USAGE
    except UsbHubError as error:
        print(f"error: {error}")
        return EXIT_ERROR
