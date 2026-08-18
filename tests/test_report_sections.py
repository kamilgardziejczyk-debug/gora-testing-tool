"""Tests for grouping report rows into `!Group` sections.

Run with `python3 -m unittest discover tests` from the repository root.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from reporting import TestResult, build_sections  # noqa: E402


def _result(
    name: str,
    group: str | None = None,
    group_id: int | None = None,
    passed: bool = True,
) -> TestResult:
    """A TestResult with only the fields sectioning actually reads."""
    return TestResult(
        name=name,
        tag="RelayControl",
        raw_yaml="",
        validation_expected=None,
        validation_actual=None,
        duration_s=0.0,
        passed=passed,
        error=None,
        group=group,
        group_id=group_id,
    )


class BuildSectionsTests(unittest.TestCase):
    def test_no_results_makes_no_sections(self):
        self.assertEqual(build_sections([]), [])

    def test_ungrouped_results_form_one_unnamed_section(self):
        sections = build_sections([_result("a"), _result("b")])
        self.assertEqual(len(sections), 1)
        self.assertIsNone(sections[0].name)
        self.assertEqual([r.name for r in sections[0].results], ["a", "b"])

    def test_a_group_becomes_its_own_named_section(self):
        sections = build_sections(
            [
                _result("loose"),
                _result("a", "Init", 1),
                _result("b", "Init", 1),
                _result("tail"),
            ]
        )
        self.assertEqual([s.name for s in sections], [None, "Init", None])
        self.assertEqual([s.total_count for s in sections], [1, 2, 1])

    def test_each_section_counts_its_own_passes(self):
        sections = build_sections(
            [
                _result("a", "Init", 1, passed=True),
                _result("b", "Init", 1, passed=False),
                _result("c", "Flash", 2, passed=True),
            ]
        )
        self.assertEqual([(s.passed_count, s.total_count) for s in sections], [(1, 2), (1, 1)])

    def test_adjacent_groups_sharing_a_name_stay_separate(self):
        """Split on the id, not the name - that is what ids are distinct for."""
        sections = build_sections(
            [_result("a", "Check", 1), _result("b", "Check", 2)]
        )
        self.assertEqual(len(sections), 2)
        self.assertEqual([s.name for s in sections], ["Check", "Check"])

    def test_a_looped_group_renders_once_per_iteration(self):
        sections = build_sections(
            [_result("step", "Cycle", 1), _result("step", "Cycle", 2), _result("step", "Cycle", 3)]
        )
        self.assertEqual([s.total_count for s in sections], [1, 1, 1])

    def test_returning_to_an_outer_group_opens_a_new_section(self):
        """The rows are no longer consecutive, so they cannot share one band."""
        sections = build_sections(
            [
                _result("before", "Flashing", 1),
                _result("inner", "Flashing / Erase", 2),
                _result("after", "Flashing", 1),
            ]
        )
        self.assertEqual(
            [s.name for s in sections], ["Flashing", "Flashing / Erase", "Flashing"]
        )

    def test_every_result_survives_sectioning(self):
        results = [_result("a"), _result("b", "G", 1), _result("c", "G", 1), _result("d")]
        flattened = [r for section in build_sections(results) for r in section.results]
        self.assertEqual(flattened, results)


if __name__ == "__main__":
    unittest.main()
