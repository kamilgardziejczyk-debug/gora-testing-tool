r"""!DutLogExpect - assert that the DUT's console emitted a matching line.

The check a firmware log line is worth: rather than reading `device.log` by eye
after the run, a scenario states what the DUT must say and fails if it never
does. Written for questions the cloud side cannot answer, such as whether the
wall clock was ever set:

    - !DutLogExpect:
      name: "Wall Clock Set From NTP"
      validation: 'matches({line}, "Wall clock set from \S+: unix=[0-9]+")'
      timeout_s: 60

`validation` is a Python expression over the captured console (see
`wrappers/expression.py`).

Two properties matter more than they look:

*   It searches output captured **before** it runs, not only after. A DUT does
    not wait to be asked - the gateway syncs its clock about 16 s into boot,
    which on a scenario that resets it early is several commands before
    anything reads for it. A wait-only check would sit out its whole timeout
    while the line it wanted was already on disk.
*   A retry is not a failure. The gateway's first NTP query routinely fails
    with -11 (EAGAIN, DNS not yet usable after DHCP) and the next one succeeds,
    so this asserts a line eventually *appears*. Bound how long that may take
    with `timeout_s`; do not try to assert that a warning never appeared.

That last point is now something the syntax lets you write by mistake. This
tag waits for its expression to become true, so a negative one - `not
matches({log}, "PANIC")` - is already true before the DUT has said anything
and passes instantly, testing nothing. Assert what the DUT *must* say.
"""

from __future__ import annotations

import logging
import time

import yaml

from tools.dut_logger import DeviceLine, LogSession

from .expression import Expression, compile_expression
from .wrapper import Wrapper


LOGGER = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 30.0

# How much of the capture a check considers. "scenario" is the default because
# output arriving before the check is the normal case, not the exception (see
# the module docstring); "command" exists for re-checking something after a
# deliberate reset, where a match from before it would be a false pass.
# "group" sits between them: a line the scenario provokes more than once, which
# can still arrive while the previous command is finishing.
SINCE_SCENARIO = "scenario"
SINCE_COMMAND = "command"
SINCE_GROUP = "group"
SINCE_CHOICES = (SINCE_SCENARIO, SINCE_COMMAND, SINCE_GROUP)

# Tail of the capture quoted on failure, to show what the DUT *was* saying
# instead of only that it never said the wanted thing.
FAILURE_CONTEXT_LINES = 15

# Variables a `validation` expression may use, and what they hold.
VARIABLES = ("line", "log", "lines")

# Shown when a scenario still carries a bare regular expression, which is what
# this field used to be.
MIGRATION_HINT = 'A bare pattern becomes matches({line}, "..."), keeping the same meaning.'


