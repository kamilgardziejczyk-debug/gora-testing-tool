"""Simulated sensor state and behavior."""

from __future__ import annotations

import random
from typing import Iterable, List, Optional, Sequence, Tuple

from . import frame

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

STATUS_VALUES = {"online": True, "offline": False}
ALARM_VALUES = {"on": True, "off": False}


def parse_field_pairs(tokens: Sequence[str]) -> List[Tuple[str, str]]:
    """Group a flat `<field> <value> ...` token list into pairs.

    Raises `ValueError` if the tokens do not pair up, which is the one mistake
    that can be caught without knowing the target sensor's type.
    """
    if not tokens or len(tokens) % 2 != 0:
        raise ValueError("fields must be given as '<field> <value>' pairs")
    return list(zip(tokens[0::2], tokens[1::2]))


def apply_fields(sensor: Sensor, fields: Iterable[Tuple[str, str]]) -> None:
    """Apply `(field, value)` pairs to a sensor, validating against its type.

    Values arrive as text from every front end (a REPL line, a YAML scalar), so
    the coercion lives here instead of being repeated by each caller.

    Every pair is validated before any of them is applied, so a rejected update
    never leaves a sensor half-changed and reporting a state no test asked for.
    Raises `ValueError` on an unknown field or an unusable value.
    """
    if isinstance(sensor, TempHum):
        _apply_temp_hum_fields(sensor, fields)
    else:
        _apply_alarm_fields(sensor, fields)


def _apply_temp_hum_fields(sensor: TempHum, fields: Iterable[Tuple[str, str]]) -> None:
    online: Optional[bool] = None
    temp_c: Optional[float] = None
    humidity_pct: Optional[float] = None

    for field, value in fields:
        if field == "status":
            online = _parse_choice("status", value, STATUS_VALUES)
        elif field == "temp":
            temp_c = _parse_number("temp", value)
        elif field == "humidity":
            humidity_pct = _parse_number("humidity", value)
        else:
            raise ValueError(
                f"unrecognized field '{field}' for {sensor.type_name} "
                f"(expected status, temp or humidity)"
            )

    if online is not None:
        sensor.online = online
    if temp_c is not None or humidity_pct is not None:
        # Pins the value: a manually set sensor stops drifting on each tick.
        sensor.set_values(temp_c=temp_c, humidity_pct=humidity_pct)


def _apply_alarm_fields(sensor: _AlarmSensor, fields: Iterable[Tuple[str, str]]) -> None:
    online: Optional[bool] = None
    alarm: Optional[bool] = None

    for field, value in fields:
        if field == "status":
            online = _parse_choice("status", value, STATUS_VALUES)
        elif field == "alarm":
            alarm = _parse_choice("alarm", value, ALARM_VALUES)
        else:
            raise ValueError(
                f"unrecognized field '{field}' for {sensor.type_name} "
                f"(expected status or alarm)"
            )

    if online is not None:
        sensor.online = online
    if alarm is not None:
        sensor.alarm = alarm


def _parse_choice(field: str, value: str, choices: dict) -> bool:
    if value not in choices:
        expected = " or ".join(sorted(choices))
        raise ValueError(f"unrecognized {field} '{value}' (expected {expected})")
    return choices[value]


def _parse_number(field: str, value: str) -> float:
    try:
        return float(value)
    except ValueError:
        raise ValueError(f"{field} must be a number, got '{value}'") from None
