"""Tests for which of a run's logs are reported and kept.

Run with `python3 -m unittest discover tests` from the repository root.
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from reporting.report import _log_file_names  # noqa: E402
from tools.dut_logger import CLI_LOG, DEVICE_LOG, MQTT_LOG, LogSession  # noqa: E402


class LogUsageTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.report_path = Path(self._tmp.name) / "run.html"
        self.session = LogSession(self.report_path)
        self.session.open()

    def tearDown(self):
        self.session.close()
        self._tmp.cleanup()

    def kinds(self):
        return [kind for kind, _ in self.session.log_files()]

    def test_a_bare_run_reports_only_tool_and_combined(self):
        self.session.write_tool("started")
        self.assertEqual(self.kinds(), ["tool", "combined"])

    def test_mqtt_appears_once_traffic_is_logged(self):
        self.session.write_tool("started")
        self.session.write_mqtt("connected")
        self.assertEqual(self.kinds(), ["tool", "mqtt", "combined"])

    def test_cli_appears_once_a_shell_is_marked(self):
        self.session.write_tool("started")
        self.session.mark_used(CLI_LOG)
        self.assertEqual(self.kinds(), ["tool", "cli", "combined"])

    def test_device_marked_on_activation_even_while_silent(self):
        """A configured console that said nothing is still evidence."""
        self.session.write_tool("started")
        self.session.mark_used(DEVICE_LOG)
        self.assertIn("device", self.kinds())

    def test_device_note_alone_does_not_count_as_a_capture(self):
        self.session.write_tool("started")
        self.session.write_device_note("no DUT console configured for this run")
        self.assertNotIn("device", self.kinds())

    def test_unknown_kind_rejected(self):
        with self.assertRaisesRegex(ValueError, "unknown log kind"):
            self.session.mark_used("serial")

    def test_report_lists_only_used_logs(self):
        self.session.write_tool("started")
        self.session.write_mqtt("connected")
        labels = [label for label, _ in _log_file_names(self.session)]
        self.assertEqual(labels, ["Tool log", "MQTT log", "Combined log"])

    def test_report_without_a_session_lists_nothing(self):
        self.assertEqual(_log_file_names(None), [])


class DiscardUnusedTests(unittest.TestCase):
    def test_unused_empty_logs_are_removed_on_close(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = LogSession(Path(tmp) / "run.html")
            session.open()
            session.write_tool("started")
            session.close()

            self.assertTrue(session.tool_path.exists())
            self.assertTrue(session.combined_path.exists())
            self.assertFalse(session.mqtt_path.exists())
            self.assertFalse(session.cli_path.exists())
            self.assertFalse(session.device_path.exists())

    def test_a_used_log_survives_even_if_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = LogSession(Path(tmp) / "run.html")
            session.open()
            session.write_tool("started")
            session.mark_used(MQTT_LOG)
            session.close()
            self.assertTrue(session.mqtt_path.exists())

    def test_a_device_note_keeps_the_file_but_not_the_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = LogSession(Path(tmp) / "run.html")
            session.open()
            session.write_tool("started")
            session.write_device_note("no DUT console configured for this run")
            session.close()
            self.assertTrue(session.device_path.exists())

    def test_close_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = LogSession(Path(tmp) / "run.html")
            session.open()
            session.write_tool("started")
            session.close()
            session.close()


if __name__ == "__main__":
    unittest.main()
