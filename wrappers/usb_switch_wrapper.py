import logging

import yaml

from .wrapper import Wrapper


LOGGER = logging.getLogger(__name__)


class UsbSwitchWrapper(Wrapper):
    def __init__(self, command_node: yaml.MappingNode):
        self.command_node = command_node
        self.name: str | None = None
        self.state: bool | None = None

    def parse(self) -> None:
        tag_name = self.command_node.tag.lstrip("!").rstrip(":")
        if tag_name != "UsbSwitch":
            raise ValueError("Expected !UsbSwitch command")

        for key_node, value_node in self.command_node.value:
            if not isinstance(key_node, yaml.ScalarNode) or not isinstance(value_node, yaml.ScalarNode):
                continue

            key = key_node.value
            if key == "name":
                self.name = value_node.value
            elif key == "state":
                self.state = value_node.value.lower() == "true"

        if self.state is None:
            raise ValueError("UsbSwitch: 'state' field is required")

        LOGGER.info("Parsed UsbSwitch values: name=%s, state=%s", self.name, self.state)

    def execute(self) -> None:
        LOGGER.warning(
            "UsbSwitch is a no-op stub: requested state=%s was not applied to any hardware. "
            "Implement UsbSwitchWrapper.execute() before relying on this in a real scenario.",
            self.state,
        )
