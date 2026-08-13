"""What a shell command said, and the views a test can read it through.

`Response` keeps the raw lines and offers three progressively structured views
on top of them - `fields()`, `json()` and `match()`. None of them is privileged:
Zephyr's built-in commands print human-readable text, while a custom command is
free to print JSON, and one scenario can assert against both.

Nothing here decides whether a response means pass or fail. `error` reports only
that the *shell itself* refused the command (an unknown command, a bad argument
count), which is a different thing from a command that ran and returned news the
test doesn't like - that judgement belongs to the caller's `validation`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

# Colour and cursor-movement escapes. Zephyr's shell emits these around the
# prompt and in coloured log output, and they would otherwise end up inside
# matched values.
ANSI_ESCAPE = re.compile(r"\x1b(?:\[[0-9;?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")

# A Zephyr log line, in either the uptime form the logging subsystem uses by
# default (`[00:00:06.312,000] <inf> module: text`) or the wall-clock form a
# device prints once its RTC is set (`[2026-08-12T12:24:25,428000Z] <inf> ...`).
# The logging backend usually shares the shell's UART, so these can arrive in
# the middle of a response and must never be mistaken for part of it.
LOG_LINE = re.compile(r"^\[[^\]]+\]\s*<(?:err|wrn|inf|dbg)>\s")

# The logging subsystem's own notice that it could not keep up. Not a response
# line either, and worth keeping because it explains a gap in the log output.
DROPPED_LINE = re.compile(r"^-{2,}\s*\d+\s+messages? dropped\s*-{2,}$")

# How the Zephyr shell complains about a command it could not run at all. Kept
# deliberately specific: a broad match like "not found" would also flag a
# working command that reported "device not found", which is a real answer.
SHELL_COMPLAINTS = (
    "command not found",
    "wrong parameter count",
    "unknown parameter",
    "please press the <tab> button",
)

# Separators a `key: value` line may use, in the order they are tried.
FIELD_SEPARATORS = (":", "=")


def strip_ansi(text: str) -> str:
    """`text` without terminal escape sequences."""
    return ANSI_ESCAPE.sub("", text)


def is_log_line(line: str) -> bool:
    """Whether `line` is Zephyr log output rather than a command's response."""
    stripped = line.strip()
    return bool(LOG_LINE.match(stripped) or DROPPED_LINE.match(stripped))


@dataclass(frozen=True)
class Response:
    """One command's reply, already framed and cleaned of shell mechanics."""

    command: str
    """The command as sent, without its line ending."""

    lines: list[str] = field(default_factory=list)
    """The response itself: the command's echo, the trailing prompt, and any
    interleaved log output removed."""

    log_lines: list[str] = field(default_factory=list)
    """Zephyr log output that arrived while this command was running.

    Kept apart rather than discarded: it is often the reason a command answered
    the way it did, but letting it into `lines` would corrupt every view below.
    """

    duration_s: float = 0.0
    """Seconds from sending the command to the prompt coming back."""

    @property
    def text(self) -> str:
        """The response as one string, one line per line."""
        return "\n".join(self.lines)

    @property
    def error(self) -> str | None:
        """The shell's complaint if it refused the command, else None.

        Says nothing about whether a command that *did* run did what the test
        wanted; that is for the caller to assert on.
        """
        for line in self.lines:
            lowered = line.lower()
            if any(complaint in lowered for complaint in SHELL_COMPLAINTS):
                return line.strip()
        return None

    def fields(self) -> dict[str, str]:
        """`key: value` (or `key=value`) lines as a dict.

        Indentation is ignored and values keep their spacing, so
        `  rssi: -31 dBm` yields `{"rssi": "-31 dBm"}`. Lines with no separator
        are skipped rather than raising - a command is free to print a heading
        above its fields. A repeated key keeps the last occurrence.
        """
        parsed: dict[str, str] = {}
        for line in self.lines:
            key, value = _split_field(line)
            if key:
                parsed[key] = value
        return parsed

    def json(self) -> object:
        """The response parsed as JSON.

        Tries the whole response first, then the first line that starts like
        JSON - so a command may print a heading before its payload. Raises
        `ValueError` (quoting the text) when there is nothing parseable, since
        a caller asking for JSON cannot do anything useful with prose.
        """
        candidates = [self.text] + [line for line in self.lines if line.strip()[:1] in "{["]
        for candidate in candidates:
            try:
                return json.loads(candidate)
            except ValueError:
                continue
        raise ValueError(
            f"DutCli: the response to '{self.command}' is not JSON: {self.text!r}"
        )

    def match(self, pattern: str) -> re.Match | None:
        """The first match of `pattern` anywhere in the response, or None.

        Searched against the whole response text, so a pattern may span lines
        with an explicit `\\n`. Named groups are the intended way to pull values
        out: `r"unix=(?P<unix>\\d+)"` then gives `match["unix"]`.
        """
        return re.search(pattern, self.text)


def _split_field(line: str) -> tuple[str | None, str]:
    """`line` as a `(key, value)` pair, or `(None, "")` if it isn't one.

    The earliest separator in the line wins, so a value containing the other
    separator (`endpoint: host=a2on...`) still splits in the right place.
    """
    positions = [line.find(sep) for sep in FIELD_SEPARATORS]
    found = [index for index in positions if index > 0]
    if not found:
        return None, ""

    at = min(found)
    return line[:at].strip(), line[at + 1:].strip()
