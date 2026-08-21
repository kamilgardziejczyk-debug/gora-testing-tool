"""Tests for the beat generator and advertising budget in tools/ble_gatt.

Both are pure arithmetic - the signal owns its own random source and the
advertising check is byte counting - so neither needs a Bluetooth adapter.

Run with `python3 -m unittest discover tests` from the repository root.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.ble_gatt.hrv import HrvSignal, rmssd  # noqa: E402
from tools.ble_gatt.peripheral import (  # noqa: E402
    AD_MAX_BYTES,
    AdvertisingDataTooLarge,
    CharacteristicSpec,
    ServiceSpec,
    advertising_data_size,
    advertising_uuid,
    check_advertising_data,
)
from tools.ble_gatt.profiles.heart_rate import rr_to_ms  # noqa: E402


def beats_over(signal: HrvSignal, seconds: float, step_s: float = 1.0) -> list:
    """Collect every RR interval the signal produces over `seconds`."""
    intervals = []
    for _ in range(int(seconds / step_s)):
        intervals.extend(signal.advance(step_s)[1])
    return intervals


class SignalTests(unittest.TestCase):
    def test_beat_count_matches_the_configured_rate(self):
        """Fractional beats carry across windows instead of being dropped."""
        signal = HrvSignal(bpm=60, jitter_ms=0, seed=1)
        self.assertEqual(len(beats_over(signal, 60)), 60)

    def test_a_rate_that_does_not_divide_the_cadence_still_averages_out(self):
        """At 40 bpm a 1s window holds a beat only two thirds of the time."""
        signal = HrvSignal(bpm=40, jitter_ms=0, seed=1)
        self.assertEqual(len(beats_over(signal, 60)), 40)

    def test_a_faster_rate_puts_several_beats_in_one_window(self):
        signal = HrvSignal(bpm=180, jitter_ms=0, seed=1)
        _, intervals = signal.advance(1.0)
        self.assertEqual(len(intervals), 3)

    def test_jitter_is_what_produces_variability(self):
        """Without jitter every interval is identical and HRV is zero."""
        flat = [rr_to_ms(i) for i in beats_over(HrvSignal(bpm=60, jitter_ms=0, seed=1), 30)]
        self.assertEqual(rmssd(flat), 0.0)

        varied = [rr_to_ms(i) for i in beats_over(HrvSignal(bpm=60, jitter_ms=25, seed=1), 30)]
        self.assertGreater(rmssd(varied), 10.0)

    def test_the_same_seed_replays_the_same_beats(self):
        self.assertEqual(
            beats_over(HrvSignal(bpm=72, jitter_ms=30, seed=99), 20),
            beats_over(HrvSignal(bpm=72, jitter_ms=30, seed=99), 20),
        )

    def test_two_signals_do_not_share_a_random_source(self):
        """Per-instance randomness: one simulator must not perturb another."""
        first = HrvSignal(bpm=72, jitter_ms=30, seed=5)
        alone = beats_over(HrvSignal(bpm=72, jitter_ms=30, seed=5), 20)
        HrvSignal(bpm=99, jitter_ms=30, seed=7).advance(10.0)
        self.assertEqual(beats_over(first, 20), alone)

    def test_drift_moves_the_pulse_at_the_configured_rate(self):
        signal = HrvSignal(bpm=60, jitter_ms=0, drift_bpm_per_min=30, seed=1)
        beats_over(signal, 60)
        self.assertAlmostEqual(signal.bpm, 90.0, places=3)

    def test_drift_is_clamped_to_a_believable_pulse(self):
        signal = HrvSignal(bpm=200, jitter_ms=0, drift_bpm_per_min=600, seed=1)
        beats_over(signal, 60)
        self.assertLessEqual(signal.bpm, 250.0)

    def test_an_impossible_pulse_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "bpm must be between"):
            HrvSignal(bpm=5)
        with self.assertRaisesRegex(ValueError, "bpm must be between"):
            HrvSignal(bpm=60).set_bpm(400)

    def test_rmssd_is_zero_rather_than_undefined_for_one_interval(self):
        self.assertEqual(rmssd([]), 0.0)
        self.assertEqual(rmssd([800.0]), 0.0)


class AdvertisingBudgetTests(unittest.TestCase):
    def test_a_base_range_uuid_advertises_in_16_bit_form(self):
        """A central matching a standard service reads the 16-bit list only."""
        self.assertEqual(advertising_uuid("180d"), "180d")
        self.assertEqual(advertising_uuid("0000180d-0000-1000-8000-00805f9b34fb"), "180d")

    def test_a_vendor_uuid_stays_128_bit(self):
        vendor = "0000ffe0-1234-1000-8000-00805f9b34fb"
        self.assertEqual(advertising_uuid(vendor), vendor)

    def test_the_128_bit_form_costs_14_bytes_more(self):
        vendor = "0000ffe0-1234-1000-8000-00805f9b34fb"
        self.assertEqual(
            advertising_data_size("", [vendor]) - advertising_data_size("", ["180d"]), 14
        )

    def test_the_name_budget_is_22_characters_with_one_16_bit_service(self):
        check_advertising_data("X" * 22, ["180d"])
        with self.assertRaisesRegex(AdvertisingDataTooLarge, "at most 22"):
            check_advertising_data("X" * 23, ["180d"])

    def test_the_budget_is_measured_in_bytes_not_characters(self):
        """A multi-byte name overflows sooner than its length suggests."""
        self.assertGreater(advertising_data_size("é" * 11, ["180d"]), advertising_data_size("X" * 11, ["180d"]))

    def test_every_extra_advertised_service_costs_name_characters(self):
        with_one = advertising_data_size("", ["180d"])
        with_two = advertising_data_size("", ["180d", "180f"])
        self.assertEqual(with_two - with_one, 2)
        self.assertLessEqual(with_two, AD_MAX_BYTES)


class PeripheralConstructionTests(unittest.TestCase):
    """Construction only - starting one needs an adapter."""

    def service(self, uuid: str = "180d") -> ServiceSpec:
        return ServiceSpec(uuid=uuid, characteristics=(CharacteristicSpec("2a37", ("notify",)),))

    def test_an_unknown_characteristic_flag_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unknown characteristic flag"):
            CharacteristicSpec("2a37", ("notifyy",))

    def test_advertising_a_service_that_is_not_served_is_rejected(self):
        from tools.ble_gatt.peripheral import BlePeripheral

        with self.assertRaisesRegex(ValueError, "does not expose them"):
            BlePeripheral("name", [self.service("180f")], advertise=["180d"])

    def test_a_peripheral_needs_at_least_one_service(self):
        from tools.ble_gatt.peripheral import BlePeripheral

        with self.assertRaisesRegex(ValueError, "at least one service"):
            BlePeripheral("name", [])

    def test_an_over_budget_name_fails_at_construction_not_at_start(self):
        """The consequence of overflow is a silent non-match, so it fails early."""
        from tools.ble_gatt.peripheral import BlePeripheral

        with self.assertRaises(AdvertisingDataTooLarge):
            BlePeripheral("X" * 30, [self.service()])


if __name__ == "__main__":
    unittest.main()
