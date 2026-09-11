"""Tests for the !DutLogExpect tag's `since` scopes.

The search runs against a real LogSession fed by hand, so no DUT is needed.

Run with `python3 -m unittest discover tests` from the repository root.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from typing import Optional

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.dut_logger import LogSession  # noqa: E402
from wrappers.dut_log_expect_wrapper import DutLogExpectWrapper  # noqa: E402

HANDOFF = "I (14:15:02.887) USB_STORAGE_TASK: USB Storage is now ACTIVE - host can access SD card"


def build(since: str, group_id: Optional[int] = 1) -> DutLogExpectWrapper:
    """Parse one check for the card hand-off, as a command of group `group_id`."""
    wrapper = DutLogExpectWrapper(yaml.compose(
        "!DutLogExpect\n"
        "validation: 'matches({line}, \"USB Storage is now ACTIVE\")'\n"
        f"since: {since}\n"
        "timeout_s: 0.2"
    ))
    wrapper.group = None if group_id is None else "Mass Storage Mode"
    wrapper.group_id = group_id
    wrapper.parse()
    return wrapper


class SinceParseTests(unittest.TestCase):
    def test_group_is_accepted_inside_a_group(self):
        self.assertEqual(build("group").since, "group")

    def test_group_is_rejected_outside_a_group(self):
        """A plain commands: list has no group start to search from."""
        with self.assertRaisesRegex(ValueError, "inside a !Group"):
            build("group", group_id=None)


class SinceSearchTests(unittest.TestCase):
    """An earlier group logged the hand-off, then this check's group started."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.session = LogSession(Path(self._tmp.name) / "run.html")
        self.session.open()
        self.session.write_device(HANDOFF)
        self.group_start = self.session.device_seq()

    def tearDown(self):
        self.session.close()
        self._tmp.cleanup()

    def check(self, since: str) -> None:
        wrapper = build(since)
        wrapper.log_session = self.session
        wrapper.group_start_seq = self.group_start
        wrapper.execute()

    def test_scenario_scope_is_satisfied_by_the_earlier_handoff(self):
        """The false pass `since: group` exists to prevent."""
        self.check("scenario")

    def test_group_scope_ignores_lines_from_before_the_group(self):
        with self.assertRaisesRegex(ValueError, "never satisfied"):
            self.check("group")

    def test_group_scope_sees_a_line_that_arrived_before_the_check(self):
        """Unlike `since: command`, which only looks from the check itself."""
        self.session.write_device(HANDOFF)
        self.check("group")
        with self.assertRaisesRegex(ValueError, "never satisfied"):
            self.check("command")


if __name__ == "__main__":
    unittest.main()
