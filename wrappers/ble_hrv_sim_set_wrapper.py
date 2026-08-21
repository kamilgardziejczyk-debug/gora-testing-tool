"""!BleHrvSimSet - drive a running heart rate sensor simulation."""

import logging
import time
from typing import NamedTuple

import yaml

from . import ble_hrv_registry
from .wrapper import Wrapper


LOGGER = logging.getLogger(__name__)

CONTACT_STATES = {"yes": True, "no": False, "none": None}

# Every verb an action may use. Split by argument shape so a typo in the
# argument is caught while the scenario loads rather than mid-run.
NUMERIC_VERBS = {"bpm", "battery", "burst", "stall"}
BARE_VERBS = {"resume", "bounce"}
ACTION_VERBS = NUMERIC_VERBS | BARE_VERBS | {"contact", "energy"}


class HrvAction(NamedTuple):
    """One simulator step, its argument already validated at parse time.

    `arg` is kept verbatim only for logging, so the log reads like the
    equivalent REPL session.
    """

    verb: str
    arg: str
    wait_after_ms: int | None = None
    number: float | None = None
    contact: bool | None = None


class BleHrvSimSetWrapper(Wrapper):
    """Applies a sequence of changes to a sensor started by !BleHrvSimStart.

    Verbs, each taking the argument its name implies:

    *   `bpm: <n>` - change the pulse
    *   `contact: <yes|no|none>` - skin contact; `none` stops reporting it
    *   `battery: <percent>` - set and notify the battery level
    *   `energy: <kJ|off>` - report energy expended in every frame, or stop
    *   `burst: <count>` - send that many RR intervals in one frame, to exceed
        what a central budgeted for
    *   `stall: <seconds>` - stay connected but send nothing; returns at once,
        so the gap runs while later commands do
    *   `resume` - end a stall early
    *   `bounce` - drop off the air and come back, forcing a reconnect

    `resume` and `bounce` take no argument, so they are written as bare list
    entries (`- resume`) rather than a mapping.

    `wait_after_ms` on any action pauses before the next one.
    """

    def __init__(self, command_node: yaml.MappingNode):
        self.command_node = command_node
        self.name: str | None = None
        self.session: str | None = None
        self.actions: list[HrvAction] = []

    def parse(self) -> None:
        """Read the action list, validating every verb and argument as it goes."""
        tag_name = self.command_node.tag.lstrip("!").rstrip(":")
        if tag_name != "BleHrvSimSet":
            raise ValueError("Expected !BleHrvSimSet command")

        for key_node, value_node in self.command_node.value:
            if not isinstance(key_node, yaml.ScalarNode):
                continue
            key = key_node.value
            if key == "actions":
                self.actions = self._parse_actions(value_node)
            elif isinstance(value_node, yaml.ScalarNode):
                if key == "name":
                    self.name = value_node.value
                elif key == "session":
                    self.session = value_node.value

        if self.session is None:
            raise ValueError("BleHrvSimSet: 'session' field is required")
        if not self.actions:
            raise ValueError("BleHrvSimSet: 'actions' must be a non-empty list")

        LOGGER.info(
            "Parsed BleHrvSimSet: name=%s, session=%s, actions=%d",
            self.name, self.session, len(self.actions),
        )

    def _parse_actions(self, actions_node: yaml.Node) -> list[HrvAction]:
        """Extract one HrvAction per entry from a sequence of action mappings."""
        if not isinstance(actions_node, yaml.SequenceNode):
            return []

        actions: list[HrvAction] = []
        for index, action_node in enumerate(actions_node.value):
            # `resume` and `bounce` take no argument, so they are written as a
            # bare scalar rather than a mapping to nothing.
            if isinstance(action_node, yaml.ScalarNode):
                actions.append(self._parse_bare_action(index, action_node.value))
                continue
            if not isinstance(action_node, yaml.MappingNode):
                raise ValueError(f"BleHrvSimSet: action #{index + 1} must be a mapping")

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
                raise ValueError(
                    f"BleHrvSimSet: action #{index + 1} must have one of: "
                    f"{', '.join(sorted(ACTION_VERBS))}"
                )
            actions.append(self._build_action(index, verb, arg, wait_after_ms))

        return actions

    def _parse_bare_action(self, index: int, token: str) -> HrvAction:
        """Read an action written as a bare verb, rejecting one that needs an argument."""
        verb = token.strip()
        if verb in BARE_VERBS:
            return HrvAction(verb, "")
        if verb in ACTION_VERBS:
            raise ValueError(
                f"BleHrvSimSet: action #{index + 1} '{verb}' needs an argument, "
                f"write it as '{verb}: <value>'"
            )
        raise ValueError(
            f"BleHrvSimSet: action #{index + 1} '{verb}' is not a known action "
            f"(one of: {', '.join(sorted(ACTION_VERBS))})"
        )

    def _build_action(self, index: int, verb: str, arg: str, wait_after_ms: int | None) -> HrvAction:
        """Validate one action's argument, so a typo fails before the run starts."""
        if verb in BARE_VERBS:
            return HrvAction(verb, arg, wait_after_ms)

        if verb == "contact":
            token = arg.strip().lower()
            if token not in CONTACT_STATES:
                raise ValueError(
                    f"BleHrvSimSet: action #{index + 1} 'contact' must be one of "
                    f"{', '.join(sorted(CONTACT_STATES))}, got '{arg}'"
                )
            return HrvAction(verb, arg, wait_after_ms, contact=CONTACT_STATES[token])

        if verb == "energy" and arg.strip().lower() == "off":
            return HrvAction(verb, arg, wait_after_ms)

        number = self._parse_number(index, verb, arg)
        if verb in {"burst", "battery"} and number < 0:
            raise ValueError(f"BleHrvSimSet: action #{index + 1} '{verb}' cannot be negative")
        return HrvAction(verb, arg, wait_after_ms, number=number)

    def _parse_number(self, index: int, verb: str, arg: str) -> float:
        """Read a numeric argument, naming the action that carried a bad one."""
        try:
            return float(arg)
        except ValueError:
            extra = " (or 'off')" if verb == "energy" else ""
            raise ValueError(
                f"BleHrvSimSet: action #{index + 1} '{verb}' needs a number{extra}, got '{arg}'"
            ) from None

    def execute(self) -> None:
        """Apply each action to the running sensor, pausing where asked."""
        simulator = ble_hrv_registry.get(self.session)
        for action in self.actions:
            LOGGER.info("BleHrvSimSet [%s]: %s %s", self.session, action.verb, action.arg)
            self._apply(simulator, action)
            if action.wait_after_ms:
                time.sleep(action.wait_after_ms / 1000.0)

        LOGGER.info("BleHrvSimSet [%s] done: %s", self.session, simulator.stats.describe())

    def _apply(self, simulator, action: HrvAction) -> None:
        """Run one action against the live simulator."""
        if action.verb == "bpm":
            simulator.set_bpm(action.number)
        elif action.verb == "contact":
            simulator.set_contact(action.contact)
        elif action.verb == "battery":
            simulator.set_battery(int(action.number))
        elif action.verb == "energy":
            simulator.set_energy(None if action.number is None else int(action.number))
        elif action.verb == "burst":
            if not simulator.send_burst(int(action.number)):
                LOGGER.warning(
                    "BleHrvSimSet [%s]: burst went nowhere, no central is subscribed",
                    self.session,
                )
        elif action.verb == "stall":
            simulator.stall(action.number)
        elif action.verb == "resume":
            simulator.resume()
        elif action.verb == "bounce":
            simulator.bounce()
