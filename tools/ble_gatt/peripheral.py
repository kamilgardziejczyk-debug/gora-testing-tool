"""BLE peripheral: advertise a GATT server for a DUT to connect to as central.

Sync API over BlueZ's D-Bus interfaces. bleak is deliberately absent here: it
is central-only by design and offers nothing for this role, so the peripheral
talks to `bluetoothd` directly through `dbus-fast` (already present as bleak's
own Linux dependency). What it does share with `central.py` is the background
event loop in `loop.py` and UUID normalization in `uuids.py`.

Knows nothing about any particular profile - a Heart Rate Service is just a
`ServiceSpec` a caller hands in. Profile definitions live in `profiles/`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from dbus_fast import BusType, DBusError
from dbus_fast.aio import MessageBus, ProxyInterface
from dbus_fast.service import PropertyAccess, ServiceInterface, dbus_property, method

from .loop import AsyncLoop
from .mgmt import MgmtError, MgmtSocket, MgmtUnavailable, adapter_index
from .uuids import normalize_uuid

LOGGER = logging.getLogger(__name__)

BLUEZ_SERVICE = "org.bluez"
GATT_MANAGER_INTERFACE = "org.bluez.GattManager1"

DEFAULT_ADAPTER = "hci0"
DEFAULT_TIMEOUT_S = 15.0

# Object path this process exports its GATT application under. A fixed
# constant is safe even with several peripherals in one process: BlueZ keys an
# application on (D-Bus sender, path), and each `BlePeripheral` opens its own
# bus connection, so each gets its own unique sender name.
APP_ROOT_PATH = "/com/gora/blesim"

# The Bluetooth Base UUID's tail. A 128-bit UUID ending in this is really a
# 16-bit UUID, which matters for advertising - see `advertising_uuid`.
BASE_UUID_TAIL = "-0000-1000-8000-00805f9b34fb"

# One legacy advertising PDU carries 31 bytes of AD structures. BlueZ prepends
# a 3-byte Flags structure to a connectable advertisement, and every other
# structure costs a 2-byte length+type header on top of its payload.
AD_MAX_BYTES = 31
AD_FLAGS_BYTES = 3
AD_HEADER_BYTES = 2

# AD structure types, from the Bluetooth assigned numbers.
AD_TYPE_FLAGS = 0x01
AD_TYPE_UUID16_COMPLETE = 0x03
AD_TYPE_UUID128_COMPLETE = 0x07
AD_TYPE_LOCAL_NAME_COMPLETE = 0x09

# LE General Discoverable Mode | BR/EDR Not Supported. Sent as our own Flags
# structure rather than left to the kernel, so the whole PDU is built here.
AD_FLAGS_VALUE = 0x06

CHARACTERISTIC_FLAGS = ("read", "write", "write-without-response", "notify", "indicate")


class AdvertisingDataTooLarge(ValueError):
    """The name and service UUIDs together overflow one advertising PDU.

    Raised while building the peripheral rather than when it starts, because
    the consequence is silent: BlueZ moves the overflowing name into the scan
    response, and a central that parses each received PDU on its own then
    never sees the name and the service UUID together, so it simply never
    matches - with no error on either side.
    """


class UnknownCharacteristic(ValueError):
    """No characteristic with this UUID was declared on this peripheral.

    Distinct from the central's `CharacteristicNotFound`, which means a remote
    device lacks a characteristic. This one is about the simulated GATT table
    the caller defined itself, so it is a scenario or programming error.
    """


class AdvertisingRejected(RuntimeError):
    """BlueZ refused to register the advertisement or the GATT application."""


def advertising_uuid(uuid: str) -> str:
    """Render `uuid` in the shortest form that advertises the same thing.

    A UUID inside the Bluetooth Base range collapses to its 16-bit shorthand.
    This is not cosmetic: BlueZ advertises whichever form it is handed, and a
    central looking for a standard service reads the 16-bit UUID list only, so
    advertising the 128-bit form of the same service makes the peripheral
    invisible to it. The long form also costs 18 bytes against `AD_MAX_BYTES`
    where the short one costs 4.
    """
    full = normalize_uuid(uuid)
    if full.startswith("0000") and full.endswith(BASE_UUID_TAIL):
        return full[4:8]
    return full


def build_advertising_data(local_name: str, service_uuids: Sequence[str]) -> bytes:
    """Build the advertising PDU: flags, the service UUID list, then the name.

    Everything goes in this one payload, with no scan response. That is the
    point of building it here rather than describing it to BlueZ, which splits
    the name off into a scan response - a separate PDU, and therefore invisible
    to a central that only matches a peripheral advertising its name and a
    service UUID together.
    """
    short = [advertising_uuid(uuid) for uuid in service_uuids]
    uuid16 = b"".join(int(uuid, 16).to_bytes(2, "little") for uuid in short if len(uuid) == 4)
    uuid128 = b"".join(
        bytes.fromhex(uuid.replace("-", ""))[::-1] for uuid in short if len(uuid) != 4
    )

    data = _ad_structure(AD_TYPE_FLAGS, bytes([AD_FLAGS_VALUE]))
    if uuid16:
        data += _ad_structure(AD_TYPE_UUID16_COMPLETE, uuid16)
    if uuid128:
        data += _ad_structure(AD_TYPE_UUID128_COMPLETE, uuid128)
    if local_name:
        data += _ad_structure(AD_TYPE_LOCAL_NAME_COMPLETE, local_name.encode("utf-8"))
    return data


def advertising_data_size(local_name: str, service_uuids: Sequence[str]) -> int:
    """Bytes the advertising PDU needs for flags, the UUID list and the name.

    Measured by building the real payload rather than predicting it, so the
    budget check can never disagree with what is actually broadcast.
    """
    return len(build_advertising_data(local_name, service_uuids))


def _ad_structure(ad_type: int, payload: bytes) -> bytes:
    """One AD structure: a length byte covering the type and its payload."""
    return bytes([len(payload) + 1, ad_type]) + payload


def check_advertising_data(local_name: str, service_uuids: Sequence[str]) -> None:
    """Raise `AdvertisingDataTooLarge` if the advertisement would not fit."""
    size = advertising_data_size(local_name, service_uuids)
    if size <= AD_MAX_BYTES:
        return

    budget = AD_MAX_BYTES - (size - len(local_name.encode("utf-8")))
    raise AdvertisingDataTooLarge(
        f"advertising '{local_name}' with {len(service_uuids)} service UUID(s) needs {size} bytes, "
        f"but one advertising PDU holds {AD_MAX_BYTES}. Shorten the name to at most {budget} "
        f"character(s), or advertise fewer services."
    )


@dataclass(frozen=True)
class CharacteristicSpec:
    """One characteristic to expose, as the peripheral will declare it."""

    uuid: str
    properties: Tuple[str, ...] = ("read",)
    initial_value: bytes = b""

    def __post_init__(self) -> None:
        unknown = [flag for flag in self.properties if flag not in CHARACTERISTIC_FLAGS]
        if unknown:
            raise ValueError(
                f"unknown characteristic flag(s) {', '.join(unknown)} on {self.uuid} "
                f"(expected one of {', '.join(CHARACTERISTIC_FLAGS)})"
            )

    def describe(self) -> str:
        """One indented line naming the characteristic and its properties."""
        return f"    char {normalize_uuid(self.uuid)}  [{','.join(self.properties)}]"


@dataclass(frozen=True)
class ServiceSpec:
    """One GATT service to expose, with its characteristics."""

    uuid: str
    characteristics: Tuple[CharacteristicSpec, ...]
    primary: bool = True

    def describe(self) -> str:
        """The service line followed by one line per characteristic."""
        lines = [f"  service {normalize_uuid(self.uuid)}{'' if self.primary else ' (secondary)'}"]
        lines.extend(char.describe() for char in self.characteristics)
        return "\n".join(lines)


# In the three interface classes below, a method's or property's annotation is
# the D-Bus signature dbus-fast exports it under, not a Python type hint - "s"
# is a string, "ay" a byte array, "" the empty signature of a method returning
# nothing. Their CamelCase names are likewise fixed by the BlueZ API.
class _GattService(ServiceInterface):
    """org.bluez.GattService1 - one exported service object."""

    def __init__(self, path: str, uuid: str, primary: bool):
        super().__init__("org.bluez.GattService1")
        self.path = path
        self._uuid = uuid
        self._primary = primary

    @dbus_property(access=PropertyAccess.READ)
    def UUID(self) -> "s":  # noqa: N802 - the D-Bus property name
        return self._uuid

    @dbus_property(access=PropertyAccess.READ)
    def Primary(self) -> "b":  # noqa: N802
        return self._primary


class _GattCharacteristic(ServiceInterface):
    """org.bluez.GattCharacteristic1 - value store plus notification state.

    BlueZ has no "send a notification" method: it watches this object's `Value`
    property and turns a PropertiesChanged into a notification on the wire, so
    `push` is how a notification is emitted.
    """

    def __init__(self, path: str, service_path: str, spec: CharacteristicSpec):
        super().__init__("org.bluez.GattCharacteristic1")
        self.path = path
        self.uuid = normalize_uuid(spec.uuid)
        self.notifying = False
        self.writes: List[bytes] = []
        self._service_path = service_path
        self._flags = list(spec.properties)
        self._value = bytes(spec.initial_value)

    @dbus_property(access=PropertyAccess.READ)
    def UUID(self) -> "s":  # noqa: N802
        return self.uuid

    @dbus_property(access=PropertyAccess.READ)
    def Service(self) -> "o":  # noqa: N802
        return self._service_path

    @dbus_property(access=PropertyAccess.READ)
    def Flags(self) -> "as":  # noqa: N802
        return self._flags

    @dbus_property(access=PropertyAccess.READ)
    def Value(self) -> "ay":  # noqa: N802
        return self._value

    @dbus_property(access=PropertyAccess.READ)
    def Notifying(self) -> "b":  # noqa: N802
        return self.notifying

    @method()
    def ReadValue(self, options: "a{sv}") -> "ay":  # noqa: N802
        LOGGER.info("Peer read %s -> %s", self.uuid, self._value.hex(" ") or "(empty)")
        return self._value

    @method()
    def WriteValue(self, value: "ay", options: "a{sv}") -> "":  # noqa: N802
        payload = bytes(value)
        self.writes.append(payload)
        self._value = payload
        LOGGER.info("Peer wrote %s <- %s", self.uuid, payload.hex(" ") or "(empty)")

    @method()
    def StartNotify(self) -> "":  # noqa: N802
        self.notifying = True
        LOGGER.info("Peer subscribed to %s", self.uuid)

    @method()
    def StopNotify(self) -> "":  # noqa: N802
        self.notifying = False
        LOGGER.info("Peer unsubscribed from %s", self.uuid)

    @property
    def value(self) -> bytes:
        """The characteristic's current value."""
        return self._value

    def set_value(self, payload: bytes) -> None:
        """Update the value without notifying anyone."""
        self._value = bytes(payload)

    def push(self, payload: bytes) -> None:
        """Update the value and emit the PropertiesChanged BlueZ notifies on."""
        self._value = bytes(payload)
        self.emit_properties_changed({"Value": self._value})


