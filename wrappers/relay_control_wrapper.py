import logging

import yaml

from tools.relay_board import RELAY_COUNT, RelayBoard

from .wrapper import Wrapper


LOGGER = logging.getLogger(__name__)

# Module-level singleton, shared by every !RelayControl command in a scenario
# run. Wrappers are re-instantiated per command by the Parser, which has no
# way to share state between them, so this mirrors the same rendezvous-
# through-module pattern mqtt_registry uses for the same reason: a fresh
# RelayBoard per command would re-run GPIO.setup() on every single command
# (see RelayBoard._ensure_configured), which briefly re-drives the pin to its
# de-energized level before the command's real level takes effect.
_board: RelayBoard | None = None


def _get_board() -> RelayBoard:
    global _board
    if _board is None:
        _board = RelayBoard()
    return _board


class RelayControlWrapper(Wrapper):
    """Wrapper for energizing/de-energizing one channel of the relay board."""

    def __init__(self, command_node: yaml.MappingNode):
        self.command_node = command_node
        self.name: str | None = None
        self.relay: int | None = None
        self.state: bool | None = None
        self.pulse_s: float | None = None

    def parse(self) -> None:
        tag_name = self.command_node.tag.lstrip("!").rstrip(":")
        if tag_name != "RelayControl":
            raise ValueError("Expected !RelayControl tag")

        for key_node, value_node in self.command_node.value:
            if not isinstance(key_node, yaml.ScalarNode) or not isinstance(value_node, yaml.ScalarNode):
                continue

            key = key_node.value
            if key == "name":
                self.name = value_node.value
            elif key == "relay":
                self.relay = int(value_node.value)
            elif key == "state":
                self.state = self._parse_state(value_node.value)
            elif key == "pulse_s":
                self.pulse_s = float(value_node.value)

        self._validate_parsed_fields()

        LOGGER.info(
            "Parsed RelayControl: name=%s, relay=%s, state=%s, pulse_s=%s",
            self.name,
            self.relay,
            self.state,
            self.pulse_s,
        )

    @staticmethod
    def _parse_state(raw: str) -> bool:
        """Parse the 'state' field: 1 for energized, 0 for de-energized."""
        if raw == "1":
            return True
        if raw == "0":
            return False
        raise ValueError(f"RelayControl: 'state' must be 1 or 0, got '{raw}'")

    def _validate_parsed_fields(self) -> None:
        """Reject a scenario missing/conflicting YAML fields, before any hardware is touched."""
        if self.relay is None:
            raise ValueError("RelayControl: 'relay' field is required")
        if not 1 <= self.relay <= RELAY_COUNT:
            raise ValueError(f"RelayControl: 'relay' must be between 1 and {RELAY_COUNT}, got {self.relay}")
        if self.state is not None and self.pulse_s is not None:
            raise ValueError("RelayControl: specify either 'state' or 'pulse_s', not both")
        if self.state is None and self.pulse_s is None:
            raise ValueError("RelayControl: one of 'state' or 'pulse_s' is required")
        if self.pulse_s is not None and self.pulse_s < 0:
            raise ValueError(f"RelayControl: 'pulse_s' must be >= 0, got {self.pulse_s}")

    def execute(self) -> None:
        board = _get_board()
        if self.pulse_s is not None:
            LOGGER.info("Pulsing relay %s for %ss to simulate a button push", self.relay, self.pulse_s)
            board.pulse(self.relay, self.pulse_s)
        else:
            board.set(self.relay, self.state)


def cleanup_all() -> None:
    """De-energize and release every relay this scenario configured.

    Called from the scenario runner's `finally`, so a command that raises
    part-way through still leaves relays released rather than lingering
    energized. Safe to call when no !RelayControl command has run.
    """
    global _board
    if _board is None:
        return
    _board.release()
    _board = None
