import logging
import operator as operator_module
import time
from typing import NamedTuple, Union

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

DEFAULT_NOTIFY_TIMEOUT_S = 30.0
DEFAULT_ATTEMPTS = 1
DEFAULT_RETRY_WAIT_MS = 1000

# Every action type can be retried: `attempts` (default 1, i.e. no retry) and
# `retry_wait_ms` are attached to write/read/notify directly rather than a
# separate wrapping verb, since the retry logic is identical regardless of
# which action it applies to. Distinct from `wait_after_ms`, which is the
# pause after this action *succeeds*, before the next one in `actions:` runs.
ACTION_VERBS = ("write", "read", "notify")

# Only equality is meaningful in general: unlike !MqttExpect's count, a GATT
# payload has no ordering once encodings other than a fixed-width integer are
# allowed, so >=/<=/>/< would mean something different depending on `encoding`
# (or nothing at all for hex/utf8). Kept as a comparison table anyway, matching
# !MqttExpect's 'validation' pattern, so it reads the same way. Shared by the
# `read` and `notify` actions, which both use "value <op> <literal>".
VALUE_COMPARISONS = {
    "==": operator_module.eq,
    "!=": operator_module.ne,
}


class WriteAction(NamedTuple):
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
    attempts: int
    retry_wait_ms: int


class ReadAction(NamedTuple):
    """One characteristic read, with an optional `validation` to check it against.

    `operator` is None when there is no `validation` field: the read still
    happens (and is logged), but nothing is asserted about its value - in
    which case `attempts` > 1 would just retry a read that always "succeeds".
    """

    uuid: str
    operator: str | None
    expected_value: bytes | None
    raw_value: str | None
    encoding: str
    service_uuid: str | None
    wait_after_ms: int | None
    attempts: int
    retry_wait_ms: int


class NotifyAction(NamedTuple):
    """A wait for a `validation` expression to be satisfied by a pushed notification.

    `raw_value` and `encoding` are kept only for logging, matching the other actions.
    """

    uuid: str
    operator: str
    expected_value: bytes
    raw_value: str
    encoding: str
    service_uuid: str | None
    timeout_s: float
    wait_after_ms: int | None
    attempts: int
    retry_wait_ms: int


Action = Union[WriteAction, ReadAction, NotifyAction]


