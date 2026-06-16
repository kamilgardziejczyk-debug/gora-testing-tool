import logging

import yaml

from .wrapper import Wrapper


LOGGER = logging.getLogger(__name__)


class SDCardMountWarpper(Wrapper):
    def __init__(self, command_node: yaml.MappingNode):
        self.command_node = command_node
        self.name: str | None = None
        self.sd_card_path: str | None = None
        self.wait_after_s: int | None = None

    def parse(self) -> None:
        tag_name = self.command_node.tag.lstrip("!").rstrip(":")
        if tag_name != "SDCardMount":
            raise ValueError("Expected !SDCardMount command")

        values: dict[str, str | int] = {}
        for key_node, value_node in self.command_node.value:
            if not isinstance(key_node, yaml.ScalarNode) or not isinstance(value_node, yaml.ScalarNode):
                continue

            key = key_node.value
            if key in {"name", "sd_card_path"}:
                values[key] = value_node.value
            elif key == "wait_after_s":
                values[key] = int(value_node.value)

        self.name = str(values.get("name")) if values.get("name") is not None else None
        self.sd_card_path = str(values.get("sd_card_path")) if values.get("sd_card_path") is not None else None
        self.wait_after_s = int(values.get("wait_after_s")) if values.get("wait_after_s") is not None else None

        LOGGER.info(
            "Parsed SDCardMount values: name=%s, sd_card_path=%s, wait_after_s=%s",
            self.name,
            self.sd_card_path,
            self.wait_after_s,
        )

    def execute(self) -> None:
        LOGGER.info("SDCardMount wrapper execute called")
