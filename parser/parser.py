from pathlib import Path
from typing import NamedTuple
import logging

import yaml

from tools.dut_cli import DEFAULT_BAUD as DEFAULT_CLI_BAUD
from tools.dut_logger import DEFAULT_BAUD as DEFAULT_DUT_BAUD
from wrappers import (
    BleCentralWrapper,
    DutCliWrapper,
    DutLogControlWrapper,
    DutLogExpectWrapper,
    ExecuteCommandWrapper,
    MqttDisconnectWrapper,
    MqttExpectWrapper,
    MqttSubscribeWrapper,
    ProgramEsptoolWrapper,
    ProgramJlinkWrapper,
    RelayControlWrapper,
    SubghzSimWrapper,
    UsbSwitchWrapper,
    Wrapper,
)


LOGGER = logging.getLogger(__name__)

WRAPPER_BY_TAG = {
    "ProgramEsptool": ProgramEsptoolWrapper,
    "ProgramJlink": ProgramJlinkWrapper,
    "ExecuteCommand": ExecuteCommandWrapper,
    "RelayControl": RelayControlWrapper,
    "UsbSwitch": UsbSwitchWrapper,
    "SubghzSim": SubghzSimWrapper,
    "BleCentral": BleCentralWrapper,
    "MqttSubscribe": MqttSubscribeWrapper,
    "MqttExpect": MqttExpectWrapper,
    "MqttDisconnect": MqttDisconnectWrapper,
    "DutLogExpect": DutLogExpectWrapper,
    "DutLogControl": DutLogControlWrapper,
    "DutCli": DutCliWrapper,
}


def _mapping_get(mapping_node: yaml.MappingNode, field_name: str) -> yaml.Node | None:
    """Look up a mapping key by name, returning its value node or None."""
    for key_node, value_node in mapping_node.value:
        if isinstance(key_node, yaml.ScalarNode) and key_node.value == field_name:
            return value_node
    return None


def _parse_wait_after_s(command_node: yaml.MappingNode) -> float | None:
    """Extract a command's optional wait_after_s field."""
    wait_node = _mapping_get(command_node, "wait_after_s")
    if wait_node is None or not isinstance(wait_node, yaml.ScalarNode):
        return None
    try:
        return float(wait_node.value)
    except (TypeError, ValueError):
        LOGGER.warning("Ignoring invalid wait_after_s value: %s", wait_node.value)
        return None


class DutLogConfig(NamedTuple):
    """A scenario's top-level `dut_log:` settings."""

    port: str
    baud: int


class DutCliConfig(NamedTuple):
    """A scenario's top-level `dut_cli:` settings.

    Separate from `DutLogConfig` because they name two different UARTs on the
    same DUT - the console it talks *out* of, and the shell it listens *on* -
    and a bench that has one need not have the other.
    """

    port: str
    baud: int


def _parse_serial_block(
    document: yaml.Node,
    block_name: str,
    default_baud: int,
) -> tuple[str, int] | None:
    """Extract an optional top-level `<block_name>: {port, baud}` mapping.

    Shared by `dut_log` and `dut_cli` so the two are configured identically:
    the only thing that differs between them is which UART they name.
    """
    if not isinstance(document, yaml.MappingNode):
        return None

    block_node = _mapping_get(document, block_name)
    if not isinstance(block_node, yaml.MappingNode):
        return None

    port: str | None = None
    baud = default_baud
    for key_node, value_node in block_node.value:
        if not isinstance(key_node, yaml.ScalarNode) or not isinstance(value_node, yaml.ScalarNode):
            continue
        if key_node.value == "port":
            port = value_node.value
        elif key_node.value == "baud":
            baud = int(value_node.value)

    if port is None:
        raise ValueError(f"{block_name}: 'port' is required when a {block_name} block is present")
    return port, baud


def _parse_dut_log(document: yaml.Node) -> DutLogConfig | None:
    """Extract the optional top-level `dut_log` mapping, or None if absent."""
    parsed = _parse_serial_block(document, "dut_log", DEFAULT_DUT_BAUD)
    return None if parsed is None else DutLogConfig(*parsed)


def _parse_dut_cli(document: yaml.Node) -> DutCliConfig | None:
    """Extract the optional top-level `dut_cli` mapping, or None if absent."""
    parsed = _parse_serial_block(document, "dut_cli", DEFAULT_CLI_BAUD)
    return None if parsed is None else DutCliConfig(*parsed)


def _parse_iterations(loop_body: yaml.MappingNode) -> int:
    """Extract a !Loop command's iterations count, or 0 if missing/invalid."""
    iterations_node = _mapping_get(loop_body, "iterations")
    if iterations_node is None or not isinstance(iterations_node, yaml.ScalarNode):
        return 0
    try:
        return int(iterations_node.value)
    except (TypeError, ValueError):
        return 0


