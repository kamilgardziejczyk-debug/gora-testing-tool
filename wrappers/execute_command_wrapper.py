import logging
import subprocess
import time

import yaml

from .wrapper import Wrapper


LOGGER = logging.getLogger(__name__)


class ExecuteCommandWrapper(Wrapper):
    def __init__(self, command_node: yaml.MappingNode):
        self.command_node = command_node
        self.name: str | None = None
        self.command: str | None = None
        self.wait_after_s: int | None = None

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
            elif key == "wait_after_s":
                self.wait_after_s = int(value_node.value)

        if not self.command:
            raise ValueError("ExecuteCommand: 'command' field is required")

        LOGGER.info(
            "Parsed ExecuteCommand: name=%s, command=%s, wait_after_s=%s",
            self.name,
            self.command,
            self.wait_after_s,
        )

    def execute(self) -> None:
        LOGGER.info("Executing command: %s", self.command)
        subprocess.run(self.command, shell=True, check=True)

        if self.wait_after_s is not None:
            LOGGER.info("Waiting %s second(s) after command", self.wait_after_s)
            time.sleep(self.wait_after_s)
