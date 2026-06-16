import logging

import yaml

from .wrapper import Wrapper


LOGGER = logging.getLogger(__name__)


class SdCardFindFilesWarpper(Wrapper):
    def __init__(self, command_node: yaml.MappingNode):
        self.command_node = command_node
        self.name: str | None = None
        self.sd_card_path: str | None = None
        self.file_pattern: str | None = None

    def parse(self) -> None:
        tag_name = self.command_node.tag.lstrip("!").rstrip(":")
        if tag_name != "SdCardFindFiles":
            raise ValueError("Expected !SdCardFindFiles command")

        values: dict[str, str] = {}
        for key_node, value_node in self.command_node.value:
            if not isinstance(key_node, yaml.ScalarNode) or not isinstance(value_node, yaml.ScalarNode):
                continue

            key = key_node.value
            if key in {"name", "sd_card_path", "file_pattern"}:
                values[key] = value_node.value

        self.name = values.get("name")
        self.sd_card_path = values.get("sd_card_path")
        self.file_pattern = values.get("file_pattern")

        LOGGER.info(
            "Parsed SdCardFindFiles values: name=%s, sd_card_path=%s, file_pattern=%s",
            self.name,
            self.sd_card_path,
            self.file_pattern,
        )

    def execute(self) -> None:
        LOGGER.info("SdCardFindFiles wrapper execute called")
