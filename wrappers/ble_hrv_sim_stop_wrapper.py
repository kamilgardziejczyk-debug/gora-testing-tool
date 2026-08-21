"""!BleHrvSimStop - stop a running heart rate sensor simulation."""

import logging

import yaml

from . import ble_hrv_registry
from .expression import Expression, compile_expression
from .wrapper import Wrapper


LOGGER = logging.getLogger(__name__)

# Variables a `validation` expression may use, and what they hold. Read once,
# at the moment the sensor stops.
VARIABLES = ("subscribed", "notifications", "rr_intervals", "bpm", "writes")


class BleHrvSimStopWrapper(Wrapper):
    """Stops a sensor started by !BleHrvSimStart, optionally asserting on it.

    Optional in a scenario: the runner stops any session still running when the
    scenario ends. Use it to free the adapter partway through, or - with
    `validation` - to state what the sensor should have managed to send, which
    is the number a later check of what the DUT recorded compares against.

    The expression, if given, is evaluated once against:

    *   `{subscribed}` - bool, whether a central was still subscribed at the end
    *   `{notifications}` - int, measurements actually sent
    *   `{rr_intervals}` - int, RR intervals actually sent
    *   `{bpm}` - int, the sensor's final pulse
    *   `{writes}` - int, writes the central made to the control point

    `{rr_intervals}` is the useful one: a central logging one row per RR
    interval should have exactly this many rows, so it turns a vague "did it
    record anything" into an exact count.
    """

    def __init__(self, command_node: yaml.MappingNode):
        self.command_node = command_node
        self.name: str | None = None
        self.session: str | None = None
        self.validation: str | None = None
        self.expression: Expression | None = None

    def parse(self) -> None:
        """Read the fields, compiling `validation` only if one was given."""
        tag_name = self.command_node.tag.lstrip("!").rstrip(":")
        if tag_name != "BleHrvSimStop":
            raise ValueError("Expected !BleHrvSimStop command")

        for key_node, value_node in self.command_node.value:
            if not isinstance(key_node, yaml.ScalarNode) or not isinstance(value_node, yaml.ScalarNode):
                continue
            key = key_node.value
            if key == "name":
                self.name = value_node.value
            elif key == "session":
                self.session = value_node.value
            elif key == "validation":
                self.validation = value_node.value

        if self.session is None:
            raise ValueError("BleHrvSimStop: 'session' field is required")
        if self.validation is not None:
            self.expression = compile_expression(
                self.validation, VARIABLES, "BleHrvSimStop: 'validation'"
            )

        LOGGER.info(
            "Parsed BleHrvSimStop: name=%s, session=%s, validation=%s",
            self.name, self.session, self.validation,
        )

    def execute(self) -> None:
        """Read the final statistics, stop the sensor, then assert on them."""
        simulator = ble_hrv_registry.pop(self.session)
        if simulator is None:
            # Warn rather than raise: a leftover stop from a commented-out
            # start should not abort a scenario, and the runner closes any
            # session that really is still running anyway.
            LOGGER.warning(
                "BleHrvSimStop: no running BLE HRV session named '%s', nothing to do", self.session
            )
            return

        # Read the statistics before stopping: `subscribed` is false the moment
        # the peripheral stops advertising, so asking afterwards would report
        # that no central was ever there.
        variables = self._variables(simulator)
        simulator.close()
        LOGGER.info("Stopped BLE HRV session '%s': %s", self.session, variables)

        if self.expression is None:
            return

        self.validation_expected = self.validation
        self.validation_actual = ", ".join(f"{name}={value}" for name, value in variables.items())
        if not self.expression.evaluate(variables):
            raise AssertionError(
                f"BleHrvSimStop [{self.session}]: '{self.validation}' is not satisfied: "
                f"{self.validation_actual}"
            )

    def _variables(self, simulator) -> dict:
        """Bind the final statistics for the expression to be evaluated against."""
        stats = simulator.stats
        return {
            "subscribed": stats.subscribed,
            "notifications": stats.notifications,
            "rr_intervals": stats.rr_intervals,
            "bpm": stats.bpm,
            "writes": len(simulator.control_point_writes()),
        }
