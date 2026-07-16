"""Simulated sensor state and behavior."""

from __future__ import annotations

import random
from typing import Optional

import frame

TEMP_MIN_C = -10.0
TEMP_MAX_C = 45.0
TEMP_STEP_MAX_C = 0.3

HUMIDITY_MIN_PCT = 0
HUMIDITY_MAX_PCT = 100
HUMIDITY_STEP_MAX_PCT = 2.0


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


class Sensor:
    """Base for all simulated sensors; sensor_id is assigned by the registry."""

    type_code: int
    type_name: str

    def __init__(self, sensor_id: int, online: bool = True):
        self.sensor_id = sensor_id
        self.online = online

    def tick(self) -> None:
        """Advance simulated state by one heartbeat interval. No-op by default."""

    def to_frame(self) -> bytes:
        raise NotImplementedError

    def describe(self) -> str:
        raise NotImplementedError


class _AlarmSensor(Sensor):
    """Shared behavior for the heat/smoke/CO alarm types (online + alarm_active only)."""

    def __init__(self, sensor_id: int, online: bool = True, alarm: bool = False):
        super().__init__(sensor_id, online)
        self.alarm = alarm

    def to_frame(self) -> bytes:
        return frame.build_frame(self.type_code, self.sensor_id, self.online, self.alarm)

    def describe(self) -> str:
        return f"#{self.sensor_id} {self.type_name} online={self.online} alarm={self.alarm}"


class HeatAlarm(_AlarmSensor):
    type_code = frame.TYPE_HEAT_ALARM
    type_name = "heat"


class SmokeAlarm(_AlarmSensor):
    type_code = frame.TYPE_SMOKE_ALARM
    type_name = "smoke"


class COAlarm(_AlarmSensor):
    type_code = frame.TYPE_CO_ALARM
    type_name = "co"


class TempHum(Sensor):
    type_code = frame.TYPE_TEMP_HUM
    type_name = "temp_hum"

    def __init__(self, sensor_id: int, online: bool = True):
        super().__init__(sensor_id, online)
        self.temp_c = round(random.uniform(15.0, 28.0), 1)
        self.humidity_pct = random.randint(30, 60)
        self.manual_override = False

    def set_values(self, temp_c: Optional[float] = None,
                    humidity_pct: Optional[float] = None) -> None:
        if temp_c is not None:
            self.temp_c = round(temp_c, 1)
        if humidity_pct is not None:
            self.humidity_pct = round(humidity_pct)
        self.manual_override = True

    def tick(self) -> None:
        if self.manual_override:
            return
        self.temp_c = round(_clamp(
            self.temp_c + random.uniform(-TEMP_STEP_MAX_C, TEMP_STEP_MAX_C),
            TEMP_MIN_C, TEMP_MAX_C), 1)
        self.humidity_pct = round(_clamp(
            self.humidity_pct + random.uniform(-HUMIDITY_STEP_MAX_PCT, HUMIDITY_STEP_MAX_PCT),
            HUMIDITY_MIN_PCT, HUMIDITY_MAX_PCT))

    def to_frame(self) -> bytes:
        payload = frame.encode_temp_hum_payload(self.temp_c, self.humidity_pct)
        return frame.build_frame(self.type_code, self.sensor_id, self.online, False, payload)

    def describe(self) -> str:
        tag = " (manual)" if self.manual_override else ""
        return (f"#{self.sensor_id} {self.type_name} online={self.online} "
                f"temp_c={self.temp_c:.1f} humidity={self.humidity_pct}{tag}")


SENSOR_TYPES = {
    "heat": HeatAlarm,
    "smoke": SmokeAlarm,
    "co": COAlarm,
    "temp_hum": TempHum,
    "temp": TempHum,  # convenience alias
}
