"""Request/response against a Zephyr shell over UART.

One command, one response, framed by the shell's prompt:

    shell = DutShell(port="/dev/ttyACM1")
    shell.open()
    response = shell.command("kernel version")

The awkward parts this exists to absorb, all of them normal Zephyr behaviour
rather than faults:

*   The response ends at the *next* prompt, which arrives with no newline after
    it, so it has to be recognised in mid-stream rather than waited out.
*   The shell echoes back what was typed.
*   The prompt and coloured output carry terminal escape sequences.
*   The logging backend usually shares this UART, so `<inf>` lines can land in
    the middle of a response. They are separated out (see `Response.log_lines`)
    instead of being parsed as part of it.

A command that the shell refuses (unknown command, bad arguments) is *not* an
error here: it returns a `Response` whose `error` says so. Only losing the port
or never seeing a prompt raises, since those mean the answer is unknown rather
than unwelcome.
"""

from __future__ import annotations

import logging
import re
import time
from typing import TYPE_CHECKING

from .parsing import Response, is_log_line, strip_ansi
from .transport import DEFAULT_BAUD, SerialTransport

if TYPE_CHECKING:  # imported for typing only, keeping this tool usable on its own
    from tools.dut_logger import LogSession

LOGGER = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 3.0

# How long `open()` waits for the shell to answer a bare newline with a prompt.
# Generous compared to a command: the DUT may still be booting when a scenario
# reaches its first command.
DEFAULT_SYNC_TIMEOUT_S = 10.0

# Zephyr's prompt: `CONFIG_SHELL_PROMPT_UART`, "uart:~$ " out of the box. Matched
# as a pattern rather than a literal so a device with a renamed prompt, or one
# that has `select`ed a subcommand, still frames correctly. Anchored at the end
# of what has arrived so far, which is what makes it a usable read terminator.
DEFAULT_PROMPT = r"[^\r\n]*:~\$ \Z"


class DutShell:
    """A Zephyr shell on a serial port, one command at a time."""

    def __init__(
        self,
        port: str,
        baud: int = DEFAULT_BAUD,
        prompt: str = DEFAULT_PROMPT,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        newline: str = "\r\n",
        log_session: LogSession | None = None,
    ):
        """Talk to the shell on `port`.

        `prompt` is a regex matching the shell's prompt at the end of the
        stream; `timeout_s` is the default per-command budget. Given a
        `log_session`, every command and response line is written to that run's
        `.cli.log` and `.combined.log` as it happens.
        """
        self.port = port
        self.baud = baud
        self.prompt = re.compile(prompt)
        self.timeout_s = timeout_s
        self.newline = newline
        self._log_session = log_session
        self._transport = SerialTransport(port, baud)

    def __enter__(self) -> "DutShell":
        self.open()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def open(self, sync_timeout_s: float = DEFAULT_SYNC_TIMEOUT_S) -> None:
        """Open the port and confirm a live shell is on the other end.

        Sends a bare newline and waits for a prompt. Raises `TimeoutError` if
        none comes: that is the difference between "the DUT is wired up" and
        "the DUT has a shell listening here", and finding out now beats having
        the first real command fail for reasons that look like its own.
        """
        self._transport.open()
        self._transport.drain()
        self._log(f"open     -- {self.port} at {self.baud} baud")

        deadline = time.monotonic() + sync_timeout_s
        self._transport.write_line("", self.newline)
        text, found = self._transport.read_until(self.prompt, deadline, strip_ansi)
        if not found:
            self.close()
            raise TimeoutError(
                f"no Zephyr shell prompt from {self.port} within {sync_timeout_s}s. Check the "
                f"port is the DUT's shell UART (not its console), the baud rate is {self.baud}, "
                f"and that CONFIG_SHELL is enabled. Received: {strip_ansi(text)!r}"
            )
        self._log("sync     <- prompt")

    def close(self) -> None:
        """Close the port. Idempotent."""
        if self._transport.is_open:
            self._log(f"close    -- {self.port}")
        self._transport.close()

    def command(self, command: str, timeout_s: float | None = None) -> Response:
        """Send `command` and return everything up to the next prompt.

        Raises `TimeoutError` if no prompt arrives within the budget, quoting
        what did arrive - a half-finished response is the most useful evidence
        there is about a command that hung or a DUT that reset mid-answer.
        """
        budget = self.timeout_s if timeout_s is None else timeout_s
        discarded = self._transport.drain()
        if discarded.strip():
            LOGGER.debug("DutCli: discarded %r before sending %r", discarded, command)

        self._log(f"-> {command}")
        started = time.monotonic()
        self._transport.write_line(command, self.newline)
        text, found = self._transport.read_until(self.prompt, started + budget, strip_ansi)
        duration_s = time.monotonic() - started

        response = self._build_response(command, text, duration_s)
        if not found:
            raise TimeoutError(
                f"DutCli: no prompt after '{command}' within {budget}s "
                f"(read {len(response.lines)} response line(s) so far: {response.text!r})"
            )
        return response

    def _build_response(self, command: str, raw: str, duration_s: float) -> Response:
        """Turn what arrived into a `Response`, minus the shell's own noise."""
        cleaned = strip_ansi(raw).replace("\r\n", "\n").replace("\r", "\n")
        lines = cleaned.split("\n")

        body: list[str] = []
        logs: list[str] = []
        for line in self._without_echo(lines, command):
            # Once split, the prompt may or may not have kept its trailing
            # space, so both forms are tested against the caller's pattern.
            if self.prompt.search(line) or self.prompt.search(line + " "):
                continue
            if is_log_line(line):
                logs.append(line.strip())
                self._log(f"<~ {line.strip()}")
            elif line.strip():
                body.append(line.rstrip())
                self._log(f"<- {line.rstrip()}")

        return Response(command=command, lines=body, log_lines=logs, duration_s=duration_s)

    @staticmethod
    def _without_echo(lines: list[str], command: str) -> list[str]:
        """`lines` with the shell's echo of `command` dropped.

        Only the first line is considered, and only when it really is the echo:
        a command whose own output repeats it (`history`, say) must keep it.
        """
        if not lines:
            return lines
        first = lines[0].strip()
        if first == command.strip() or first.endswith(command.strip()):
            return lines[1:]
        return lines

    def _log(self, line: str) -> None:
        """Write one line to the run's CLI log, if this shell has a session."""
        if self._log_session is not None:
            self._log_session.write_cli(line)
