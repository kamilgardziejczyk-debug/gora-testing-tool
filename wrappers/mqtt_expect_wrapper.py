import logging
import operator as operator_module

import yaml

from . import mqtt_registry
from .wrapper import Wrapper


LOGGER = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 10.0

# Comparison functions for the operators a `validation` string may use.
COMPARISONS = {
    "==": operator_module.eq,
    "!=": operator_module.ne,
    ">=": operator_module.ge,
    "<=": operator_module.le,
    ">": operator_module.gt,
    "<": operator_module.lt,
}

# The message count only ever grows during the wait, so for some operators the
# final verdict is already certain before `timeout_s` elapses:
#   - "==", "<=", "<"  can only ever fail once `count` has been overshot; a
#     pass can't be confirmed early since more could still arrive.
#   - "!=", ">=", ">"  can only ever pass once `count` has been reached or
#     overshot; a fail can't be confirmed early since more could still arrive.
# Each entry is (verdict once crossed, the crossing condition).
EARLY_DECISION = {
    "==": (False, lambda actual, expected: actual > expected),
    "<=": (False, lambda actual, expected: actual > expected),
    "<": (False, lambda actual, expected: actual >= expected),
    "!=": (True, lambda actual, expected: actual > expected),
    ">=": (True, lambda actual, expected: actual >= expected),
    ">": (True, lambda actual, expected: actual > expected),
}


class MqttExpectWrapper(Wrapper):
    """Asserts a `validation` expression against the message count on an
    !MqttSubscribe session, e.g. `validation: "count == 2"`.

    MQTT delivery has no "no more messages coming" signal, so this waits out
    the full `timeout_s` window rather than stopping as soon as the count
    looks right: a straggler arriving just after would otherwise go unnoticed.
    The one exception is when the count already makes the final verdict
    certain (see EARLY_DECISION) - waiting out the rest of the window then
    would only slow the scenario down for nothing.

    Does not close the session; use !MqttDisconnect for that, so a scenario
    can !MqttExpect more than once against the same session.
    """

    def __init__(self, command_node: yaml.MappingNode):
        self.command_node = command_node
        self.name: str | None = None
        self.session: str | None = None
        self.operator: str | None = None
        self.expected_count: int | None = None
        self.timeout_s: float = DEFAULT_TIMEOUT_S

    def parse(self) -> None:
        tag_name = self.command_node.tag.lstrip("!").rstrip(":")
        if tag_name != "MqttExpect":
            raise ValueError("Expected !MqttExpect command")

        validation: str | None = None
        for key_node, value_node in self.command_node.value:
            if not isinstance(key_node, yaml.ScalarNode) or not isinstance(value_node, yaml.ScalarNode):
                continue

            key = key_node.value
            if key == "name":
                self.name = value_node.value
            elif key == "session":
                self.session = value_node.value
            elif key == "validation":
                validation = value_node.value
            elif key == "timeout_s":
                self.timeout_s = float(value_node.value)

        if validation is not None:
            self.operator, self.expected_count = self._parse_validation(validation)

        self._validate()

        LOGGER.info(
            "Parsed MqttExpect: name=%s, session=%s, validation='count %s %s', timeout_s=%s",
            self.name,
            self.session,
            self.operator,
            self.expected_count,
            self.timeout_s,
        )

    def _parse_validation(self, validation: str) -> tuple:
        """Parse a `"count <op> <n>"` expression into `(op, n)`.

        `count` is the only supported left-hand side today, kept as a literal
        word rather than assumed, so the syntax has room to grow other
        comparisons later without becoming ambiguous with this one.
        """
        tokens = validation.split()
        if len(tokens) != 3 or tokens[0] != "count":
            operators = ", ".join(COMPARISONS)
            raise ValueError(
                f"MqttExpect: 'validation' must look like 'count <op> <n>' "
                f"(op one of {operators}), got '{validation}'"
            )

        _, op, value = tokens
        if op not in COMPARISONS:
            operators = ", ".join(COMPARISONS)
            raise ValueError(f"MqttExpect: unrecognized operator '{op}' (expected one of {operators})")

        try:
            expected_count = int(value)
        except ValueError:
            raise ValueError(f"MqttExpect: expected count must be an integer, got '{value}'") from None

        return op, expected_count

    def _validate(self) -> None:
        """Fail at parse time on anything we can check without a broker."""
        if self.session is None:
            raise ValueError("MqttExpect: 'session' field is required")
        if self.operator is None:
            raise ValueError("MqttExpect: 'validation' field is required")
        if self.expected_count < 0:
            raise ValueError(f"MqttExpect: expected count must be >= 0, got {self.expected_count}")
        if self.timeout_s <= 0:
            raise ValueError(f"MqttExpect: timeout_s must be > 0, got {self.timeout_s}")

    def execute(self) -> None:
        listener = mqtt_registry.get(self.session)
        early_verdict, crossed = EARLY_DECISION[self.operator]

        received = []
        passed = None
        for message in listener.stream(duration_s=self.timeout_s):
            received.append(message)
            if crossed(len(received), self.expected_count):
                passed = early_verdict
                break

        if passed is None:
            passed = COMPARISONS[self.operator](len(received), self.expected_count)

        if not passed:
            raise ValueError(self._failure_message(listener, received))

        LOGGER.info(
            "MqttExpect: session '%s' satisfied 'count %s %s' (got %d)",
            self.session,
            self.operator,
            self.expected_count,
            len(received),
        )

    def _failure_message(self, listener, received: list) -> str:
        """Explain a failed assertion without conflating two different counts.

        `received` is only what this check counted before the verdict became
        certain - a `==`/`<=`/`<` check stops as soon as it's overshot, so this
        can be far smaller than everything the session has actually buffered
        (e.g. a session left open through a long heartbeat window). Showing
        only `received` would look right but hide where the extra messages
        came from; showing only `listener.recent()` would contradict the count
        in the headline. So both are printed, each labeled with what it is.
        """
        counted = "\n".join(f"  {m.topic}  {m.payload}" for m in received) or "  (none)"
        history = listener.recent()
        message = (
            f"MqttExpect: session '{self.session}' failed 'count {self.operator} {self.expected_count}': "
            f"counted {len(received)} message(s) before the result was already decided "
            f"(budget was {self.timeout_s}s).\n"
            f"Messages counted for this check:\n{counted}"
        )
        if len(history) != len(received):
            recent = "\n".join(f"  {m.topic}  {m.payload}" for m in history) or "  (none)"
            message += (
                f"\n\nFor context: this session has buffered {len(history)} message(s) in total "
                f"since !MqttSubscribe started listening (up to the last 100), which can include "
                f"ones from before this check started or after it stopped counting:\n{recent}"
            )
        return message
