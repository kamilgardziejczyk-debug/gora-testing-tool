import logging
import time
from typing import NamedTuple

import yaml

from tools.ble_gatt import (
    DEFAULT_CONNECT_TIMEOUT_S,
    DEFAULT_ENCODING,
    DEFAULT_SCAN_TIMEOUT_S,
    BleCentral,
    encode_value,
    format_value,
    normalize_uuid,
)

from .wrapper import Wrapper


LOGGER = logging.getLogger(__name__)


class Write(NamedTuple):
    """One characteristic write, with its value already encoded at parse time.

    `raw_value` and `encoding` are kept only for logging, so the log shows what
    the scenario asked for next to the bytes that went out.
    """

    uuid: str
    value: bytes
    raw_value: str
    encoding: str
    service_uuid: str | None
    response: bool
    wait_after_ms: int | None


class BleCentralWrapper(Wrapper):
    """
    Wrapper that drives tools/ble_gatt as a BLE central: connects to a
    peripheral by advertised name (or address), writes a sequence of GATT
    characteristics, then disconnects.

    Self-contained like !SubghzSim - the connection lives for this command
    only, so nothing is left holding the adapter afterwards.
    """

    def __init__(self, command_node: yaml.MappingNode):
        self.command_node = command_node
        self.name: str | None = None
        self.device: str | None = None
        self.service: str | None = None
        self.adapter: str | None = None
        self.scan_timeout_s: float = DEFAULT_SCAN_TIMEOUT_S
        self.connect_timeout_s: float = DEFAULT_CONNECT_TIMEOUT_S
        self.writes: list[Write] = []

    def parse(self) -> None:
        tag_name = self.command_node.tag.lstrip("!").rstrip(":")
        if tag_name != "BleCentral":
            raise ValueError("Expected !BleCentral command")

        characteristics_node = None
        for key_node, value_node in self.command_node.value:
            if not isinstance(key_node, yaml.ScalarNode):
                continue

            key = key_node.value
            if key == "characteristics":
                characteristics_node = value_node
            elif isinstance(value_node, yaml.ScalarNode):
                if key == "name":
                    self.name = value_node.value
                elif key == "device":
                    self.device = value_node.value
                elif key == "service":
                    self.service = value_node.value
                elif key == "adapter":
                    self.adapter = value_node.value
                elif key == "scan_timeout_s":
                    self.scan_timeout_s = float(value_node.value)
                elif key == "connect_timeout_s":
                    self.connect_timeout_s = float(value_node.value)

        if self.device is None:
            raise ValueError("BleCentral: 'device' field is required")
        # Normalized here so a typo in the shared service UUID is caught once,
        # before it is inherited by every characteristic below.
        if self.service is not None:
            self.service = self._normalize("service", self.service)

        self.writes = self._parse_characteristics(characteristics_node)
        if not self.writes:
            raise ValueError("BleCentral: 'characteristics' field is required and must not be empty")

        LOGGER.info(
            "Parsed BleCentral: name=%s, device=%s, service=%s, adapter=%s, characteristics=%d",
            self.name,
            self.device,
            self.service,
            self.adapter,
            len(self.writes),
        )

    def _parse_characteristics(self, characteristics_node) -> list[Write]:
        """Build one Write per entry, encoding every value up front.

        Encoding at parse time means a bad UUID or an unencodable value fails
        before the radio is touched, rather than half-way through a sequence
        of writes with the device already in a changed state.
        """
        if characteristics_node is None:
            return []
        if not isinstance(characteristics_node, yaml.SequenceNode):
            raise ValueError("BleCentral: 'characteristics' must be a list")

        writes = []
        for index, entry_node in enumerate(characteristics_node.value):
            if not isinstance(entry_node, yaml.MappingNode):
                raise ValueError(f"BleCentral: characteristic #{index + 1} must be a mapping")
            writes.append(self._parse_characteristic(entry_node, index))
        return writes

    def _parse_characteristic(self, entry_node: yaml.MappingNode, index: int) -> Write:
        """Read one characteristic entry's fields and validate them."""
        label = f"characteristic #{index + 1}"
        uuid = None
        value = None
        encoding = DEFAULT_ENCODING
        service_uuid = self.service
        response = True
        wait_after_ms = None

        for key_node, value_node in entry_node.value:
            if not isinstance(key_node, yaml.ScalarNode) or not isinstance(value_node, yaml.ScalarNode):
                continue

            key = key_node.value
            if key == "uuid":
                uuid = value_node.value
            elif key == "value":
                value = value_node.value
            elif key == "encoding":
                encoding = value_node.value
            elif key == "service":
                service_uuid = self._normalize(f"{label} service", value_node.value)
            elif key == "response":
                response = value_node.value.strip().lower() not in ("false", "no", "0")
            elif key == "wait_after_ms":
                wait_after_ms = int(value_node.value)

        if uuid is None:
            raise ValueError(f"BleCentral: {label} is missing its 'uuid' field")
        if value is None:
            raise ValueError(f"BleCentral: {label} ({uuid}) is missing its 'value' field")

        try:
            encoded = encode_value(value, encoding)
        except ValueError as error:
            raise ValueError(f"BleCentral: {label} ({uuid}) has an invalid value: {error}") from None

        return Write(
            uuid=self._normalize(f"{label} uuid", uuid),
            value=encoded,
            raw_value=value,
            encoding=encoding,
            service_uuid=service_uuid,
            response=response,
            wait_after_ms=wait_after_ms,
        )

    def _normalize(self, label: str, uuid: str) -> str:
        try:
            return normalize_uuid(uuid)
        except ValueError as error:
            raise ValueError(f"BleCentral: {label}: {error}") from None

    def execute(self) -> None:
        central = BleCentral(
            adapter=self.adapter,
            scan_timeout_s=self.scan_timeout_s,
            connect_timeout_s=self.connect_timeout_s,
        )
        try:
            central.connect(self.device)
            for write in self.writes:
                self._run_write(central, write)
        finally:
            # Always release the adapter, even if a write failed: a peripheral
            # left connected keeps advertising off, so the next command (or the
            # next run) would not be able to find it.
            central.close()

        LOGGER.info("BleCentral: wrote %d characteristic(s) to '%s'", len(self.writes), self.device)

    def _run_write(self, central: BleCentral, write: Write) -> None:
        LOGGER.info(
            "ble -> %s = %s (%s '%s')",
            write.uuid,
            format_value(write.value),
            write.encoding,
            write.raw_value,
        )
        central.write_characteristic(
            write.uuid,
            write.value,
            service_uuid=write.service_uuid,
            response=write.response,
        )
        if write.wait_after_ms is not None:
            time.sleep(write.wait_after_ms / 1000)
