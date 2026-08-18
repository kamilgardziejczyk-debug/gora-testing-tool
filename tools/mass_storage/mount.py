"""Mounting, unmounting and ejecting the DUT's card as a real host would.

The point of mounting rather than reading the raw blocks is that it exercises
the firmware. The tracker hands its card to the host by unmounting it from its
own filesystem and exposing it over SCSI, and takes it back in two distinct
steps: an eject returns control to the ESP32, and only the later loss of VBUS
makes it re-mount the card for itself. A raw-block reader triggers neither.

That is also why `eject` is a separate call from `unmount`. Linux `umount`
flushes and detaches the filesystem; it never sends SCSI START_STOP_UNIT, so
the firmware never learns it happened. Only `eject` reaches it - and keeping
the two apart lets a scenario test the cable-yank path (unmount, then cut
power without ejecting) as deliberately as the clean one.
"""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .device import BlockDevice, MassStorageError


LOGGER = logging.getLogger(__name__)

MOUNT_BIN = "mount"
UMOUNT_BIN = "umount"
# From sg3-utils. Preferred over eject(1), which unmounts as a side effect and
# would blur the two steps this module exists to keep apart.
SG_START_BIN = "sg_start"
LSBLK_BIN = "lsblk"

# Where a scenario's mounts live. Under /run because it is tmpfs on every
# distribution this runs on, so a mountpoint left behind by a killed run does
# not survive a reboot.
DEFAULT_MOUNT_ROOT = Path("/run/gora")

DEFAULT_TIMEOUT_S = 30.0

READ_ONLY = "ro"
READ_WRITE = "rw"
MODES = (READ_ONLY, READ_WRITE)

# noatime because a read-only test should not be rewriting the DUT's card
# just by listing it, and because it is meaningless on a `ro` mount anyway -
# stated explicitly so an `rw` mount does not quietly start doing it.
MOUNT_OPTIONS = ("noatime",)


class ToolNotFoundError(MassStorageError):
    """A required binary is not installed or not on PATH."""


class CommandError(MassStorageError):
    """A command ran but exited non-zero, or did not finish in time."""


class MountError(MassStorageError):
    """The filesystem could not be mounted or unmounted."""


@dataclass(frozen=True)
class Mount:
    """One mounted filesystem, as returned by `mount`."""

    device: BlockDevice
    mountpoint: Path
    mode: str

    def __str__(self) -> str:
        """One line naming what is mounted where, as a log line wants it."""
        return f"{self.device.filesystem_node} at {self.mountpoint} ({self.mode})"


def mount(
    device: BlockDevice,
    mountpoint: Path | None = None,
    mode: str = READ_ONLY,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> Mount:
    """Mount `device`'s filesystem, read-only unless `mode` is `rw`.

    No `-t` is passed, so the kernel identifies the filesystem itself. That is
    deliberately more permissive than naming `vfat`: the card is expected to
    be FAT32, but forcing a type onto a card that turns out to be exFAT would
    fail with a far less obvious message than letting detection do its job.
    """
    if mode not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}, got '{mode}'")

    target = mountpoint if mountpoint is not None else default_mountpoint(device)
    target.mkdir(parents=True, exist_ok=True)
    if is_mounted(target):
        raise MountError(f"{target} already has something mounted on it")

    options = ",".join((mode, *MOUNT_OPTIONS))
    _run([MOUNT_BIN, "-o", options, str(device.filesystem_node), str(target)], timeout_s)

    LOGGER.info("Mounted %s at %s (%s, %s)", device.filesystem_node, target, mode, _describe(device))
    return Mount(device=device, mountpoint=target, mode=mode)


def unmount(mountpoint: Path, lazy: bool = False, timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
    """Flush and unmount whatever is at `mountpoint`.

    `os.sync()` first, unconditionally: a read-only mount has nothing to
    flush, and paying for it anyway is cheaper than depending on the caller
    having tracked the mode correctly before the DUT's card loses power.

    `lazy` adds `-l`, which detaches the filesystem now and cleans up when the
    last user lets go. Reserved for cleanup paths - it can hide a genuine
    "device is busy" from a scenario that should have been told about it.
    """
    if not is_mounted(mountpoint):
        LOGGER.info("Nothing mounted at %s, nothing to unmount", mountpoint)
        return

    os.sync()
    args = [UMOUNT_BIN, "-l", str(mountpoint)] if lazy else [UMOUNT_BIN, str(mountpoint)]
    _run(args, timeout_s)
    LOGGER.info("Unmounted %s%s", mountpoint, " (lazily)" if lazy else "")


def eject(device: BlockDevice, timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
    """Send SCSI START_STOP_UNIT with LoEj, telling the DUT to take its card back.

    This is the step the firmware actually observes: it answers by logging
    'TinyUSB: Storage control returned to ESP32' and then waits for the port
    to lose power before re-mounting the card for its own use.

    Addressed to the whole disk, not a partition - START_STOP_UNIT is a
    property of the unit, and a partition node would be the wrong target.
    """
    _run([SG_START_BIN, "--stop", "--loej", str(device.disk)], timeout_s)
    LOGGER.info("Ejected %s; the DUT should now report the card returned to it", device.disk)


def is_mounted(mountpoint: Path) -> bool:
    """Whether anything is currently mounted at `mountpoint`."""
    return os.path.ismount(mountpoint)


def default_mountpoint(device: BlockDevice, root: Path = DEFAULT_MOUNT_ROOT) -> Path:
    """Where `device` is mounted when a caller names no mountpoint."""
    return root / device.filesystem_node.name


def filesystem_type(device: BlockDevice, timeout_s: float = DEFAULT_TIMEOUT_S) -> str | None:
    """The filesystem on `device`, or None if it could not be identified.

    Informational only - `mount` detects the type itself. This exists so a
    failure can say *what* the card actually holds, which is the first thing
    anyone asks when a mount is refused.
    """
    try:
        output = _run(
            [LSBLK_BIN, "-no", "FSTYPE", str(device.filesystem_node)], timeout_s
        )
    except MassStorageError as error:
        LOGGER.debug("Could not read the filesystem type of %s: %s", device.filesystem_node, error)
        return None
    return output.strip() or None


def _describe(device: BlockDevice) -> str:
    """`device`'s filesystem type for a log line, without failing if unknown."""
    return filesystem_type(device) or "unrecognized filesystem"


def _run(command: Sequence[str], timeout_s: float) -> str:
    """Run `command` and return its stdout, mapping failures to this package's errors."""
    LOGGER.debug("Running %s", " ".join(command))
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout_s, check=False
        )
    except FileNotFoundError:
        raise ToolNotFoundError(
            f"{command[0]} is not installed or not on PATH. On a test node this comes from "
            f"util-linux (mount/umount/lsblk) or sg3-utils (sg_start)."
        ) from None
    except subprocess.TimeoutExpired:
        raise CommandError(f"{' '.join(command)} did not finish within {timeout_s}s") from None

    if result.returncode != 0:
        raise CommandError(
            f"{' '.join(command)} exited {result.returncode}: "
            f"{(result.stderr or result.stdout).strip()}"
        )
    return result.stdout
