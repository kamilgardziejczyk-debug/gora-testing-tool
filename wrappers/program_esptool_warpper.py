import logging
from pathlib import Path

import esptool
import yaml

from .wrapper import Wrapper


LOGGER = logging.getLogger(__name__)

DEFAULT_BAUDRATE = 460800
BOOTLOADER_FLASH_ADDRESS = 0x0000
PARTITION_TABLE_FLASH_ADDRESS = 0x8000
FIRMWARE_FLASH_ADDRESS = 0x10000


class ProgramEsptoolWarpper(Wrapper):
    def __init__(self, command_node: yaml.MappingNode):
        self.command_node = command_node
        self.name: str | None = None
        self.port: str | None = None
        self.baudrate: int = DEFAULT_BAUDRATE
        self.firmware_dir: str | None = None
        self.bootloader: str | None = None
        self.partition_table: str | None = None
        self.firmware: str | None = None

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
            elif key == "bootloader":
                self.bootloader = value_node.value
            elif key == "partition_table":
                self.partition_table = value_node.value
            elif key == "firmware":
                self.firmware = value_node.value

        LOGGER.info(
            "Parsed ProgramEsptool values: name=%s, port=%s, baudrate=%s, bootloader=%s, partition_table=%s, firmware=%s",
            self.name,
            self.port,
            self.baudrate,
            self.bootloader,
            self.partition_table,
            self.firmware,
        )

    def execute(self) -> None:
        if self.port is None:
            raise ValueError("ProgramEsptool: no serial port specified (set in YAML or pass --port on the command line)")
        if self.firmware_dir is None:
            raise ValueError("ProgramEsptool: no firmware directory specified (pass --firmware on the command line)")
        if self.bootloader is None:
            raise ValueError("ProgramEsptool: no bootloader filename specified in YAML")
        if self.partition_table is None:
            raise ValueError("ProgramEsptool: no partition_table filename specified in YAML")
        if self.firmware is None:
            raise ValueError("ProgramEsptool: no firmware filename specified in YAML")

        base = Path(self.firmware_dir)
        if not base.is_dir():
            raise ValueError(f"ProgramEsptool: firmware path is not a directory: {self.firmware_dir}")

        bootloader_path = base / self.bootloader
        partition_table_path = base / self.partition_table
        firmware_path = base / self.firmware

        for path in (bootloader_path, partition_table_path, firmware_path):
            if not path.is_file():
                raise FileNotFoundError(f"ProgramEsptool: binary not found: {path}")

        flash_data = [
            (BOOTLOADER_FLASH_ADDRESS, str(bootloader_path)),
            (PARTITION_TABLE_FLASH_ADDRESS, str(partition_table_path)),
            (FIRMWARE_FLASH_ADDRESS, str(firmware_path)),
        ]

        LOGGER.info("Connecting to ESP32 on %s at %d baud", self.port, self.baudrate)
        esp = esptool.detect_chip(port=self.port, baud=self.baudrate)

        try:
            esp = esptool.run_stub(esp)
            LOGGER.info(
                "Flashing bootloader=%s, partition_table=%s, firmware=%s",
                bootloader_path,
                partition_table_path,
                firmware_path,
            )
            esptool.write_flash(esp, flash_data)
            LOGGER.info("Flash complete, resetting device")
            esp.hard_reset()
        finally:
            esp._port.close()
