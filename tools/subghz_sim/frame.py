"""Wire frame encoding for the sub-GHz sensor simulator link.

Matches the RW612 gateway's parser (src/gora_subghz/gora_subghz_task.c):

    SOF(1) VER(1) TYPE(1) SENSOR_ID(1) STATUS(1) LEN(1) PAYLOAD(0-3) CRC8(1)

STATUS bit0 = online, bit1 = alarm_active (alarm sensor types only).
TEMP_HUM payload = int16 LE tenths-degC + uint8 humidity%.
"""

from __future__ import annotations

SOF = 0xA5
VERSION = 1

TYPE_HEAT_ALARM = 0
TYPE_SMOKE_ALARM = 1
TYPE_CO_ALARM = 2
TYPE_TEMP_HUM = 3

STATUS_ONLINE = 0x01
STATUS_ALARM = 0x02

MAX_PAYLOAD = 3


def crc8(data: bytes, poly: int = 0x07, init: int = 0x00) -> int:
    """CRC-8, poly 0x07, init 0x00, no reflect - matches Zephyr's crc8()."""
    crc = init
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ poly) & 0xFF if (crc & 0x80) else (crc << 1) & 0xFF
    return crc


def build_frame(sensor_type: int, sensor_id: int, online: bool, alarm_active: bool,
                 payload: bytes = b"") -> bytes:
    if len(payload) > MAX_PAYLOAD:
        raise ValueError(f"payload too long: {len(payload)} > {MAX_PAYLOAD}")
    if not (0 <= sensor_id <= 0xFF):
        raise ValueError(f"sensor_id out of range: {sensor_id}")

    status = (STATUS_ONLINE if online else 0) | (STATUS_ALARM if alarm_active else 0)
    body = bytes([VERSION, sensor_type, sensor_id, status, len(payload)]) + payload
    return bytes([SOF]) + body + bytes([crc8(body)])


def encode_temp_hum_payload(temp_c: float, humidity_pct: float) -> bytes:
    temp_tenths = max(-32768, min(32767, round(temp_c * 10)))
    humidity = max(0, min(255, round(humidity_pct)))
    return temp_tenths.to_bytes(2, byteorder="little", signed=True) + bytes([humidity])
