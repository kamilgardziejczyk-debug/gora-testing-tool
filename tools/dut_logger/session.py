"""Three log files for one scenario run, written side by side.

A run produces:

*   `<stem>.tool.log`     - the testing tool's own log output.
*   `<stem>.device.log`   - the DUT's serial output, timestamped.
*   `<stem>.combined.log` - both of the above interleaved, plus test start/stop
    markers, so DUT output can be read against the command that provoked it.

`<stem>` is taken from the HTML report's path, so the four artefacts of a run
always share a name.
"""

from __future__ import annotations

import threading
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
    """

    def __init__(self, report_path: Path):
        """Prepare (but do not yet open) the logs pairing with `report_path`."""
        self.tool_path, self.device_path, self.combined_path = log_paths(report_path)
        self._lock = threading.RLock()
        self._tool: TextIO | None = None
        self._device: TextIO | None = None
        self._combined: TextIO | None = None

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

    def write_tool(self, line: str) -> None:
        """Record one line of the tool's own output, in tool + combined."""
        stamped = f"[{timestamp()}] {line}"
        with self._lock:
            self._write(self._tool, stamped)
            self._write(self._combined, f"[{timestamp()}] {TOOL_PREFIX} | {line}")

    def write_device(self, line: str) -> None:
        """Record one line of DUT serial output, in device + combined."""
        stamped = f"[{timestamp()}] {line}"
        with self._lock:
            self._write(self._device, stamped)
            self._write(self._combined, f"[{timestamp()}] {DEVICE_PREFIX} | {line}")

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
