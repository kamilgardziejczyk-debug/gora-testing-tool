"""Tests for the !BleHrvSimStart, !BleHrvSimSet and !BleHrvSimStop tags.

Parse-time checks need nothing. The execute paths run against a stub standing
in for a live `HeartRateSimulator`, so they exercise the wrappers' own logic -
action dispatch, variable binding, assertion, registry lifecycle - without an
adapter or a radio anywhere near them.

Run with `python3 -m unittest discover tests` from the repository root.
"""

import sys
import unittest
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.ble_gatt.hr_simulator import SimulatorStats  # noqa: E402
from wrappers import ble_hrv_registry  # noqa: E402
from wrappers.ble_hrv_sim_set_wrapper import BleHrvSimSetWrapper  # noqa: E402
from wrappers.ble_hrv_sim_start_wrapper import BleHrvSimStartWrapper  # noqa: E402
from wrappers.ble_hrv_sim_stop_wrapper import BleHrvSimStopWrapper  # noqa: E402


class StubSimulator:
    """Records what a wrapper asked of it, and reports whatever it is told to."""

    def __init__(self, subscribed=True, notifications=0, rr_intervals=0, bpm=60):
        self.calls = []
        self.closed = False
        self.writes = []
        self._stats = SimulatorStats(notifications, rr_intervals, subscribed, bpm)
        self.burst_succeeds = True

    @property
    def stats(self) -> SimulatorStats:
        self.calls.append(("stats", None))
        return self._stats

    def set_stats(self, **fields) -> None:
        self._stats = SimulatorStats(**{**self._stats.__dict__, **fields})

    def control_point_writes(self):
        return list(self.writes)

    def set_bpm(self, bpm):
        self.calls.append(("set_bpm", bpm))

    def set_contact(self, contact):
        self.calls.append(("set_contact", contact))

    def set_battery(self, level):
        self.calls.append(("set_battery", level))

    def set_energy(self, kilojoules):
        self.calls.append(("set_energy", kilojoules))

    def send_burst(self, count):
        self.calls.append(("send_burst", count))
        return self.burst_succeeds

    def stall(self, seconds):
        self.calls.append(("stall", seconds))

    def resume(self):
        self.calls.append(("resume", None))

    def bounce(self):
        self.calls.append(("bounce", None))

    def close(self):
        self.closed = True
        self.calls.append(("close", None))


def build(wrapper_class, text: str):
    """Parse one command of `wrapper_class` from YAML."""
    wrapper = wrapper_class(yaml.compose(text))
    wrapper.parse()
    return wrapper


class RegistryTestCase(unittest.TestCase):
    """Keeps the module-level registry clean between tests."""

    def setUp(self):
        ble_hrv_registry.close_all()
        self.addCleanup(ble_hrv_registry.close_all)

    def register(self, name: str = "hrv", **stats) -> StubSimulator:
        simulator = StubSimulator(**stats)
        ble_hrv_registry.register(name, simulator)
        return simulator


class StartParseTests(unittest.TestCase):
    def test_session_and_device_are_both_required(self):
        with self.assertRaisesRegex(ValueError, "'session' field is required"):
            build(BleHrvSimStartWrapper, "!BleHrvSimStart\ndevice: GoraHRV_01")
        with self.assertRaisesRegex(ValueError, "'device' field is required"):
            build(BleHrvSimStartWrapper, "!BleHrvSimStart\nsession: hrv")

    def test_defaults_are_filled_in(self):
        wrapper = build(BleHrvSimStartWrapper, "!BleHrvSimStart\nsession: hrv\ndevice: GoraHRV_01")
        self.assertEqual(wrapper.bpm, 60.0)
        self.assertEqual(wrapper.interval_s, 1.0)
        self.assertIs(wrapper.contact, True)

    def test_contact_is_three_state_not_boolean(self):
        for token, expected in (("yes", True), ("no", False), ("none", None)):
            wrapper = build(
                BleHrvSimStartWrapper,
                f"!BleHrvSimStart\nsession: hrv\ndevice: D\ncontact: {token}",
            )
            self.assertIs(wrapper.contact, expected)

    def test_bad_values_are_rejected_while_loading(self):
        for field, message in (
            ("location: elbow", "unknown location"),
            ("interval_s: 0", "must be positive"),
            ("battery_pct: 120", "between 0 and 100"),
            ("contact: maybe", "'contact' must be one of"),
        ):
            with self.assertRaisesRegex(ValueError, message):
                build(BleHrvSimStartWrapper, f"!BleHrvSimStart\nsession: hrv\ndevice: D\n{field}")

    def test_an_over_budget_device_name_is_caught_before_the_radio(self):
        """The peripheral checks this in its constructor, which execute() hits."""
        from tools.ble_gatt.peripheral import AdvertisingDataTooLarge

        wrapper = build(
            BleHrvSimStartWrapper,
            f"!BleHrvSimStart\nsession: hrv\ndevice: {'X' * 30}",
        )
        with self.assertRaises(AdvertisingDataTooLarge):
            wrapper.execute()


