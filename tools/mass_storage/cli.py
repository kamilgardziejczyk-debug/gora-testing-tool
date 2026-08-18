"""Command line for the DUT's SD card over USB mass storage.

Only parses input and prints results - the work goes through `device`,
`mount` and `files`, the same API the `!DutStorage` wrapper uses.

This exists to be run by hand during bring-up, because the interesting half of
this feature is on the *device* side: the tracker only learns the card has
been given back when an eject reaches it. Running `eject` here with the DUT's
console open is how you confirm that, before any scenario depends on it.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Callable

from .device import (
    DEFAULT_SETTLE_TIMEOUT_S,
    BlockDevice,
    MassStorageError,
    find_block_device,
)
from .files import copy_from, delete, list_entries, read_file
from .mount import (
    DEFAULT_MOUNT_ROOT,
    DEFAULT_TIMEOUT_S,
    MODES,
    READ_ONLY,
    READ_WRITE,
    Mount,
    default_mountpoint,
    eject,
    filesystem_type,
    is_mounted,
    mount,
    unmount,
)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2


def build_arg_parser() -> argparse.ArgumentParser:
    """The CLI's flags and subcommands."""
    parser = argparse.ArgumentParser(
        prog="mass_storage.py",
        description="Inspect the DUT's SD card exposed over USB mass storage.",
    )
    parser.add_argument(
        "-l", "--location", required=True, help="USB2 location of the hub, e.g. 1-1.2"
    )
    parser.add_argument("-p", "--port", type=int, required=True, help="Hub port the DUT is on")
    parser.add_argument(
        "--settle-timeout",
        type=float,
        default=DEFAULT_SETTLE_TIMEOUT_S,
        help=f"Seconds to wait for the card to enumerate (default: {DEFAULT_SETTLE_TIMEOUT_S})",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_S,
        help=f"Seconds any one command may take (default: {DEFAULT_TIMEOUT_S})",
    )
    parser.add_argument(
        "--mount-root",
        type=Path,
        default=DEFAULT_MOUNT_ROOT,
        help=f"Where cards are mounted (default: {DEFAULT_MOUNT_ROOT})",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Log what is being run")

    _add_subcommands(parser)
    return parser


def _add_subcommands(parser: argparse.ArgumentParser) -> None:
    """Add the card lifecycle and file subcommands to `parser`."""
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("discover", help="Show the block device on the port, and its filesystem")

    mount_parser = subparsers.add_parser("mount", help="Mount the card")
    mount_parser.add_argument(
        "--mode", choices=MODES, default=READ_ONLY, help=f"Mount mode (default: {READ_ONLY})"
    )

    subparsers.add_parser("unmount", help="Flush and unmount the card")
    subparsers.add_parser("eject", help="Tell the DUT to take its card back (SCSI START_STOP_UNIT)")

    list_parser = subparsers.add_parser("list", help="List a directory on the card")
    list_parser.add_argument("path", nargs="?", default="/", help="Card path (default: /)")

    read_parser = subparsers.add_parser("read", help="Print a file from the card")
    read_parser.add_argument("path", help="Card path of the file")

    copy_parser = subparsers.add_parser("copy", help="Copy files off the card")
    copy_parser.add_argument("path", help="Card path or glob, e.g. /logs/*.log")
    copy_parser.add_argument("dest", type=Path, help="Host directory to copy into")

    delete_parser = subparsers.add_parser(
        "delete", help="Delete files or directories from the card (needs a rw mount)"
    )
    delete_parser.add_argument("path", help="Card path or glob, e.g. /logs/* or '**/tmp'")


def main(argv: list[str] | None = None) -> int:
    """Entry point: resolve the port to a device, then dispatch."""
    args = build_arg_parser().parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.command is None:
        build_arg_parser().print_help()
        return EXIT_USAGE

    return _guard(lambda: _dispatch(args))


def _dispatch(args: argparse.Namespace) -> int:
    """Run the chosen subcommand against the device on the given port."""
    device = find_block_device(args.location, args.port, args.settle_timeout)
    mountpoint = default_mountpoint(device, args.mount_root)

    if args.command == "discover":
        print(f"{device}  {filesystem_type(device, args.timeout) or 'unrecognized filesystem'}")
        print(f"mountpoint would be {mountpoint} ({'mounted' if is_mounted(mountpoint) else 'not mounted'})")
        return EXIT_OK

    if args.command == "mount":
        print(mount(device, mountpoint, args.mode, args.timeout))
        return EXIT_OK

    if args.command == "unmount":
        unmount(mountpoint, timeout_s=args.timeout)
        print(f"unmounted {mountpoint}")
        return EXIT_OK

    if args.command == "eject":
        eject(device, args.timeout)
        print(f"ejected {device.disk}; watch the DUT console for the card being returned")
        return EXIT_OK

    return _run_file_command(args, _require_mounted(device, mountpoint, args.command))


def _run_file_command(args: argparse.Namespace, card: Mount) -> int:
    """Run one of the read-only card commands against an already-mounted card."""
    if args.command == "list":
        listing = list_entries(card, args.path)
        for name in listing.dirs:
            print(f"{name}/")
        for name in listing.files:
            print(name)
        return EXIT_OK

    if args.command == "read":
        print(read_file(card, args.path).content, end="")
        return EXIT_OK

    if args.command == "delete":
        for removed in delete(card, args.path):
            print(removed)
        return EXIT_OK

    for copied in copy_from(card, args.path, args.dest):
        print(copied)
    return EXIT_OK


def _require_mounted(device: BlockDevice, mountpoint: Path, command: str) -> Mount:
    """The card as an already-mounted filesystem, or an error saying to mount it.

    The file commands deliberately do not mount on demand: mounting is a step
    the DUT observes, so it stays something a caller asks for explicitly rather
    than a side effect of listing a directory.

    The mode is read from `/proc/mounts` rather than assumed, because `delete`
    refuses on a read-only card and this process may not be the one that
    mounted it.
    """
    if not is_mounted(mountpoint):
        raise MassStorageError(
            f"nothing is mounted at {mountpoint}. Run the 'mount' command first."
        )
    mode = READ_WRITE if _is_writable(mountpoint) else READ_ONLY
    if command == "delete" and mode != READ_WRITE:
        raise MassStorageError(
            f"{mountpoint} is mounted read-only. Unmount it and mount again with --mode rw."
        )
    return Mount(device=device, mountpoint=mountpoint, mode=mode)


def _is_writable(mountpoint: Path) -> bool:
    """Whether `mountpoint` is currently mounted read-write, per /proc/mounts."""
    try:
        entries = Path("/proc/mounts").read_text().splitlines()
    except OSError:
        return False

    target = str(mountpoint)
    for entry in entries:
        fields = entry.split()
        if len(fields) >= 4 and fields[1] == target:
            return "rw" in fields[3].split(",")
    return False


def _guard(action: Callable[[], int]) -> int:
    """Run `action`, turning a bad argument or a hardware failure into an exit code.

    Split the same way `tools/usb_hub/cli.py` splits them, so a caller can tell
    "you asked wrong" apart from "the rig is not in the state you thought".
    """
    try:
        return action()
    except ValueError as error:
        print(f"error: {error}")
        return EXIT_USAGE
    except MassStorageError as error:
        print(f"error: {error}")
        return EXIT_ERROR
