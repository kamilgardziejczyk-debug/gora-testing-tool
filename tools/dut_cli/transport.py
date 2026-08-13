"""The serial half of talking to a DUT's shell: bytes out, bytes in.

Kept separate from `shell.py` because the two change for different reasons. The
framing rules there follow Zephyr's shell; what is here follows the bench - a
USB CDC port that disappears when the DUT resets, and reads that must be sliced
finely enough to notice a prompt without waiting for a timeout to expire.

This module knows nothing about prompts, commands or responses. It reads until
the caller's pattern shows up, or until the caller's deadline passes.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Callable

import serial

LOGGER = logging.getLogger(__name__)

DEFAULT_BAUD = 115200

# Read slice. Short so a prompt is noticed as soon as it lands rather than at
# the end of a fixed wait, and so a deadline is honoured to within one slice.
READ_TIMEOUT_S = 0.05

# Gap between reconnect attempts while the DUT is off the bus. A reset plus USB
# re-enumeration takes a second or two; polling faster only spins the CPU.
RECONNECT_DELAY_S = 0.5

# How long the port must stay silent for `drain_idle` to call it quiet, and how
# long it may spend trying. A prompt still in flight when a command is sent
# would otherwise be read back as that command's terminator, ending the read
# before the DUT has answered - so it is worth a few tens of milliseconds per
# command to start from a genuinely empty wire.
DRAIN_QUIET_S = 0.05
DRAIN_MAX_S = 0.5


class SerialTransport:
    """A serial port to the DUT's shell, reopened if the DUT re-enumerates."""

    def __init__(self, port: str, baud: int = DEFAULT_BAUD, reconnect: bool = True):
        """Talk to `port` at `baud`.

        `reconnect` keeps the transport usable across a DUT reset: a read that
        fails because the port vanished waits for it to come back instead of
        raising. Turn it off to have any disappearance surface immediately.
        """
        self.port = port
        self.baud = baud
        self.reconnect = reconnect
        self._serial: serial.Serial | None = None

    @property
    def is_open(self) -> bool:
        """Whether the port is currently open."""
        return self._serial is not None and self._serial.is_open

    def open(self) -> None:
        """Open the port.

        Raises `ConnectionError` if it cannot be opened now - unlike a mid-run
        disappearance, a port that was never there is a bench misconfiguration
        (wrong path, DUT unplugged, another process holding it) and pretending
        otherwise would only defer the same error to the first command.
        """
        try:
            self._serial = serial.Serial(self.port, self.baud, timeout=READ_TIMEOUT_S)
        except (serial.SerialException, ValueError, OSError) as error:
            raise ConnectionError(
                f"could not open DUT CLI port {self.port} at {self.baud} baud ({error})"
            ) from error
        LOGGER.info("Opened DUT CLI port %s at %d baud", self.port, self.baud)

    def close(self) -> None:
        """Close the port. Idempotent, and safe after a failure."""
        if self._serial is not None:
            try:
                self._serial.close()
            except (serial.SerialException, OSError):  # already gone; nothing to do
                pass
            self._serial = None

    def drain(self) -> str:
        """Read and discard whatever is already buffered, returning it.

        Called before sending a command so that the previous command's tail, or
        log output that arrived while nothing was reading, cannot be mistaken
        for the new command's response.
        """
        if not self.is_open:
            return ""
        pending = b""
        while self._serial.in_waiting:
            pending += self._serial.read(self._serial.in_waiting)
        return pending.decode("utf-8", errors="replace")

    def drain_idle(self, quiet_s: float = DRAIN_QUIET_S, max_s: float = DRAIN_MAX_S) -> str:
        """Discard input until the port has stayed silent for `quiet_s`.

        Stronger than `drain()`, which only takes what has already landed: a
        prompt the DUT sent microseconds ago is still in flight at that instant
        and arrives right after the next command goes out, where it reads as
        that command's terminator and yields an empty response in ~0s. Waiting
        for silence instead means a command is always sent onto an empty wire.

        Gives up after `max_s` rather than waiting out a DUT that is talking
        continuously (a reboot loop, a chatty log backend): the caller's own
        framing has to cope with that case regardless.
        """
        if not self.is_open:
            return ""

        deadline = time.monotonic() + max_s
        pending = b""
        last_arrival = time.monotonic()
        while time.monotonic() < deadline:
            if self._serial.in_waiting:
                pending += self._serial.read(self._serial.in_waiting)
                last_arrival = time.monotonic()
            elif time.monotonic() - last_arrival >= quiet_s:
                break
            else:
                time.sleep(0.005)
        return pending.decode("utf-8", errors="replace")

    def write_line(self, text: str, newline: str = "\r\n") -> None:
        """Send `text` followed by `newline`."""
        if not self.is_open:
            raise ConnectionError(f"DUT CLI port {self.port} is not open")
        try:
            self._serial.write((text + newline).encode("utf-8"))
            self._serial.flush()
        except (serial.SerialException, OSError) as error:
            raise ConnectionError(f"writing to {self.port} failed ({error})") from error

    def read_until(
        self,
        pattern: re.Pattern,
        deadline: float,
        normalize: Callable[[str], str] | None = None,
    ) -> tuple[str, bool]:
        """Read until `pattern` matches what has arrived, or `deadline` passes.

        `deadline` is an absolute `time.monotonic()` value, so a caller looping
        over several reads cannot extend its own budget. Returns everything read
        *raw* and whether the pattern was found - a caller that wanted the
        pattern is expected to report the partial text, which is the most useful
        thing it has when a DUT answers half a response and stops.

        `normalize` is applied before matching only. A terminator that the
        device decorates - Zephyr wraps its prompt in colour escapes, so the
        stream does not literally end with the prompt text - is otherwise
        unmatchable, while the raw bytes still have to reach the caller intact.
        """
        buffer = b""
        while True:
            text = buffer.decode("utf-8", errors="replace")
            if pattern.search(normalize(text) if normalize else text):
                return text, True
            if time.monotonic() >= deadline:
                return text, False
            buffer += self._read_slice(deadline)

    def _read_slice(self, deadline: float) -> bytes:
        """Read whatever arrives within one slice, tolerating a vanished port."""
        try:
            return self._serial.read(max(1, self._serial.in_waiting))
        except (serial.SerialException, OSError, TypeError) as error:
            # TypeError: pyserial raises it on a closed handle in some versions.
            LOGGER.warning("DUT CLI port %s dropped while reading (%s)", self.port, error)
            self._reopen(deadline)
            return b""

    def _reopen(self, deadline: float) -> None:
        """Wait for the port to come back, until `deadline`.

        A reset mid-command loses that command's response either way; the point
        is to leave the transport usable for the next one instead of poisoning
        every command after a reboot.
        """
        self.close()
        if not self.reconnect:
            raise ConnectionError(f"DUT CLI port {self.port} disappeared")

        while time.monotonic() < deadline:
            time.sleep(RECONNECT_DELAY_S)
            try:
                self._serial = serial.Serial(self.port, self.baud, timeout=READ_TIMEOUT_S)
            except (serial.SerialException, ValueError, OSError):
                continue
            LOGGER.info("Reopened DUT CLI port %s", self.port)
            return
