"""!DutLogSend - type a line on the DUT's console.

For a board whose console is also its input: the ESP32 `tekpadz>` console shares
one wire with its log output, so the line is written on the port the capture is
already holding rather than through a second opener.

    - !DutLogSend
      name: "Enter calibration"
      command: "app calibrate start"

Nothing is asserted here, so no `validation` field; what the DUT does with the
line is `!DutLogExpect`'s business. Capture must be running at that point in the
scenario, as for `!DutLogExpect`.
"""

from __future__ import annotations

import logging

import yaml

from tools.dut_logger import DutLogger

from .wrapper import Wrapper


LOGGER = logging.getLogger(__name__)


class DutLogSendWrapper(Wrapper):
    """Sends one line to the run's DUT console."""

    requires_dut_log = True
    sends_dut_log = True

    def __init__(self, command_node: yaml.MappingNode):
        self.command_node = command_node
        self.name: str | None = None
        self.command: str | None = None

    def parse(self) -> None:
        tag_name = self.command_node.tag.lstrip("!").rstrip(":")
        if tag_name != "DutLogSend":
            raise ValueError("Expected !DutLogSend command")

        for key_node, value_node in self.command_node.value:
            if not isinstance(key_node, yaml.ScalarNode) or not isinstance(value_node, yaml.ScalarNode):
                continue

            if key_node.value == "name":
                self.name = value_node.value
            elif key_node.value == "command":
                self.command = value_node.value

        if not self.command:
            raise ValueError("DutLogSend: 'command' field is required")

        LOGGER.info("Parsed DutLogSend: name=%s, command=%s", self.name, self.command)

    def execute(self) -> None:
        self._require_logger().send_line(self.command)

    def _require_logger(self) -> DutLogger:
        """The run's console reader, or an explanation of why there isn't one."""
        if self.dut_logger is None:
            raise ValueError(
                "DutLogSend: no DUT console is being captured for this run, so there is "
                "nothing to send to. Pass --dut-log <port> (e.g. /dev/ttyUSB0), or add "
                "a top-level 'dut_log' block to the scenario."
            )
        return self.dut_logger