class BleCentralWrapper(Wrapper):
    """
    Wrapper that drives tools/ble_gatt as a BLE central: connects to a
    peripheral by advertised name (or address), runs a sequence of `actions`
    (write / read / notify), then disconnects.

    Self-contained like !SubghzSim - the connection lives for this command
    only, so nothing is left holding the adapter afterwards. This also makes
    it the right tool for waiting on something *after* a device reset that
    drops the BLE link: reconnecting is a fresh !BleCentral command rather
    than something the wrapper that triggered the reset stays open for.
    """

    def __init__(self, command_node: yaml.MappingNode):
        self.command_node = command_node
        self.name: str | None = None
        self.device: str | None = None
        self.service: str | None = None
        self.adapter: str | None = None
        self.scan_timeout_s: float = DEFAULT_SCAN_TIMEOUT_S
        self.connect_timeout_s: float = DEFAULT_CONNECT_TIMEOUT_S
        self.actions: list = []

    def parse(self) -> None:
        tag_name = self.command_node.tag.lstrip("!").rstrip(":")
        if tag_name != "BleCentral":
            raise ValueError("Expected !BleCentral command")

        actions_node = None
        for key_node, value_node in self.command_node.value:
            if not isinstance(key_node, yaml.ScalarNode):
                continue

            key = key_node.value
            if key == "actions":
                actions_node = value_node
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
        # before it is inherited by every action below.
        if self.service is not None:
            self.service = self._normalize("service", self.service)

        self.actions = self._parse_actions(actions_node)
        if not self.actions:
            raise ValueError("BleCentral: 'actions' field is required and must not be empty")

        LOGGER.info(
            "Parsed BleCentral: name=%s, device=%s, service=%s, adapter=%s, actions=%d",
            self.name,
            self.device,
            self.service,
            self.adapter,
            len(self.actions),
        )

    def _parse_actions(self, actions_node) -> list:
        """Build one Action per entry, encoding every value up front.

        Encoding at parse time means a bad UUID or an unencodable value fails
        before the radio is touched, rather than half-way through a sequence
        of actions with the device already in a changed state.
        """
        if actions_node is None:
            return []
        if not isinstance(actions_node, yaml.SequenceNode):
            raise ValueError("BleCentral: 'actions' must be a list")

        return [self._parse_action(entry_node, index) for index, entry_node in enumerate(actions_node.value)]

    def _parse_action(self, entry_node: yaml.Node, index: int) -> Action:
        """Find the one verb key an action entry must have, and dispatch to it."""
        if not isinstance(entry_node, yaml.MappingNode):
            raise ValueError(f"BleCentral: action #{index + 1} must be a mapping")

        verb = None
        verb_node = None
        for key_node, value_node in entry_node.value:
            if isinstance(key_node, yaml.ScalarNode) and key_node.value in ACTION_VERBS:
                if verb is not None:
                    raise ValueError(
                        f"BleCentral: action #{index + 1} has more than one verb "
                        f"('{verb}' and '{key_node.value}') - each action needs exactly one"
                    )
                verb, verb_node = key_node.value, value_node

        if verb is None:
            raise ValueError(
                f"BleCentral: action #{index + 1} must have one of: {', '.join(ACTION_VERBS)}"
            )
        if not isinstance(verb_node, yaml.MappingNode):
            raise ValueError(f"BleCentral: action #{index + 1} ('{verb}') must be a mapping")

        label = f"action #{index + 1} ({verb})"
        if verb == "write":
            return self._parse_write(verb_node, label)
        if verb == "read":
            return self._parse_read(verb_node, label)
        return self._parse_notify(verb_node, label)

    def _parse_retry_fields(self, label: str, attempts: int | None, retry_wait_ms: int | None) -> tuple[int, int]:
        """Validate the `attempts`/`retry_wait_ms` pair shared by every action type."""
        attempts = DEFAULT_ATTEMPTS if attempts is None else attempts
        retry_wait_ms = DEFAULT_RETRY_WAIT_MS if retry_wait_ms is None else retry_wait_ms
        if attempts < 1:
            raise ValueError(f"BleCentral: {label} attempts must be >= 1, got {attempts}")
        if retry_wait_ms < 0:
            raise ValueError(f"BleCentral: {label} retry_wait_ms must be >= 0, got {retry_wait_ms}")
        return attempts, retry_wait_ms

    def _parse_write(self, node: yaml.MappingNode, label: str) -> WriteAction:
        uuid = None
        value = None
        encoding = DEFAULT_ENCODING
        service_uuid = self.service
        response = True
        wait_after_ms = None
        attempts = None
        retry_wait_ms = None

        for key_node, value_node in node.value:
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
            elif key == "attempts":
                attempts = int(value_node.value)
            elif key == "retry_wait_ms":
                retry_wait_ms = int(value_node.value)

        if uuid is None:
            raise ValueError(f"BleCentral: {label} is missing its 'uuid' field")
        if value is None:
            raise ValueError(f"BleCentral: {label} ({uuid}) is missing its 'value' field")

        try:
            encoded = encode_value(value, encoding)
        except ValueError as error:
            raise ValueError(f"BleCentral: {label} ({uuid}) has an invalid value: {error}") from None

        attempts, retry_wait_ms = self._parse_retry_fields(label, attempts, retry_wait_ms)
        return WriteAction(
            uuid=self._normalize(f"{label} uuid", uuid),
            value=encoded,
            raw_value=value,
            encoding=encoding,
            service_uuid=service_uuid,
            response=response,
            wait_after_ms=wait_after_ms,
            attempts=attempts,
            retry_wait_ms=retry_wait_ms,
        )

    def _parse_read(self, node: yaml.MappingNode, label: str) -> ReadAction:
        uuid = None
        validation = None
        encoding = DEFAULT_ENCODING
        service_uuid = self.service
        wait_after_ms = None
        attempts = None
        retry_wait_ms = None

        for key_node, value_node in node.value:
            if not isinstance(key_node, yaml.ScalarNode) or not isinstance(value_node, yaml.ScalarNode):
                continue

            key = key_node.value
            if key == "uuid":
                uuid = value_node.value
            elif key == "validation":
                validation = value_node.value
            elif key == "encoding":
                encoding = value_node.value
            elif key == "service":
                service_uuid = self._normalize(f"{label} service", value_node.value)
            elif key == "wait_after_ms":
                wait_after_ms = int(value_node.value)
            elif key == "attempts":
                attempts = int(value_node.value)
            elif key == "retry_wait_ms":
                retry_wait_ms = int(value_node.value)

        if uuid is None:
            raise ValueError(f"BleCentral: {label} is missing its 'uuid' field")

        operator = None
        expected = None
        raw_value = None
        if validation is not None:
            operator, literal = self._parse_value_validation(label, validation)
            try:
                expected = encode_value(literal, encoding)
            except ValueError as error:
                raise ValueError(f"BleCentral: {label} ({uuid}) has an invalid value: {error}") from None
            raw_value = literal

        attempts, retry_wait_ms = self._parse_retry_fields(label, attempts, retry_wait_ms)
        return ReadAction(
            uuid=self._normalize(f"{label} uuid", uuid),
            operator=operator,
            expected_value=expected,
            raw_value=raw_value,
            encoding=encoding,
            service_uuid=service_uuid,
            wait_after_ms=wait_after_ms,
            attempts=attempts,
            retry_wait_ms=retry_wait_ms,
        )

    def _parse_notify(self, node: yaml.MappingNode, label: str) -> NotifyAction:
        uuid = None
        validation = None
        encoding = DEFAULT_ENCODING
        service_uuid = self.service
        timeout_s = DEFAULT_NOTIFY_TIMEOUT_S
        wait_after_ms = None
        attempts = None
        retry_wait_ms = None

        for key_node, value_node in node.value:
            if not isinstance(key_node, yaml.ScalarNode) or not isinstance(value_node, yaml.ScalarNode):
                continue

            key = key_node.value
            if key == "uuid":
                uuid = value_node.value
            elif key == "validation":
                validation = value_node.value
            elif key == "encoding":
                encoding = value_node.value
            elif key == "service":
                service_uuid = self._normalize(f"{label} service", value_node.value)
            elif key == "timeout_s":
                timeout_s = float(value_node.value)
            elif key == "wait_after_ms":
                wait_after_ms = int(value_node.value)
            elif key == "attempts":
                attempts = int(value_node.value)
            elif key == "retry_wait_ms":
                retry_wait_ms = int(value_node.value)

        if uuid is None:
            raise ValueError(f"BleCentral: {label} is missing its 'uuid' field")
        if validation is None:
            raise ValueError(f"BleCentral: {label} ({uuid}) is missing its 'validation' field")
        if timeout_s <= 0:
            raise ValueError(f"BleCentral: {label} ({uuid}) timeout_s must be > 0, got {timeout_s}")

        operator, literal = self._parse_value_validation(label, validation)
        try:
            encoded = encode_value(literal, encoding)
        except ValueError as error:
            raise ValueError(f"BleCentral: {label} ({uuid}) has an invalid value: {error}") from None

        attempts, retry_wait_ms = self._parse_retry_fields(label, attempts, retry_wait_ms)
        return NotifyAction(
            uuid=self._normalize(f"{label} uuid", uuid),
            operator=operator,
            expected_value=encoded,
            raw_value=literal,
            encoding=encoding,
            service_uuid=service_uuid,
            timeout_s=timeout_s,
            wait_after_ms=wait_after_ms,
            attempts=attempts,
            retry_wait_ms=retry_wait_ms,
        )

    def _parse_value_validation(self, label: str, validation: str) -> tuple[str, str]:
        """Parse a `"value <op> <literal>"` expression into `(op, literal)`.

        Shared by `read` and `notify`, which both check a characteristic's
        value the same way. Split with a max of 2 splits, not a plain
        `.split()`, so a `utf8` literal containing spaces
        (`"value == hello world"`) stays intact as one token.
        """
        tokens = validation.split(None, 2)
        if len(tokens) != 3 or tokens[0] != "value":
            operators = ", ".join(VALUE_COMPARISONS)
            raise ValueError(
                f"BleCentral: {label} 'validation' must look like 'value <op> <literal>' "
                f"(op one of {operators}), got '{validation}'"
            )

        _, op, literal = tokens
        if op not in VALUE_COMPARISONS:
            operators = ", ".join(VALUE_COMPARISONS)
            raise ValueError(f"BleCentral: {label} unrecognized operator '{op}' (expected one of {operators})")

        return op, literal

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
            for action in self.actions:
                self._run_action(central, action)
        finally:
            # Always release the adapter, even if an action failed: a
            # peripheral left connected keeps advertising off, so the next
            # command (or the next run) would not be able to find it.
            central.close()

        LOGGER.info("BleCentral: completed %d action(s) on '%s'", len(self.actions), self.device)

    def _run_action(self, central: BleCentral, action: Action) -> None:
        """Run one action, retrying it up to `action.attempts` times.

        Every action type carries its own `attempts`/`retry_wait_ms`, so this
        one retry loop covers write/read/notify alike rather than needing a
        separate wrapping construct.
        """
        last_error: Exception | None = None
        for attempt in range(1, action.attempts + 1):
            try:
                self._execute_action(central, action)
            except (ValueError, TimeoutError, ConnectionError, IOError, RuntimeError) as error:
                last_error = error
                if attempt < action.attempts:
                    LOGGER.info(
                        "BleCentral: attempt %d/%d failed, retrying in %dms: %s",
                        attempt, action.attempts, action.retry_wait_ms, error,
                    )
                    time.sleep(action.retry_wait_ms / 1000)
                    continue
                if action.attempts > 1:
                    raise TimeoutError(
                        f"BleCentral: exhausted {action.attempts} attempt(s); last error: {last_error}"
                    ) from last_error
                raise
            else:
                if action.attempts > 1:
                    LOGGER.info("BleCentral: succeeded on attempt %d/%d", attempt, action.attempts)
                return

    def _execute_action(self, central: BleCentral, action: Action) -> None:
        if isinstance(action, WriteAction):
            self._run_write(central, action)
        elif isinstance(action, ReadAction):
            self._run_read(central, action)
        elif isinstance(action, NotifyAction):
            self._run_notify(central, action)
        else:
            raise AssertionError(f"BleCentral: unhandled action type {type(action).__name__}")

    def _run_write(self, central: BleCentral, action: WriteAction) -> None:
        LOGGER.info(
            "ble -> %s = %s (%s '%s')",
            action.uuid,
            format_value(action.value),
            action.encoding,
            action.raw_value,
        )
        central.write_characteristic(
            action.uuid,
            action.value,
            service_uuid=action.service_uuid,
            response=action.response,
        )
        if action.wait_after_ms is not None:
            time.sleep(action.wait_after_ms / 1000)

    def _run_read(self, central: BleCentral, action: ReadAction) -> None:
        data = central.read_characteristic(action.uuid, action.service_uuid)
        LOGGER.info("ble <- %s = %s", action.uuid, format_value(data))

        if action.operator is not None:
            compare = VALUE_COMPARISONS[action.operator]
            if not compare(data, action.expected_value):
                raise ValueError(
                    f"BleCentral: read {action.uuid} got {format_value(data)}, "
                    f"which does not satisfy 'value {action.operator} {format_value(action.expected_value)}'"
                )

        if action.wait_after_ms is not None:
            time.sleep(action.wait_after_ms / 1000)

    def _run_notify(self, central: BleCentral, action: NotifyAction) -> None:
        LOGGER.info(
            "ble: waiting up to %.1fs for %s value %s %s (%s '%s')",
            action.timeout_s,
            action.uuid,
            action.operator,
            format_value(action.expected_value),
            action.encoding,
            action.raw_value,
        )

        compare = VALUE_COMPARISONS[action.operator]
        for data in central.stream_notifications(action.uuid, action.service_uuid, action.timeout_s):
            LOGGER.info("ble <- %s = %s", action.uuid, format_value(data))
            if compare(data, action.expected_value):
                LOGGER.info("BleCentral: notify ('value %s %s') satisfied on %s",
                            action.operator, format_value(action.expected_value), action.uuid)
                if action.wait_after_ms is not None:
                    time.sleep(action.wait_after_ms / 1000)
                return

        raise TimeoutError(
            f"BleCentral: no value satisfying 'value {action.operator} "
            f"{format_value(action.expected_value)}' seen on {action.uuid} within {action.timeout_s}s"
        )
