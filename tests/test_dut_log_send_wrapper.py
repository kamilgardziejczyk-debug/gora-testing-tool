"""Tests for the !DutLogSend tag.

Run with `python3 -m unittest discover tests` from the repository root.
"""

import sys
import unittest
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main  # noqa: E402
from parser.parser import DutLogConfig  # noqa: E402
from wrappers.dut_log_control_wrapper import DutLogControlWrapper  # noqa: E402
from wrappers.dut_log_send_wrapper import DutLogSendWrapper  # noqa: E402


class FakeLogger:
    """Stands in for DutLogger, recording what was sent."""

    def __init__(self):
        self.sent = []

    def send_line(self, text: str) -> None:
        self.sent.append(text)


def build(document: str = '!DutLogSend\nname: "Go"\ncommand: "app calibrate start"') -> DutLogSendWrapper:
    """Parse a !DutLogSend block."""
    wrapper = DutLogSendWrapper(yaml.compose(document))
    wrapper.tag = "DutLogSend"
    wrapper.parse()
    return wrapper


def control(state: int) -> DutLogControlWrapper:
    """A !DutLogControl block with the given capture state."""
    wrapper = DutLogControlWrapper(yaml.compose(f"!DutLogControl\nstate: {state}"))
    wrapper.tag = "DutLogControl"
    wrapper.parse()
    return wrapper


class DutLogSendTests(unittest.TestCase):
    def test_parses_the_command(self):
        self.assertEqual(build().command, "app calibrate start")

    def test_command_is_required(self):
        with self.assertRaisesRegex(ValueError, "'command' field is required"):
            build("!DutLogSend\nname: Go")

    def test_execute_sends_the_command(self):
        wrapper = build()
        wrapper.dut_logger = FakeLogger()
        wrapper.execute()
        self.assertEqual(wrapper.dut_logger.sent, ["app calibrate start"])

    def test_execute_without_a_logger_explains_itself(self):
        with self.assertRaisesRegex(ValueError, "no DUT console"):
            build().execute()

    def test_attach_gives_it_the_logger(self):
        wrapper, logger = build(), FakeLogger()
        main.attach_dut_logger([wrapper], logger)
        self.assertIs(wrapper.dut_logger, logger)

    def test_rejected_while_capture_is_stopped(self):
        config = DutLogConfig(port="/dev/ttyUSB0", baud=115200)
        with self.assertRaisesRegex(ValueError, "capture is stopped"):
            main.validate_dut_log_handover([control(0), build()], config, None)

    def test_accepted_while_capture_is_running(self):
        config = DutLogConfig(port="/dev/ttyUSB0", baud=115200)
        main.validate_dut_log_handover([control(0), control(1), build()], config, None)


if __name__ == "__main__":
    unittest.main()
