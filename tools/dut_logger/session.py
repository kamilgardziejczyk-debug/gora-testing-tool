"""Three log files for one scenario run, written side by side.

A run produces:

*   `<stem>.tool.log`     - the testing tool's own log output.
*   `<stem>.device.log`   - the DUT's serial output, timestamped.
*   `<stem>.combined.log` - both of the above interleaved, plus test start/stop
    markers, so DUT output can be read against the command that provoked it.

`<stem>` is taken from the HTML report's path, so the four artefacts of a run
always share a name.

Device lines are also kept in memory as they are written (see
`LogSession.device_lines`), so a scenario command can assert against what the
DUT said instead of only leaving it on disk for a human to read afterwards.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TextIO

TOOL_SUFFIX = ".tool.log"
DEVICE_SUFFIX = ".device.log"
COMBINED_SUFFIX = ".combined.log"

# Prefixes distinguishing the two sources once they are interleaved. Markers
# carry no prefix, so they stand out as structure rather than content.
TOOL_PREFIX = "tool"
DEVICE_PREFIX = "dut "

# How many device lines stay readable in memory. A quiet run emits a few
# hundred; the cap is set well above that so a whole scenario normally stays
# available, while a DUT stuck in a reboot loop still cannot exhaust memory.
# Whatever falls off the back is counted (`device_lines_dropped`) rather than
# forgotten, so a caller can tell "no match" from "may have missed it".
DEVICE_BUFFER_LINES = 5000


@dataclass(frozen=True)
class DeviceLine:
    """One captured line of DUT output, as held in `LogSession`'s buffer."""

    seq: int
    """Position in the capture, counting from 1 and never reused.

    Passed back as `since_seq` to read only what has arrived since, which is
    how a caller anchors a wait to "from here on" without re-reading history.
    """

    at: float
    """`time.monotonic()` when the line was captured.

    Monotonic rather than wall-clock deliberately: the host may itself be
    correcting its clock while a scenario runs, and an elapsed-time comparison
    must not be able to go backwards.
    """

    text: str
    """The line as the DUT emitted it, without the log files' `[HH:MM:SS]`
    prefix - a pattern written against firmware output should not have to know
    how this tool formats its logs."""


def log_paths(report_path: Path) -> tuple[Path, Path, Path]:
    """The tool/device/combined log paths that pair with `report_path`."""
    stem = report_path.parent / report_path.stem
    return (
        Path(f"{stem}{TOOL_SUFFIX}"),
        Path(f"{stem}{DEVICE_SUFFIX}"),
        Path(f"{stem}{COMBINED_SUFFIX}"),
    )


def timestamp() -> str:
    """Wall-clock stamp with milliseconds, as used at the start of every line."""
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


