"""Tests for the !DutStorage tag.

Parse-time checks need nothing. The file actions run against a `Mount`
pointing at a temporary directory, so they exercise the wrapper's own logic -
variable binding, assertion, cleanup - without a card or root.

Run with `python3 -m unittest discover tests` from the repository root.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.mass_storage.device import BlockDevice  # noqa: E402
from tools.mass_storage.mount import Mount  # noqa: E402
from wrappers import dut_storage_wrapper  # noqa: E402
from wrappers.dut_storage_wrapper import DutStorageWrapper, restore_all  # noqa: E402


def build(text: str) -> DutStorageWrapper:
    """Parse one !DutStorage command from YAML."""
    wrapper = DutStorageWrapper(yaml.compose(text))
    wrapper.parse()
    return wrapper


class ParseTests(unittest.TestCase):
    def test_lifecycle_actions_take_no_validation(self):
        """What the DUT made of a mount shows up on its console, not here."""
        with self.assertRaisesRegex(ValueError, "nothing to assert about"):
            build('!DutStorage\naction: eject\nport: 1\nvalidation: "{files} == []"')

    def test_action_specific_variables(self):
        wrapper = build('!DutStorage\naction: list\nvalidation: "len({dirs}) == 3"')
        self.assertEqual(wrapper.expression.used, ("dirs",))

    def test_a_variable_from_another_action_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unknown variable"):
            build('!DutStorage\naction: list\nvalidation: "{size} > 0"')

    def test_mode_only_means_something_to_mount(self):
        with self.assertRaisesRegex(ValueError, "'mode' means nothing"):
            build("!DutStorage\naction: unmount\nmode: rw")

    def test_read_write_is_opt_in(self):
        self.assertEqual(build("!DutStorage\naction: mount\nport: 1").mode, "ro")
        self.assertEqual(build("!DutStorage\naction: mount\nport: 1\nmode: rw").mode, "rw")


class CardTestCase(unittest.TestCase):
    """A temporary directory standing in for the scenario's mounted card."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "card"
        (self.root / "logs").mkdir(parents=True)
        (self.root / "logs" / "tracker.log").write_text("boot ok\n")
        (self.root / "logs" / "old.log").write_text("older\n")

        device = BlockDevice(
            disk=Path("/dev/sda"), partition=Path("/dev/sda1"), usb_path=Path("/sys/fake")
        )
        self.card = Mount(device=device, mountpoint=self.root, mode="ro")
        patcher = mock.patch.object(dut_storage_wrapper, "_mounted", self.card)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self._tmp.cleanup()


class FileActionTests(CardTestCase):
    def test_list_passes_and_reports_both_sides(self):
        wrapper = build(
            '!DutStorage\naction: list\npath: "/logs"\nvalidation: "\'tracker.log\' in {files}"'
        )
        wrapper.execute()
        self.assertIn("tracker.log", wrapper.validation_actual)
        self.assertIn("in {files}", wrapper.validation_expected)

    def test_list_failure_names_what_was_there(self):
        wrapper = build(
            '!DutStorage\naction: list\npath: "/logs"\nvalidation: "\'missing.log\' in {files}"'
        )
        with self.assertRaises(ValueError) as caught:
            wrapper.execute()
        self.assertIn("tracker.log", str(caught.exception))
        self.assertIsNotNone(wrapper.validation_actual)

    def test_read_binds_content_and_size(self):
        wrapper = build(
            '!DutStorage\naction: read\npath: "/logs/tracker.log"\n'
            'validation: "\'boot ok\' in {content} and {size} == 8"'
        )
        wrapper.execute()

    def test_list_counts_are_spelled_out(self):
        """No {count} on `list`: with {files} and {dirs} both in scope it is ambiguous."""
        build('!DutStorage\naction: list\npath: "/logs"\nvalidation: "len({files}) == 2"').execute()

    def test_count_is_rejected_on_list(self):
        with self.assertRaisesRegex(ValueError, "unknown variable"):
            build('!DutStorage\naction: list\nvalidation: "{count} == 2"')

    def test_count_still_offered_where_it_is_unambiguous(self):
        for action in ("copy_from", "delete"):
            wrapper = build(
                f'!DutStorage\naction: {action}\npath: "/x"\ndest: "o"\nvalidation: "{{count}} == 1"'
                if action == "copy_from"
                else f'!DutStorage\naction: {action}\npath: "/x"\nvalidation: "{{count}} == 1"'
            )
            self.assertEqual(wrapper.expression.used, ("count",))

    def test_action_without_validation_still_runs(self):
        wrapper = build('!DutStorage\naction: list\npath: "/logs"')
        wrapper.execute()
        self.assertIsNone(wrapper.validation_expected)

    def test_copy_from_resolves_dest_against_the_scenario(self):
        wrapper = build('!DutStorage\naction: copy_from\npath: "/logs/*.log"\ndest: "out"')
        wrapper.scenario_dir = Path(self._tmp.name)
        wrapper.execute()
        self.assertTrue((Path(self._tmp.name) / "out" / "tracker.log").is_file())

    def test_copy_from_binds_copied(self):
        wrapper = build(
            '!DutStorage\naction: copy_from\npath: "/logs/*.log"\ndest: "out"\n'
            'validation: "{count} == 2 and \'/logs/tracker.log\' in {copied}"'
        )
        wrapper.scenario_dir = Path(self._tmp.name)
        wrapper.execute()


class MountStateTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(dut_storage_wrapper, "_mounted", None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_file_actions_need_an_explicit_mount(self):
        """Mounting is a step the DUT observes, so it is never implicit."""
        wrapper = build('!DutStorage\naction: list\npath: "/logs"')
        with self.assertRaisesRegex(ValueError, "action: mount"):
            wrapper.execute()

    def test_unmount_without_a_mount_is_not_an_error(self):
        build("!DutStorage\naction: unmount").execute()

    def test_restore_all_with_nothing_mounted_does_nothing(self):
        restore_all()


class RestoreTests(CardTestCase):
    def test_restore_all_unmounts_what_the_scenario_left(self):
        with mock.patch.object(dut_storage_wrapper, "unmount") as unmount:
            restore_all()
        unmount.assert_called_once_with(self.card.mountpoint)
        self.assertIsNone(dut_storage_wrapper._mounted)

    def test_restore_all_falls_back_to_a_lazy_unmount(self):
        with mock.patch.object(dut_storage_wrapper, "unmount") as unmount:
            unmount.side_effect = [OSError("device is busy"), None]
            restore_all()
        self.assertEqual(unmount.call_count, 2)
        self.assertEqual(unmount.call_args.kwargs, {"lazy": True})

    def test_restore_all_never_raises(self):
        """It runs in the runner's finally; raising would mask the real failure."""
        with mock.patch.object(dut_storage_wrapper, "unmount") as unmount:
            unmount.side_effect = OSError("gone")
            restore_all()

    def test_eject_refuses_while_still_mounted(self):
        wrapper = build("!DutStorage\naction: eject\nport: 1")
        with self.assertRaisesRegex(ValueError, "still mounted"):
            wrapper.execute()


if __name__ == "__main__":
    unittest.main()
