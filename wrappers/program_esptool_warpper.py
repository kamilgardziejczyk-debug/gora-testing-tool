import logging

import yaml

from .wrapper import Wrapper


LOGGER = logging.getLogger(__name__)


class ProgramEsptoolWarpper(Wrapper):
    def __init__(self, command_node: yaml.MappingNode):
        self.command_node = command_node
        self.name: str | None = None
        self.port: str | None = None
        self.baudrate: int | None = None

    def parse(self) -> None:
        tag_name = self.command_node.tag.lstrip("!").rstrip(":")
        if tag_name not in {"ProgramEsptool"}:
            raise ValueError("Expected !ProgramEsptool command")

        values: dict[str, str | int | float | bool] = {}
        for key_node, value_node in self.command_node.value:
            if not isinstance(key_node, yaml.ScalarNode) or not isinstance(value_node, yaml.ScalarNode):
                continue

            key = key_node.value
            if key == "name":
                self.name =  value_node.value
            elif key == "port":
                self.port = value_node.value
            elif key == "baudrate":
                self.baudrate = int(value_node.value)
            else:
                continue
        
        LOGGER.info(
            "Parsed ProgramEsptool values: name=%s, port=%s, baudrate=%s",
            self.name,
            self.port,
            self.baudrate,
        )

    def execute(self) -> None:
        LOGGER.info("ProgramEsptool wrapper execute called")
