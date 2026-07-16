from pathlib import Path
import logging

import yaml

from wrappers import (
    ExecuteCommandWrapper,
    GpioControlWrapper,
    ProgramEsptoolWarpper,
    ProgramJlinkWarpper,
    SubghzSimWrapper,
    UsbSwitchWarpper,
    Wrapper,
)


LOGGER = logging.getLogger(__name__)

WRAPPER_BY_TAG = {
    "ProgramEsptool": ProgramEsptoolWarpper,
    "ProgramJlink": ProgramJlinkWarpper,
    "ExecuteCommand": ExecuteCommandWrapper,
    "GpioControl": GpioControlWrapper,
    "UsbSwitch": UsbSwitchWarpper,
    "SubghzSim": SubghzSimWrapper,
}


class Parser:
    def __init__(self, file_path: str):
        self.file_path = Path(file_path)

    def validate(self) -> bool:
        LOGGER.info("Validating YAML file: %s", self.file_path)
        try:
            yaml.compose(self.file_path.read_text(encoding="utf-8"))
            LOGGER.info("YAML file is valid")
            return True
        except yaml.YAMLError:
            LOGGER.exception("YAML validation failed")
            return False

    def parse(self) -> list[Wrapper]:
        document = yaml.compose(self.file_path.read_text(encoding="utf-8"))
        if document is None:
            return []

        def parse_wrapper_for_command(command_node: yaml.MappingNode) -> Wrapper | None:
            command_tag = command_node.tag.lstrip("!").rstrip(":")
            wrapper_class = WRAPPER_BY_TAG.get(command_tag)
            if wrapper_class is None:
                return None

            LOGGER.info("Parsing wrapper for tag: %s", command_tag)
            wrapper = wrapper_class(command_node)
            wrapper.parse()
            return wrapper

        def mapping_get(mapping_node: yaml.MappingNode, field_name: str) -> yaml.Node | None:
            for key_node, value_node in mapping_node.value:
                if (
                    isinstance(key_node, yaml.ScalarNode)
                    and key_node.value == field_name
                ):
                    return value_node
            return None

        def parse_iterations(loop_body: yaml.MappingNode) -> int:
            iterations_node = mapping_get(loop_body, "iterations")
            if iterations_node is None or not isinstance(iterations_node, yaml.ScalarNode):
                return 0
            try:
                return int(iterations_node.value)
            except (TypeError, ValueError):
                return 0

        def expand_commands(commands_node: yaml.SequenceNode) -> list[yaml.Node]:
            expanded: list[yaml.Node] = []

            for command_node in commands_node.value:
                if not isinstance(command_node, yaml.MappingNode):
                    expanded.append(command_node)
                    continue

                command_tag = command_node.tag.lstrip("!").rstrip(":")
                if command_tag != "Loop":
                    expanded.append(command_node)
                    continue

                loop_body = command_node
                iterations = parse_iterations(loop_body)
                nested_commands_node = mapping_get(loop_body, "commands")

                if not isinstance(nested_commands_node, yaml.SequenceNode) or iterations <= 0:
                    LOGGER.info("Skipping empty/invalid loop block")
                    continue

                LOGGER.info("Expanding !Loop with iterations=%s", iterations)
                nested_expanded = expand_commands(nested_commands_node)
                for _ in range(iterations):
                    expanded.extend(nested_expanded)

            return expanded

        wrappers: list[Wrapper] = []

        if isinstance(document, yaml.MappingNode):
            commands_node = mapping_get(document, "commands")
            if isinstance(commands_node, yaml.SequenceNode):
                expanded_commands = expand_commands(commands_node)
                for expanded_command in expanded_commands:
                    if not isinstance(expanded_command, yaml.MappingNode):
                        continue
                    wrapper = parse_wrapper_for_command(expanded_command)
                    if wrapper is not None:
                        wrappers.append(wrapper)

        return wrappers
