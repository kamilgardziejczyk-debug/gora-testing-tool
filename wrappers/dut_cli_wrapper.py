r"""!DutCli - send a command to the DUT's shell and assert on the reply.

The other side of `!DutLogExpect`: rather than waiting for the DUT to volunteer
something on its console, this asks it a question over its Zephyr shell and
checks the answer.

    - !DutCli:
      name: "Gateway Reports Itself Online"
      command: "gora status"
      validation: 'state:\s*connected'

`command` is what is typed at the shell; `validation` is a Python regular
expression searched against the reply. Omit `validation` to just run a command
for its effect (a reset, a provisioning write) and log what came back.

The shell UART is configured for the whole run, not per command - a `dut_cli:`
block in the scenario or `--dut-cli` on the command line, exactly as `dut_log`
configures the console. One shell is opened for the run and shared by every
`!DutCli` in it: re-opening a USB CDC port per command costs a prompt re-sync
each time, and on some boards toggles DTR at the DUT.
"""

from __future__ import annotations

import logging
import re

import yaml

from tools.dut_cli import DEFAULT_TIMEOUT_S, DutShell, Response

from .wrapper import Wrapper


LOGGER = logging.getLogger(__name__)

# Tail of the reply quoted on failure. A shell reply is a handful of lines, so
# unlike the DUT console there is rarely anything to trim - the cap only stops
# a command that dumps a table (`net conn`, `kernel stacks`) from burying the
# failure itself.
FAILURE_CONTEXT_LINES = 30


class DutCliWrapper(Wrapper):
    """Runs one shell command on the DUT, optionally asserting on its reply.

    `validation` is compiled at parse time, so a malformed pattern fails the
    scenario before any hardware is touched rather than at the moment the
    command runs.

    A reply the *shell itself* refused - an unknown command, a wrong argument
    count - fails the command regardless of `validation`: that is a scenario
    written against a firmware that does not have this command, which no
    pattern could sensibly be asserted against.
    """

    requires_dut_cli = True

    def __init__(self, command_node: yaml.MappingNode):
        self.command_node = command_node
        self.name: str | None = None
        self.command: str | None = None
        self.pattern: str | None = None
        self.regex: re.Pattern[str] | None = None
        self.timeout_s: float = DEFAULT_TIMEOUT_S

    def parse(self) -> None:
        tag_name = self.command_node.tag.lstrip("!").rstrip(":")
        if tag_name != "DutCli":
            raise ValueError("Expected !DutCli command")

        for key_node, value_node in self.command_node.value:
            if not isinstance(key_node, yaml.ScalarNode) or not isinstance(value_node, yaml.ScalarNode):
                continue

            key = key_node.value
            if key == "name":
                self.name = value_node.value
            elif key == "command":
                self.command = value_node.value
            elif key == "validation":
                self.pattern = value_node.value
            elif key == "timeout_s":
                self.timeout_s = float(value_node.value)

        self._validate()

        LOGGER.info(
            "Parsed DutCli: name=%s, command=%s, validation=%s, timeout_s=%s",
            self.name,
            self.command,
            self.pattern,
            self.timeout_s,
        )

    def _validate(self) -> None:
        """Fail at parse time on anything checkable without a DUT attached."""
        if self.command is None or not self.command.strip():
            raise ValueError("DutCli: 'command' field is required")
        if self.timeout_s <= 0:
            raise ValueError(f"DutCli: timeout_s must be > 0, got {self.timeout_s}")
        if self.pattern is None:
            return

        try:
            self.regex = re.compile(self.pattern)
        except re.error as error:
            raise ValueError(
                f"DutCli: 'validation' is not a valid regular expression "
                f"({error}): {self.pattern}"
            ) from None

    def execute(self) -> None:
        shell = self._require_shell()
        response = self._send(shell)

        if response.error is not None:
            raise ValueError(
                f"DutCli: the DUT's shell refused '{self.command}': {response.error}. "
                f"Check the command exists in this firmware and takes these arguments."
            )

        if self.regex is None:
            LOGGER.info(
                "DutCli: '%s' replied with %d line(s) in %.2fs",
                self.command,
                len(response.lines),
                response.duration_s,
            )
            return

        # Set either way, so the report shows both sides of the assertion.
        match = self.regex.search(response.text)
        self.validation_expected = f"reply to '{self.command}' matches /{self.pattern}/"
        self.validation_actual = match.group(0) if match is not None else self._summarize(response)

        if match is None:
            raise ValueError(self._failure_message(response))

        LOGGER.info("DutCli: '%s' replied matching /%s/: %s", self.command, self.pattern, match.group(0))

    def _require_shell(self) -> DutShell:
        """The run's shell, opened on first use, or why there isn't one."""
        if self.dut_shell is None:
            raise ValueError(
                "DutCli: no DUT shell UART is configured for this run, so there is nothing to "
                "send the command to. Pass --dut-cli <port> (e.g. /dev/ttyACM1), or add a "
                "top-level 'dut_cli' block to the scenario."
            )
        # Opened here rather than at start-up: a scenario that flashes the DUT
        # first has no shell answering until that has run, and failing the
        # whole run before the flash would be a false negative.
        if not self.dut_shell.is_open:
            self.dut_shell.open()
        return self.dut_shell

    def _send(self, shell: DutShell) -> Response:
        """Send the command, reopening the shell once if the port has gone.

        A DUT that reset since the previous command leaves a stale handle
        behind: the reset is usually something the scenario asked for, so the
        first command afterwards reconnects instead of failing.
        """
        try:
            return shell.command(self.command, timeout_s=self.timeout_s)
        except ConnectionError as error:
            LOGGER.warning("DutCli: shell port dropped (%s), reopening for '%s'", error, self.command)
            shell.close()
            shell.open()
            return shell.command(self.command, timeout_s=self.timeout_s)

    def _summarize(self, response: Response) -> str:
        """One-line description of a reply that failed to match."""
        if not response.lines:
            return "no reply (the shell returned an empty response)"
        return f"no match in {len(response.lines)} reply line(s)"

    def _failure_message(self, response: Response) -> str:
        """Explain a failed match, quoting what the DUT actually replied.

        Log output that arrived while the command ran is quoted separately: it
        is not part of the reply and is never matched against, but it is often
        the reason the reply reads the way it does.
        """
        message = (
            f"DutCli: the DUT's reply to '{self.command}' did not match /{self.pattern}/ "
            f"({len(response.lines)} line(s), answered in {response.duration_s:.2f}s)."
        )

        replied = response.lines[-FAILURE_CONTEXT_LINES:]
        quoted = "\n".join(f"  {line}" for line in replied) or "  (the shell replied nothing at all)"
        message += f"\n\nReply:\n{quoted}"

        if response.log_lines:
            logs = "\n".join(f"  {line}" for line in response.log_lines[-FAILURE_CONTEXT_LINES:])
            message += f"\n\nDUT log output while the command ran:\n{logs}"
        return message
