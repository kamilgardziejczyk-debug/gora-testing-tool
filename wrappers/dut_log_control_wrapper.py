"""!DutLogControl - suspend and resume capture of the DUT's console.

For the bench where the console and the programming port are the same wire. An
ESP32 behind a USB-UART bridge logs on `/dev/ttyUSB0` and is flashed on
`/dev/ttyUSB0`, and both cannot read it at once: the kernel hands each byte to
whichever reader asks first, so a capture left running through a flash quietly
eats parts of esptool's handshake and the flash fails in ways that look random.

The port is therefore handed over explicitly, in the scenario, where it can be
read:

    - !DutLogControl
      name: "Release the console for flashing"
      state: 0

    - !ProgramEsptool:
      name: "Flash The Tracker"
      ...

    - !DutLogControl
      name: "Recapture the console"
      state: 1

`state` follows `!RelayControl`: 1 captures, 0 stops capturing. Both are
idempotent, so a scenario that stops twice, or resumes something already
running, is not an error - the tag states the capture's intended state, not a
transition it must be in the right position to perform.

`reset_dut` (default 0) says whether taking the console back may reset the
device. On these boards DTR and RTS are wired to the reset and boot-mode pins,
so opening the port reboots the chip; the default leaves it alone, since a
`state: 1` after flashing is usually there to capture the boot esptool has just
started, and resetting would discard exactly that.

Nothing here is asserted, so no `validation` field: like `!RelayControl`, this
tag changes the state of the bench rather than testing it. What the DUT then
says is `!DutLogExpect`'s business.

A scenario using this tag without a console configured is rejected before the
first command runs, as `!DutLogExpect` is. So is one that flashes a port while
the capture still holds it - see `validate_dut_log_handover` in main.py.
"""

from __future__ import annotations

import logging

import yaml

from tools.dut_logger import DutLogger

from .wrapper import Wrapper


LOGGER = logging.getLogger(__name__)


class DutLogControlWrapper(Wrapper):
    """Pauses or resumes the run's DUT console capture."""

    requires_dut_log = True
    controls_dut_log = True

    def __init__(self, command_node: yaml.MappingNode):
        self.command_node = command_node
        self.name: str | None = None
        self.state: bool | None = None
        self.reset_dut: bool = False

    def parse(self) -> None:
        tag_name = self.command_node.tag.lstrip("!").rstrip(":")
        if tag_name != "DutLogControl":
            raise ValueError("Expected !DutLogControl command")

        for key_node, value_node in self.command_node.value:
            if not isinstance(key_node, yaml.ScalarNode) or not isinstance(value_node, yaml.ScalarNode):
                continue

            key = key_node.value
            if key == "name":
                self.name = value_node.value
            elif key == "state":
                self.state = self._parse_flag("state", value_node.value)
            elif key == "reset_dut":
                self.reset_dut = self._parse_flag("reset_dut", value_node.value)

        self._validate_parsed_fields()

        LOGGER.info(
            "Parsed DutLogControl: name=%s, state=%s, reset_dut=%s",
            self.name,
            self.state,
            self.reset_dut,
        )

    @staticmethod
    def _parse_flag(field_name: str, raw: str) -> bool:
        """Parse a 1/0 field, matching how `!RelayControl` spells its `state`."""
        if raw == "1":
            return True
        if raw == "0":
            return False
        raise ValueError(f"DutLogControl: '{field_name}' must be 1 or 0, got '{raw}'")

    def _validate_parsed_fields(self) -> None:
        """Reject a malformed command before any hardware is touched."""
        if self.state is None:
            raise ValueError("DutLogControl: 'state' field is required (1 to capture, 0 to stop)")
        if self.reset_dut and not self.state:
            raise ValueError(
                "DutLogControl: 'reset_dut' only applies when resuming capture (state: 1); "
                "stopping capture never resets the DUT"
            )

    def execute(self) -> None:
        logger = self._require_logger()
        if self.state:
            logger.resume(reset_dut=self.reset_dut)
        else:
            logger.pause()

    def _require_logger(self) -> DutLogger:
        """The run's console reader, or an explanation of why there isn't one."""
        if self.dut_logger is None:
            raise ValueError(
                "DutLogControl: no DUT console is being captured for this run, so there is "
                "nothing to start or stop. Pass --dut-log <port> (e.g. /dev/ttyUSB0), or add "
                "a top-level 'dut_log' block to the scenario."
            )
        return self.dut_logger