class BlePeripheral:
    """A GATT server advertising itself for one central to connect to.

    Usage:

        peripheral = BlePeripheral("GoraHRV_01", [service_spec])
        peripheral.start()
        peripheral.notify("2a37", b"\\x00\\x3c")
        peripheral.stop()

    Also usable as a context manager, which stops it on exit.

    The advertising budget is checked here, in the constructor, so a name that
    cannot fit alongside its service UUIDs fails while the scenario is being
    built rather than after the radio is already running.
    """

    def __init__(
        self,
        local_name: str,
        services: Sequence[ServiceSpec],
        adapter: str = DEFAULT_ADAPTER,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        advertise: Optional[Sequence[str]] = None,
    ):
        if not services:
            raise ValueError("a peripheral needs at least one service to expose")

        self.services = tuple(services)
        self.advertised_services = self._resolve_advertised(advertise)
        check_advertising_data(local_name, self.advertised_services)

        self.local_name = local_name
        self.adapter = adapter
        self.timeout_s = timeout_s

        self._index = adapter_index(adapter)
        self._advertising_data = build_advertising_data(local_name, self.advertised_services)
        self._loop = AsyncLoop()
        self._bus: Optional[MessageBus] = None
        self._characteristics: Dict[str, _GattCharacteristic] = {}
        self._exported: List[str] = []
        self._mgmt: Optional[MgmtSocket] = None
        self._instance: Optional[int] = None
        self._started = False
        self._closed = False

    def _resolve_advertised(self, advertise: Optional[Sequence[str]]) -> Tuple[str, ...]:
        """Which services go in the advertisement, defaulting to all of them.

        Naming a subset is the normal case for a device with more than one
        service: a sensor advertises the service it is looked up by and leaves
        the rest to be discovered after connecting. The advertising PDU is only
        31 bytes, so every service left out buys characters for the name.
        """
        served = {normalize_uuid(service.uuid) for service in self.services}
        if advertise is None:
            return tuple(service.uuid for service in self.services)

        unknown = [uuid for uuid in advertise if normalize_uuid(uuid) not in served]
        if unknown:
            raise ValueError(
                f"cannot advertise service(s) {', '.join(unknown)}: this peripheral does not "
                f"expose them (it has: {', '.join(sorted(served))})"
            )
        return tuple(advertise)

    def __enter__(self) -> "BlePeripheral":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    @property
    def is_started(self) -> bool:
        """Whether the peripheral is currently advertising."""
        return self._started

    def describe(self) -> str:
        """The advertised name and the whole GATT table, one service per block."""
        advertised = ", ".join(advertising_uuid(uuid) for uuid in self.advertised_services)
        lines = [f"{self.local_name} on {self.adapter}  (advertising {advertised})"]
        lines.extend(service.describe() for service in self.services)
        return "\n".join(lines)

    def start(self) -> None:
        """Register the GATT application with BlueZ, then start advertising.

        The two halves go to different places on purpose: the GATT server is
        bluetoothd's to serve, over D-Bus, while the advertisement is sent
        straight to the kernel - see `mgmt.py` for why BlueZ cannot be asked
        to do the second part.
        """
        self._ensure_open()
        if self._started:
            raise RuntimeError(f"'{self.local_name}' is already advertising")

        LOGGER.info("Starting peripheral '%s' on %s", self.local_name, self.adapter)
        self._loop.run(self._register_application(), self.timeout_s)
        try:
            self._start_advertising()
        except Exception:
            # The GATT half is already registered; leaving it behind would hold
            # the application on the adapter with nothing advertising it.
            self._loop.run(self._release_application(), self.timeout_s)
            raise

        self._started = True
        LOGGER.info(
            "Peripheral '%s' advertising %s",
            self.local_name,
            ", ".join(advertising_uuid(uuid) for uuid in self.advertised_services),
        )

    def stop(self) -> None:
        """Stop advertising and unregister, keeping this peripheral restartable."""
        if not self._started:
            return

        self._started = False
        try:
            self._stop_advertising()
        except (MgmtError, OSError) as error:
            # Local state is already cleared, so a failed teardown must not
            # stop the caller from moving on - same reasoning as the central's.
            # It does leak an advertising slot on the adapter, so it is a
            # warning rather than something to swallow silently.
            LOGGER.warning("Could not remove the advertisement for '%s': %s", self.local_name, error)

        try:
            self._loop.run(self._release_application(), self.timeout_s)
        except (DBusError, TimeoutError, OSError) as error:
            LOGGER.warning("Error while stopping peripheral '%s': %s", self.local_name, error)
        else:
            LOGGER.info("Peripheral '%s' stopped", self.local_name)

    def close(self) -> None:
        """Stop advertising and shut down the background event loop. Idempotent."""
        if self._closed:
            return
        self.stop()
        self._closed = True
        self._loop.stop()

    def notify(self, char_uuid: str, payload: bytes) -> bool:
        """Push `payload` to a subscribed central; returns whether one was subscribed.

        An unsubscribed characteristic still takes the new value, so a later
        read sees it. That is what a real sensor does, and it keeps a scenario
        that starts streaming before the DUT has written the CCCD honest: the
        return value says the notification went nowhere instead of pretending.
        """
        characteristic = self._characteristic(char_uuid)
        if not characteristic.notifying:
            characteristic.set_value(payload)
            LOGGER.debug("No subscriber on %s; value updated but not notified", characteristic.uuid)
            return False

        self._loop.run(self._push(characteristic, payload), self.timeout_s)
        LOGGER.info("Notified %s -> %s", characteristic.uuid, payload.hex(" ") or "(empty)")
        return True

    def set_value(self, char_uuid: str, payload: bytes) -> None:
        """Set a characteristic's value without notifying."""
        self._characteristic(char_uuid).set_value(payload)

    def value(self, char_uuid: str) -> bytes:
        """A characteristic's current value."""
        return self._characteristic(char_uuid).value

    def is_notifying(self, char_uuid: str) -> bool:
        """Whether a central has subscribed to this characteristic."""
        return self._characteristic(char_uuid).notifying

    def writes(self, char_uuid: str) -> List[bytes]:
        """Every value the connected central has written to this characteristic."""
        return list(self._characteristic(char_uuid).writes)

    def clear_writes(self, char_uuid: str) -> None:
        """Forget writes recorded so far, so a later assertion starts clean."""
        self._characteristic(char_uuid).writes.clear()

    async def _register_application(self) -> None:
        """Export every object and hand the GATT application to BlueZ."""
        self._bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        self._export_objects()
        gatt_manager = await self._gatt_manager()

        try:
            await gatt_manager.call_register_application(APP_ROOT_PATH, {})
        except DBusError as error:
            await self._release_application()
            raise AdvertisingRejected(
                f"BlueZ refused the GATT application for '{self.local_name}': {error}"
            ) from error

    async def _release_application(self) -> None:
        """Unregister the GATT application and drop every exported object.

        Safe to call whether or not registration got as far as succeeding: an
        unregister BlueZ does not recognise is logged and ignored, because the
        exported objects and the bus still have to be cleaned up either way.
        """
        if self._bus is None:
            return

        try:
            gatt_manager = await self._gatt_manager()
            await gatt_manager.call_unregister_application(APP_ROOT_PATH)
        except DBusError as error:
            LOGGER.debug("BlueZ had no GATT application to unregister: %s", error)

        for path in self._exported:
            self._bus.unexport(path)
        self._exported.clear()
        self._bus.disconnect()
        self._bus = None
        self._characteristics.clear()

    def _start_advertising(self) -> None:
        """Put the advertising payload on the air through the kernel."""
        try:
            self._mgmt = MgmtSocket(self.timeout_s)
            features = self._mgmt.read_advertising_features(self._index)
            self._check_payload_fits(features.max_adv_data_len)
            self._instance = features.free_instance()
            self._mgmt.add_advertising(self._index, self._instance, self._advertising_data)
        except (MgmtError, MgmtUnavailable, OSError) as error:
            self._close_mgmt()
            raise AdvertisingRejected(
                f"could not advertise '{self.local_name}' on {self.adapter}: {error}"
            ) from error

    def _stop_advertising(self) -> None:
        """Remove the advertisement and close the management socket."""
        try:
            if self._mgmt is not None and self._instance is not None:
                self._mgmt.remove_advertising(self._index, self._instance)
        finally:
            self._close_mgmt()

    def _close_mgmt(self) -> None:
        """Drop the management socket and forget the instance it held."""
        if self._mgmt is not None:
            self._mgmt.close()
            self._mgmt = None
        self._instance = None

    def _check_payload_fits(self, max_adv_data_len: int) -> None:
        """Reject a payload this adapter cannot carry.

        The constructor already checked it against the 31 bytes a legacy
        advertising PDU holds. This is the adapter's own answer to the same
        question, which can be smaller, and is only knowable once a socket to
        it is open.
        """
        if len(self._advertising_data) <= max_adv_data_len:
            return
        raise MgmtError(
            f"the advertising payload is {len(self._advertising_data)} bytes but {self.adapter} "
            f"accepts at most {max_adv_data_len}. Shorten the advertised name or advertise "
            f"fewer services."
        )

    async def _push(self, characteristic: _GattCharacteristic, payload: bytes) -> None:
        """Emit a notification from the event loop thread, where the bus lives."""
        characteristic.push(payload)

    def _export_objects(self) -> None:
        """Build and export the advertisement and the whole GATT tree."""
        bus = self._bus
        if bus is None:
            raise RuntimeError("bus is not connected")

        for service_index, spec in enumerate(self.services):
            service_path = f"{APP_ROOT_PATH}/service{service_index}"
            service = _GattService(service_path, normalize_uuid(spec.uuid), spec.primary)
            self._export(service_path, service)

            for char_index, char_spec in enumerate(spec.characteristics):
                char_path = f"{service_path}/char{char_index}"
                characteristic = _GattCharacteristic(char_path, service_path, char_spec)
                self._export(char_path, characteristic)
                self._characteristics[characteristic.uuid] = characteristic

    def _export(self, path: str, interface: ServiceInterface) -> None:
        """Export one object and remember its path so `_stop` can undo it."""
        self._bus.export(path, interface)
        self._exported.append(path)

    async def _gatt_manager(self) -> ProxyInterface:
        """Proxy interface for the adapter's GATT manager."""
        adapter_path = f"/org/bluez/{self.adapter}"
        introspection = await self._bus.introspect(BLUEZ_SERVICE, adapter_path)
        proxy = self._bus.get_proxy_object(BLUEZ_SERVICE, adapter_path, introspection)
        return proxy.get_interface(GATT_MANAGER_INTERFACE)

    def _characteristic(self, char_uuid: str) -> _GattCharacteristic:
        """Look up a declared characteristic, naming what is available if absent."""
        if not self._started:
            raise RuntimeError(f"'{self.local_name}' is not started; call start() first")

        uuid = normalize_uuid(char_uuid)
        characteristic = self._characteristics.get(uuid)
        if characteristic is None:
            known = ", ".join(sorted(self._characteristics)) or "none"
            raise UnknownCharacteristic(
                f"'{self.local_name}' does not expose characteristic {uuid} (it has: {known})"
            )
        return characteristic

    def _ensure_open(self) -> None:
        """Reject use after `close()`, which has already stopped the event loop."""
        if self._closed:
            raise RuntimeError(f"peripheral '{self.local_name}' is closed")
