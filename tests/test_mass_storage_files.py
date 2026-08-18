"""Tests for reading a mounted card.

A `Mount` pointing at a temporary directory stands in for a real one: every
function here works on the mounted filesystem, so nothing below needs a card,
a hub, or root.

Run with `python3 -m unittest discover tests` from the repository root.
"""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.mass_storage.device import BlockDevice  # noqa: E402
from tools.mass_storage.files import (  # noqa: E402
    CardCopyError,
    CardPathNotFoundError,
    CardReadOnlyError,
    PathEscapesCardError,
    copy_from,
    delete,
    list_entries,
    read_file,
    resolve,
)
from tools.mass_storage.mount import Mount  # noqa: E402


class CardTestCase(unittest.TestCase):
    """A temporary directory standing in for a mounted card."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "card"
        (self.root / "logs").mkdir(parents=True)
        (self.root / "logs" / "tracker-2026-08-18.log").write_text("boot ok\nsample 1\n")
        (self.root / "logs" / "tracker-2026-08-17.log").write_text("older\n")
        (self.root / "BOOT.CFG").write_text("cfg\n")
        (self.root / "empty").mkdir()

        device = BlockDevice(
            disk=Path("/dev/sda"), partition=Path("/dev/sda1"), usb_path=Path("/sys/fake")
        )
        self.mount = Mount(device=device, mountpoint=self.root, mode="ro")

    def tearDown(self):
        self._tmp.cleanup()


class ListTests(CardTestCase):
    def test_root_listing(self):
        listing = list_entries(self.mount, "/")
        self.assertEqual(listing.files, ["BOOT.CFG"])
        self.assertEqual(listing.dirs, ["empty", "logs"])
        self.assertEqual(listing.entries, ["BOOT.CFG", "empty", "logs"])

    def test_subdirectory_listing_is_sorted(self):
        listing = list_entries(self.mount, "/logs")
        self.assertEqual(
            listing.files, ["tracker-2026-08-17.log", "tracker-2026-08-18.log"]
        )
        self.assertEqual(listing.dirs, [])

    def test_empty_directory(self):
        listing = list_entries(self.mount, "/empty")
        self.assertEqual(listing.entries, [])

    def test_listing_a_file_is_an_error(self):
        with self.assertRaisesRegex(CardPathNotFoundError, "it is a file"):
            list_entries(self.mount, "/BOOT.CFG")

    def test_listing_a_missing_directory_is_an_error(self):
        with self.assertRaisesRegex(CardPathNotFoundError, "does not exist"):
            list_entries(self.mount, "/nope")


class ReadTests(CardTestCase):
    def test_read_content_and_size(self):
        result = read_file(self.mount, "/logs/tracker-2026-08-18.log")
        self.assertEqual(result.content, "boot ok\nsample 1\n")
        self.assertEqual(result.size, 17)
        self.assertEqual(result.lines, ["boot ok", "sample 1"])

    def test_reading_a_directory_is_an_error(self):
        with self.assertRaisesRegex(CardPathNotFoundError, "it is a directory"):
            read_file(self.mount, "/logs")

    def test_undecodable_bytes_do_not_raise(self):
        """A log truncated mid-write is the thing a test wants to assert about."""
        (self.root / "binary.log").write_bytes(b"ok\xff\xfetail")
        result = read_file(self.mount, "/binary.log")
        self.assertIn("ok", result.content)
        self.assertIn("tail", result.content)

    def test_size_is_bytes_not_decoded_characters(self):
        """Replacement changes the character count, so {size} must come from the file."""
        (self.root / "binary.log").write_bytes(b"ok\xff\xfetail")
        result = read_file(self.mount, "/binary.log")
        self.assertEqual(result.size, 8)


class CopyTests(CardTestCase):
    def setUp(self):
        super().setUp()
        self.dest = Path(self._tmp.name) / "out"

    def test_copy_glob(self):
        copied = copy_from(self.mount, "/logs/*.log", self.dest)
        self.assertEqual(
            copied, ["/logs/tracker-2026-08-17.log", "/logs/tracker-2026-08-18.log"]
        )
        self.assertTrue((self.dest / "tracker-2026-08-18.log").is_file())

    def test_copy_single_file(self):
        copied = copy_from(self.mount, "/BOOT.CFG", self.dest)
        self.assertEqual(copied, ["/BOOT.CFG"])
        self.assertEqual((self.dest / "BOOT.CFG").read_text(), "cfg\n")

    def test_copy_directory_tree(self):
        copy_from(self.mount, "/logs", self.dest)
        self.assertTrue((self.dest / "logs" / "tracker-2026-08-18.log").is_file())

    def test_matching_nothing_is_an_error(self):
        """A run that collected no logs has found a problem, not succeeded."""
        with self.assertRaisesRegex(CardPathNotFoundError, "nothing on the card matches"):
            copy_from(self.mount, "/logs/*.csv", self.dest)

    def test_copying_the_root_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "not the card root"):
            copy_from(self.mount, "/", self.dest)


class PartialCopyFailureTests(CardTestCase):
    """A bad match must not cost the run every match after it.

    This is the exact shape of the hardware failure: `.Trash-1000` sorts
    before `session_*` and was unreadable, so the un-hardened copy_from threw
    before ever attempting the DUT's own directories.
    """

    def test_an_early_unreadable_match_does_not_block_later_ones(self):
        (self.root / ".Trash-1000").mkdir()
        (self.root / "session_54").mkdir()
        (self.root / "session_54" / "log.txt").write_text("data\n")
        dest = Path(self._tmp.name) / "out"

        real_copytree = shutil.copytree

        def flaky_copytree(source, *args, **kwargs):
            if source.name == ".Trash-1000":
                raise OSError(5, "Input/output error")
            return real_copytree(source, *args, **kwargs)

        with mock.patch("tools.mass_storage.files.shutil.copytree", side_effect=flaky_copytree):
            with self.assertRaises(CardCopyError) as caught:
                copy_from(self.mount, "/*", dest)

        # The item after the broken one was still attempted and landed at dest.
        self.assertTrue((dest / "session_54" / "log.txt").is_file())
        self.assertIn("/.Trash-1000", str(caught.exception))
        self.assertIn("1 of", str(caught.exception))

    def test_failure_message_names_what_survived(self):
        (self.root / "broken").mkdir()
        dest = Path(self._tmp.name) / "out"

        real_copytree = shutil.copytree

        def flaky(source, *args, **kwargs):
            if source.name == "broken":
                raise OSError(5, "I/O error")
            return real_copytree(source, *args, **kwargs)

        with mock.patch("tools.mass_storage.files.shutil.copytree", side_effect=flaky):
            with self.assertRaises(CardCopyError) as caught:
                copy_from(self.mount, "/*", dest)

        message = str(caught.exception)
        self.assertIn("1 of", message)
        self.assertIn("copied successfully", message)
        # BOOT.CFG is a plain file (copy2, not copytree) and was not the
        # broken match, so it must have survived alongside the others.
        self.assertTrue((dest / "BOOT.CFG").is_file())
        self.assertTrue((dest / "logs" / "tracker-2026-08-18.log").is_file())

    def test_a_bad_directory_does_not_stop_a_later_file(self):
        (self.root / "zzz-broken").mkdir()
        dest = Path(self._tmp.name) / "out"

        real_copytree = shutil.copytree

        def flaky(source, *args, **kwargs):
            if source.name == "zzz-broken":
                raise OSError(5, "Input/output error")
            return real_copytree(source, *args, **kwargs)

        with mock.patch("tools.mass_storage.files.shutil.copytree", side_effect=flaky):
            with self.assertRaises(CardCopyError):
                copy_from(self.mount, "/*", dest)

        self.assertTrue((dest / "BOOT.CFG").is_file())


class DeleteTests(CardTestCase):
    def setUp(self):
        super().setUp()
        # A card the scenario mounted rw, which is what `delete` requires.
        self.writable = Mount(
            device=self.mount.device, mountpoint=self.root, mode="rw"
        )

    def test_delete_glob(self):
        deleted = delete(self.writable, "/logs/*.log")
        self.assertEqual(
            deleted, ["/logs/tracker-2026-08-17.log", "/logs/tracker-2026-08-18.log"]
        )
        self.assertEqual(list_entries(self.writable, "/logs").files, [])
        self.assertTrue((self.root / "logs").is_dir())

    def test_delete_a_directory_takes_its_contents(self):
        deleted = delete(self.writable, "/logs")
        self.assertEqual(deleted, ["/logs"])
        self.assertFalse((self.root / "logs").exists())

    def test_delete_directories_by_name_at_any_depth(self):
        """What `**` is for: one rule clearing every session directory."""
        (self.root / "a" / "tmp").mkdir(parents=True)
        (self.root / "b" / "tmp").mkdir(parents=True)
        (self.root / "b" / "keep").mkdir()

        deleted = delete(self.writable, "**/tmp")
        self.assertEqual(deleted, ["/a/tmp", "/b/tmp"])
        self.assertTrue((self.root / "b" / "keep").is_dir())

    def test_matching_nothing_is_a_success(self):
        """Clearing a card has to be idempotent or a rerun fails on its own success."""
        self.assertEqual(delete(self.writable, "/logs/*.csv"), [])

    def test_read_only_mount_is_refused(self):
        with self.assertRaisesRegex(CardReadOnlyError, "mode: rw"):
            delete(self.mount, "/logs/*.log")
        self.assertTrue((self.root / "logs" / "tracker-2026-08-18.log").is_file())

    def test_deleting_the_card_root_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "not the card root"):
            delete(self.writable, "/")
        self.assertTrue(self.root.is_dir())

    def test_delete_cannot_escape_the_card(self):
        outside = Path(self._tmp.name) / "outside.txt"
        outside.write_text("host file")

        with self.assertRaises(PathEscapesCardError):
            delete(self.writable, "/../outside.txt")
        self.assertTrue(outside.is_file())

    def test_delete_glob_cannot_escape_the_card(self):
        outside = Path(self._tmp.name) / "outside.txt"
        outside.write_text("host file")

        self.assertEqual(delete(self.writable, "/../*.txt"), [])
        self.assertTrue(outside.is_file())


class ProgressLoggingTests(CardTestCase):
    """A slow copy must look like it is working, not like it has hung."""

    def test_a_start_line_is_logged_before_any_copying(self):
        dest = Path(self._tmp.name) / "out"
        with self.assertLogs("tools.mass_storage.files", level="INFO") as captured:
            copy_from(self.mount, "/logs/*.log", dest)

        self.assertTrue(
            any("Copying 2 item(s)" in line for line in captured.output),
            captured.output,
        )
        # The start line comes before either item's own completion line.
        start_index = next(i for i, line in enumerate(captured.output) if "Copying 2 item(s)" in line)
        item_index = next(i for i, line in enumerate(captured.output) if "(1/2)" in line)
        self.assertLess(start_index, item_index)

    def test_each_item_logs_its_own_progress(self):
        dest = Path(self._tmp.name) / "out"
        with self.assertLogs("tools.mass_storage.files", level="INFO") as captured:
            copy_from(self.mount, "/logs/*.log", dest)

        self.assertTrue(any("(1/2)" in line for line in captured.output), captured.output)
        self.assertTrue(any("(2/2)" in line for line in captured.output), captured.output)


class ContainmentTests(CardTestCase):
    def test_relative_path_resolves_inside(self):
        self.assertEqual(resolve(self.mount, "/logs"), (self.root / "logs").resolve())

    def test_leading_slash_is_optional(self):
        self.assertEqual(resolve(self.mount, "logs"), resolve(self.mount, "/logs"))

    def test_parent_traversal_is_rejected(self):
        with self.assertRaises(PathEscapesCardError):
            resolve(self.mount, "/../../etc/passwd")

    def test_absolute_looking_escape_is_rejected(self):
        with self.assertRaises(PathEscapesCardError):
            resolve(self.mount, "/logs/../../../etc")

    def test_root_itself_is_allowed(self):
        self.assertEqual(resolve(self.mount, "/"), self.root.resolve())

    def test_glob_cannot_escape(self):
        with self.assertRaises(CardPathNotFoundError):
            copy_from(self.mount, "/../*", Path(self._tmp.name) / "out")


if __name__ == "__main__":
    unittest.main()
