import logging
import time
from typing import Callable, NamedTuple, Union

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

from .expression import Expression, compile_expression
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

# Variables a read/notify `validation` expression may use. A characteristic
# is raw bytes, and which reading of them is meaningful is a property of the
# characteristic, not of the test framework - so rather than an `encoding`
# field deciding how a literal is interpreted, all four readings are offered
# and the expression picks the one it means.
VALUE_VARIABLES = ("value", "text", "number", "size")

# Shown when a scenario still carries the pre-expression `value == 01` syntax.
MIGRATION_HINT = "The value is now written {value} as a hex string, e.g. '{value} == \"01\"'."


def _value_variables(data: bytes) -> dict[str, object]:
    """Bind one characteristic value's four readings for a validation.

    `text` decodes with replacement rather than raising: a characteristic that
    is not UTF-8 is a perfectly ordinary thing to point a `{value}` assertion
    at, and it must not make the whole expression unevaluatable.
    """
    return {
        "value": data.hex(),
        "text": data.decode("utf-8", errors="replace"),
        "number": int.from_bytes(data, "little"),
        "size": len(data),
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

    `expression` is None when there is no `validation` field: the read still
    happens (and is logged), but nothing is asserted about its value - in
    which case `attempts` > 1 would just retry a read that always "succeeds".
    """

    uuid: str
    validation: str | None
    expression: Expression | None
    service_uuid: str | None
    wait_after_ms: int | None
    attempts: int
    retry_wait_ms: int


class NotifyAction(NamedTuple):
    """A wait for a `validation` expression to be satisfied by a pushed notification."""

    uuid: str
    validation: str
    expression: Expression
    service_uuid: str | None
    timeout_s: float
    wait_after_ms: int | None
    attempts: int
    retry_wait_ms: int


BleAction = Union[WriteAction, ReadAction, NotifyAction]

FieldSpecs = dict[str, tuple[Callable[[str], object], object]]


def _parse_response_flag(text: str) -> bool:
    return text.strip().lower() not in ("false", "no", "0")


def _extract_fields(node: yaml.MappingNode, specs: FieldSpecs) -> dict[str, object]:
    """Extract and type-convert a mapping's scalar fields per `specs`.

    `specs` maps a YAML key to `(converter, default)`. Every declared key
    starts at its default; a present key is overwritten by
    `converter(value_node.value)`. Keys not in `specs` are ignored, matching
    every action type's existing silent-skip-of-unknown-keys behavior.
    Shared by `_parse_write`/`_parse_read`/`_parse_notify`, which otherwise
    repeat this same key-loop for a near-identical set of fields.
    """
    values = {key: default for key, (_, default) in specs.items()}
    for key_node, value_node in node.value:
        if not isinstance(key_node, yaml.ScalarNode) or not isinstance(value_node, yaml.ScalarNode):
            continue
        key = key_node.value
        if key in specs:
            converter, _ = specs[key]
            values[key] = converter(value_node.value)
    return values


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

    The `read` and `notify` actions take a `validation` expression over the
    characteristic's value, read four ways:

        {value}   str   the bytes as lowercase hex, e.g. "01ff"
        {text}    str   the bytes decoded as UTF-8, undecodable bytes replaced
        {number}  int   the bytes as a little-endian unsigned integer
        {size}    int   how many bytes arrived

    `read` without a `validation` performs the read and logs it, asserting
    nothing. `notify` requires one, since the value it is waiting for is the
    only thing that ends the wait.

    Unlike `write`, these take no `encoding` field: which reading of the bytes
    is meaningful belongs to the characteristic, so the expression names it
    directly (`{number} == 1` rather than `encoding: uint8` plus `value == 1`).
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
        """Build one BleAction per entry, encoding every value up front.

        Encoding at parse time means a bad UUID or an unencodable value fails
        before the radio is touched, rather than half-way through a sequence
        of actions with the device already in a changed state.
        """
        if actions_node is None:
            return []
        if not isinstance(actions_node, yaml.SequenceNode):
            raise ValueError("BleCentral: 'actions' must be a list")

        return [self._parse_action(entry_node, index) for index, entry_node in enumerate(actions_node.value)]

    def _parse_action(self, entry_node: yaml.Node, index: int) -> BleAction:
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

    def _resolve_service(self, label: str, service: object) -> str | None:
        """A field-level `service` overrides the command-level default, once normalized."""
        return self._normalize(f"{label} service", service) if service else self.service

    def _parse_write(self, node: yaml.MappingNode, label: str) -> WriteAction:
        fields = _extract_fields(node, {
            "uuid": (str, None),
            "value": (str, None),
            "encoding": (str, DEFAULT_ENCODING),
            "service": (str, None),
            "response": (_parse_response_flag, True),
            "wait_after_ms": (int, None),
            "attempts": (int, None),
            "retry_wait_ms": (int, None),
        })

        uuid, value, encoding = fields["uuid"], fields["value"], fields["encoding"]
        if uuid is None:
            raise ValueError(f"BleCentral: {label} is missing its 'uuid' field")
        if value is None:
            raise ValueError(f"BleCentral: {label} ({uuid}) is missing its 'value' field")

        try:
            encoded = encode_value(value, encoding)
        except ValueError as error:
            raise ValueError(f"BleCentral: {label} ({uuid}) has an invalid value: {error}") from None

        attempts, retry_wait_ms = self._parse_retry_fields(label, fields["attempts"], fields["retry_wait_ms"])
        if attempts > 1:
            raise ValueError(
                f"BleCentral: {label} ({uuid}) sets attempts={attempts}, but writes cannot be retried safely - "
                f"a characteristic may already have acted on the first write before a timeout/error is reported, "
                f"so a retry could apply it twice. Use 'attempts: 1' (or omit it) for writes; retries are only "
                f"supported on 'read' and 'notify' actions."
            )
        return WriteAction(
            uuid=self._normalize(f"{label} uuid", uuid),
            value=encoded,
            raw_value=value,
            encoding=encoding,
            service_uuid=self._resolve_service(label, fields["service"]),
            response=fields["response"],
            wait_after_ms=fields["wait_after_ms"],
            attempts=attempts,
            retry_wait_ms=retry_wait_ms,
        )

    def _parse_read(self, node: yaml.MappingNode, label: str) -> ReadAction:
        fields = _extract_fields(node, {
            "uuid": (str, None),
            "validation": (str, None),
            "service": (str, None),
            "wait_after_ms": (int, None),
            "attempts": (int, None),
            "retry_wait_ms": (int, None),
        })

        uuid = fields["uuid"]
        if uuid is None:
            raise ValueError(f"BleCentral: {label} is missing its 'uuid' field")

        validation = fields["validation"]
        expression = None
        if validation is not None:
            expression = compile_expression(
                validation, VALUE_VARIABLES, f"BleCentral: {label} ({uuid}) 'validation'", MIGRATION_HINT
            )

        attempts, retry_wait_ms = self._parse_retry_fields(label, fields["attempts"], fields["retry_wait_ms"])
        return ReadAction(
            uuid=self._normalize(f"{label} uuid", uuid),
            validation=validation,
            expression=expression,
            service_uuid=self._resolve_service(label, fields["service"]),
            wait_after_ms=fields["wait_after_ms"],
            attempts=attempts,
            retry_wait_ms=retry_wait_ms,
        )

    def _parse_notify(self, node: yaml.MappingNode, label: str) -> NotifyAction:
        fields = _extract_fields(node, {
            "uuid": (str, None),
            "validation": (str, None),
            "service": (str, None),
            "timeout_s": (float, DEFAULT_NOTIFY_TIMEOUT_S),
            "wait_after_ms": (int, None),
            "attempts": (int, None),
            "retry_wait_ms": (int, None),
        })

        uuid, validation, timeout_s = fields["uuid"], fields["validation"], fields["timeout_s"]
        if uuid is None:
            raise ValueError(f"BleCentral: {label} is missing its 'uuid' field")
        if validation is None:
            raise ValueError(f"BleCentral: {label} ({uuid}) is missing its 'validation' field")
        if timeout_s <= 0:
            raise ValueError(f"BleCentral: {label} ({uuid}) timeout_s must be > 0, got {timeout_s}")

        expression = compile_expression(
            validation, VALUE_VARIABLES, f"BleCentral: {label} ({uuid}) 'validation'", MIGRATION_HINT
        )

        attempts, retry_wait_ms = self._parse_retry_fields(label, fields["attempts"], fields["retry_wait_ms"])
        return NotifyAction(
            uuid=self._normalize(f"{label} uuid", uuid),
            validation=validation,
            expression=expression,
            service_uuid=self._resolve_service(label, fields["service"]),
            timeout_s=timeout_s,
            wait_after_ms=fields["wait_after_ms"],
            attempts=attempts,
            retry_wait_ms=retry_wait_ms,
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
            for action in self.actions:
                self._run_action(central, action)
        finally:
            # Always release the adapter, even if an action failed: a
            # peripheral left connected keeps advertising off, so the next
            # command (or the next run) would not be able to find it.
            central.close()

        LOGGER.info("BleCentral: completed %d action(s) on '%s'", len(self.actions), self.device)

    def _run_action(self, central: BleCentral, action: BleAction) -> None:
        """Run one action, retrying it up to `action.attempts` times.

        Every action type carries its own `attempts`/`retry_wait_ms`, so this
        one retry loop covers write/read/notify alike rather than needing a
        separate wrapping construct.
        """
        last_error: Exception | None = None
        for attempt in range(1, action.attempts + 1):
            try:
                self._execute_action(central, action)
            except (ValueError, TimeoutError, IOError, RuntimeError) as error:
                last_error = error
                if attempt < action.attempts:
                    if not central.is_connected:
                        # The link itself dropped, not just this action - retrying on a dead
                        # connection would fail identically `attempts` times. Reconnect once
                        # instead, so the retry has something to actually run against; if that
                        # fails too, there's no point burning the remaining attempts.
                        LOGGER.warning(
                            "BleCentral: link dropped on attempt %d/%d, reconnecting before retrying: %s",
                            attempt, action.attempts, error,
                        )
                        try:
                            central.connect(self.device)
                        except (ConnectionError, TimeoutError) as reconnect_error:
                            raise ConnectionError(
                                f"BleCentral: link dropped on attempt {attempt}/{action.attempts} and "
                                f"reconnect failed: {reconnect_error}"
                            ) from error
                    else:
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

    def _execute_action(self, central: BleCentral, action: BleAction) -> None:
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

        if action.expression is not None:
            variables = _value_variables(data)
            if not action.expression.evaluate(variables):
                raise ValueError(
                    f"BleCentral: read {action.uuid} got {format_value(data)}, "
                    f"which does not satisfy '{action.validation}' ({action.expression.describe(variables)})"
                )

        if action.wait_after_ms is not None:
            time.sleep(action.wait_after_ms / 1000)

    def _run_notify(self, central: BleCentral, action: NotifyAction) -> None:
        LOGGER.info(
            "ble: waiting up to %.1fs for %s to satisfy '%s'",
            action.timeout_s,
            action.uuid,
            action.validation,
        )

        for data in central.stream_notifications(action.uuid, action.service_uuid, action.timeout_s):
            LOGGER.info("ble <- %s = %s", action.uuid, format_value(data))
            if action.expression.evaluate(_value_variables(data)):
                LOGGER.info("BleCentral: notify satisfied '%s' on %s", action.validation, action.uuid)
                if action.wait_after_ms is not None:
                    time.sleep(action.wait_after_ms / 1000)
                return

        raise TimeoutError(
            f"BleCentral: no value satisfying '{action.validation}' seen on {action.uuid} "
            f"within {action.timeout_s}s"
        )
