import logging

import esptool
import yaml

from .wrapper import Wrapper


LOGGER = logging.getLogger(__name__)

DEFAULT_BAUDRATE = 460800
DEFAULT_FLASH_ADDRESS = "0x0"


class ProgramEsptoolWarpper(Wrapper):
    def __init__(self, command_node: yaml.MappingNode):
        self.command_node = command_node
        self.name: str | None = None
        self.port: str | None = None
        self.baudrate: int = DEFAULT_BAUDRATE
        self.firmware: str | None = None
        self.flash_address: str = DEFAULT_FLASH_ADDRESS

    def parse(self) -> None:
        tag_name = self.command_node.tag.lstrip("!").rstrip(":")
        if tag_name not in {"ProgramEsptool", "ProgrammEsptool"}:
            raise ValueError("Expected !ProgramEsptool command")

        for key_node, value_node in self.command_node.value:
            if not isinstance(key_node, yaml.ScalarNode) or not isinstance(value_node, yaml.ScalarNode):
                continue

            key = key_node.value
            if key == "name":
                self.name = value_node.value
            elif key == "port":
                self.port = value_node.value
            elif key == "baudrate":
                self.baudrate = int(value_node.value)
            elif key == "firmware":
                self.firmware = value_node.value
            elif key == "flash_address":
                self.flash_address = value_node.value

        LOGGER.info(
            "Parsed ProgramEsptool values: name=%s, port=%s, baudrate=%s, firmware=%s, flash_address=%s",
            self.name,
            self.port,
            self.baudrate,
            self.firmware,
            self.flash_address,
        )

    def execute(self) -> None:
        if self.port is None:
            raise ValueError("ProgramEsptool: no serial port specified (set in YAML or pass --port on the command line)")
        if self.firmware is None:
            raise ValueError("ProgramEsptool: no firmware path specified")

        flash_address = int(self.flash_address, 0)

        LOGGER.info("Connecting to ESP32 on %s at %d baud", self.port, self.baudrate)
        esp = esptool.detect_chip(port=self.port, baud=self.baudrate)

        try:
            esp = esptool.run_stub(esp)
            LOGGER.info("Flashing %s at address %s", self.firmware, self.flash_address)
            esptool.write_flash(esp, [(flash_address, self.firmware)])
            LOGGER.info("Flash complete, resetting device")
            esp.hard_reset()
        finally:
            esp._port.close()
