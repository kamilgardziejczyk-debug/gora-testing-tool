import logging

try:
    import RPi.GPIO as GPIO
except (ImportError, RuntimeError):
    # ImportError: RPi.GPIO not installed. RuntimeError: installed, but this is
    # not a Raspberry Pi - the module raises on import off-device. Either way we
    # fall back to logging the pin change instead of performing it.
    GPIO = None

import yaml

from .wrapper import Wrapper


LOGGER = logging.getLogger(__name__)

# Module-level state tracking which BCM pins have already been configured with
# GPIO.setup(), so repeated commands on the same pin within a scenario don't
# re-trigger "channel already in use" warnings. Wrappers are re-instantiated
# per command by the Parser, which has no way to share state between them, so
# this mirrors the same rendezvous-through-module pattern used by
# mqtt_registry for the same reason.
_configured_pins: set[int] = set()


class GpioControlWrapper(Wrapper):
    def __init__(self, command_node: yaml.MappingNode):
        self.command_node = command_node
        self.name: str | None = None
        self.pin: int | None = None
        self.state: bool | None = None

    def parse(self) -> None:
        tag_name = self.command_node.tag.lstrip("!").rstrip(":")
        if tag_name != "GpioControl":
            raise ValueError("Expected !GpioControl tag")

        for key_node, value_node in self.command_node.value:
            if not isinstance(key_node, yaml.ScalarNode) or not isinstance(value_node, yaml.ScalarNode):
                continue

            key = key_node.value
            if key == "name":
                self.name = value_node.value
            elif key == "pin":
                self.pin = int(value_node.value)
            elif key == "state":
                self.state = value_node.value.lower() == "true"

        if self.pin is None:
            raise ValueError("GpioControl: 'pin' field is required")
        if self.state is None:
            raise ValueError("GpioControl: 'state' field is required")

        LOGGER.info("Parsed GpioControl: name=%s, pin=%s, state=%s", self.name, self.pin, self.state)

    def execute(self) -> None:
        if GPIO is None:
            LOGGER.warning(
                "RPi.GPIO is not available on this platform. Simulating: GPIO pin %s set to %s",
                self.pin,
                "HIGH" if self.state else "LOW",
            )
            return

        GPIO.setmode(GPIO.BCM)
        if self.pin not in _configured_pins:
            GPIO.setup(self.pin, GPIO.OUT)
            _configured_pins.add(self.pin)

        level = GPIO.HIGH if self.state else GPIO.LOW
        GPIO.output(self.pin, level)
        LOGGER.info("GPIO pin %s set to %s", self.pin, "HIGH" if self.state else "LOW")


def cleanup_all() -> None:
    """Release every pin configured by a !GpioControl command this scenario.

    Called from the scenario runner's `finally`, so a command that raises
    part-way through still leaves the pins released cleanly. Safe to call
    when none are configured, or when RPi.GPIO isn't available.
    """
    if GPIO is None or not _configured_pins:
        return
    LOGGER.info("Releasing GPIO pins: %s", sorted(_configured_pins))
    GPIO.cleanup()
    _configured_pins.clear()
