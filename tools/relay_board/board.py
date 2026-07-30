"""8-channel relay board driven over the Raspberry Pi GPIO header.

Relays are addressed 1..8 to match the IN1..IN8 silkscreen on the board.
Callers only ever ask for "energized" or "de-energized"; mapping that to an
electrical level (active-low vs active-high) is this module's business.
"""

from __future__ import annotations

import logging
import time
from typing import Sequence

try:
    import RPi.GPIO as GPIO
except (ImportError, RuntimeError):
    # ImportError: RPi.GPIO not installed. RuntimeError: installed, but this is
    # not a Raspberry Pi - the module raises on import off-device. Either way we
    # fall back to simulating the board rather than failing, so scenarios and
    # the CLI stay runnable on a PC.
    GPIO = None


LOGGER = logging.getLogger(__name__)

RELAY_COUNT = 8

# Relay 1..8 -> BCM pin. All are general-purpose with no boot-time pull-up and
# no I2C/UART/SPI function, and all sit on the lower half of the 40-pin header
# so the 10-wire run stays contiguous.
DEFAULT_PINS: tuple[int, ...] = (5, 6, 13, 19, 16, 26, 20, 21)

# Most 8-channel opto-isolated boards energize a relay when its IN pin is
# pulled LOW. Pass active_low=False for a board that energizes on HIGH.
DEFAULT_ACTIVE_LOW = True


