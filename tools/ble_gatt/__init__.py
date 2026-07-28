"""Bluetooth Low Energy GATT tool.

Runnable from the command line (see `ble_gatt.py`) and importable as an API,
which is how the !BleCentral wrapper drives it.

Central role only for now: scan, connect, and read/write GATT characteristics.
A peripheral role belongs in its own module beside `central.py` and can reuse
`loop`, `uuids` and `values` as they are.
"""

from .central import (
    DEFAULT_CONNECT_TIMEOUT_S,
    DEFAULT_SCAN_TIMEOUT_S,
    AmbiguousCharacteristic,
    BleCentral,
    CharacteristicInfo,
    CharacteristicNotFound,
    DeviceNotFound,
    DiscoveredDevice,
    ServiceInfo,
    ServiceNotFound,
)
from .uuids import normalize_uuid
from .values import DEFAULT_ENCODING, ENCODINGS, encode_value, format_value

__all__ = [
    "DEFAULT_CONNECT_TIMEOUT_S",
    "DEFAULT_ENCODING",
    "DEFAULT_SCAN_TIMEOUT_S",
    "ENCODINGS",
    "AmbiguousCharacteristic",
    "BleCentral",
    "CharacteristicInfo",
    "CharacteristicNotFound",
    "DeviceNotFound",
    "DiscoveredDevice",
    "ServiceInfo",
    "ServiceNotFound",
    "encode_value",
    "format_value",
    "normalize_uuid",
]
