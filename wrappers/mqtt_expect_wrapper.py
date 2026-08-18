import json
import logging

import yaml
from paho.mqtt.client import topic_matches_sub

from . import mqtt_registry
from .expression import Expression, compile_expression
from .wrapper import Wrapper


LOGGER = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 10.0

# How much of a payload a failure message quotes. A batching device puts
# dozens of samples in one publish, and a handful of those would bury the
# failure itself; the run's mqtt.log holds every payload in full.
MAX_QUOTED_PAYLOAD_CHARS = 300


def _abbreviate(payload: str) -> str:
    """`payload` shortened for quoting, pointing at the log for the rest."""
    if len(payload) <= MAX_QUOTED_PAYLOAD_CHARS:
        return payload
    trimmed = len(payload) - MAX_QUOTED_PAYLOAD_CHARS
    return f"{payload[:MAX_QUOTED_PAYLOAD_CHARS]}... (+{trimmed} chars, in full in mqtt.log)"

# Variables a `validation` expression may use, and what they hold.
VARIABLES = ("count", "payloads", "values")

# Shown when a scenario still carries the pre-expression `count == n` syntax.
MIGRATION_HINT = "The count is now written {count}, e.g. '{count} == 192'."

# The message count only ever grows during the wait, so for some operators the
# final verdict is already certain before `timeout_s` elapses:
#   - "==", "<=", "<"  can only ever fail once `count` has been overshot; a
#     pass can't be confirmed early since more could still arrive.
#   - "!=", ">=", ">"  can only ever pass once `count` has been reached or
#     overshot; a fail can't be confirmed early since more could still arrive.
# Each entry is (verdict once crossed, the crossing condition).
#
# Only reachable for a validation that is exactly `{count} <op> <int>`; any
# richer expression cannot be reasoned about this way and waits out the full
# window instead. See `_early_decision`.
EARLY_DECISION = {
    "==": (False, lambda actual, expected: actual > expected),
    "<=": (False, lambda actual, expected: actual > expected),
    "<": (False, lambda actual, expected: actual >= expected),
    "!=": (True, lambda actual, expected: actual > expected),
    ">=": (True, lambda actual, expected: actual >= expected),
    ">": (True, lambda actual, expected: actual > expected),
}


