import logging
from pathlib import Path

import yaml

from tools.mqtt_listener import DEFAULT_CONNECT_TIMEOUT_S, DEFAULT_PORT, DEFAULT_QOS, MqttListener

from . import mqtt_registry
from .wrapper import Wrapper


LOGGER = logging.getLogger(__name__)

REQUIRED_FIELDS = ("session", "endpoint", "client_id", "cert", "private_key", "root_ca")
CERT_FIELDS = ("cert", "private_key", "root_ca")


class MqttSubscribeWrapper(Wrapper):
    """Opens one MQTT session and starts buffering messages on every topic in `topics`.

    Non-blocking: it returns as soon as the broker confirms every subscription, so
    place it *before* the command that triggers a publish. The session stays open
    under its `session` name, for !MqttExpect (or any later command) to read from,
    until the runner tears it down at the end of the scenario.

    One session can carry several topics because a broker allows only one
    connection per `client_id`: a device shadow check and a sub-GHz uplink
    check both needing "gateway-01"'s client_id would otherwise evict each
    other. !MqttExpect's own `topic` field then picks which topic each check
    is counting.
    """

    def __init__(self, command_node: yaml.MappingNode):
        self.command_node = command_node
        self.name: str | None = None
        self.session: str | None = None
        self.endpoint: str | None = None
        self.port: int = DEFAULT_PORT
        self.client_id: str | None = None
        self.cert: str | None = None
        self.private_key: str | None = None
        self.root_ca: str | None = None
        self.topics: list[str] = []
        self.qos: int = DEFAULT_QOS
        self.connect_timeout_s: float = DEFAULT_CONNECT_TIMEOUT_S

    def parse(self) -> None:
        tag_name = self.command_node.tag.lstrip("!").rstrip(":")
        if tag_name != "MqttSubscribe":
            raise ValueError("Expected !MqttSubscribe command")

        for key_node, value_node in self.command_node.value:
            if not isinstance(key_node, yaml.ScalarNode):
                continue

            key = key_node.value
            if key == "topics":
                self.topics = self._parse_topics(value_node)
            elif isinstance(value_node, yaml.ScalarNode):
                if key == "name":
                    self.name = value_node.value
                elif key == "session":
                    self.session = value_node.value
                elif key == "endpoint":
                    self.endpoint = value_node.value
                elif key == "port":
                    self.port = int(value_node.value)
                elif key == "client_id":
                    self.client_id = value_node.value
                elif key == "cert":
                    self.cert = value_node.value
                elif key == "private_key":
                    self.private_key = value_node.value
                elif key == "root_ca":
                    self.root_ca = value_node.value
                elif key == "qos":
                    self.qos = int(value_node.value)
                elif key == "connect_timeout_s":
                    self.connect_timeout_s = float(value_node.value)

        self._validate()

        LOGGER.info(
            "Parsed MqttSubscribe: name=%s, session=%s, endpoint=%s:%s, client_id=%s, topics=%s, qos=%s",
            self.name,
            self.session,
            self.endpoint,
            self.port,
            self.client_id,
            self.topics,
            self.qos,
        )

    def _parse_topics(self, topics_node: yaml.Node) -> list[str]:
        """Extract topic filter strings from a `topics` sequence."""
        if not isinstance(topics_node, yaml.SequenceNode):
            raise ValueError("MqttSubscribe: 'topics' must be a list of topic filter strings")

        topics = []
        for item_node in topics_node.value:
            if not isinstance(item_node, yaml.ScalarNode):
                raise ValueError("MqttSubscribe: 'topics' entries must be plain strings")
            topics.append(item_node.value)
        return topics

    def _validate(self) -> None:
        """Fail at parse time on anything we can check without a broker."""
        for field in REQUIRED_FIELDS:
            if getattr(self, field) is None:
                raise ValueError(f"MqttSubscribe: '{field}' field is required")
        if not self.topics:
            raise ValueError("MqttSubscribe: 'topics' field is required and must not be empty")
        if self.qos not in (0, 1):
            raise ValueError(f"MqttSubscribe: qos must be 0 or 1, got {self.qos}")

    def execute(self) -> None:
        cert_paths = {field: self._resolve_cert(field) for field in CERT_FIELDS}

        listener = MqttListener(
            endpoint=self.endpoint,
            port=self.port,
            client_id=self.client_id,
            cert=str(cert_paths["cert"]),
            private_key=str(cert_paths["private_key"]),
            root_ca=str(cert_paths["root_ca"]),
        )

        try:
            listener.connect(timeout_s=self.connect_timeout_s)
            for topic in self.topics:
                listener.subscribe(topic, qos=self.qos)
            mqtt_registry.register(self.session, listener)
        except Exception:
            # Never leave a connected client with nothing tracking it: its
            # network thread would keep the broker connection alive, and the
            # client id would collide with the next attempt to use it.
            listener.close()
            raise

        LOGGER.info("MQTT session '%s' is buffering messages on %s", self.session, self.topics)

    def _resolve_cert(self, field: str) -> Path:
        """Resolve a certificate path, relative ones against the scenario file."""
        path = Path(getattr(self, field))
        if not path.is_absolute() and self.scenario_dir is not None:
            path = self.scenario_dir / path
        if not path.is_file():
            raise FileNotFoundError(f"MqttSubscribe: {field} file not found: {path}")
        return path