class LogSession:
    """Open handles to a run's three log files, safe to write from any thread.

    The device reader runs on its own thread while the scenario runner writes
    markers from the main one, and both land in the combined log - so every
    write takes a lock. It is an `RLock` because a caller holding it must be
    able to log without deadlocking against itself.

    Device lines are additionally buffered in memory for `device_lines()` and
    `wait_for_device_lines()`, letting the main thread read what the reader
    thread has captured.
    """

    def __init__(self, report_path: Path):
        """Prepare (but do not yet open) the logs pairing with `report_path`."""
        self.tool_path, self.device_path, self.combined_path = log_paths(report_path)
        self._lock = threading.RLock()
        self._tool: TextIO | None = None
        self._device: TextIO | None = None
        self._combined: TextIO | None = None
        self._device_lines: deque[DeviceLine] = deque(maxlen=DEVICE_BUFFER_LINES)
        self._device_seq = 0
        self._device_lines_dropped = 0
        # Shares the write lock, so a line cannot be appended between a waiter
        # finding the buffer empty and it starting to wait - which would
        # otherwise lose that wake-up and stall until the timeout.
        self._device_line_added = threading.Condition(self._lock)

    def open(self) -> None:
        """Create the parent directory and open all three files for writing."""
        self.tool_path.parent.mkdir(parents=True, exist_ok=True)
        self._tool = self.tool_path.open("w", encoding="utf-8")
        self._device = self.device_path.open("w", encoding="utf-8")
        self._combined = self.combined_path.open("w", encoding="utf-8")

    def close(self) -> None:
        """Close every open handle. Idempotent, and safe to call after a failure."""
        with self._lock:
            for stream in (self._tool, self._device, self._combined):
                if stream is not None and not stream.closed:
                    stream.close()
            self._tool = self._device = self._combined = None
            # No more device lines are coming, so release anyone still waiting
            # for one instead of leaving them to sit out their full timeout.
            self._device_line_added.notify_all()

    def write_tool(self, line: str) -> None:
        """Record one line of the tool's own output, in tool + combined."""
        stamped = f"[{timestamp()}] {line}"
        with self._lock:
            self._write(self._tool, stamped)
            self._write(self._combined, f"[{timestamp()}] {TOOL_PREFIX} | {line}")

    def write_device(self, line: str) -> None:
        """Record one line of DUT serial output, in device + combined.

        Also appends it to the in-memory buffer and wakes any waiter, so a
        command asserting on DUT output sees it as it arrives.
        """
        stamped = f"[{timestamp()}] {line}"
        with self._lock:
            self._write(self._device, stamped)
            self._write(self._combined, f"[{timestamp()}] {DEVICE_PREFIX} | {line}")
            self._buffer_device(line)

    def _buffer_device(self, line: str) -> None:
        """Append `line` to the readable buffer. Caller must hold the lock."""
        if len(self._device_lines) == DEVICE_BUFFER_LINES:
            self._device_lines_dropped += 1
        self._device_seq += 1
        self._device_lines.append(DeviceLine(seq=self._device_seq, at=time.monotonic(), text=line))
        self._device_line_added.notify_all()

    def device_seq(self) -> int:
        """Sequence of the newest captured line, or 0 if nothing yet.

        Taken before an action to read only what that action provoked, by
        passing it as `since_seq` afterwards.
        """
        with self._lock:
            return self._device_seq

    def device_lines(self, since_seq: int = 0) -> list[DeviceLine]:
        """Buffered device lines with `seq` greater than `since_seq`.

        The default returns everything still buffered, so a caller can check
        output that arrived before it started looking - a DUT does not wait to
        be asked, and a reset early in a scenario can have it say the
        interesting thing several commands before anything reads for it.
        """
        with self._lock:
            return [entry for entry in self._device_lines if entry.seq > since_seq]

    def device_lines_dropped(self) -> int:
        """How many captured lines have been evicted from the buffer.

        Non-zero means the in-memory view is no longer the whole capture, so a
        failed search over it cannot prove the DUT never said the thing - the
        log files remain complete either way.
        """
        with self._lock:
            return self._device_lines_dropped

    def wait_for_device_lines(self, since_seq: int, timeout_s: float) -> list[DeviceLine]:
        """Lines after `since_seq`, waiting up to `timeout_s` for the first one.

        Returns at once (with whatever is already buffered) if anything newer
        than `since_seq` is there, so no wait is spent on output that has
        already arrived. Returns an empty list if the timeout passes with
        nothing new, or if the session is closed while waiting.

        Waits for *any* new line rather than a matching one: what counts as a
        match belongs to the caller, which loops until it is satisfied or its
        own deadline passes.
        """
        with self._device_line_added:
            pending = self.device_lines(since_seq)
            if pending:
                return pending
            self._device_line_added.wait(timeout_s)
            return self.device_lines(since_seq)

    def write_device_note(self, text: str) -> None:
        """Record a note *about* the device log, in device + combined.

        For saying why a device log is empty. Marked as a note so it can't be
        confused with something the DUT actually emitted.
        """
        self.write_device(f"[no-dut] {text}")

    def write_marker(self, text: str) -> None:
        """Record a test start/stop marker. Combined log only, by design.

        The per-source logs stay faithful to their single source; the combined
        log is the one that gains structure.
        """
        with self._lock:
            self._write(self._combined, f"[{timestamp()}] --- {text} ---")

    @staticmethod
    def _write(stream: TextIO | None, line: str) -> None:
        """Append one line and flush it.

        Flushed per line rather than buffered: a scenario that is killed
        mid-run (or a DUT that hangs the Pi) must still leave the log written
        up to that point, since that is exactly when the log matters most.
        """
        if stream is None or stream.closed:
            return
        stream.write(line + "\n")
        stream.flush()