class MqttExpectWrapper(Wrapper):
    """Asserts a `validation` expression against what arrived on one `topic`
    filter within an !MqttSubscribe session, e.g. `validation: "{count} == 2"`.

    The expression is evaluated against:

        {count}     int        messages matched, or distinct `count_by` values
        {payloads}  list[str]  the matched messages' payloads, in arrival order
        {values}    list       the distinct `count_by` values seen, or []

    By default `count` is a count of *messages*. With `count_by` set it is
    instead the number of distinct values of that field across the messages'
    JSON payloads - for a device that batches (a journal uploading everything
    it has accumulated on a fixed timer, say), how many messages the data was
    split into is an artefact of the device's upload schedule, not something a
    test should assert on. Counting `count_by: "seq"` items instead makes the
    check independent of the batching, and because the values are de-duplicated
    it also absorbs the redeliveries QoS 1 is allowed to produce.

    A session can carry several topics (see !MqttSubscribe), so this only
    counts messages whose topic matches `topic` - a plain topic or one with
    `+`/`#` wildcards, matched the same way a broker matches a subscription
    filter against a concrete topic. Anything read off the session that
    doesn't match is put back afterwards, so a later !MqttExpect on a
    different topic still sees it.

    MQTT delivery has no "no more messages coming" signal, so this waits out
    the full `timeout_s` window rather than stopping as soon as the count
    looks right: a straggler arriving just after would otherwise go unnoticed.
    The one exception is when the count already makes the final verdict
    certain (see EARLY_DECISION) - waiting out the rest of the window then
    would only slow the scenario down for nothing.

    Does not close the session, so a scenario can !MqttExpect more than once
    against the same session - it stays open until the runner tears it down
    at the end of the scenario.
    """

    def __init__(self, command_node: yaml.MappingNode):
        self.command_node = command_node
        self.name: str | None = None
        self.session: str | None = None
        self.topic: str | None = None
        self.count_by: str | None = None
        self.validation: str | None = None
        self.expression: Expression | None = None
        self.timeout_s: float = DEFAULT_TIMEOUT_S

    def parse(self) -> None:
        tag_name = self.command_node.tag.lstrip("!").rstrip(":")
        if tag_name != "MqttExpect":
            raise ValueError("Expected !MqttExpect command")

        for key_node, value_node in self.command_node.value:
            if not isinstance(key_node, yaml.ScalarNode) or not isinstance(value_node, yaml.ScalarNode):
                continue

            key = key_node.value
            if key == "name":
                self.name = value_node.value
            elif key == "session":
                self.session = value_node.value
            elif key == "topic":
                self.topic = value_node.value
            elif key == "count_by":
                self.count_by = value_node.value
            elif key == "validation":
                self.validation = value_node.value
            elif key == "timeout_s":
                self.timeout_s = float(value_node.value)

        self._validate()

        LOGGER.info(
            "Parsed MqttExpect: name=%s, session=%s, topic=%s, count_by=%s, "
            "validation='%s', timeout_s=%s",
            self.name,
            self.session,
            self.topic,
            self.count_by,
            self.validation,
            self.timeout_s,
        )

    def _early_decision(self) -> tuple[bool, object] | None:
        """The early-exit rule for this validation, or None if it has none.

        Only a validation of exactly `{count} <op> <int>` can be settled
        before the window ends, because only then does a count that has
        already overshot make the final verdict certain. Anything richer -
        a condition on `{payloads}`, a compound expression - has to see the
        whole window, which is slower but never wrong.
        """
        simple = self.expression.as_simple_comparison()
        if simple is None:
            return None
        name, operator, literal = simple
        if name != "count" or not isinstance(literal, int) or isinstance(literal, bool):
            return None
        verdict, crossed = EARLY_DECISION[operator]
        return verdict, lambda actual: crossed(actual, literal)

    def _validate(self) -> None:
        """Fail at parse time on anything we can check without a broker."""
        if self.session is None:
            raise ValueError("MqttExpect: 'session' field is required")
        if self.topic is None:
            raise ValueError("MqttExpect: 'topic' field is required")
        if self.count_by is not None and not self.count_by.strip():
            raise ValueError("MqttExpect: 'count_by' must name a field in the payload, not be empty")
        if self.validation is None:
            raise ValueError("MqttExpect: 'validation' field is required")
        if self.timeout_s <= 0:
            raise ValueError(f"MqttExpect: timeout_s must be > 0, got {self.timeout_s}")

        self.expression = compile_expression(
            self.validation, VARIABLES, "MqttExpect: 'validation'", MIGRATION_HINT
        )

    def execute(self) -> None:
        listener = mqtt_registry.get(self.session)
        matched, tally, early_verdict = self._collect(listener)
        actual = len(tally) if self.count_by is not None else len(matched)

        passed = early_verdict
        if passed is None:
            passed = self.expression.evaluate(self._variables(matched, tally, actual))

        # Set regardless of the verdict, so the HTML report can show both
        # sides of the assertion whether it passed or failed.
        self.validation_expected = self.validation
        self.validation_actual = f"count={actual}{self._unit_note(matched, tally)}"

        if not passed:
            raise ValueError(self._failure_message(listener, matched, actual, tally))

        LOGGER.info(
            "MqttExpect: session '%s' topic '%s' satisfied '%s' (count=%d%s)",
            self.session,
            self.topic,
            self.validation,
            actual,
            self._unit_note(matched, tally),
        )

    def _variables(self, matched: list, tally: dict, actual: int) -> dict[str, object]:
        """Bind this check's variables for the expression to be evaluated against."""
        return {
            "count": actual,
            "payloads": [message.payload for message in matched],
            "values": list(tally),
        }

    def _collect(self, listener) -> tuple[list, dict, bool | None]:
        """Read the session for `timeout_s`, counting in this check's unit.

        Returns the messages matching `topic`, the tally of `count_by` values
        (each mapped to how many times it arrived, in arrival order; empty when
        counting messages), and an early verdict if the count settled the
        result before the window ran out.
        """
        decision = self._early_decision()
        matched: list = []
        unmatched: list = []
        tally: dict = {}
        verdict: bool | None = None

        for message in listener.stream(duration_s=self.timeout_s):
            if not topic_matches_sub(self.topic, message.topic):
                unmatched.append(message)
                continue

            matched.append(message)
            for value in self._payload_values(message.payload):
                tally[value] = tally.get(value, 0) + 1

            # Tested once per message rather than once per counted item: one
            # publish is one delivery, and stopping half way through a batch
            # would report a count that no subscriber ever actually saw.
            if decision is None:
                continue
            early_verdict, crossed = decision
            actual = len(tally) if self.count_by is not None else len(matched)
            if crossed(actual):
                verdict = early_verdict
                break

        # Only requeued once this check is done reading - putting them back
        # while still draining the same stream() call would just hand them
        # straight back to this same loop, spinning for the rest of timeout_s.
        for message in unmatched:
            listener.requeue(message)

        return matched, tally, verdict

    def _payload_values(self, payload: str) -> list:
        """The `count_by` values one payload carries, or nothing when unset.

        A payload may be a single JSON object or an array of them: a device
        batching on its own schedule decides that, and this check deliberately
        does not care which it gets.

        A payload that cannot be counted raises rather than counting zero. A
        silent zero here is indistinguishable from "the device published
        nothing", which is the one conclusion a reader must never draw by
        mistake.
        """
        if self.count_by is None:
            return []

        try:
            document = json.loads(payload)
        except ValueError as error:
            raise ValueError(
                f"MqttExpect: counting by '{self.count_by}' needs JSON payloads, but a message on "
                f"'{self.topic}' was not JSON ({error}). Payload: {payload}"
            ) from None

        items = document if isinstance(document, list) else [document]
        return [self._item_value(item) for item in items]

    def _item_value(self, item) -> object:
        """One item's `count_by` value, rejecting anything uncountable."""
        if not isinstance(item, dict) or self.count_by not in item:
            raise ValueError(
                f"MqttExpect: a message on '{self.topic}' carried an item with no "
                f"'{self.count_by}' field, so it cannot be counted: {item!r}"
            )

        value = item[self.count_by]
        if isinstance(value, (dict, list)):
            raise ValueError(
                f"MqttExpect: '{self.count_by}' must be a single value to count distinct ones "
                f"of, but a message on '{self.topic}' carried {value!r}"
            )
        return value

    def _unit_note(self, matched: list, tally: dict) -> str:
        """What the count is a count *of*, when it isn't simply messages."""
        if self.count_by is None:
            return ""
        return f" distinct '{self.count_by}' across {len(matched)} message(s)"

    def _tally_detail(self, tally: dict) -> str:
        """A sentence about the counted values themselves, or "" if there is
        nothing worth saying.

        A monotonic counter (a journal `seq`) turns a bare shortfall into
        something locatable: the range that did arrive, and how many values are
        missing inside it - i.e. whether the device sent fewer samples or
        dropped some in the middle. Only attempted when every counted value is
        an integer, since a gap is meaningless otherwise.
        """
        if self.count_by is None or not tally:
            return ""

        notes = []
        numbers = [value for value in tally if isinstance(value, int) and not isinstance(value, bool)]
        if len(numbers) == len(tally):
            low, high = min(numbers), max(numbers)
            missing = (high - low + 1) - len(numbers)
            span = f"Counted '{self.count_by}' values span {low}..{high}"
            if missing > 0:
                span += f", with {missing} value(s) missing inside that range"
            notes.append(f"{span}.")

        redelivered = sum(tally.values()) - len(tally)
        if redelivered:
            notes.append(
                f"{redelivered} repeated value(s) were counted once each, as a QoS 1 "
                f"redelivery is entitled to arrive twice."
            )
        return " ".join(notes)

    def _failure_message(self, listener, matched: list, actual: int, tally: dict) -> str:
        """Explain a failed assertion without conflating two different counts.

        `matched` is only messages on `self.topic`, and only what this check
        counted before the verdict became certain - a simple `==`/`<=`/`<`
        count check stops as soon as it's overshot, so this can be far smaller than
        everything the session has actually buffered (e.g. other topics on
        the same session, or a long heartbeat window). Showing only `matched`
        would look right but hide where the discrepancy came from; showing
        only `listener.recent()` (every topic, uncounted) would contradict the
        count in the headline. So both are printed, each labeled with what it is.
        """
        counted = "\n".join(f"  {m.topic}  {_abbreviate(m.payload)}" for m in matched) or "  (none)"
        history = listener.recent()
        lines = [
            f"MqttExpect: session '{self.session}' topic '{self.topic}' failed "
            f"'{self.validation}': counted {actual}"
            f"{self._unit_note(matched, tally)} before the result was already decided "
            f"(budget was {self.timeout_s}s)."
        ]

        detail = self._tally_detail(tally)
        if detail:
            lines.append(detail)
        lines.append(f"Messages counted for this check:\n{counted}")

        if len(history) != len(matched):
            recent = "\n".join(f"  {m.topic}  {_abbreviate(m.payload)}" for m in history) or "  (none)"
            lines.append(
                f"\nFor context: this session has buffered {len(history)} message(s) in total "
                f"(any topic, up to the last 100), which can include ones on other topics carried "
                f"by the same session, or ones from before this check started or after it stopped "
                f"counting:\n{recent}"
            )
        return "\n".join(lines)
