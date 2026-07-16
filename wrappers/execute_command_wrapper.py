import logging
import subprocess

import yaml

from .wrapper import Wrapper


LOGGER = logging.getLogger(__name__)


class ExecuteCommandWrapper(Wrapper):
    def __init__(self, command_node: yaml.MappingNode):
        self.command_node = command_node
        self.name: str | None = None
        self.command: str | None = None

    def parse(self) -> None:
        tag_name = self.command_node.tag.lstrip("!").rstrip(":")
        if tag_name != "ExecuteCommand":
            raise ValueError("Expected !ExecuteCommand tag")

        for key_node, value_node in self.command_node.value:
            if not isinstance(key_node, yaml.ScalarNode) or not isinstance(value_node, yaml.ScalarNode):
                continue

            key = key_node.value
            if key == "name":
                self.name = value_node.value
            elif key == "command":
                self.command = value_node.value

        if not self.command:
            raise ValueError("ExecuteCommand: 'command' field is required")

        LOGGER.info("Parsed ExecuteCommand: name=%s, command=%s", self.name, self.command)

    def execute(self) -> None:
        LOGGER.info("Executing command: %s", self.command)
        subprocess.run(self.command, shell=True, check=True)
