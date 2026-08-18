"""Tests for `--clean-results`.

Run with `python3 -m unittest discover tests` from the repository root.
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from main import clean_results_dir  # noqa: E402


class CleanResultsDirTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_files_and_directories_are_both_removed(self):
        (self.root / "old_report.html").write_text("x")
        (self.root / "sd").mkdir()
        (self.root / "sd" / "session_1").mkdir()
        (self.root / "sd" / "session_1" / "data.log").write_text("stale")

        clean_results_dir(self.root)

        self.assertEqual(list(self.root.iterdir()), [])

    def test_the_directory_itself_survives(self):
        """Not rmtree+mkdir: this may be a host bind mount."""
        (self.root / "x.html").write_text("x")
        clean_results_dir(self.root)
        self.assertTrue(self.root.is_dir())

    def test_a_missing_directory_is_not_an_error(self):
        """A fresh node has nothing to clean yet; LogSession.open() creates it."""
        clean_results_dir(self.root / "nested" / "missing")

    def test_an_empty_directory_is_left_alone(self):
        clean_results_dir(self.root)
        self.assertTrue(self.root.is_dir())

    def test_hidden_files_are_removed_too(self):
        """No glob pattern to accidentally skip - the whole directory is emptied."""
        (self.root / ".hidden").write_text("x")
        clean_results_dir(self.root)
        self.assertEqual(list(self.root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
