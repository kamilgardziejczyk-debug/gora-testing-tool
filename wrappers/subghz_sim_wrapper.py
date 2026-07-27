import logging
import time
from typing import NamedTuple

import yaml

from tools.subghz_sim import (
    DEFAULT_BAUD,
    DEFAULT_INTERVAL_S,
    SENSOR_TYPES,
    SubghzSimulator,
    UnknownSensorId,
    parse_field_pairs,
)

from .wrapper import Wrapper


LOGGER = logging.getLogger(__name__)

ACTION_VERBS = {"add", "set", "del", "list"}


class Action(NamedTuple):
    """One simulator step, with its argument already validated at parse time.

    `arg` is kept verbatim only for logging, so the log still reads like the
    equivalent REPL session.
    """

    verb: str
    arg: str
    wait_after_ms: int | None
    sensor_id: int | None = None
    fields: tuple = ()


class SubghzSimWrapper(Wrapper):
    """
    Wrapper that drives tools/subghz_sim through its Python API: opens the
    serial link, applies a sequence of sensor actions with waits in between,
    keeps the heartbeat running for `duration_s`, then closes the link.
    """

    def __init__(self, command_node: yaml.MappingNode):
        self.command_node = command_node
        self.name: str | None = None
        self.port: str | None = None
        self.baud: int = DEFAULT_BAUD
        self.interval_s: float = DEFAULT_INTERVAL_S
        self.duration_s: float | None = None
        self.actions: list[Action] = []

    def parse(self) -> None:
        tag_name = self.command_node.tag.lstrip("!").rstrip(":")
        if tag_name != "SubghzSim":
            raise ValueError("Expected !SubghzSim command")

        for key_node, value_node in self.command_node.value:
            if not isinstance(key_node, yaml.ScalarNode):
                continue

            key = key_node.value
            if key == "actions":
                self.actions = self._parse_actions(value_node)
            elif isinstance(value_node, yaml.ScalarNode):
                if key == "name":
                    self.name = value_node.value
                elif key == "port":
                    self.port = value_node.value
                elif key == "baud":
                    self.baud = int(value_node.value)
                elif key == "interval_s":
                    self.interval_s = float(value_node.value)
                elif key == "duration_s":
                    self.duration_s = float(value_node.value)

        if self.port is None:
            raise ValueError("SubghzSim: 'port' field is required")

        LOGGER.info(
            "Parsed SubghzSim values: name=%s, port=%s, baud=%s, interval_s=%s, "
            "duration_s=%s, actions=%d",
            self.name,
            self.port,
            self.baud,
            self.interval_s,
            self.duration_s,
            len(self.actions),
        )

    def _parse_actions(self, actions_node: yaml.Node) -> list[Action]:
        """Extract one Action per entry from a sequence of action mappings."""
        if not isinstance(actions_node, yaml.SequenceNode):
            return []

        actions: list[Action] = []
        for action_node in actions_node.value:
            if not isinstance(action_node, yaml.MappingNode):
                continue

            verb: str | None = None
            arg = ""
            wait_after_ms: int | None = None
            for key_node, value_node in action_node.value:
                if not isinstance(key_node, yaml.ScalarNode) or not isinstance(value_node, yaml.ScalarNode):
                    continue
                key = key_node.value
                if key == "wait_after_ms":
                    wait_after_ms = int(value_node.value)
                elif key in ACTION_VERBS:
                    verb = key
                    arg = value_node.value

            if verb is None:
                LOGGER.warning("Skipping SubghzSim action with no recognized verb (%s)", ACTION_VERBS)
                continue

            actions.append(self._build_action(verb, arg, wait_after_ms))

        return actions

    def _build_action(self, verb: str, arg: str, wait_after_ms: int | None) -> Action:
        """Validate one action's argument, so a typo fails before any hardware is touched.

        Field *names* can only be checked against a live sensor's type, so those
        are left to `execute()`; everything decidable from the YAML alone is
        rejected here.
        """
        if verb == "add":
            type_name = arg.strip().lower()
            if type_name not in SENSOR_TYPES:
                choices = ", ".join(sorted(set(SENSOR_TYPES)))
                raise ValueError(f"SubghzSim: unknown sensor type '{arg}' (choices: {choices})")
            return Action(verb, type_name, wait_after_ms)

        if verb == "del":
            return Action(verb, arg, wait_after_ms, sensor_id=self._parse_id(verb, arg))

        if verb == "set":
            tokens = arg.split()
            if len(tokens) < 3:
                raise ValueError(
                    f"SubghzSim: 'set' needs '<sensor_id> <field> <value> ...', got '{arg}'"
                )
            try:
                fields = tuple(parse_field_pairs(tokens[1:]))
            except ValueError as error:
                raise ValueError(f"SubghzSim: invalid 'set' action '{arg}': {error}") from error
            return Action(verb, arg, wait_after_ms, self._parse_id(verb, tokens[0]), fields)

        return Action(verb, arg, wait_after_ms)

    def _parse_id(self, verb: str, token: str) -> int:
        try:
            return int(token.strip())
        except ValueError:
            raise ValueError(f"SubghzSim: '{verb}' needs a numeric sensor id, got '{token}'") from None

    def execute(self) -> None:
        LOGGER.info("Starting subghz_sim on %s at %d baud", self.port, self.baud)
        start_time = time.monotonic()

        simulator = SubghzSimulator(port=self.port, baud=self.baud, interval_s=self.interval_s)
        simulator.open()
        try:
            for action in self.actions:
                self._run_action(simulator, action)
                if action.wait_after_ms is not None:
                    time.sleep(action.wait_after_ms / 1000)
            self._hold(start_time)
        finally:
            simulator.close()

        LOGGER.info("subghz_sim session finished")

    def _run_action(self, simulator: SubghzSimulator, action: Action) -> None:
        """Run one action, reporting an unknown sensor id as a scenario error."""
        LOGGER.info("subghz_sim <- %s %s", action.verb, action.arg)

        try:
            if action.verb == "add":
                simulator.add_sensor(action.arg)
            elif action.verb == "set":
                simulator.update_sensor(action.sensor_id, action.fields)
            elif action.verb == "del":
                simulator.remove_sensor(action.sensor_id)
            else:
                self._log_sensors(simulator)
        except UnknownSensorId:
            raise ValueError(
                f"SubghzSim: no sensor #{action.sensor_id}. Ids are assigned in the order "
                f"this command's 'add' actions run, starting at 1."
            ) from None

    def _log_sensors(self, simulator: SubghzSimulator) -> None:
        sensors = simulator.list_sensors()
        if not sensors:
            LOGGER.info("subghz_sim -> (no sensors)")
        for sensor in sensors:
            LOGGER.info("subghz_sim -> %s", sensor.describe())

    def _hold(self, start_time: float) -> None:
        """Keep the heartbeat running until `duration_s` has elapsed in total."""
        if self.duration_s is None:
            return
        remaining_s = self.duration_s - (time.monotonic() - start_time)
        if remaining_s > 0:
            LOGGER.info("Keeping subghz_sim active for %.1f more second(s)", remaining_s)
            time.sleep(remaining_s)
