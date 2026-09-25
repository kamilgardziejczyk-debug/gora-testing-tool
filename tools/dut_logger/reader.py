"""Background capture of a DUT's serial console into a `LogSession`.

The awkward part this exists to handle: a scenario that flashes or resets the
DUT (`!ProgramJlink`, a BLE write that reboots it) makes a USB CDC console such
as `/dev/ttyACM0` disappear and re-enumerate part-way through the run. That is
expected behaviour, not a failure, so the reader treats a dropped port as
something to wait out and reconnect to - while still refusing to start at all
if the console was never there in the first place.

A second, harder case is a board whose console *is* its programming port, as an
ESP32 wired through a USB-UART bridge is: `/dev/ttyUSB0` carries the firmware's
log output and esptool's flashing protocol both. That port belongs to the
bridge chip rather than to the firmware, so it never disappears - it stays
openable while the ROM bootloader runs and while flash is being written - and
two readers on it simply split the byte stream between them, costing esptool
parts of its handshake. Reconnect logic cannot help, because nothing ever
disconnects. `pause()` and `resume()` exist for that: the scenario hands the
port over for the duration of the flash (see `!DutLogControl`) and takes it
back afterwards.
"""

from __future__ import annotations

import logging
import threading
import time

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

# How long a paused reader sleeps between checks that it may resume. Only
# governs how quickly the thread starts reading again - `resume()` reopens the
# port itself, so nothing the DUT says in the meantime is lost to this.
PAUSE_POLL_S = 0.1

# How long `pause()` waits for the reader thread to actually close the port.
# It may be sitting in a `readline()` that runs to `READ_TIMEOUT_S`, so the
# budget covers several of those; exceeding it means the thread is wedged, and
# handing the port to esptool anyway is exactly what pausing exists to prevent.
RELEASE_TIMEOUT_S = READ_TIMEOUT_S * 4

