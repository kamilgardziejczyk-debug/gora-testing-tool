"""Tests for `DutLogger.send_line`, using a fake serial handle instead of a DUT.

Run with `python3 -m unittest discover tests` from the repository root.
"""

import sys
import tempfile
import unittest
from pathlib import Path

import serial

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.dut_logger import DutLogger, LogSession  # noqa: E402


class FakeSerial:
    """Records what is written; can be told to fail like a vanished port."""

    def __init__(self, fail: bool = False):
        self.written = b""
        self.fail = fail

    def write(self, data: bytes) -> int:
        if self.fail:
            raise serial.SerialException("device disappeared")
        self.written += data
        return len(data)

    def flush(self) -> None:
        pass


class SendLineTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.session = LogSession(Path(self._tmp.name) / "run.html")
        self.session.open()
        self.logger = DutLogger(self.session, "/dev/null")

    def tearDown(self):
        self.session.close()
        self._tmp.cleanup()

    def test_sends_the_command_with_a_newline(self):
        self.logger._serial = FakeSerial()
        self.logger.send_line("app calibrate start")
        self.assertEqual(self.logger._serial.written, b"app calibrate start\n")

    def test_refused_while_capture_is_paused(self):
        self.logger._serial = FakeSerial()
        self.logger._paused = True
        with self.assertRaisesRegex(ConnectionError, "paused"):
            self.logger.send_line("app status")
        self.assertEqual(self.logger._serial.written, b"")

    def test_refused_when_the_port_is_not_open(self):
        with self.assertRaisesRegex(ConnectionError, "not open"):
            self.logger.send_line("app status")

    def test_a_write_failure_is_reported_as_a_connection_error(self):
        self.logger._serial = FakeSerial(fail=True)
        with self.assertRaisesRegex(ConnectionError, "could not send"):
            self.logger.send_line("app status")


if __name__ == "__main__":
    unittest.main()
