"""Bluetooth Low Energy GATT tool.

Runnable from the command line (see `ble_gatt.py`) and importable as an API,
which is how the !BleCentral wrapper drives it.

One module per role. `central.py` scans, connects, and reads or writes another
device's characteristics, over bleak. `peripheral.py` runs a GATT server of its
own and advertises it, over BlueZ's D-Bus API - bleak has no peripheral role -
so that a DUT acting as central has something to connect to. Both share
`loop`, `uuids` and `values`.
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
from .peripheral import (
    DEFAULT_ADAPTER,
    AdvertisingDataTooLarge,
    AdvertisingRejected,
    BlePeripheral,
    CharacteristicSpec,
    ServiceSpec,
    UnknownCharacteristic,
    advertising_data_size,
    advertising_uuid,
    check_advertising_data,
)
from .uuids import normalize_uuid
from .values import DEFAULT_ENCODING, ENCODINGS, encode_value, format_value

__all__ = [
    "DEFAULT_ADAPTER",
    "DEFAULT_CONNECT_TIMEOUT_S",
    "DEFAULT_ENCODING",
    "DEFAULT_SCAN_TIMEOUT_S",
    "ENCODINGS",
    "AdvertisingDataTooLarge",
    "AdvertisingRejected",
    "AmbiguousCharacteristic",
    "BleCentral",
    "BlePeripheral",
    "CharacteristicInfo",
    "CharacteristicNotFound",
    "CharacteristicSpec",
    "DeviceNotFound",
    "DiscoveredDevice",
    "ServiceInfo",
    "ServiceNotFound",
    "ServiceSpec",
    "UnknownCharacteristic",
    "advertising_data_size",
    "advertising_uuid",
    "check_advertising_data",
    "encode_value",
    "format_value",
    "normalize_uuid",
]