# How long `resume()` keeps trying to reopen the console. A device still
# rebooting after a flash can refuse the port briefly; one that never comes
# back is a failure worth reporting rather than logging nothing for the rest of
# the run.
RESUME_TIMEOUT_S = 10.0


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
        # False, not True: on a board where the console shares the
        # programming header (DTR/RTS wired to EN/GPIO0 through the usual
        # auto-reset transistors), leaving both lines asserted - what
        # pyserial does on an open() with neither explicitly set - can hold
        # EN low for as long as this port stays open, which is normally the
        # whole run. That reads as a DUT that never boots at all, on every
        # console/programming-port-shared board, regardless of relay or
        # battery state. Nothing in this codebase ever asks `start()` for a
        # reset (`!DutLogControl`'s own `reset_dut` defaults to and is
        # overwhelmingly left at False), so deasserting DTR/RTS here as well
        # costs boards without that wiring nothing.
        self.reset_dut_on_open = False
        self._serial: serial.Serial | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # Guards `_paused` and `_port_released` together, so the reader thread
        # cannot announce a release that a concurrent `resume()` has already
        # cancelled - which would leave a later `pause()` believing the port was
        # free while the thread was still reading it.
        self._state_lock = threading.Lock()
        self._paused = False
        self._port_released = threading.Event()

    def start(self) -> None:
        """Open the console and begin capturing.

        Raises `ConnectionError` if the port cannot be opened *now*. This is
        deliberately fatal: a run whose DUT console was never attached would
        otherwise finish with a convincing-looking but empty device log. Once
        capture is under way the opposite rule applies - see `_reopen()`.
        """
        try:
            self._serial = self._open_serial()
        except (serial.SerialException, ValueError, OSError) as error:
            raise ConnectionError(
                f"could not open DUT log port {self.port} at {self.baud} baud ({error})"
            ) from error

        self._thread = threading.Thread(target=self._run, name="dut-logger", daemon=True)
        self._thread.start()
        LOGGER.info("Capturing DUT log from %s at %d baud", self.port, self.baud)

    @property
    def is_paused(self) -> bool:
        """Whether capture is currently suspended and the port handed over."""
        with self._state_lock:
            return self._paused

    def pause(self) -> None:
        """Close the console and stop reading it, until `resume()`.

        Returns once the port is genuinely closed, not merely once the reader
        has been asked to close it: the caller's next act is to hand the port to
        something that needs it exclusively, so returning early would reintroduce
        the very overlap this prevents. Idempotent.

        Raises `TimeoutError` if the reader does not let go within
        `RELEASE_TIMEOUT_S`.
        """
        with self._state_lock:
            if self._paused:
                return
            self._paused = True
            self._port_released.clear()

        if self._thread is None or not self._thread.is_alive():
            # Nothing running to hand the port back; close it here instead.
            self._close_serial()
        elif not self._port_released.wait(RELEASE_TIMEOUT_S):
            raise TimeoutError(
                f"DUT log reader did not release {self.port} within {RELEASE_TIMEOUT_S}s"
            )

        self.session.write_marker(f"DUT LOG PORT RELEASED: {self.port}")
        LOGGER.info("Released DUT log port %s", self.port)

    def resume(self, reset_dut: bool = False) -> None:
        """Reopen the console and start reading it again. Idempotent.

        Opens the port here rather than leaving it to the reader thread, so a
        console that cannot be recovered is reported to the scenario command
        that asked for it instead of quietly logging nothing for the rest of the
        run.

        `reset_dut` says whether opening the port may reset the device. It
        stays off by default because the caller is typically taking the console
        back from a flash that has just reset the DUT on its way out, and
        resetting it again would discard the boot this is here to capture. The
        choice sticks for later reconnects too - a port shared with a
        programmer keeps the same wiring for the rest of the run.

        Anything that arrived while capture was stopped is dropped rather than
        replayed: pyserial clears the input buffer as it opens, and on a shared
        port that buffer holds the programmer's own traffic, which has no
        business being decoded as console output.

        Raises `ConnectionError` if the port cannot be reopened in
        `RESUME_TIMEOUT_S`.
        """
        with self._state_lock:
            if not self._paused:
                return

        self.reset_dut_on_open = reset_dut
        console = self._reopen_until(time.monotonic() + RESUME_TIMEOUT_S)

        # Published under the lock, together with the flag that stops the reader
        # closing it: a paused reader closes whatever handle it finds, so a port
        # handed over before the flag is cleared would be shut again the moment
        # the thread next woke.
        with self._state_lock:
            self._serial = console
            self._paused = False
            self._port_released.clear()

        self.session.write_marker(f"DUT LOG PORT RECLAIMED: {self.port}")
        LOGGER.info("Reclaimed DUT log port %s", self.port)

    def send_line(self, text: str) -> None:
        """Type `text` and a newline on the console the reader is capturing.

        Uses the reader's own handle, so a console that is also the programming
        port needs no second opener. Raises `ConnectionError` while capture is
        paused or the port is down: a command sent into nothing would otherwise
        look like a DUT that ignored it.
        """
        with self._state_lock:
            console = None if self._paused else self._serial
        if console is None:
            raise ConnectionError(
                f"cannot send to DUT log port {self.port}: capture is paused or the port is not open"
            )
        try:
            console.write(text.encode("utf-8") + b"\n")
            console.flush()
        except (serial.SerialException, OSError) as error:
            raise ConnectionError(f"could not send to DUT log port {self.port} ({error})") from error
        self.session.write_marker(f"DUT LOG PORT SENT: {text}")

    def _reopen_until(self, deadline: float) -> serial.Serial:
        """Keep trying to open the console until `deadline`, or raise."""
        last_error: Exception | None = None
        while True:
            try:
                return self._open_serial()
            except (serial.SerialException, ValueError, OSError) as error:
                last_error = error
            if time.monotonic() >= deadline:
                raise ConnectionError(
                    f"could not reopen DUT log port {self.port} at {self.baud} baud "
                    f"({last_error})"
                ) from last_error
            time.sleep(RECONNECT_DELAY_S)

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
            if self._idle_while_paused():
                self._stop.wait(PAUSE_POLL_S)
                continue

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

    def _idle_while_paused(self) -> bool:
        """Close the port if paused, reporting whether the caller should idle.

        The close and the release announcement both happen under the lock, so
        `resume()` cannot clear the flag in between and leave a stale "port is
        free" behind it.
        """
        with self._state_lock:
            if not self._paused:
                return False
            self._close_serial()
            self._port_released.set()
            return True

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
            self._serial = self._open_serial()
        except (serial.SerialException, ValueError, OSError):
            return False

        self.session.write_marker(f"DUT LOG PORT REATTACHED: {self.port}")
        LOGGER.info("DUT log port %s reattached", self.port)
        return True

    def _open_serial(self) -> serial.Serial:
        """Open the console, optionally without resetting the DUT.

        On boards where the console is the programming port, DTR and RTS drive
        the reset and boot-mode pins through the usual auto-reset transistor
        pair, so merely opening the port reboots the chip. Deasserting both
        before the handle is opened keeps a reattach from throwing away the
        boot output it was reopened to capture. pyserial applies the two line
        states immediately after the file descriptor is opened, which is why
        the handle is configured unopened and opened afterwards rather than
        constructed in one call.

        Deasserted by default (`reset_dut_on_open` starts `False`): on a board
        with no auto-reset wiring on DTR/RTS this changes nothing, and on one
        that shares the console with the programming header it is what keeps
        `start()` from holding EN low for the rest of the run. Something that
        actually wants a fresh boot - a reattach right after a flash the
        scenario just performed, say - asks for it explicitly by setting
        `reset_dut_on_open` (or via `resume(reset_dut=True)`) rather than
        relying on this default.
        """
        console = serial.Serial()
        console.port = self.port
        console.baudrate = self.baud
        console.timeout = READ_TIMEOUT_S
        if not self.reset_dut_on_open:
            console.dtr = False
            console.rts = False
        console.open()
        return console

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
