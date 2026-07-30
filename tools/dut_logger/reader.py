"""Background capture of a DUT's serial console into a `LogSession`.

The awkward part this exists to handle: a scenario that flashes or resets the
DUT (`!ProgramJlink`, a BLE write that reboots it) makes a USB CDC console such
as `/dev/ttyACM0` disappear and re-enumerate part-way through the run. That is
expected behaviour, not a failure, so the reader treats a dropped port as
something to wait out and reconnect to - while still refusing to start at all
if the console was never there in the first place.
"""

from __future__ import annotations

import logging
import threading

import serial

from .session import LogSession

LOGGER = logging.getLogger(__name__)

DEFAULT_BAUD = 115200

# Short read timeout so the thread notices `stop()` promptly instead of
# blocking until the DUT happens to say something.
READ_TIMEOUT_S = 0.5

# Gap between reconnect attempts after the DUT drops off the bus. A reset plus
# USB re-enumeration takes a second or two; polling faster just spins.
RECONNECT_DELAY_S = 0.5


class DutLogger:
    """Reads a DUT's serial console on a background thread until stopped."""

    def __init__(
        self,
        session: LogSession,
        port: str,
        baud: int = DEFAULT_BAUD,
    ):
        """Capture from `port` at `baud` into `session`."""
        self.session = session
        self.port = port
        self.baud = baud
        self._serial: serial.Serial | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Open the console and begin capturing.

        Raises `ConnectionError` if the port cannot be opened *now*. This is
        deliberately fatal: a run whose DUT console was never attached would
        otherwise finish with a convincing-looking but empty device log. Once
        capture is under way the opposite rule applies - see `_reopen()`.
        """
        try:
            self._serial = serial.Serial(self.port, self.baud, timeout=READ_TIMEOUT_S)
        except (serial.SerialException, ValueError, OSError) as error:
            raise ConnectionError(
                f"could not open DUT log port {self.port} at {self.baud} baud ({error})"
            ) from error

        self._thread = threading.Thread(target=self._run, name="dut-logger", daemon=True)
        self._thread.start()
        LOGGER.info("Capturing DUT log from %s at %d baud", self.port, self.baud)

    def stop(self) -> None:
        """Stop capturing and close the port. Idempotent."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=READ_TIMEOUT_S * 4)
            self._thread = None
        self._close_serial()

    def _run(self) -> None:
        """Read lines until stopped, reconnecting whenever the DUT drops off."""
        while not self._stop.is_set():
            if self._serial is None:
                if not self._reopen():
                    self._stop.wait(RECONNECT_DELAY_S)
                continue

            try:
                raw = self._serial.readline()
            except (serial.SerialException, OSError) as error:
                self._on_disconnect(error)
                continue

            # An empty read is just the timeout expiring with the DUT quiet,
            # which is normal and must not be mistaken for a disconnect.
            if raw:
                self.session.write_device(self._decode(raw))

    def _on_disconnect(self, error: Exception) -> None:
        """Note that the console vanished and drop the handle ready for a retry."""
        self._close_serial()
        self.session.write_marker(f"DUT LOG PORT LOST: {self.port} ({error})")
        LOGGER.warning("DUT log port %s disconnected (%s); will retry", self.port, error)

    def _reopen(self) -> bool:
        """Try once to reattach to the console, reporting whether it worked.

        Unlike `start()`, failure here is not fatal: the DUT is expected to
        disappear across a reset, and the scenario should keep running (and
        keep capturing once it returns) rather than fail for it.
        """
        try:
            self._serial = serial.Serial(self.port, self.baud, timeout=READ_TIMEOUT_S)
        except (serial.SerialException, ValueError, OSError):
            return False

        self.session.write_marker(f"DUT LOG PORT REATTACHED: {self.port}")
        LOGGER.info("DUT log port %s reattached", self.port)
        return True

    def _close_serial(self) -> None:
        """Close the serial handle, ignoring errors from an already-gone device."""
        if self._serial is None:
            return
        try:
            self._serial.close()
        except (serial.SerialException, OSError):
            pass
        self._serial = None

    @staticmethod
    def _decode(raw: bytes) -> str:
        """Decode one line of DUT output as leniently as possible.

        A device mid-reset emits partial characters and line noise;
        `errors="replace"` keeps that in the log as replacement characters
        instead of throwing away the line (or the capture thread).
        """
        return raw.decode("utf-8", errors="replace").rstrip("\r\n")
