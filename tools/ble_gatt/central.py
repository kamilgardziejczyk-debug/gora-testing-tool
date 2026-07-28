"""BLE central: scan for peripherals, connect to one, read/write its GATT characteristics.

Sync API over bleak (which is async-only) - see `loop.py` for why the event
loop lives on a background thread.

Central role only. A peripheral role, if added, belongs in its own module
beside this one and can reuse `loop`, `uuids` and `values` unchanged.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import List, Optional

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError

from .loop import AsyncLoop
from .uuids import normalize_uuid

LOGGER = logging.getLogger(__name__)

DEFAULT_SCAN_TIMEOUT_S = 8.0
DEFAULT_CONNECT_TIMEOUT_S = 15.0

# A Bluetooth address, e.g. "AA:BB:CC:DD:EE:FF". Used to tell an address apart
# from a device name so `connect()` can take either without a second field.
ADDRESS_PATTERN = re.compile(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$", re.IGNORECASE)


class DeviceNotFound(ConnectionError):
    """No peripheral with the requested name or address answered the scan.

    A ConnectionError so callers can treat every "could not reach the device"
    failure alike, matching how the MQTT and sub-GHz tools report theirs.
    """


class ServiceNotFound(ValueError):
    """The connected peripheral does not expose the requested service UUID."""


class CharacteristicNotFound(ValueError):
    """The connected peripheral does not expose the requested characteristic UUID."""


class AmbiguousCharacteristic(ValueError):
    """The characteristic UUID appears in more than one service; `service` is needed."""


@dataclass(frozen=True)
class DiscoveredDevice:
    """A peripheral seen while scanning."""

    address: str
    name: Optional[str]
    rssi: Optional[int]

    def describe(self) -> str:
        return f"{self.address}  {self.name or '(no name)'}  rssi={self.rssi if self.rssi is not None else '?'}"


@dataclass(frozen=True)
class CharacteristicInfo:
    """One characteristic of a connected peripheral's service."""

    uuid: str
    handle: int
    properties: tuple
    description: Optional[str]

    def describe(self) -> str:
        properties = ",".join(self.properties) or "none"
        label = f" ({self.description})" if self.description else ""
        return f"    char {self.uuid}{label}  handle={self.handle}  [{properties}]"


@dataclass(frozen=True)
class ServiceInfo:
    """One service of a connected peripheral, with its characteristics."""

    uuid: str
    description: Optional[str]
    characteristics: tuple

    def describe(self) -> str:
        label = f" ({self.description})" if self.description else ""
        lines = [f"  service {self.uuid}{label}"]
        lines.extend(char.describe() for char in self.characteristics)
        return "\n".join(lines)


