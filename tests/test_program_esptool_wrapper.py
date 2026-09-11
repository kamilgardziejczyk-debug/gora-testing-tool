"""Tests for the !ProgramEsptool tag.

Covers parsing and the flash plan - which images get written where - against
files in a temporary directory. Nothing here connects to a chip.

Run with `python3 -m unittest discover tests` from the repository root.
"""

import sys
import tempfile
import unittest
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wrappers.program_esptool_wrapper import ProgramEsptoolWrapper  # noqa: E402


def build(text: str) -> ProgramEsptoolWrapper:
    """Parse one !ProgramEsptool command from YAML."""
    wrapper = ProgramEsptoolWrapper(yaml.compose(text))
    wrapper.parse()
    return wrapper


class ParseTests(unittest.TestCase):
    def test_addresses_default_to_the_single_app_layout(self):
        wrapper = build("!ProgramEsptool\nfirmware: app.bin")
        self.assertEqual(
            (wrapper.bootloader_address, wrapper.partition_table_address, wrapper.address),
            (0x0, 0x8000, 0x10000),
        )

    def test_every_image_takes_its_own_address(self):
        wrapper = build(
            "!ProgramEsptool\nfirmware: app.bin\naddress: 0x20000\n"
            "bootloader_address: 0x1000\npartition_table_address: 0x9000\n"
            "ota_data: ota.bin\nota_data_address: 0x10000"
        )
        self.assertEqual(
            (wrapper.bootloader_address, wrapper.partition_table_address, wrapper.ota_data_address, wrapper.address),
            (0x1000, 0x9000, 0x10000, 0x20000),
        )

    def test_ota_data_needs_an_address(self):
        """otadata has no standard offset; it comes from the partition table."""
        with self.assertRaisesRegex(ValueError, "ota_data_address"):
            build("!ProgramEsptool\nfirmware: app.bin\nota_data: ota.bin")

    def test_a_bad_address_names_its_key(self):
        with self.assertRaisesRegex(ValueError, "'bootloader_address'"):
            build("!ProgramEsptool\nfirmware: app.bin\nbootloader_address: zero")


class FlashPlanTests(unittest.TestCase):
    """_resolve_flash_data() against images of a real OTA build's sizes."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        sizes = {"bootloader.bin": 20832, "partition-table.bin": 3072, "ota_data_initial.bin": 8192, "app.bin": 895232}
        for name, size in sizes.items():
            (self.dir / name).write_bytes(b"\xff" * size)

    def plan(self, text: str) -> list:
        """The (address, path) list one command would flash."""
        wrapper = build("!ProgramEsptool\nport: /dev/null\n" + text)
        wrapper.firmware_dir = str(self.dir)
        return wrapper._resolve_flash_data()

    def test_app_alone(self):
        self.assertEqual(self.plan("firmware: app.bin"), [(0x10000, str(self.dir / "app.bin"))])

    def test_ota_layout_is_written_in_address_order(self):
        plan = self.plan(
            "firmware: app.bin\naddress: 0x20000\nota_data: ota_data_initial.bin\nota_data_address: 0x10000\n"
            "partition_table: partition-table.bin\nbootloader: bootloader.bin"
        )
        self.assertEqual([address for address, _ in plan], [0x0, 0x8000, 0x10000, 0x20000])

    def test_an_app_left_at_the_default_address_overlaps_otadata(self):
        with self.assertRaisesRegex(ValueError, "overlaps"):
            self.plan("firmware: app.bin\nota_data: ota_data_initial.bin\nota_data_address: 0x10000")

    def test_images_sharing_a_flash_sector_overlap(self):
        """esptool erases whole 4 KB sectors, so a byte-level gap is not enough."""
        with self.assertRaisesRegex(ValueError, "overlaps"):
            self.plan(
                "firmware: app.bin\nbootloader: bootloader.bin\n"
                "partition_table: partition-table.bin\npartition_table_address: 0x5800"
            )


if __name__ == "__main__":
    unittest.main()
