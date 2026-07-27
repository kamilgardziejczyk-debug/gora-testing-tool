"""Sub-GHz sensor simulator.

Runnable from the command line (see `subghz_sim.py`) and importable as an API,
which is how the !SubghzSim wrapper drives it.
"""

from .registry import UnknownSensorId, UnknownSensorType
from .sensors import SENSOR_TYPES, Sensor, apply_fields, parse_field_pairs
from .simulator import DEFAULT_BAUD, DEFAULT_INTERVAL_S, SubghzSimulator

__all__ = [
    "DEFAULT_BAUD",
    "DEFAULT_INTERVAL_S",
    "SENSOR_TYPES",
    "Sensor",
    "SubghzSimulator",
    "UnknownSensorId",
    "UnknownSensorType",
    "apply_fields",
    "parse_field_pairs",
]
