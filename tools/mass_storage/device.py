"""Finding the block device behind one USB hub port.

The DUT's SD card reaches the host as a USB mass storage device on a known
hub port - the same port `!UsbSwitch` powers. This module turns that port
number into the `/dev` node the kernel gave it.

It walks sysfs from the port rather than looking at what is new in `/dev`,
because the alternative is unsafe in a way that is easy to miss: `/dev/sd*`
ordering is not stable across runs, and a rig that guesses wrong writes to
whichever disk it happened to pick - on a Raspberry Pi test node, quite
possibly its own. Anchoring to the physical port means a wrong answer is an
error rather than a wrong disk.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path


LOGGER = logging.getLogger(__name__)

SYSFS_USB_DEVICES = Path("/sys/bus/usb/devices")
SYSFS_BLOCK = Path("/sys/block")
DEV_DIR = Path("/dev")

# Enumeration plus the SCSI probe takes a second or two after the port powers
# up, so discovery polls rather than looking once.
DEFAULT_SETTLE_TIMEOUT_S = 15.0
POLL_INTERVAL_S = 0.25

# Once the disk itself is visible, the kernel reads its partition table almost
# immediately. This is how long to keep waiting for a partition to show up
# before concluding there is genuinely no partition table and the filesystem
# sits directly on the disk (a "superfloppy", which is how some devices format
# removable media).
PARTITION_GRACE_S = 2.0


class MassStorageError(Exception):
    """Base class for every failure this package raises."""


class BlockDeviceNotFoundError(MassStorageError):
    """No block device appeared on the given hub port."""


@dataclass(frozen=True)
class BlockDevice:
    """The disk a hub port presented, and its first partition if it has one."""

    disk: Path
    """The whole-disk node, e.g. `/dev/sda`. What `eject` is addressed to."""

    partition: Path | None
    """The first partition node, e.g. `/dev/sda1`, or None for a superfloppy."""

    usb_path: Path
    """The sysfs directory of the USB device this came from, for error messages."""

    @property
    def filesystem_node(self) -> Path:
        """The node holding the filesystem: the partition, or the disk itself."""
        return self.partition if self.partition is not None else self.disk

    def __str__(self) -> str:
        """One line naming both nodes, as a log line wants them."""
        if self.partition is None:
            return f"{self.disk} (no partition table)"
        return f"{self.partition} on {self.disk}"


def usb_device_path(hub_location: str, port: int) -> Path:
    """The sysfs directory for whatever is plugged into `port` of the hub.

    USB topology names a device by the path taken to reach it, so a device on
    port 1 of a hub at `1-1.2` is `1-1.2.1`. That is the whole trick this
    module rests on: the port is known before anything is plugged in, so the
    device's sysfs name is known too.
    """
    return SYSFS_USB_DEVICES / f"{hub_location}.{port}"


def find_block_device(
    hub_location: str,
    port: int,
    settle_timeout_s: float = DEFAULT_SETTLE_TIMEOUT_S,
) -> BlockDevice:
    """Wait for a block device on `port` of the hub at `hub_location`.

    Raises `BlockDeviceNotFoundError` if none appears within
    `settle_timeout_s`, naming the port rather than the sysfs path, since the
    port is what a scenario author can act on.
    """
    if settle_timeout_s <= 0:
        raise ValueError(f"settle_timeout_s must be > 0, got {settle_timeout_s}")

    usb_path = usb_device_path(hub_location, port)
    deadline = time.monotonic() + settle_timeout_s

    while True:
        disk = _find_disk(usb_path)
        if disk is not None:
            device = _with_partition(disk, usb_path)
            LOGGER.info("USB port %s presented %s", port, device)
            return device
        if time.monotonic() >= deadline:
            raise BlockDeviceNotFoundError(_not_found_message(usb_path, port, settle_timeout_s))
        time.sleep(POLL_INTERVAL_S)


def _find_disk(usb_path: Path) -> Path | None:
    """The whole-disk node under `usb_path`, or None if nothing is there yet.

    The glob crosses the USB interface, the SCSI host, the target and the LUN -
    none of whose numbers are predictable - to reach the `block/` directory the
    disk is named in.
    """
    names = sorted(entry.name for entry in usb_path.glob("*/host*/target*/*/block/*"))
    if not names:
        return None
    if len(names) > 1:
        # A card reader with several slots would do this. Refusing is better
        # than picking one, since which slot holds the DUT's card is not
        # something this module can know.
        raise MassStorageError(
            f"{usb_path.name} presented {len(names)} block devices ({', '.join(names)}), "
            f"so which one holds the DUT's card is ambiguous."
        )

    node = DEV_DIR / names[0]
    # sysfs can name the disk a moment before udev creates its node.
    return node if node.exists() else None


def _with_partition(disk: Path, usb_path: Path) -> BlockDevice:
    """Pair `disk` with its first partition, waiting briefly for one to appear."""
    deadline = time.monotonic() + PARTITION_GRACE_S
    while True:
        partition = _first_partition(disk)
        if partition is not None:
            return BlockDevice(disk=disk, partition=partition, usb_path=usb_path)
        if time.monotonic() >= deadline:
            LOGGER.info(
                "%s has no partition table after %.1fs; treating the filesystem as sitting "
                "directly on the disk",
                disk,
                PARTITION_GRACE_S,
            )
            return BlockDevice(disk=disk, partition=None, usb_path=usb_path)
        time.sleep(POLL_INTERVAL_S)


def _first_partition(disk: Path) -> Path | None:
    """The lowest-numbered partition of `disk`, or None if it has none.

    Partitions appear in sysfs as subdirectories of the disk carrying a
    `partition` file, which is what distinguishes them from the disk's other
    attribute directories (`queue`, `power`, `holders` and friends).
    """
    block_dir = SYSFS_BLOCK / disk.name
    if not block_dir.is_dir():
        return None

    names = sorted(
        entry.name for entry in block_dir.iterdir() if (entry / "partition").is_file()
    )
    for name in names:
        node = DEV_DIR / name
        if node.exists():
            return node
    return None


def _not_found_message(usb_path: Path, port: int, settle_timeout_s: float) -> str:
    """Explain an empty port, distinguishing the two reasons it can be empty."""
    if not usb_path.exists():
        return (
            f"nothing enumerated on USB port {port} within {settle_timeout_s}s "
            f"({usb_path} does not exist). Check the port is powered (!UsbSwitch state: 1) "
            f"and that the DUT is exposing its card - the firmware logs "
            f"'USB Storage is now ACTIVE' when it has."
        )
    return (
        f"a USB device is on port {port} ({usb_path.name}) but it presented no block device "
        f"within {settle_timeout_s}s. It enumerated as something other than mass storage, or "
        f"the kernel's usb-storage module is not available to this process."
    )
