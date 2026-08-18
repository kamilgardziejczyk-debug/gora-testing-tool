"""Tests for top-level `!Group` blocks in the scenario parser.

A scenario is either wholly ungrouped (a top-level `commands:` list) or wholly
grouped (a top-level `groups:` list of !Group blocks, each holding its own
`commands:`). Commands nest inside groups; groups never nest inside commands.

Run with `python3 -m unittest discover tests` from the repository root.
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from parser import Parser  # noqa: E402


def _command(name: str, relay: int = 1) -> str:
    """A minimal !RelayControl block - the simplest real command."""
    return f'- !RelayControl\n  name: "{name}"\n  relay: {relay}\n  state: 1\n'


def _indent(block: str, spaces: int) -> str:
    pad = " " * spaces
    return "".join(f"{pad}{line}" if line.strip() else line for line in block.splitlines(True))


def _group(name: str, body: str) -> str:
    return f'- !Group\n  name: "{name}"\n  commands:\n' + _indent(body, 4)


def _loop(iterations: int, body: str) -> str:
    return f"- !Loop\n  iterations: {iterations}\n  commands:\n" + _indent(body, 4)


class GroupParsingTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _parse(self, document: str):
        path = self.root / "scenario.yml"
        path.write_text(f"---\n{document}", encoding="utf-8")
        return Parser(str(path)).parse()

    def _grouped(self, body: str):
        """Parse `body` as the scenario's top-level `groups:` list."""
        return self._parse("groups:\n" + _indent(body, 2))

    def _ungrouped(self, body: str):
        """Parse `body` as the scenario's top-level `commands:` list."""
        return self._parse("commands:\n" + _indent(body, 2))

    def _stamps(self, wrappers) -> list[tuple[str, str | None, int | None]]:
        return [(w.name, w.group, w.group_id) for w in wrappers]

    # --- the grouped form -------------------------------------------------

    def test_a_group_stamps_its_name_on_every_command_inside_it(self):
        body = _group("Initialization", _command("first") + _command("second", 2))
        self.assertEqual(
            self._stamps(self._grouped(body)),
            [("first", "Initialization", 1), ("second", "Initialization", 1)],
        )

    def test_groups_run_in_order_and_are_numbered_by_position(self):
        body = _group("Init", _command("a")) + _group("Flashing", _command("b", 2))
        self.assertEqual(
            self._stamps(self._grouped(body)),
            [("a", "Init", 1), ("b", "Flashing", 2)],
        )

    def test_two_groups_sharing_a_name_stay_distinguishable(self):
        body = _group("Check", _command("a")) + _group("Check", _command("b", 2))
        self.assertEqual([stamp[2] for stamp in self._stamps(self._grouped(body))], [1, 2])

    def test_a_loop_inside_a_group_stays_in_that_group(self):
        body = _group("Soak", _loop(3, _command("step")))
        self.assertEqual(
            self._stamps(self._grouped(body)),
            [("step", "Soak", 1)] * 3,
        )

    def test_an_empty_group_is_skipped_without_stopping_the_scenario(self):
        body = '- !Group\n  name: "Empty"\n' + _group("Real", _command("after"))
        self.assertEqual(self._stamps(self._grouped(body)), [("after", "Real", 2)])

    # --- the ungrouped form still works -----------------------------------

    def test_a_top_level_commands_list_leaves_everything_ungrouped(self):
        self.assertEqual(
            self._stamps(self._ungrouped(_command("a") + _command("b", 2))),
            [("a", None, None), ("b", None, None)],
        )

    def test_a_loop_in_an_ungrouped_scenario_still_expands(self):
        self.assertEqual(len(self._ungrouped(_loop(3, _command("step")))), 3)

    # --- rejections -------------------------------------------------------

    def test_a_group_inside_a_commands_list_is_rejected(self):
        """Groups do not nest in commands - and would be silently skipped."""
        body = _group("Init", _command("hidden"))
        with self.assertRaises(ValueError) as caught:
            self._ungrouped(body)
        self.assertIn("belongs at the top level", str(caught.exception))

    def test_a_group_nested_inside_another_group_is_rejected(self):
        body = _group("Outer", _group("Inner", _command("x")))
        with self.assertRaises(ValueError) as caught:
            self._grouped(body)
        self.assertIn("belongs at the top level", str(caught.exception))

    def test_declaring_both_commands_and_groups_is_rejected(self):
        document = (
            "commands:\n"
            + _indent(_command("loose"), 2)
            + "\ngroups:\n"
            + _indent(_group("Init", _command("a")), 2)
        )
        with self.assertRaises(ValueError) as caught:
            self._parse(document)
        self.assertIn("not both", str(caught.exception))

    def test_a_group_without_a_name_is_rejected(self):
        body = "- !Group\n  commands:\n" + _indent(_command("x"), 4)
        with self.assertRaises(ValueError) as caught:
            self._grouped(body)
        self.assertIn("'name' is required", str(caught.exception))

    def test_a_group_with_a_blank_name_is_rejected(self):
        body = '- !Group\n  name: "   "\n  commands:\n' + _indent(_command("x"), 4)
        with self.assertRaises(ValueError):
            self._grouped(body)

    def test_an_untagged_entry_in_groups_is_rejected(self):
        """A missing '!' would otherwise drop the whole group silently."""
        body = '- name: "Init"\n  commands:\n' + _indent(_command("x"), 4)
        with self.assertRaises(ValueError) as caught:
            self._grouped(body)
        self.assertIn("missing leading", str(caught.exception))

    def test_a_command_placed_directly_in_groups_is_rejected(self):
        with self.assertRaises(ValueError) as caught:
            self._grouped(_command("stray"))
        self.assertIn("!Group blocks", str(caught.exception))

    def test_a_groups_key_that_is_not_a_list_is_rejected(self):
        with self.assertRaises(ValueError) as caught:
            self._parse('groups:\n  name: "Init"\n')
        self.assertIn("must be a list", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
