"""Standalone DUT console capture, for checking a bench before running scenarios.

Answers "is the DUT actually talking on this port?" without involving a
scenario, using the same reader and log files a real run would produce.
"""

from __future__ import annotations

import argparse
import logging
import time
from pathlib import Path

from .reader import DEFAULT_BAUD, DutLogger
from .session import LogSession

EXIT_OK = 0
EXIT_CONNECTION_ERROR = 2


def build_arg_parser() -> argparse.ArgumentParser:
    """Build the standalone capture parser."""
    parser = argparse.ArgumentParser(description="Capture a DUT's serial console to log files")
    parser.add_argument("--port", required=True, help="DUT console port, e.g. /dev/ttyACM0")
    parser.add_argument(
        "--baud", type=int, default=DEFAULT_BAUD, help=f"Baud rate (default: {DEFAULT_BAUD})"
    )
    parser.add_argument(
        "--out",
        default="results/dut_capture.html",
        help="Report path the log names are derived from; the .html itself is not "
        "written (default: results/dut_capture.html)",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=None,
        help="Seconds to capture for (default: until Ctrl-C)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Capture until the duration elapses or Ctrl-C, then report where it went."""
    args = build_arg_parser().parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    session = LogSession(Path(args.out))
    session.open()
    logger = DutLogger(session, port=args.port, baud=args.baud)

    try:
        logger.start()
    except ConnectionError as error:
        print(f"error: {error}")
        session.close()
        return EXIT_CONNECTION_ERROR

    session.write_marker(f"STANDALONE CAPTURE START: {args.port}")
    try:
        _wait(args.duration)
    except KeyboardInterrupt:
        print()
    finally:
        session.write_marker("STANDALONE CAPTURE END")
        logger.stop()
        session.close()

    print(f"device log:   {session.device_path}")
    print(f"tool log:     {session.tool_path}")
    print(f"combined log: {session.combined_path}")
    return EXIT_OK


def _wait(duration_s: float | None) -> None:
    """Block for `duration_s`, or until interrupted when it is None."""
    if duration_s is not None:
        time.sleep(duration_s)
        return
    print("Capturing... press Ctrl-C to stop.")
    while True:
        time.sleep(0.5)