class RelayBoard:
    """An 8-channel relay board wired to the Pi's GPIO header.

    State is deliberately *latching*: energizing a relay leaves it energized
    after the call, and after the process exits, because the pin direction and
    output value live in the SoC rather than in this object. That is what makes
    one-shot CLI use (`relay_board.py on 3`) behave the way an operator
    expects. Call `release()` explicitly to hand the pins back, which
    de-energizes every relay this instance configured.
    """

    def __init__(
        self,
        pins: Sequence[int] = DEFAULT_PINS,
        active_low: bool = DEFAULT_ACTIVE_LOW,
    ) -> None:
        """Bind relays 1..8 to `pins` in order, using `active_low` polarity."""
        if len(pins) != RELAY_COUNT:
            raise ValueError(f"expected {RELAY_COUNT} pins, got {len(pins)}")
        if len(set(pins)) != len(pins):
            raise ValueError(f"duplicate pins in {list(pins)}")

        self.pins: tuple[int, ...] = tuple(pins)
        self.active_low = active_low
        # Pins this instance has already called GPIO.setup() on, so a second
        # command on the same relay doesn't re-configure it.
        self._configured: set[int] = set()
        # Simulated pin levels, used only when RPi.GPIO is unavailable.
        self._simulated: dict[int, bool] = {}

    @property
    def simulated(self) -> bool:
        """Whether this board is simulating instead of driving real pins."""
        return GPIO is None

    def pin_for(self, relay: int) -> int:
        """BCM pin driving `relay`, which must be in 1..8."""
        if not 1 <= relay <= RELAY_COUNT:
            raise ValueError(f"relay must be in 1..{RELAY_COUNT}, got {relay}")
        return self.pins[relay - 1]

    def set(self, relay: int, energized: bool) -> None:
        """Energize or de-energize `relay`."""
        pin = self.pin_for(relay)
        level = self._level_for(energized)
        self._ensure_configured(pin)

        if GPIO is None:
            self._simulated[pin] = level
            LOGGER.warning(
                "RPi.GPIO is not available on this platform. Simulating: relay %d "
                "(BCM %d) %s",
                relay,
                pin,
                "energized" if energized else "de-energized",
            )
            return

        GPIO.output(pin, GPIO.HIGH if level else GPIO.LOW)
        LOGGER.info(
            "Relay %d (BCM %d) %s", relay, pin, "energized" if energized else "de-energized"
        )

    def on(self, relay: int) -> None:
        """Energize `relay` (closes its NO contact)."""
        self.set(relay, True)

    def off(self, relay: int) -> None:
        """De-energize `relay` (opens its NO contact)."""
        self.set(relay, False)

    def toggle(self, relay: int) -> bool:
        """Invert `relay`, returning its new energized state."""
        new_state = not self.state(relay)
        self.set(relay, new_state)
        return new_state

    def pulse(self, relay: int, duration_s: float) -> None:
        """Energize `relay` for `duration_s` seconds, then de-energize it.

        De-energizes even if interrupted part-way through, so a Ctrl-C or a
        failing scenario doesn't leave the coil held in.
        """
        if duration_s < 0:
            raise ValueError(f"duration_s must be >= 0, got {duration_s}")
        self.on(relay)
        try:
            time.sleep(duration_s)
        finally:
            self.off(relay)

    def set_all(self, energized: bool) -> None:
        """Energize or de-energize all 8 relays."""
        for relay in range(1, RELAY_COUNT + 1):
            self.set(relay, energized)

    def state(self, relay: int) -> bool:
        """Whether `relay` is currently energized.

        Reads the pin back rather than trusting a cached value, so this stays
        correct across separate CLI invocations - each is a fresh process,
        while the pin keeps its state in the SoC.
        """
        pin = self.pin_for(relay)
        if GPIO is None:
            if pin not in self._simulated:
                return False
            return self._energized_from_level(self._simulated[pin])

        self._ensure_mode()
        if GPIO.gpio_function(pin) != GPIO.OUT:
            # Never configured as an output, so the board sees a high-Z input.
            # That leaves the relay de-energized on any board that pulls IN up
            # to VCC through its opto LED, which is every board this targets.
            return False
        return self._energized_from_level(bool(GPIO.input(pin)))

    def states(self) -> list[bool]:
        """Energized state of relays 1..8, in order."""
        return [self.state(relay) for relay in range(1, RELAY_COUNT + 1)]

    def release(self) -> None:
        """Hand back every pin this instance configured, de-energizing them.

        Safe to call when nothing has been configured, and when RPi.GPIO is
        unavailable. Note the scenario runner's own `gpio_cleanup_all()` calls
        `GPIO.cleanup()` across the whole process, which releases relay pins
        too.
        """
        if not self._configured:
            return
        LOGGER.info("Releasing relay pins: %s", sorted(self._configured))
        if GPIO is not None:
            GPIO.cleanup(sorted(self._configured))
        self._configured.clear()
        self._simulated.clear()

    def describe(self) -> str:
        """One line per relay: number, BCM pin, and energized state."""
        lines = []
        for relay, energized in enumerate(self.states(), start=1):
            state = "ON " if energized else "off"
            lines.append(f"relay {relay}  BCM {self.pins[relay - 1]:<2}  {state}")
        return "\n".join(lines)

    def _level_for(self, energized: bool) -> bool:
        """Electrical level (True=HIGH) that puts `relay` in `energized`."""
        return (not energized) if self.active_low else energized

    def _energized_from_level(self, level: bool) -> bool:
        """Inverse of `_level_for`: what `level` means for the coil."""
        return (not level) if self.active_low else level

    def _ensure_configured(self, pin: int) -> None:
        """Make `pin` an output, driving the de-energized level first.

        The initial level matters: a pin coming out of reset reads 0 in its
        output register, so switching it to output without saying otherwise
        would drive LOW and clatter every relay in on an active-low board.
        Only touches pins this instance hasn't configured yet, so addressing
        one relay never disturbs the other seven.
        """
        if pin in self._configured:
            return

        idle_level = self._level_for(False)
        if GPIO is None:
            self._simulated.setdefault(pin, idle_level)
        else:
            self._ensure_mode()
            GPIO.setup(pin, GPIO.OUT, initial=GPIO.HIGH if idle_level else GPIO.LOW)
        self._configured.add(pin)

    def _ensure_mode(self) -> None:
        """Select BCM numbering and silence re-use warnings.

        Warnings are suppressed because a one-shot CLI run legitimately finds
        pins still configured as outputs by the previous run - that is the
        latching behaviour working, not a mistake worth printing about.
        """
        if GPIO is None:
            return
        GPIO.setwarnings(False)
        GPIO.setmode(GPIO.BCM)


def parse_pins(value: str) -> tuple[int, ...]:
    """Parse a comma-separated BCM pin list, as accepted by `--pins`."""
    try:
        pins = tuple(int(part) for part in value.split(","))
    except ValueError:
        raise ValueError(f"pins must be comma-separated integers, got '{value}'") from None
    if len(pins) != RELAY_COUNT:
        raise ValueError(f"expected {RELAY_COUNT} comma-separated pins, got {len(pins)}")
    return pins
