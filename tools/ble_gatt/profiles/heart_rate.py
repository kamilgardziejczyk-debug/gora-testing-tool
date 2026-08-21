"""The Bluetooth SIG Heart Rate Service, as a simulated peripheral exposes it.

Covers the three characteristics of service 0x180D plus the Battery and Device
Information services a heart rate sensor is normally expected to carry, and the
encoding of the Heart Rate Measurement characteristic (0x2A37) that carries both
the pulse and, for HRV, the RR intervals between beats.

Nothing here clamps a value to what a well-behaved sensor would send. Producing
an over-long measurement, or more RR intervals than a central is likely to
parse, is the point of a simulator - the profile encodes what it is told and
leaves judging it to the caller.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence, Tuple

from ..peripheral import CharacteristicSpec, ServiceSpec

HEART_RATE_SERVICE_UUID = "180d"
HEART_RATE_MEASUREMENT_UUID = "2a37"
BODY_SENSOR_LOCATION_UUID = "2a38"
HEART_RATE_CONTROL_POINT_UUID = "2a39"

BATTERY_SERVICE_UUID = "180f"
BATTERY_LEVEL_UUID = "2a19"

DEVICE_INFORMATION_SERVICE_UUID = "180a"
MANUFACTURER_NAME_UUID = "2a29"
MODEL_NUMBER_UUID = "2a24"

# Flags byte of 0x2A37, bit by bit.
FLAG_HR_16BIT = 0x01
FLAG_CONTACT_DETECTED = 0x02
FLAG_CONTACT_SUPPORTED = 0x04
FLAG_ENERGY_EXPENDED = 0x08
FLAG_RR_INTERVALS = 0x10

# The spec's own unit for an RR interval: 1/1024 of a second, not milliseconds.
# Reporting milliseconds by mistake overstates every interval by ~2.4%, which
# is small enough to look plausible in a log and wreck an HRV calculation.
RR_UNITS_PER_SECOND = 1024

MAX_UINT8 = 0xFF
MAX_UINT16 = 0xFFFF

# Body sensor locations, per the spec's enumeration.
BODY_SENSOR_LOCATIONS = {
    "other": 0,
    "chest": 1,
    "wrist": 2,
    "finger": 3,
    "hand": 4,
    "ear-lobe": 5,
    "foot": 6,
}
DEFAULT_BODY_SENSOR_LOCATION = "chest"


def rr_from_ms(milliseconds: float) -> int:
    """Convert an RR interval in milliseconds to the characteristic's units."""
    return round(milliseconds * RR_UNITS_PER_SECOND / 1000)


def rr_to_ms(units: int) -> float:
    """Convert an RR interval from the characteristic's units to milliseconds."""
    return units * 1000 / RR_UNITS_PER_SECOND


@dataclass(frozen=True)
class Measurement:
    """A decoded Heart Rate Measurement.

    `contact` follows the spec's two flag bits: `None` means the sensor does
    not report contact at all, `True`/`False` that it does and what it found.
    """

    bpm: int
    rr_intervals: Tuple[int, ...] = ()
    energy_expended: Optional[int] = None
    contact: Optional[bool] = True

    def describe(self) -> str:
        """One line naming the pulse, contact state and RR intervals."""
        contact = "n/a" if self.contact is None else ("yes" if self.contact else "no")
        rr = ", ".join(f"{value} ({rr_to_ms(value):.0f}ms)" for value in self.rr_intervals)
        energy = "" if self.energy_expended is None else f"  energy={self.energy_expended}kJ"
        return f"{self.bpm} bpm  contact={contact}{energy}  rr=[{rr or '-'}]"


def encode_measurement(
    bpm: int,
    rr_intervals: Sequence[int] = (),
    energy_expended: Optional[int] = None,
    contact: Optional[bool] = True,
    wide: bool = False,
) -> bytes:
    """Encode characteristic 0x2A37 from a pulse and its RR intervals.

    `rr_intervals` are in 1/1024 s units - see `rr_from_ms`. `wide` forces the
    16-bit pulse format, which is otherwise chosen only when `bpm` needs it, so
    a scenario can exercise a central's handling of both formats at any rate.

    Raises `ValueError` for a value no encoding of this characteristic can
    carry, so a malformed frame is rejected before it reaches the radio.
    """
    use_wide = wide or bpm > MAX_UINT8
    _check_range("bpm", bpm, MAX_UINT16 if use_wide else MAX_UINT8)
    for interval in rr_intervals:
        _check_range("RR interval", interval, MAX_UINT16)
    if energy_expended is not None:
        _check_range("energy expended", energy_expended, MAX_UINT16)

    flags = FLAG_HR_16BIT if use_wide else 0x00
    if contact is not None:
        flags |= FLAG_CONTACT_SUPPORTED
        if contact:
            flags |= FLAG_CONTACT_DETECTED
    if energy_expended is not None:
        flags |= FLAG_ENERGY_EXPENDED
    if rr_intervals:
        flags |= FLAG_RR_INTERVALS

    data = bytearray([flags])
    data += bpm.to_bytes(2 if use_wide else 1, "little")
    if energy_expended is not None:
        data += energy_expended.to_bytes(2, "little")
    for interval in rr_intervals:
        data += interval.to_bytes(2, "little")
    return bytes(data)


