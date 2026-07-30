"""Command line and REPL for the 8-channel relay board.

Only parses input and prints results - every state change goes through
`RelayBoard`, the same API a `!RelayControl` wrapper will use.
"""

from __future__ import annotations

import argparse
import cmd
import logging

from .board import (
    DEFAULT_ACTIVE_LOW,
    DEFAULT_PINS,
    RELAY_COUNT,
    RelayBoard,
    parse_pins,
)

EXIT_OK = 0
EXIT_USAGE = 2

DEFAULT_PULSE_S = 0.5


class RelayShell(cmd.Cmd):
    """Interactive shell, for poking relays by hand during bring-up."""

    intro = (
        "8-channel relay board. Type 'help' for commands, 'quit' to exit.\n"
        "Relays stay in whatever state you leave them on exit."
    )
    prompt = "relay> "

    def __init__(self, board: RelayBoard):
        super().__init__()
        self.board = board

    def do_on(self, arg: str) -> None:
        """on <1-8> - energize a relay."""
        relay = self._parse_relay(arg)
        if relay is None:
            return
        self.board.on(relay)
        print(f"relay {relay} ON")

    def do_off(self, arg: str) -> None:
        """off <1-8> - de-energize a relay."""
        relay = self._parse_relay(arg)
        if relay is None:
            return
        self.board.off(relay)
        print(f"relay {relay} off")

    def do_toggle(self, arg: str) -> None:
        """toggle <1-8> - invert a relay."""
        relay = self._parse_relay(arg)
        if relay is None:
            return
        energized = self.board.toggle(relay)
        print(f"relay {relay} {'ON' if energized else 'off'}")

    def do_pulse(self, arg: str) -> None:
        """pulse <1-8> [seconds] - energize briefly, then de-energize."""
        parts = arg.split()
        if not parts:
            print("usage: pulse <1-8> [seconds]")
            return
        relay = self._parse_relay(parts[0])
        if relay is None:
            return
        duration_s = DEFAULT_PULSE_S
        if len(parts) > 1:
            try:
                duration_s = float(parts[1])
            except ValueError:
                print(f"invalid duration '{parts[1]}'")
                return
        self.board.pulse(relay, duration_s)
        print(f"relay {relay} pulsed for {duration_s}s")

    def do_all(self, arg: str) -> None:
        """all <on|off> - drive every relay at once."""
        choice = arg.strip().lower()
        if choice not in {"on", "off"}:
            print("usage: all <on|off>")
            return
        self.board.set_all(choice == "on")
        print(f"all relays {'ON' if choice == 'on' else 'off'}")

    def do_status(self, arg: str) -> None:
        """status - show every relay's pin and state."""
        print(self.board.describe())

    def do_quit(self, arg: str) -> bool:
        """quit - exit, leaving relays as they are."""
        return True

    do_EOF = do_quit

    def _parse_relay(self, token: str) -> int | None:
        """Parse a relay number, printing a message and returning None if bad."""
        try:
            relay = int(token.strip())
        except ValueError:
            print(f"invalid relay '{token.strip()}' (expected 1-{RELAY_COUNT})")
            return None
        if not 1 <= relay <= RELAY_COUNT:
            print(f"relay must be in 1..{RELAY_COUNT}, got {relay}")
            return None
        return relay


def build_arg_parser() -> argparse.ArgumentParser:
    """Build the top-level parser, with one subcommand per relay action."""
    parser = argparse.ArgumentParser(description="8-channel relay board control")
    parser.add_argument(
        "--pins",
        default=",".join(str(pin) for pin in DEFAULT_PINS),
        help=f"Comma-separated BCM pins for relays 1..8 (default: "
        f"{','.join(str(pin) for pin in DEFAULT_PINS)})",
    )
    parser.add_argument(
        "--active-high",
        action="store_true",
        help="Board energizes a relay on a HIGH input rather than LOW "
        f"(default: active-{'low' if DEFAULT_ACTIVE_LOW else 'high'})",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Log each pin change"
    )

    subparsers = parser.add_subparsers(dest="command")
    for name, help_text in (
        ("on", "Energize one relay"),
        ("off", "De-energize one relay"),
        ("toggle", "Invert one relay"),
    ):
        sub = subparsers.add_parser(name, help=help_text)
        sub.add_argument("relay", type=int, help=f"Relay number, 1-{RELAY_COUNT}")

    pulse = subparsers.add_parser("pulse", help="Energize one relay briefly")
    pulse.add_argument("relay", type=int, help=f"Relay number, 1-{RELAY_COUNT}")
    pulse.add_argument(
        "--duration",
        type=float,
        default=DEFAULT_PULSE_S,
        help=f"Seconds to stay energized (default: {DEFAULT_PULSE_S})",
    )

    subparsers.add_parser("all-on", help="Energize every relay")
    subparsers.add_parser("all-off", help="De-energize every relay")
    subparsers.add_parser("status", help="Show every relay's pin and state")
    subparsers.add_parser("repl", help="Interactive shell (default if no command)")
    return parser


def run_command(board: RelayBoard, args: argparse.Namespace) -> int:
    """Run a single non-interactive subcommand, returning an exit code."""
    try:
        if args.command == "on":
            board.on(args.relay)
        elif args.command == "off":
            board.off(args.relay)
        elif args.command == "toggle":
            board.toggle(args.relay)
        elif args.command == "pulse":
            board.pulse(args.relay, args.duration)
        elif args.command == "all-on":
            board.set_all(True)
        elif args.command == "all-off":
            board.set_all(False)
    except ValueError as error:
        print(f"error: {error}")
        return EXIT_USAGE

    print(board.describe())
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    """Entry point: build a board from the flags, then dispatch."""
    args = build_arg_parser().parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        board = RelayBoard(pins=parse_pins(args.pins), active_low=not args.active_high)
    except ValueError as error:
        print(f"error: {error}")
        return EXIT_USAGE

    if board.simulated:
        print("warning: RPi.GPIO unavailable - simulating, no pins are driven")

    if args.command in (None, "repl"):
        try:
            RelayShell(board).cmdloop()
        except KeyboardInterrupt:
            print()
        return EXIT_OK

    if args.command == "status":
        print(board.describe())
        return EXIT_OK

    return run_command(board, args)
