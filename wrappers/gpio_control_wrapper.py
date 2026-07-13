import logging
import time

import RPi.GPIO as GPIO
import yaml

from .wrapper import Wrapper


LOGGER = logging.getLogger(__name__)


class GpioControlWrapper(Wrapper):
    def __init__(self, command_node: yaml.MappingNode):
        self.command_node = command_node
        self.name: str | None = None
        self.pin: int | None = None
        self.state: bool | None = None
        self.wait_after_s: int | None = None

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
            elif key == "wait_after_s":
                self.wait_after_s = int(value_node.value)

        if self.pin is None:
            raise ValueError("GpioControl: 'pin' field is required")
        if self.state is None:
            raise ValueError("GpioControl: 'state' field is required")

        LOGGER.info(
            "Parsed GpioControl: name=%s, pin=%s, state=%s, wait_after_s=%s",
            self.name,
            self.pin,
            self.state,
            self.wait_after_s,
        )

    def execute(self) -> None:
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(self.pin, GPIO.OUT)

        level = GPIO.HIGH if self.state else GPIO.LOW
        GPIO.output(self.pin, level)
        LOGGER.info("GPIO pin %s set to %s", self.pin, "HIGH" if self.state else "LOW")

        if self.wait_after_s is not None:
            LOGGER.info("Waiting %s second(s) after GPIO change", self.wait_after_s)
            time.sleep(self.wait_after_s)
