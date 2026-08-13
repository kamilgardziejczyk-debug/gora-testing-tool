"""Command line front end for the DUT shell client.

For poking at a device by hand, or scripting a sequence of commands from a
shell without going through a scenario:

    python tools/dut_cli/dut_cli.py --port /dev/ttyACM1 "kernel version"
    python tools/dut_cli/dut_cli.py --port /dev/ttyACM1 -c "gora status" --json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .shell import DEFAULT_PROMPT, DEFAULT_SYNC_TIMEOUT_S, DEFAULT_TIMEOUT_S, DutShell
from .transport import DEFAULT_BAUD

EXIT_OK = 0
EXIT_CONNECTION_ERROR = 2
EXIT_TIMEOUT = 3


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the command line."""
    parser = argparse.ArgumentParser(
        description="Send commands to a DUT's Zephyr shell over UART and print the replies."
    )
    parser.add_argument("commands", nargs="*", help="Commands to send, in order.")
    parser.add_argument(
        "-c",
        "--command",
        action="append",
        default=[],
        dest="extra_commands",
        help="A command to send. Repeatable, and combines with positional ones.",
    )
    parser.add_argument("--port", required=True, help="Shell UART, e.g. /dev/ttyACM1.")
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUD, help="Defaults to 115200.")
    parser.add_argument(
        "--script",
        type=Path,
        help="File of commands, one per line. Blank lines and #comments are skipped.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_S,
        help=f"Seconds to wait for each command's prompt. Defaults to {DEFAULT_TIMEOUT_S}.",
    )
    parser.add_argument(
        "--sync-timeout",
        type=float,
        default=DEFAULT_SYNC_TIMEOUT_S,
        help=f"Seconds to wait for the first prompt. Defaults to {DEFAULT_SYNC_TIMEOUT_S}.",
    )
    parser.add_argument("--prompt", default=DEFAULT_PROMPT, help="Prompt regex.")
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Print one JSON object per command instead of plain text.",
    )
    return parser.parse_args(argv)


def collect_commands(args: argparse.Namespace) -> list[str]:
    """Every command to run, positional then -c then --script, in that order."""
    commands = list(args.commands) + list(args.extra_commands)
    if args.script is not None:
        for line in args.script.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                commands.append(stripped)
    return commands


def main(argv: list[str] | None = None) -> int:
    """Run the requested commands, printing each response."""
    args = parse_args(argv)
    commands = collect_commands(args)
    if not commands:
        print("error: no commands given (pass them positionally, with -c, or via --script)")
        return EXIT_OK

    shell = DutShell(
        port=args.port,
        baud=args.baud,
        prompt=args.prompt,
        timeout_s=args.timeout,
    )
    try:
        shell.open(sync_timeout_s=args.sync_timeout)
    except ConnectionError as error:
        print(f"error: {error}")
        return EXIT_CONNECTION_ERROR
    except TimeoutError as error:
        print(f"error: {error}")
        return EXIT_TIMEOUT

    try:
        return _run_commands(shell, commands, args.as_json)
    finally:
        shell.close()


def _run_commands(shell: DutShell, commands: list[str], as_json: bool) -> int:
    """Send each command in turn, stopping at the first one that times out."""
    for command in commands:
        try:
            response = shell.command(command)
        except TimeoutError as error:
            print(f"error: {error}")
            return EXIT_TIMEOUT
        except ConnectionError as error:
            print(f"error: {error}")
            return EXIT_CONNECTION_ERROR

        if as_json:
            print(json.dumps(_as_dict(response)))
        else:
            _print_plain(response)
    return EXIT_OK


def _as_dict(response) -> dict:
    """One response as a JSON-serializable dict."""
    return {
        "command": response.command,
        "lines": response.lines,
        "log_lines": response.log_lines,
        "error": response.error,
        "duration_s": round(response.duration_s, 3),
    }


def _print_plain(response) -> None:
    """Print one response the way a terminal session would show it."""
    print(f"$ {response.command}")
    for line in response.lines:
        print(f"  {line}")
    for line in response.log_lines:
        print(f"  ~ {line}")
    if response.error:
        print(f"  ! the shell refused this command: {response.error}")
