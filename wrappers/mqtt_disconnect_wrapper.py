import logging

import yaml

from . import mqtt_registry
from .wrapper import Wrapper


LOGGER = logging.getLogger(__name__)


class MqttDisconnectWrapper(Wrapper):
    """Closes an MQTT session opened by !MqttSubscribe.

    Optional in a scenario: the runner closes any session still open when the
    scenario ends. Use it to free a client id partway through, or to prove the
    connection was closed before a later step runs.
    """

    def __init__(self, command_node: yaml.MappingNode):
        self.command_node = command_node
        self.name: str | None = None
        self.session: str | None = None

    def parse(self) -> None:
        tag_name = self.command_node.tag.lstrip("!").rstrip(":")
        if tag_name != "MqttDisconnect":
            raise ValueError("Expected !MqttDisconnect command")

        for key_node, value_node in self.command_node.value:
            if not isinstance(key_node, yaml.ScalarNode) or not isinstance(value_node, yaml.ScalarNode):
                continue

            key = key_node.value
            if key == "name":
                self.name = value_node.value
            elif key == "session":
                self.session = value_node.value

        if self.session is None:
            raise ValueError("MqttDisconnect: 'session' field is required")

        LOGGER.info("Parsed MqttDisconnect: name=%s, session=%s", self.name, self.session)

    def execute(self) -> None:
        listener = mqtt_registry.pop(self.session)
        if listener is None:
            # Warn rather than raise: a leftover disconnect from a commented-out
            # subscribe should not abort a scenario, and the runner closes any
            # session that really is still open anyway.
            LOGGER.warning("MqttDisconnect: no open MQTT session named '%s', nothing to do", self.session)
            return

        listener.close()
        LOGGER.info("Closed MQTT session '%s'", self.session)
