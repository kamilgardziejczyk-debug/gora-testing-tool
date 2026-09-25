from pathlib import Path
from typing import NamedTuple
import logging

import yaml

from tools.dut_cli import DEFAULT_BAUD as DEFAULT_CLI_BAUD
from tools.dut_logger import DEFAULT_BAUD as DEFAULT_DUT_BAUD
from wrappers import (
    BleCentralWrapper,
    BleHrvSimSetWrapper,
    BleHrvSimStartWrapper,
    BleHrvSimStopWrapper,
    DutCliWrapper,
    DutLogControlWrapper,
    DutLogExpectWrapper,
    DutLogSendWrapper,
    DutStorageWrapper,
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
    "BleHrvSimStart": BleHrvSimStartWrapper,
    "BleHrvSimSet": BleHrvSimSetWrapper,
    "BleHrvSimStop": BleHrvSimStopWrapper,
    "MqttSubscribe": MqttSubscribeWrapper,
    "MqttExpect": MqttExpectWrapper,
    "MqttDisconnect": MqttDisconnectWrapper,
    "DutLogExpect": DutLogExpectWrapper,
    "DutLogControl": DutLogControlWrapper,
    "DutLogSend": DutLogSendWrapper,
    "DutCli": DutCliWrapper,
    "DutStorage": DutStorageWrapper,
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


class UsbHubConfig(NamedTuple):
    """A scenario's top-level `usb_hub:` settings.

    `location` pins which MEGA4 to drive, for a bench with more than one; left
    None, the hub is discovered. `ports` maps scenario-level names onto port
    numbers, so a `!UsbSwitch` command can say what it is switching ("dut_power")
    rather than where it happens to be plugged in this month.
    """

    location: str | None
    ports: dict[str, int]


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


def _parse_name(document: yaml.Node) -> str | None:
    """Extract the optional top-level `name:` scalar, or None if absent.

    This is the scenario's display name - what the report calls the run
    instead of the file it was loaded from. Optional, so an unnamed scenario
    keeps falling back to its filename; a present but unusable value (a
    mapping, or an empty string) is warned about and ignored rather than
    raised, since a bad label is not a reason to refuse to run the bench.
    """
    if not isinstance(document, yaml.MappingNode):
        return None

    name_node = _mapping_get(document, "name")
    if name_node is None:
        return None
    if not isinstance(name_node, yaml.ScalarNode) or not name_node.value.strip():
        LOGGER.warning("Ignoring invalid top-level 'name': expected a non-empty string")
        return None
    return name_node.value.strip()


def _parse_dut_log(document: yaml.Node) -> DutLogConfig | None:
    """Extract the optional top-level `dut_log` mapping, or None if absent."""
    parsed = _parse_serial_block(document, "dut_log", DEFAULT_DUT_BAUD)
    return None if parsed is None else DutLogConfig(*parsed)


def _parse_dut_cli(document: yaml.Node) -> DutCliConfig | None:
    """Extract the optional top-level `dut_cli` mapping, or None if absent."""
    parsed = _parse_serial_block(document, "dut_cli", DEFAULT_CLI_BAUD)
    return None if parsed is None else DutCliConfig(*parsed)


def _parse_usb_hub(document: yaml.Node) -> UsbHubConfig | None:
    """Extract the optional top-level `usb_hub` mapping, or None if absent.

    Every field is optional - a block naming only `ports` still gets its hub
    discovered - so an empty block is accepted rather than treated as an error.
    """
    if not isinstance(document, yaml.MappingNode):
        return None

    block_node = _mapping_get(document, "usb_hub")
    if not isinstance(block_node, yaml.MappingNode):
        return None

    location: str | None = None
    ports: dict[str, int] = {}
    for key_node, value_node in block_node.value:
        if not isinstance(key_node, yaml.ScalarNode):
            continue
        if key_node.value == "location" and isinstance(value_node, yaml.ScalarNode):
            location = value_node.value
        elif key_node.value == "ports" and isinstance(value_node, yaml.MappingNode):
            ports = _parse_port_names(value_node)

    return UsbHubConfig(location, ports)


def _parse_port_names(ports_node: yaml.MappingNode) -> dict[str, int]:
    """Parse a `usb_hub.ports` mapping of name -> port number."""
    ports: dict[str, int] = {}
    for key_node, value_node in ports_node.value:
        if not isinstance(key_node, yaml.ScalarNode) or not isinstance(value_node, yaml.ScalarNode):
            continue
        try:
            ports[key_node.value] = int(value_node.value)
        except (TypeError, ValueError):
            raise ValueError(
                f"usb_hub.ports: '{key_node.value}' must be a port number, "
                f"got '{value_node.value}'"
            ) from None
    return ports


def _parse_iterations(loop_body: yaml.MappingNode) -> int:
    """Extract a !Loop command's iterations count, or 0 if missing/invalid."""
    iterations_node = _mapping_get(loop_body, "iterations")
    if iterations_node is None or not isinstance(iterations_node, yaml.ScalarNode):
        return 0
    try:
        return int(iterations_node.value)
    except (TypeError, ValueError):
        return 0


def _parse_group_name(group_node: yaml.MappingNode) -> str:
    """Extract a !Group's required name.

    Unlike !Loop's `iterations`, a missing value here is raised rather than
    skipped: a group exists only to label the commands inside it, so one
    without a name is a scenario-authoring mistake with no sensible default.
    """
    name_node = _mapping_get(group_node, "name")
    if not isinstance(name_node, yaml.ScalarNode) or not name_node.value.strip():
        raise ValueError("!Group: 'name' is required and must be a non-empty string")
    return name_node.value.strip()


class ExpandedCommand(NamedTuple):
    """One scenario command, with the !Group (if any) it came from.

    `group` is the group's display name and `group_id` its position in the
    scenario's `groups:` list, so two groups sharing a name still report as
    two sections rather than merging into one.

    Both are None for a scenario that uses a top-level `commands:` list, where
    nothing is grouped at all.
    """

    node: yaml.Node
    group: str | None
    group_id: int | None


def _expand_scenario(document: yaml.Node) -> list[ExpandedCommand]:
    """Expand a scenario's top-level `commands:` or `groups:` into commands.

    The two are alternatives, not siblings: a scenario is either wholly
    ungrouped (a flat `commands:` list) or wholly grouped (a `groups:` list of
    !Group blocks, each with its own `commands:`). Declaring both is rejected
    rather than silently running one and ignoring the other, since which would
    win - and in what order - is not something the file says.
    """
    if not isinstance(document, yaml.MappingNode):
        return []

    commands_node = _mapping_get(document, "commands")
    groups_node = _mapping_get(document, "groups")

    if commands_node is not None and groups_node is not None:
        raise ValueError(
            "A scenario declares either a top-level 'commands:' list or a top-level "
            "'groups:' list, not both. Move the loose commands into a !Group."
        )

    if groups_node is not None:
        if not isinstance(groups_node, yaml.SequenceNode):
            raise ValueError("groups: must be a list of !Group blocks")
        return _expand_groups(groups_node)

    if isinstance(commands_node, yaml.SequenceNode):
        return _expand_commands(commands_node, None, None)

    return []


def _expand_groups(groups_node: yaml.SequenceNode) -> list[ExpandedCommand]:
    """Expand each top-level !Group into its commands, stamped with its name.

    Groups do not nest and cannot contain each other: a group's `commands:`
    holds commands (and !Loop blocks), so the id is simply the group's
    position in the list rather than anything the recursion has to carry.
    """
    expanded: list[ExpandedCommand] = []

    for group_id, group_node in enumerate(groups_node.value, start=1):
        if not isinstance(group_node, yaml.MappingNode):
            continue

        group_tag = group_node.tag.lstrip("!").rstrip(":")
        if group_tag != "Group":
            found = (
                "untagged - check for a missing leading '!'"
                if group_node.tag == "tag:yaml.org,2002:map"
                else f"tagged {group_tag!r}"
            )
            raise ValueError(
                f"groups: takes !Group blocks, but entry {group_id} is {found}. "
                f"Commands belong inside a !Group's own 'commands:' list."
            )

        name = _parse_group_name(group_node)
        commands_node = _mapping_get(group_node, "commands")

        if not isinstance(commands_node, yaml.SequenceNode) or not commands_node.value:
            LOGGER.warning("Skipping !Group %r: it has no commands", name)
            continue

        LOGGER.info("Expanding !Group %r", name)
        expanded.extend(_expand_commands(commands_node, name, group_id))

    return expanded


def _expand_commands(
    commands_node: yaml.SequenceNode,
    group: str | None,
    group_id: int | None,
) -> list[ExpandedCommand]:
    """Flatten a commands list, expanding !Loop blocks, under one group.

    A !Group here is rejected. Commands nest inside groups, never the other
    way round - and a group the parser silently walked past would drop every
    command under it from the run.
    """
    expanded: list[ExpandedCommand] = []

    for command_node in commands_node.value:
        if not isinstance(command_node, yaml.MappingNode):
            expanded.append(ExpandedCommand(command_node, group, group_id))
            continue

        command_tag = command_node.tag.lstrip("!").rstrip(":")
        if command_tag == "Group":
            raise ValueError(
                "!Group belongs at the top level, in the scenario's 'groups:' list - "
                "it cannot appear inside a 'commands:' list. Commands nest inside "
                "groups, not the other way round."
            )
        if command_tag == "Loop":
            expanded.extend(_expand_loop(command_node, group, group_id))
        else:
            expanded.append(ExpandedCommand(command_node, group, group_id))

    return expanded


def _expand_loop(
    loop_node: yaml.MappingNode,
    group: str | None,
    group_id: int | None,
) -> list[ExpandedCommand]:
    """Repeat a !Loop's nested commands, once per iteration.

    The whole loop sits inside one group, so every iteration carries that
    group - a loop cannot split its commands across sections.
    """
    iterations = _parse_iterations(loop_node)
    nested_commands_node = _mapping_get(loop_node, "commands")

    if not isinstance(nested_commands_node, yaml.SequenceNode) or iterations <= 0:
        LOGGER.info("Skipping empty/invalid loop block")
        return []

    LOGGER.info("Expanding !Loop with iterations=%s", iterations)
    nested_expanded = _expand_commands(nested_commands_node, group, group_id)
    return nested_expanded * iterations


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

    def parse_name(self) -> str | None:
        """The scenario's `name:`, or None if it declares none.

        Separate from `parse()` for the same reason as `parse_dut_log()`: the
        name labels the whole run, not any one command.
        """
        _, document = self._load()
        return _parse_name(document)

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

    def parse_usb_hub(self) -> UsbHubConfig | None:
        """The scenario's `usb_hub:` settings, or None if it declares none.

        Separate from `parse()` for the same reason as `parse_dut_log()`: which
        hub the bench has, and what its ports are called, is a property of the
        run rather than of any one command.
        """
        _, document = self._load()
        return _parse_usb_hub(document)

    def parse(self) -> list[Wrapper]:
        document_text, document = self._load()
        if document is None:
            return []

        wrappers: list[Wrapper] = []

        for expanded_command in _expand_scenario(document):
            if not isinstance(expanded_command.node, yaml.MappingNode):
                continue
            wrapper = self._parse_wrapper_for_command(expanded_command, document_text)
            if wrapper is not None:
                wrappers.append(wrapper)

        return wrappers

    def _parse_wrapper_for_command(self, expanded_command: ExpandedCommand, document_text: str) -> Wrapper | None:
        command_node = expanded_command.node
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
        wrapper.group = expanded_command.group
        wrapper.group_id = expanded_command.group_id
        wrapper.raw_yaml = document_text[command_node.start_mark.index:command_node.end_mark.index].strip()
        wrapper.parse()
        wrapper.wait_after_s = _parse_wait_after_s(command_node)
        return wrapper