def decode_measurement(data: bytes) -> Measurement:
    """Decode characteristic 0x2A37, the inverse of `encode_measurement`.

    Raises `ValueError` if the frame is shorter than its own flags claim,
    which is exactly the case a central has to survive and so worth being able
    to construct and assert on.
    """
    if not data:
        raise ValueError("heart rate measurement is empty")

    flags = data[0]
    wide = bool(flags & FLAG_HR_16BIT)
    offset = 1

    bpm, offset = _take_int(data, offset, 2 if wide else 1, "pulse")
    energy_expended = None
    if flags & FLAG_ENERGY_EXPENDED:
        energy_expended, offset = _take_int(data, offset, 2, "energy expended")

    rr_intervals = []
    if flags & FLAG_RR_INTERVALS:
        while offset < len(data):
            interval, offset = _take_int(data, offset, 2, "RR interval")
            rr_intervals.append(interval)

    contact: Optional[bool] = None
    if flags & FLAG_CONTACT_SUPPORTED:
        contact = bool(flags & FLAG_CONTACT_DETECTED)

    return Measurement(
        bpm=bpm,
        rr_intervals=tuple(rr_intervals),
        energy_expended=energy_expended,
        contact=contact,
    )


def heart_rate_service(location: str = DEFAULT_BODY_SENSOR_LOCATION) -> ServiceSpec:
    """The 0x180D service: measurement, body sensor location, control point.

    The measurement characteristic is declared last so that a central
    discovering descriptors from its value handle to the end of the service
    still finds its own CCCD first.
    """
    if location not in BODY_SENSOR_LOCATIONS:
        raise ValueError(
            f"unknown body sensor location '{location}' "
            f"(expected one of {', '.join(sorted(BODY_SENSOR_LOCATIONS))})"
        )

    return ServiceSpec(
        uuid=HEART_RATE_SERVICE_UUID,
        characteristics=(
            CharacteristicSpec(
                uuid=BODY_SENSOR_LOCATION_UUID,
                properties=("read",),
                initial_value=bytes([BODY_SENSOR_LOCATIONS[location]]),
            ),
            CharacteristicSpec(
                uuid=HEART_RATE_CONTROL_POINT_UUID,
                properties=("write",),
            ),
            CharacteristicSpec(
                uuid=HEART_RATE_MEASUREMENT_UUID,
                properties=("notify",),
            ),
        ),
    )


def battery_service(level_pct: int = 100) -> ServiceSpec:
    """The 0x180F service with a readable, notifiable battery level."""
    _check_range("battery level", level_pct, 100)
    return ServiceSpec(
        uuid=BATTERY_SERVICE_UUID,
        characteristics=(
            CharacteristicSpec(
                uuid=BATTERY_LEVEL_UUID,
                properties=("read", "notify"),
                initial_value=bytes([level_pct]),
            ),
        ),
    )


def device_information_service(manufacturer: str = "Gora", model: str = "HRV-SIM") -> ServiceSpec:
    """The 0x180A service, carrying the strings a central may read on connect."""
    return ServiceSpec(
        uuid=DEVICE_INFORMATION_SERVICE_UUID,
        characteristics=(
            CharacteristicSpec(
                uuid=MANUFACTURER_NAME_UUID,
                properties=("read",),
                initial_value=manufacturer.encode("utf-8"),
            ),
            CharacteristicSpec(
                uuid=MODEL_NUMBER_UUID,
                properties=("read",),
                initial_value=model.encode("utf-8"),
            ),
        ),
    )


def _check_range(label: str, value: int, maximum: int) -> None:
    """Reject a field value the characteristic has no encoding for."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{label} must be an integer, got {value!r}")
    if value < 0 or value > maximum:
        raise ValueError(f"{label} must be between 0 and {maximum}, got {value}")


def _take_int(data: bytes, offset: int, width: int, label: str) -> Tuple[int, int]:
    """Read a little-endian integer, reporting the frame that was too short."""
    if offset + width > len(data):
        raise ValueError(
            f"heart rate measurement is truncated: needs {offset + width} bytes for its "
            f"{label} but has {len(data)}"
        )
    return int.from_bytes(data[offset : offset + width], "little"), offset + width
