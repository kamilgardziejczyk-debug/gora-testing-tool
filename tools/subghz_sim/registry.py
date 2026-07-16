"""Thread-safe registry of simulated sensors, keyed by auto-incrementing ID."""

from __future__ import annotations

import threading
from typing import Dict, List

from sensors import SENSOR_TYPES, Sensor


class UnknownSensorType(ValueError):
    pass


class UnknownSensorId(KeyError):
    pass


class Registry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sensors: Dict[int, Sensor] = {}
        self._next_id = 1

    def add(self, type_name: str) -> Sensor:
        cls = SENSOR_TYPES.get(type_name)
        if cls is None:
            raise UnknownSensorType(type_name)

        with self._lock:
            sensor_id = self._next_id
            self._next_id += 1
            sensor = cls(sensor_id)
            self._sensors[sensor_id] = sensor
            return sensor

    def remove(self, sensor_id: int) -> None:
        with self._lock:
            if sensor_id not in self._sensors:
                raise UnknownSensorId(sensor_id)
            del self._sensors[sensor_id]

    def get(self, sensor_id: int) -> Sensor:
        with self._lock:
            sensor = self._sensors.get(sensor_id)
            if sensor is None:
                raise UnknownSensorId(sensor_id)
            return sensor

    def list(self) -> List[Sensor]:
        with self._lock:
            return [self._sensors[k] for k in sorted(self._sensors)]

    def tick_all(self) -> List[Sensor]:
        """Advance every sensor's simulated state and return the current sensor list."""
        with self._lock:
            sensors = list(self._sensors.values())
        for sensor in sensors:
            sensor.tick()
        return sensors
