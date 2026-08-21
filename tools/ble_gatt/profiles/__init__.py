"""GATT profile definitions for the peripheral role.

Each module here declares one profile's UUIDs, its value encoding, and the
`ServiceSpec`s that expose it - the parts specific to what is being simulated.
`peripheral.py` stays profile-agnostic and just serves whatever it is handed.
"""

from .heart_rate import (
    BODY_SENSOR_LOCATION_UUID,
    HEART_RATE_CONTROL_POINT_UUID,
    HEART_RATE_MEASUREMENT_UUID,
    HEART_RATE_SERVICE_UUID,
    Measurement,
    battery_service,
    decode_measurement,
    device_information_service,
    encode_measurement,
    heart_rate_service,
    rr_from_ms,
    rr_to_ms,
)

__all__ = [
    "BODY_SENSOR_LOCATION_UUID",
    "HEART_RATE_CONTROL_POINT_UUID",
    "HEART_RATE_MEASUREMENT_UUID",
    "HEART_RATE_SERVICE_UUID",
    "Measurement",
    "battery_service",
    "decode_measurement",
    "device_information_service",
    "encode_measurement",
    "heart_rate_service",
    "rr_from_ms",
    "rr_to_ms",
]
