import logging

import yaml

from .wrapper import Wrapper


LOGGER = logging.getLogger(__name__)


class UsbSwitchWarpper(Wrapper):
    def __init__(self, command_node: yaml.MappingNode):
        self.command_node = command_node
        self.name: str | None = None
        self.state: bool | None = None

    def parse(self) -> None:
        tag_name = self.command_node.tag.lstrip("!").rstrip(":")
        if tag_name != "UsbSwitch":
            raise ValueError("Expected !UsbSwitch command")

        values: dict[str, str | bool] = {}
        for key_node, value_node in self.command_node.value:
            if not isinstance(key_node, yaml.ScalarNode) or not isinstance(value_node, yaml.ScalarNode):
                continue

            key = key_node.value
            if key == "name":
                values[key] = value_node.value
            elif key == "state":
                values[key] = value_node.value.lower() == "true"

        self.name = str(values.get("name")) if values.get("name") is not None else None
        self.state = bool(values.get("state")) if values.get("state") is not None else None

        LOGGER.info("Parsed UsbSwitch values: name=%s, state=%s", self.name, self.state)

    def execute(self) -> None:
        LOGGER.info("UsbSwitch wrapper execute called")