class DutLogExpectWrapper(Wrapper):
    """Waits for `validation` to hold over the DUT's console, failing on timeout.

    The expression is evaluated once per captured line, against:

        {line}   str        the line that just arrived
        {log}    str        every line considered so far, newline-separated
        {lines}  list[str]  the same, as a list

    `{line}` is the one to reach for: the check passes as soon as any single
    line satisfies the expression, which is what "the DUT said this" means.
    `{log}` and `{lines}` are for a condition spanning several lines, such as
    `len({lines}) > 5 and matches({log}, "done")`.

    Lines are as the firmware emitted them - the `[HH:MM:SS]` prefix in the
    log files is not part of what is matched, so an expression is written
    against firmware output alone.

    It is compiled at parse time, so a malformed expression fails the scenario
    before any hardware is touched rather than at the moment the check runs.
    """

    requires_dut_log = True

    def __init__(self, command_node: yaml.MappingNode):
        self.command_node = command_node
        self.name: str | None = None
        self.validation: str | None = None
        self.expression: Expression | None = None
        self.since: str = SINCE_SCENARIO
        self.timeout_s: float = DEFAULT_TIMEOUT_S

    def parse(self) -> None:
        tag_name = self.command_node.tag.lstrip("!").rstrip(":")
        if tag_name != "DutLogExpect":
            raise ValueError("Expected !DutLogExpect command")

        for key_node, value_node in self.command_node.value:
            if not isinstance(key_node, yaml.ScalarNode) or not isinstance(value_node, yaml.ScalarNode):
                continue

            key = key_node.value
            if key == "name":
                self.name = value_node.value
            elif key == "validation":
                self.validation = value_node.value
            elif key == "since":
                self.since = value_node.value
            elif key == "timeout_s":
                self.timeout_s = float(value_node.value)

        self._validate()

        LOGGER.info(
            "Parsed DutLogExpect: name=%s, validation=%s, since=%s, timeout_s=%s",
            self.name,
            self.validation,
            self.since,
            self.timeout_s,
        )

    def _validate(self) -> None:
        """Fail at parse time on anything checkable without a DUT attached."""
        if self.validation is None:
            raise ValueError("DutLogExpect: 'validation' field is required")
        if self.since not in SINCE_CHOICES:
            choices = ", ".join(SINCE_CHOICES)
            raise ValueError(f"DutLogExpect: 'since' must be one of {choices}, got '{self.since}'")
        if self.since == SINCE_GROUP and self.group_id is None:
            raise ValueError("DutLogExpect: 'since: group' needs the command to be inside a !Group")
        if self.timeout_s <= 0:
            raise ValueError(f"DutLogExpect: timeout_s must be > 0, got {self.timeout_s}")

        self.expression = compile_expression(
            self.validation, VARIABLES, "DutLogExpect: 'validation'", MIGRATION_HINT
        )

    def execute(self) -> None:
        session = self._require_session()
        since_seq = {
            SINCE_SCENARIO: 0,
            SINCE_COMMAND: session.device_seq(),
            SINCE_GROUP: self.group_start_seq or 0,
        }[self.since]
        dropped_before = session.device_lines_dropped()

        match, scanned = self._search(session, since_seq)

        # Set either way, so the report shows both sides of the assertion.
        self.validation_expected = f"device log satisfies: {self.validation}"
        self.validation_actual = match.text if match is not None else f"not satisfied in {scanned} line(s)"

        if match is None:
            raise ValueError(self._failure_message(session, scanned, dropped_before))

        LOGGER.info(
            "DutLogExpect: satisfied '%s' after %d line(s): %s",
            self.validation,
            scanned,
            match.text,
        )

    def _require_session(self) -> LogSession:
        """The run's log session, or an explanation of why there isn't one."""
        if self.log_session is None:
            raise ValueError(
                "DutLogExpect: no DUT console is being captured for this run, so there is "
                "nothing to match against. Pass --dut-log <port> (e.g. /dev/ttyACM0), or add "
                "a top-level 'dut_log' block to the scenario."
            )
        return self.log_session

    def _search(self, session: LogSession, since_seq: int) -> tuple[DeviceLine | None, int]:
        """Scan for the first matching line, waiting out `timeout_s` for one.

        Returns the match (or None) and how many lines were examined. Anything
        already captured past `since_seq` is examined before any waiting, so a
        line that has already arrived costs no time at all.
        """
        deadline = time.monotonic() + self.timeout_s
        cursor = since_seq
        seen: list[str] = []

        while True:
            remaining = deadline - time.monotonic()
            for entry in session.wait_for_device_lines(cursor, max(remaining, 0.0)):
                cursor = entry.seq
                seen.append(entry.text)
                if self.expression.evaluate(self._variables(entry.text, seen)):
                    return entry, len(seen)
            if remaining <= 0:
                return None, len(seen)

    def _variables(self, line: str, seen: list[str]) -> dict[str, object]:
        """Bind this check's variables for one evaluation.

        `{log}` and `{lines}` are only built when the expression actually asks
        for them: joining the whole capture once per arriving line would be
        quadratic, and the common case (`{line}` alone) never needs it.
        """
        variables: dict[str, object] = {"line": line}
        if "lines" in self.expression.used:
            variables["lines"] = list(seen)
        if "log" in self.expression.used:
            variables["log"] = "\n".join(seen)
        return variables

    def _failure_message(self, session: LogSession, scanned: int, dropped_before: int) -> str:
        """Explain a failed match, with the tail of what the DUT did say.

        The eviction check is not pedantry: once lines have fallen out of the
        buffer, "no match" no longer means "the DUT never said it", and saying
        so points at the complete log file instead of implying a clean negative.
        """
        scope = {
            SINCE_SCENARIO: "the whole run",
            SINCE_COMMAND: "this command onwards",
            SINCE_GROUP: f"the start of group '{self.group}' onwards",
        }[self.since]
        message = (
            f"DutLogExpect: the DUT's console never satisfied '{self.validation}' within "
            f"{self.timeout_s}s ({scanned} line(s) examined, covering {scope})."
        )

        if session.device_lines_dropped() > dropped_before or dropped_before:
            message += (
                f"\n\nNote: {session.device_lines_dropped()} captured line(s) have been dropped from "
                f"the in-memory buffer, so this cannot prove the DUT never emitted it - check "
                f"{session.device_path.name}, which is complete."
            )

        recent = session.device_lines()[-FAILURE_CONTEXT_LINES:]
        quoted = "\n".join(f"  {entry.text}" for entry in recent) or "  (the DUT said nothing at all)"
        message += f"\n\nLast {len(recent)} line(s) captured from the DUT:\n{quoted}"
        return message