def _expand_commands(commands_node: yaml.SequenceNode) -> list[yaml.Node]:
    """Flatten !Loop blocks into their repeated nested commands."""
    expanded: list[yaml.Node] = []

    for command_node in commands_node.value:
        if not isinstance(command_node, yaml.MappingNode):
            expanded.append(command_node)
            continue

        command_tag = command_node.tag.lstrip("!").rstrip(":")
        if command_tag != "Loop":
            expanded.append(command_node)
            continue

        iterations = _parse_iterations(command_node)
        nested_commands_node = _mapping_get(command_node, "commands")

        if not isinstance(nested_commands_node, yaml.SequenceNode) or iterations <= 0:
            LOGGER.info("Skipping empty/invalid loop block")
            continue

        LOGGER.info("Expanding !Loop with iterations=%s", iterations)
        nested_expanded = _expand_commands(nested_commands_node)
        for _ in range(iterations):
            expanded.extend(nested_expanded)

    return expanded


class Parser:
    def __init__(self, file_path: str):
        self.file_path = Path(file_path)
        self._document_text: str | None = None
        self._document: yaml.Node | None = None
        self._loaded = False

    def _load(self) -> tuple[str, yaml.Node | None]:
        """Read and compose the scenario file once, caching the result.

        Shared by `validate()` and `parse()` so a scenario file is never read
        from disk (or composed) twice in one run. Only cached on success: if
        `yaml.compose()` raises, the next call retries from scratch instead of
        silently replaying the failure as an empty document.
        """
        if not self._loaded:
            document_text = self.file_path.read_text(encoding="utf-8")
            document = yaml.compose(document_text)
            self._document_text, self._document, self._loaded = document_text, document, True
        return self._document_text, self._document

    def validate(self) -> bool:
        LOGGER.info("Validating YAML file: %s", self.file_path)
        try:
            self._load()
            LOGGER.info("YAML file is valid")
            return True
        except yaml.YAMLError:
            LOGGER.exception("YAML validation failed")
            return False

    def parse_dut_log(self) -> DutLogConfig | None:
        """The scenario's `dut_log:` settings, or None if it declares none.

        Separate from `parse()` because capture has to be running before the
        first command executes, while `parse()` returns the commands
        themselves. Both share `_load()`, so the file is still read once.
        """
        _, document = self._load()
        return _parse_dut_log(document)

    def parse_dut_cli(self) -> DutCliConfig | None:
        """The scenario's `dut_cli:` settings, or None if it declares none.

        Separate from `parse()` for the same reason as `parse_dut_log()`: the
        shell port is a property of the run, not of any one command, so the
        runner resolves it (and the CLI overrides) before the first command.
        """
        _, document = self._load()
        return _parse_dut_cli(document)

    def parse(self) -> list[Wrapper]:
        document_text, document = self._load()
        if document is None:
            return []

        wrappers: list[Wrapper] = []

        if isinstance(document, yaml.MappingNode):
            commands_node = _mapping_get(document, "commands")
            if isinstance(commands_node, yaml.SequenceNode):
                expanded_commands = _expand_commands(commands_node)
                for expanded_command in expanded_commands:
                    if not isinstance(expanded_command, yaml.MappingNode):
                        continue
                    wrapper = self._parse_wrapper_for_command(expanded_command, document_text)
                    if wrapper is not None:
                        wrappers.append(wrapper)

        return wrappers

    def _parse_wrapper_for_command(self, command_node: yaml.MappingNode, document_text: str) -> Wrapper | None:
        command_tag = command_node.tag.lstrip("!").rstrip(":")
        wrapper_class = WRAPPER_BY_TAG.get(command_tag)
        if wrapper_class is None:
            tag_description = (
                "no tag - check for a missing leading '!'"
                if command_node.tag == "tag:yaml.org,2002:map"
                else f"unrecognised tag {command_tag!r} - check for a typo"
            )
            LOGGER.warning(
                "Skipping command with %s: %s",
                tag_description,
                document_text[command_node.start_mark.index:command_node.end_mark.index].strip(),
            )
            return None

        LOGGER.info("Parsing wrapper for tag: %s", command_tag)
        wrapper = wrapper_class(command_node)
        wrapper.scenario_dir = self.file_path.parent
        wrapper.tag = command_tag
        wrapper.raw_yaml = document_text[command_node.start_mark.index:command_node.end_mark.index].strip()
        wrapper.parse()
        wrapper.wait_after_s = _parse_wait_after_s(command_node)
        return wrapper