class SetParseTests(unittest.TestCase):
    def test_actions_must_be_present_and_non_empty(self):
        with self.assertRaisesRegex(ValueError, "non-empty list"):
            build(BleHrvSimSetWrapper, "!BleHrvSimSet\nsession: hrv")

    def test_an_unknown_verb_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "must have one of"):
            build(BleHrvSimSetWrapper, "!BleHrvSimSet\nsession: hrv\nactions:\n  - pulse: 90")

    def test_a_non_numeric_argument_names_the_action(self):
        with self.assertRaisesRegex(ValueError, "action #2 'bpm' needs a number"):
            build(
                BleHrvSimSetWrapper,
                "!BleHrvSimSet\nsession: hrv\nactions:\n  - bpm: 90\n  - bpm: fast",
            )

    def test_a_bare_verb_needing_an_argument_says_so(self):
        with self.assertRaisesRegex(ValueError, "needs an argument"):
            build(BleHrvSimSetWrapper, "!BleHrvSimSet\nsession: hrv\nactions:\n  - bpm")

    def test_an_unknown_bare_verb_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "is not a known action"):
            build(BleHrvSimSetWrapper, "!BleHrvSimSet\nsession: hrv\nactions:\n  - explode")

    def test_energy_accepts_off_as_well_as_a_number(self):
        wrapper = build(
            BleHrvSimSetWrapper,
            "!BleHrvSimSet\nsession: hrv\nactions:\n  - energy: off\n  - energy: 500",
        )
        self.assertIsNone(wrapper.actions[0].number)
        self.assertEqual(wrapper.actions[1].number, 500.0)


class SetExecuteTests(RegistryTestCase):
    def test_actions_run_in_order_with_their_arguments(self):
        simulator = self.register()
        build(
            BleHrvSimSetWrapper,
            "!BleHrvSimSet\nsession: hrv\nactions:\n"
            "  - bpm: 120\n  - contact: none\n  - burst: 12\n  - stall: 5\n  - resume\n  - bounce",
        ).execute()

        self.assertEqual(
            [call for call in simulator.calls if call[0] != "stats"],
            [
                ("set_bpm", 120.0),
                ("set_contact", None),
                ("send_burst", 12),
                ("stall", 5.0),
                ("resume", None),
                ("bounce", None),
            ],
        )

    def test_energy_off_clears_the_field_rather_than_setting_zero(self):
        """0 kJ is a reported value; off means the field is absent entirely."""
        simulator = self.register()
        build(BleHrvSimSetWrapper, "!BleHrvSimSet\nsession: hrv\nactions:\n  - energy: off").execute()
        self.assertIn(("set_energy", None), simulator.calls)

    def test_an_unknown_session_names_the_tag_that_opens_one(self):
        with self.assertRaisesRegex(ValueError, "BleHrvSimStart"):
            build(BleHrvSimSetWrapper, "!BleHrvSimSet\nsession: nope\nactions:\n  - bpm: 90").execute()


class StopTests(RegistryTestCase):
    def test_it_stops_and_unregisters_the_session(self):
        simulator = self.register()
        build(BleHrvSimStopWrapper, "!BleHrvSimStop\nsession: hrv").execute()
        self.assertTrue(simulator.closed)
        self.assertIsNone(ble_hrv_registry.pop("hrv"))

    def test_stopping_a_session_that_was_never_started_only_warns(self):
        """A leftover stop from a commented-out start must not abort a run."""
        build(BleHrvSimStopWrapper, "!BleHrvSimStop\nsession: nope").execute()

    def test_statistics_are_read_before_the_sensor_stops(self):
        """Asking after stopping would report that no central was ever there."""
        self.register(subscribed=True, notifications=30, rr_intervals=31)
        wrapper = build(
            BleHrvSimStopWrapper,
            "!BleHrvSimStop\nsession: hrv\nvalidation: '{subscribed} and {rr_intervals} == 31'",
        )
        wrapper.execute()
        self.assertIn("rr_intervals=31", wrapper.validation_actual)

    def test_a_failed_validation_still_stops_the_sensor(self):
        """A failed assertion must not leave the adapter advertising."""
        simulator = self.register(notifications=0)
        wrapper = build(
            BleHrvSimStopWrapper,
            "!BleHrvSimStop\nsession: hrv\nvalidation: '{notifications} > 10'",
        )
        with self.assertRaisesRegex(AssertionError, "is not satisfied"):
            wrapper.execute()
        self.assertTrue(simulator.closed)

    def test_validation_is_optional(self):
        self.register()
        wrapper = build(BleHrvSimStopWrapper, "!BleHrvSimStop\nsession: hrv")
        wrapper.execute()
        self.assertIsNone(wrapper.validation_expected)


if __name__ == "__main__":
    unittest.main()
