"""Tests for finding the block device behind a hub port.

A fake sysfs tree stands in for the real one, so the walk from "hub location
plus port number" to "/dev node" is exercised without a hub, a card, or root.

Run with `python3 -m unittest discover tests` from the repository root.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.mass_storage import device as device_module  # noqa: E402
from tools.mass_storage.device import (  # noqa: E402
    BlockDeviceNotFoundError,
    MassStorageError,
    find_block_device,
    usb_device_path,
)

HUB = "1-1.2"
PORT = 1


class FakeSysfs:
    """A temporary tree shaped like the sysfs paths discovery walks."""

    def __init__(self, root: Path):
        self.usb = root / "sys/bus/usb/devices"
        self.block = root / "sys/block"
        self.dev = root / "dev"
        for path in (self.usb, self.block, self.dev):
            path.mkdir(parents=True)

    def add_disk(self, name: str, port: int = PORT, hub: str = HUB) -> None:
        """Present `name` as a disk on `port`, with its /dev node and sysfs entry."""
        scsi = self.usb / f"{hub}.{port}" / f"{hub}.{port}:1.0" / "host0" / "target0:0:0" / "0:0:0:0"
        (scsi / "block" / name).mkdir(parents=True)
        (self.block / name).mkdir(parents=True, exist_ok=True)
        (self.dev / name).touch()

    def add_partition(self, disk: str, name: str) -> None:
        """Give `disk` a partition, the way the kernel marks one in sysfs."""
        partition_dir = self.block / disk / name
        partition_dir.mkdir(parents=True, exist_ok=True)
        (partition_dir / "partition").write_text("1\n")
        (self.dev / name).touch()

    def add_empty_port(self, port: int, hub: str = HUB) -> None:
        """Present a USB device that offers no block device at all."""
        (self.usb / f"{hub}.{port}" / f"{hub}.{port}:1.0").mkdir(parents=True)


class DiscoveryTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.fake = FakeSysfs(Path(self._tmp.name))
        patches = {
            "SYSFS_USB_DEVICES": self.fake.usb,
            "SYSFS_BLOCK": self.fake.block,
            "DEV_DIR": self.fake.dev,
            "PARTITION_GRACE_S": 0.05,
            "POLL_INTERVAL_S": 0.01,
        }
        for name, value in patches.items():
            patcher = mock.patch.object(device_module, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def tearDown(self):
        self._tmp.cleanup()


class PathTests(unittest.TestCase):
    def test_port_names_the_sysfs_device(self):
        """A device on port 1 of the hub at 1-1.2 is 1-1.2.1 - the whole trick."""
        self.assertEqual(usb_device_path("1-1.2", 1).name, "1-1.2.1")
        self.assertEqual(usb_device_path("1-1.2", 4).name, "1-1.2.4")


class FindTests(DiscoveryTestCase):
    def test_disk_with_a_partition(self):
        self.fake.add_disk("sda")
        self.fake.add_partition("sda", "sda1")

        found = find_block_device(HUB, PORT, settle_timeout_s=1)
        self.assertEqual(found.disk, self.fake.dev / "sda")
        self.assertEqual(found.partition, self.fake.dev / "sda1")
        self.assertEqual(found.filesystem_node, self.fake.dev / "sda1")

    def test_superfloppy_has_no_partition(self):
        """Some devices put the filesystem straight on the disk."""
        self.fake.add_disk("sda")

        found = find_block_device(HUB, PORT, settle_timeout_s=1)
        self.assertIsNone(found.partition)
        self.assertEqual(found.filesystem_node, self.fake.dev / "sda")

    def test_lowest_numbered_partition_wins(self):
        self.fake.add_disk("sda")
        self.fake.add_partition("sda", "sda2")
        self.fake.add_partition("sda", "sda1")

        self.assertEqual(find_block_device(HUB, PORT, 1).partition, self.fake.dev / "sda1")

    def test_another_port_is_not_picked_up(self):
        """The wrong disk is the failure mode this whole module exists to avoid."""
        self.fake.add_disk("sdb", port=3)

        with self.assertRaises(BlockDeviceNotFoundError):
            find_block_device(HUB, PORT, settle_timeout_s=0.2)

    def test_nothing_enumerated_says_so(self):
        with self.assertRaisesRegex(BlockDeviceNotFoundError, "nothing enumerated"):
            find_block_device(HUB, PORT, settle_timeout_s=0.2)

    def test_non_storage_device_is_reported_differently(self):
        """A device that is there but is not a disk needs a different fix."""
        self.fake.add_empty_port(PORT)

        with self.assertRaisesRegex(BlockDeviceNotFoundError, "presented no block device"):
            find_block_device(HUB, PORT, settle_timeout_s=0.2)

    def test_two_disks_on_one_port_is_refused(self):
        """A multi-slot reader: picking one would be a guess."""
        self.fake.add_disk("sda")
        self.fake.add_disk("sdb")

        with self.assertRaisesRegex(MassStorageError, "ambiguous"):
            find_block_device(HUB, PORT, settle_timeout_s=0.2)

    def test_sysfs_entry_without_a_dev_node_keeps_waiting(self):
        """sysfs can name a disk a moment before udev creates its node."""
        self.fake.add_disk("sda")
        (self.fake.dev / "sda").unlink()

        with self.assertRaises(BlockDeviceNotFoundError):
            find_block_device(HUB, PORT, settle_timeout_s=0.2)

    def test_zero_timeout_rejected(self):
        with self.assertRaisesRegex(ValueError, "settle_timeout_s"):
            find_block_device(HUB, PORT, settle_timeout_s=0)


if __name__ == "__main__":
    unittest.main()