class BleCentral:
    """A BLE central connected to at most one peripheral at a time.

    Usage:

        central = BleCentral()
        central.connect("GoraGateway_01B4EE")
        central.write_characteristic("2a00", b"hello")
        central.disconnect()

    Also usable as a context manager, which disconnects on exit.

    `discover()` works without connecting, so a scenario can list what is in
    range before deciding what to talk to.
    """

    def __init__(
        self,
        adapter: Optional[str] = None,
        scan_timeout_s: float = DEFAULT_SCAN_TIMEOUT_S,
        connect_timeout_s: float = DEFAULT_CONNECT_TIMEOUT_S,
    ):
        self.adapter = adapter
        self.scan_timeout_s = scan_timeout_s
        self.connect_timeout_s = connect_timeout_s

        self._loop = AsyncLoop()
        self._client: Optional[BleakClient] = None
        self._device: Optional[DiscoveredDevice] = None
        self._closed = False

    def __enter__(self) -> "BleCentral":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    @property
    def is_connected(self) -> bool:
        """Whether a peripheral is currently connected."""
        return self._client is not None and self._client.is_connected

    @property
    def device(self) -> Optional[DiscoveredDevice]:
        """The connected peripheral, or None when not connected."""
        return self._device

    def discover(self, timeout_s: Optional[float] = None) -> List[DiscoveredDevice]:
        """Scan for advertising peripherals, strongest signal first."""
        timeout_s = self.scan_timeout_s if timeout_s is None else timeout_s
        self._ensure_open()

        LOGGER.info("Scanning for BLE peripherals for %.1fs", timeout_s)
        found = self._loop.run(self._discover(timeout_s), timeout_s)
        LOGGER.info("Scan found %d peripheral(s)", len(found))
        return found

    def connect(self, device: str, timeout_s: Optional[float] = None) -> DiscoveredDevice:
        """Connect to a peripheral by advertised name or by Bluetooth address.

        An address connects directly; a name is resolved by scanning, since a
        name is only known from an advertisement. The two phases get their own
        budgets - `scan_timeout_s` to find the name, `timeout_s` (default
        `connect_timeout_s`) to establish the connection - because a slow scan
        and a slow connection are different faults and a caller usually wants
        to bound them differently.

        Raises `DeviceNotFound` if the name never shows up, or `ConnectionError`
        if it does but the connection or GATT discovery fails.
        """
        connect_timeout_s = self.connect_timeout_s if timeout_s is None else timeout_s
        self._ensure_open()
        if self.is_connected:
            raise RuntimeError(
                f"already connected to {self._device.describe() if self._device else 'a peripheral'}; "
                f"disconnect() before connecting to another"
            )

        target = self._resolve_target(device, self.scan_timeout_s)
        LOGGER.info("Connecting to %s (up to %.1fs)", target.describe(), connect_timeout_s)

        client = BleakClient(target.address, timeout=connect_timeout_s, **self._backend_kwargs())
        try:
            self._loop.run(client.connect(), connect_timeout_s)
        except (BleakError, TimeoutError, OSError) as error:
            raise ConnectionError(f"could not connect to {target.describe()} ({error})") from error

        self._client = client
        self._device = target
        LOGGER.info("Connected to %s", target.describe())
        return target

    def disconnect(self) -> None:
        """Drop the connection, keeping this central usable for another connect."""
        if self._client is None:
            return

        client, self._client = self._client, None
        device, self._device = self._device, None
        try:
            self._loop.run(client.disconnect(), self.connect_timeout_s)
        except (BleakError, TimeoutError, OSError) as error:
            # Nothing useful left to do: the local state is already cleared, so
            # a failed teardown must not stop the caller from moving on.
            LOGGER.warning("Error while disconnecting from %s: %s", device.address if device else "?", error)
        else:
            LOGGER.info("Disconnected from %s", device.describe() if device else "peripheral")

    def close(self) -> None:
        """Disconnect and shut down the background event loop. Idempotent."""
        if self._closed:
            return
        self.disconnect()
        self._closed = True
        self._loop.stop()

    def services(self) -> List[ServiceInfo]:
        """Every service the connected peripheral exposes, with characteristics."""
        client = self._require_connection()
        return [
            ServiceInfo(
                uuid=service.uuid,
                description=service.description,
                characteristics=tuple(
                    CharacteristicInfo(
                        uuid=char.uuid,
                        handle=char.handle,
                        properties=tuple(char.properties),
                        description=char.description,
                    )
                    for char in service.characteristics
                ),
            )
            for service in client.services.services.values()
        ]

    def read_characteristic(self, char_uuid: str, service_uuid: Optional[str] = None) -> bytes:
        """Read one characteristic's current value."""
        client = self._require_connection()
        characteristic = self._find_characteristic(char_uuid, service_uuid)

        data = bytes(self._loop.run(client.read_gatt_char(characteristic), self.connect_timeout_s))
        LOGGER.info("Read %s -> %s", characteristic.uuid, data.hex(" ") or "(empty)")
        return data

    def write_characteristic(
        self,
        char_uuid: str,
        data: bytes,
        service_uuid: Optional[str] = None,
        response: bool = True,
    ) -> None:
        """Write bytes to one characteristic.

        `response` picks a write-with-response (an acknowledged write, so a
        device-side rejection surfaces as an error) over a fire-and-forget
        write-without-response. Acknowledged is the default precisely so a
        failed write fails the test instead of passing silently.
        """
        client = self._require_connection()
        characteristic = self._find_characteristic(char_uuid, service_uuid)

        LOGGER.info(
            "Writing %s to %s (response=%s)",
            data.hex(" ") or "(empty)",
            characteristic.uuid,
            response,
        )
        try:
            self._loop.run(
                client.write_gatt_char(characteristic, data, response=response),
                self.connect_timeout_s,
            )
        except (BleakError, TimeoutError, OSError) as error:
            raise IOError(f"write to characteristic {characteristic.uuid} failed ({error})") from error

    async def _discover(self, timeout_s: float) -> List[DiscoveredDevice]:
        """Scan and flatten bleak's results into DiscoveredDevice records."""
        found = await BleakScanner.discover(timeout=timeout_s, return_adv=True, **self._backend_kwargs())
        devices = [
            DiscoveredDevice(address=device.address, name=device.name, rssi=advertisement.rssi)
            for device, advertisement in found.values()
        ]
        # Strongest first: the nearest match is the one a test rig means when
        # two devices happen to advertise the same name.
        devices.sort(key=lambda entry: entry.rssi if entry.rssi is not None else -999, reverse=True)
        return devices

    def _resolve_target(self, device: str, timeout_s: float) -> DiscoveredDevice:
        """Turn a name or address into a concrete DiscoveredDevice to connect to."""
        if ADDRESS_PATTERN.match(device.strip()):
            return DiscoveredDevice(address=device.strip().upper(), name=None, rssi=None)

        LOGGER.info("Scanning for a peripheral named '%s' (up to %.1fs)", device, timeout_s)
        for candidate in self.discover(timeout_s):
            if candidate.name == device:
                return candidate

        raise DeviceNotFound(
            f"no BLE peripheral advertising the name '{device}' was found within {timeout_s}s. "
            f"Check the device is powered, in range and still advertising (it stops once "
            f"something else connects to it)."
        )

    def _backend_kwargs(self) -> dict:
        """Adapter selection, passed through only when one was requested."""
        return {"adapter": self.adapter} if self.adapter else {}

    def _require_connection(self) -> BleakClient:
        self._ensure_open()
        if self._client is None or not self._client.is_connected:
            raise RuntimeError("not connected to a peripheral: call connect() first")
        return self._client

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("this BleCentral is closed: build a new one to reconnect")

    def _find_characteristic(self, char_uuid: str, service_uuid: Optional[str]):
        """Locate a characteristic, optionally scoped to one service.

        Scoping matters because a characteristic UUID is only required to be
        unique within its service: a device exposing the same UUID under two
        services would otherwise be written to at whichever one happens to come
        first. That case raises instead, so the scenario has to say which.
        """
        client = self._require_connection()
        char_uuid = normalize_uuid(char_uuid)

        if service_uuid is not None:
            service_uuid = normalize_uuid(service_uuid)
            service = client.services.get_service(service_uuid)
            if service is None:
                available = ", ".join(sorted(s.uuid for s in client.services.services.values())) or "none"
                raise ServiceNotFound(
                    f"peripheral does not expose service {service_uuid}. Services found: {available}"
                )
            candidates = [char for char in service.characteristics if char.uuid == char_uuid]
            scope = f"service {service_uuid}"
        else:
            candidates = [
                char
                for service in client.services.services.values()
                for char in service.characteristics
                if char.uuid == char_uuid
            ]
            scope = "any service"

        if not candidates:
            raise CharacteristicNotFound(
                f"peripheral does not expose characteristic {char_uuid} in {scope}"
            )
        if len(candidates) > 1:
            services = ", ".join(sorted(char.service_uuid for char in candidates))
            raise AmbiguousCharacteristic(
                f"characteristic {char_uuid} exists in more than one service ({services}); "
                f"name the one you mean with a 'service' field"
            )

        return candidates[0]
