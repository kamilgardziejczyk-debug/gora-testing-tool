"""Tests for the Heart Rate Service encoding in tools/ble_gatt/profiles.

Pure arithmetic over bytes, so none of this needs a Bluetooth adapter.

Run with `python3 -m unittest discover tests` from the repository root.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.ble_gatt.profiles.heart_rate import (  # noqa: E402
    FLAG_CONTACT_DETECTED,
    FLAG_CONTACT_SUPPORTED,
    FLAG_ENERGY_EXPENDED,
    FLAG_HR_16BIT,
    FLAG_RR_INTERVALS,
    Measurement,
    decode_measurement,
    encode_measurement,
    heart_rate_service,
    rr_from_ms,
    rr_to_ms,
)


class EncodeTests(unittest.TestCase):
    def test_minimal_frame_is_flags_plus_one_byte_pulse(self):
        self.assertEqual(encode_measurement(60, contact=None), bytes([0x00, 60]))

    def test_contact_sets_both_supported_and_detected_bits(self):
        """Reporting contact means setting two bits, not one."""
        detected = encode_measurement(60, contact=True)[0]
        self.assertEqual(detected & FLAG_CONTACT_SUPPORTED, FLAG_CONTACT_SUPPORTED)
        self.assertEqual(detected & FLAG_CONTACT_DETECTED, FLAG_CONTACT_DETECTED)

        absent = encode_measurement(60, contact=False)[0]
        self.assertEqual(absent & FLAG_CONTACT_SUPPORTED, FLAG_CONTACT_SUPPORTED)
        self.assertEqual(absent & FLAG_CONTACT_DETECTED, 0)

        self.assertEqual(encode_measurement(60, contact=None)[0] & FLAG_CONTACT_SUPPORTED, 0)

    def test_pulse_over_255_widens_to_16_bit_automatically(self):
        frame = encode_measurement(300, contact=None)
        self.assertEqual(frame[0] & FLAG_HR_16BIT, FLAG_HR_16BIT)
        self.assertEqual(frame, bytes([FLAG_HR_16BIT, 0x2C, 0x01]))

    def test_wide_can_be_forced_at_any_pulse(self):
        """A central's 16-bit path has to be reachable without a 256 bpm pulse."""
        frame = encode_measurement(60, contact=None, wide=True)
        self.assertEqual(frame, bytes([FLAG_HR_16BIT, 60, 0]))

    def test_rr_intervals_are_little_endian_after_the_pulse(self):
        frame = encode_measurement(60, [1024, 512], contact=None)
        self.assertEqual(frame[0] & FLAG_RR_INTERVALS, FLAG_RR_INTERVALS)
        self.assertEqual(frame[1:], bytes([60, 0x00, 0x04, 0x00, 0x02]))

    def test_energy_expended_precedes_rr_intervals(self):
        """Field order is fixed by the spec; a central parses positionally."""
        frame = encode_measurement(60, [1024], energy_expended=500, contact=None)
        self.assertEqual(frame[0] & FLAG_ENERGY_EXPENDED, FLAG_ENERGY_EXPENDED)
        self.assertEqual(frame, bytes([0x18, 60, 0xF4, 0x01, 0x00, 0x04]))

    def test_oversized_frames_are_allowed_on_purpose(self):
        """Producing more intervals than a central budgets for is the point."""
        frame = encode_measurement(60, [1024] * 20, contact=None)
        self.assertEqual(len(frame), 2 + 40)
        self.assertEqual(len(decode_measurement(frame).rr_intervals), 20)

    def test_out_of_range_values_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "bpm"):
            encode_measurement(70000)
        with self.assertRaisesRegex(ValueError, "RR interval"):
            encode_measurement(60, [70000])
        with self.assertRaisesRegex(ValueError, "energy expended"):
            encode_measurement(60, energy_expended=-1)


class DecodeTests(unittest.TestCase):
    def test_round_trip_preserves_every_field(self):
        original = Measurement(bpm=142, rr_intervals=(800, 812), energy_expended=42, contact=False)
        decoded = decode_measurement(
            encode_measurement(
                original.bpm, original.rr_intervals, original.energy_expended, original.contact
            )
        )
        self.assertEqual(decoded, original)

    def test_absent_contact_decodes_to_none_not_false(self):
        """None and False are different states: unsupported versus no skin."""
        self.assertIsNone(decode_measurement(encode_measurement(60, contact=None)).contact)
        self.assertIs(decode_measurement(encode_measurement(60, contact=False)).contact, False)

    def test_truncated_frame_names_the_field_it_ran_out_on(self):
        with self.assertRaisesRegex(ValueError, "truncated"):
            decode_measurement(bytes([FLAG_HR_16BIT, 60]))
        with self.assertRaisesRegex(ValueError, "empty"):
            decode_measurement(b"")

    def test_trailing_odd_byte_is_a_truncated_rr_interval(self):
        """A frame cut mid-interval must be reported, not silently dropped."""
        with self.assertRaisesRegex(ValueError, "RR interval"):
            decode_measurement(bytes([FLAG_RR_INTERVALS, 60, 0x00]))


class UnitTests(unittest.TestCase):
    def test_rr_units_are_1024ths_of_a_second_not_milliseconds(self):
        """The 2.4% difference is small enough to look plausible and wreck HRV."""
        self.assertEqual(rr_from_ms(1000), 1024)
        self.assertNotEqual(rr_from_ms(1000), 1000)
        self.assertAlmostEqual(rr_to_ms(1024), 1000.0)

    def test_round_trip_through_units_is_stable(self):
        for milliseconds in (400, 750, 1000, 1333):
            self.assertAlmostEqual(rr_to_ms(rr_from_ms(milliseconds)), milliseconds, delta=1.0)


class ServiceTests(unittest.TestCase):
    def test_measurement_is_declared_last_in_the_service(self):
        """A central discovering descriptors from the value handle to the end
        of the service must find its own CCCD, not another characteristic's."""
        uuids = [char.uuid for char in heart_rate_service().characteristics]
        self.assertEqual(uuids[-1], "2a37")

    def test_unknown_body_location_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unknown body sensor location"):
            heart_rate_service(location="elbow")


if __name__ == "__main__":
    unittest.main()
