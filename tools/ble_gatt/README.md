# ble_gatt

Talks to Bluetooth Low Energy devices that expose GATT services: scans for
peripherals, connects to one, and reads or writes its characteristics.

Usable two ways: as an interactive REPL for poking at a device by hand, and as
a Python API that the `!BleCentral` scenario wrapper drives.

Two roles, one module each. **Central** (`central.py`) is the above: this host
connects to someone else's GATT server, over bleak. **Peripheral**
(`peripheral.py`) is the reverse: this host *advertises* a GATT server of its
own so a DUT acting as central can connect to it — used to simulate a sensor
the DUT expects to find. bleak is central-only by design, so the peripheral
talks to BlueZ over D-Bus instead; both roles share `loop.py`, `uuids.py` and
`values.py`.

## Install

```bash
pip install -r tools/ble_gatt/requirements.txt   # bleak>=3.0
```

The peripheral role needs no extra package: it uses `dbus-fast`, which bleak
already installs as its Linux backend.

On Linux this drives BlueZ over D-Bus, so `bluetooth.service` must be running
and the user needs permission to use the adapter. No extra setup on a normal
desktop; a headless rig may need the user added to the `bluetooth` group.

## Command line

```bash
# What is in range?
python tools/ble_gatt/ble_gatt.py --scan

# Connect and explore interactively
python tools/ble_gatt/ble_gatt.py --connect GoraGateway_01B4EE
```

```
BLE central. Type 'help' for commands, 'quit' to exit.
ble> scan
A8:E6:E8:36:32:4B  GoraGateway_01B4EE  rssi=-54
88:49:2D:F9:4C:2C  (no name)  rssi=-81
ble> connect GoraGateway_01B4EE
connected to A8:E6:E8:36:32:4B  GoraGateway_01B4EE  rssi=-54
ble> services
  service 0000180a-0000-1000-8000-00805f9b34fb (Device Information)
    char 00002a29-0000-1000-8000-00805f9b34fb (Manufacturer Name String)  handle=12  [read]
ble> read 2a29
45 49 20 45 6c 65 63 74 72 6f 6e 69 63 73  ('EI Electronics')
ble> write ffe1 01ff
wrote 01 ff
ble> quit
```

Exit codes: `0` clean exit, `2` the `--connect` target could not be reached.

### Commands

| Command | Notes |
| --- | --- |
| `scan [seconds]` | List advertising peripherals, strongest signal first |
| `connect <name\|address>` | Connect by advertised name, or directly by `AA:BB:CC:DD:EE:FF` |
| `disconnect` | Drop the connection |
| `services` | The connected peripheral's services and characteristics |
| `read <char_uuid> [service_uuid]` | Read a characteristic |
| `write <char_uuid> <value> [encoding] [service_uuid]` | Write a characteristic |
| `quit` | Disconnect and exit (Ctrl-D also works) |

### UUIDs

Anywhere a UUID is accepted, the 16-bit shorthand from a datasheet works and is
expanded to the full 128-bit form (`180a` →
`0000180a-0000-1000-8000-00805f9b34fb`). A leading `0x` is tolerated. Full
128-bit UUIDs are passed through, lowercased.

### Value encodings

| Encoding | Meaning | Example |
| --- | --- | --- |
| `hex` (default) | Raw bytes as hex digits; `:`, `-`, `_` and spaces ignored | `01ff`, `01:ff` |
| `utf8` | The text itself, UTF-8 encoded | `GoraTest` |
| `uint8` | One byte | `42`, `0x2a` |
| `uint16` | Two bytes, little-endian | `300` → `2c 01` |
| `uint32` | Four bytes, little-endian | `70000` → `70 11 01 00` |

Integers are **little-endian** because that is the byte order the Bluetooth core
spec uses for its own numeric fields, so it is what a device datasheet almost
always means. Integers accept `0x` hex notation. `hex` requires an even number
of digits — pad the leading byte (`0f`, not `f`) so there is no guessing about
which end a stray nibble belongs to.

## Python API

```python
from tools.ble_gatt import BleCentral, encode_value

central = BleCentral(adapter="hci0", scan_timeout_s=8.0, connect_timeout_s=15.0)

for device in central.discover():          # works without connecting
    print(device.describe())

central.connect("GoraGateway_01B4EE")      # or "AA:BB:CC:DD:EE:FF"

for service in central.services():
    print(service.describe())

central.write_characteristic("ffe1", encode_value("01ff", "hex"), service_uuid="ffe0")
data = central.read_characteristic("2a29")

for value in central.stream_notifications("ffe3", service_uuid="ffe0", duration_s=30):
    if value == b"\x01":               # deciding what counts is the caller's job
        break
else:
    raise TimeoutError("device never reported online")

# poll_characteristic() is the alternative for a notification that's easy to
# miss (e.g. right after reconnecting): reads on an interval instead of
# subscribing, trading responsiveness for not depending on notify delivery.
for value in central.poll_characteristic("ffe3", service_uuid="ffe0", interval_s=2.0, duration_s=30):
    if value == b"\x01":
        break
else:
    raise TimeoutError("device never reported online")

central.close()
```

`BleCentral` is also a context manager, which disconnects and shuts down on exit:

```python
with BleCentral() as central:
    central.connect("GoraGateway_01B4EE")
    ...
```

### Reference

**`BleCentral(adapter=None, scan_timeout_s=8.0, connect_timeout_s=15.0)`**

| Method | Behaviour |
| --- | --- |
| `discover(timeout_s=None)` | Scan; returns `DiscoveredDevice` list, strongest RSSI first |
| `connect(device, timeout_s=None)` | Connect by name or address. Raises `DeviceNotFound` / `ConnectionError` |
| `disconnect()` | Drop the connection; the central stays usable for another `connect()` |
| `close()` | Disconnect and stop the background event loop. Idempotent |
| `services()` | `ServiceInfo` list, each with its `CharacteristicInfo` entries |
| `read_characteristic(char_uuid, service_uuid=None)` | Returns `bytes` |
| `write_characteristic(char_uuid, data, service_uuid=None, response=True)` | Write; raises `IOError` on failure |
| `stream_notifications(char_uuid, service_uuid=None, duration_s=None)` | Generator of `bytes`, one per notification; runs forever if `duration_s` is `None` |
| `poll_characteristic(char_uuid, service_uuid=None, interval_s=1.0, duration_s=None)` | Generator of `bytes`, one per read, taken every `interval_s`; runs forever if `duration_s` is `None` |
| `is_connected` / `device` | Current connection state, and what is connected |

Errors: `DeviceNotFound` (a `ConnectionError`, so every "could not reach it"
failure can be caught alike, as with the MQTT and sub-GHz tools),
`ServiceNotFound`, `CharacteristicNotFound`, `AmbiguousCharacteristic` (all
`ValueError`). Using the API before `connect()` or after `close()` raises
`RuntimeError`.

### Semantics worth knowing

- **Connecting by name means scanning first.** A name only exists in an
  advertisement, so `connect("SomeName")` scans (bounded by `scan_timeout_s`)
  and then connects (bounded by `connect_timeout_s`). The two phases have
  separate budgets because a device that never advertises and a device that
  advertises but refuses connections are different faults. An address skips
  the scan entirely, which is faster and immune to a missing advertisement.
- **A connected peripheral usually stops advertising**, so a second
  `connect()` by name will not find it. This is the most common reason a
  re-run fails: something (this tool, or `bluetoothctl`) is still holding the
  connection.
- **`service` is optional but disambiguating.** A characteristic UUID only has
  to be unique within its service. If the same UUID appears under two
  services, the lookup raises `AmbiguousCharacteristic` rather than picking
  one, so a scenario has to say which it means.
- **Writes are acknowledged by default** (`response=True`), so a device-side
  rejection surfaces as an error instead of a silently-dropped write. Pass
  `response=False` for a fire-and-forget write-without-response.
- **A device reset drops the connection.** `stream_notifications()` only sees
  notifications on the current connection, so waiting for one that arrives
  *after* a reset (a device coming back online, say) needs a fresh `connect()`
  once it re-advertises, not a longer `duration_s` on the same session.
- **`stream_notifications()` only receives and yields** - like
  `MqttListener.stream()`, deciding whether a value is the one being waited
  for is the caller's job, not this tool's. Same for `poll_characteristic()`.
- **Prefer `poll_characteristic()` over `stream_notifications()` for a value
  that changes in a narrow window right after connecting** - subscribing to a
  notification and the peripheral deciding to send one aren't atomic, so a
  fast device can change the value before `start_notify()` is even in place.
  Reading on an interval instead can't miss it that way, at the cost of
  noticing a change up to `interval_s` late rather than immediately.
- **The event loop is a background thread.** bleak is async-only and a
  `BleakClient` is bound to the loop that created it, so one long-lived loop
  runs per `BleCentral`, which is what lets a connection survive across
  several synchronous calls. Drive one `BleCentral` from one thread.

## Peripheral role

Advertises a GATT server so a DUT acting as central can find it, connect, and
subscribe. Simulating a sensor, in other words, rather than talking to one.

```python
from tools.ble_gatt import BlePeripheral, CharacteristicSpec, ServiceSpec

heart_rate = ServiceSpec(
    uuid="180d",
    characteristics=(
        CharacteristicSpec(uuid="2a37", properties=("notify",)),        # measurement
        CharacteristicSpec(uuid="2a38", properties=("read",), initial_value=b"\x01"),
        CharacteristicSpec(uuid="2a39", properties=("write",)),         # control point
    ),
)

with BlePeripheral("GoraHRV_01", [heart_rate], adapter="hci0") as peripheral:
    peripheral.start()
    print(peripheral.describe())

    if peripheral.is_notifying("2a37"):          # the DUT wrote the CCCD
        peripheral.notify("2a37", b"\x16\x3c\xe8\x03")

    print(peripheral.writes("2a39"))             # every value the DUT wrote
```

`start()` registers the GATT application and the advertisement; `stop()`
unregisters both and leaves the peripheral restartable; `close()` (or leaving
the `with` block) also shuts the background event loop down.

### Reference

**`BlePeripheral(local_name, services, adapter="hci0", timeout_s=15.0)`**

| Method | Does |
| --- | --- |
| `start()` / `stop()` | Begin / end advertising and serving. `stop()` is restartable |
| `close()` | `stop()` plus shutting down the event loop. Idempotent |
| `notify(uuid, payload)` | Push a notification. Returns `False` if nobody is subscribed |
| `is_notifying(uuid)` | Whether the central has subscribed (written the CCCD) |
| `value(uuid)` / `set_value(uuid, payload)` | Read / set a value without notifying |
| `writes(uuid)` / `clear_writes(uuid)` | Values the central has written, in order |
| `describe()` | The advertised name and the whole GATT table |

### Semantics worth knowing

**The advertisement does not go through BlueZ.** `LEAdvertisingManager1`
rejected every advertisement on every host tested, including BlueZ's own
`bluetoothctl` on a freshly restarted adapter — two BlueZ versions, two kernels,
two controllers, always `Invalid Parameters`. The kernel accepts the same
advertisement over its management interface, so `mgmt.py` sends it there
directly. The GATT server still belongs to `bluetoothd`, over D-Bus, which
works fine; only the advertisement is built and sent by this tool.

That turned out to be necessary for a second reason. BlueZ decides for itself
whether the local name travels in the advertisement or the scan response, and
it puts it in the **scan response** — a separate PDU. A central that will only
match a peripheral advertising its name and a service UUID *together* therefore
never matches it, with no error at either end. Building the payload here puts
both in one PDU, which is verifiable: `build_advertising_data("GoraHRV_01",
["180d"])` is exactly the 19 bytes a scanner reports as one advertisement.

**A service UUID is advertised in its shortest form, deliberately.** A central
looking for a standard service reads the *16-bit* UUID list only, so
advertising `0000180d-0000-1000-8000-00805f9b34fb` makes the peripheral
invisible to it. `advertising_uuid()` collapses any UUID in the Bluetooth Base
range to its 16-bit form, and it costs 4 bytes of the budget instead of 18.

**The advertised name has a budget, checked in the constructor.** One
advertising PDU holds 31 bytes: 3 for flags, 4 for a single 16-bit service
UUID, and 2 + the name. `BlePeripheral` raises `AdvertisingDataTooLarge` while
it is being built rather than at start, and `advertising_data_size()` measures
the real payload rather than predicting it, so the check can never disagree
with what is broadcast. With one 16-bit service the name fits up to **22
characters**. The adapter's own limit is checked too, once a socket to it is
open, since it can be lower than 31.

**Notifications go through the value.** BlueZ has no "send a notification"
call: it watches the characteristic's `Value` property and turns a change into
a notification. `notify()` therefore always updates the value, and returns
`False` when no central is subscribed — so a scenario that starts streaming
before the DUT has written the CCCD can tell.

**A failed `start()` rolls all the way back.** The GATT application is
registered first and the advertisement second; if the advertisement fails the
application is unregistered, the objects unexported and the bus dropped, so the
peripheral is left exactly as unstarted as it was and can be retried. This
matters more than it sounds: an advertising slot is a finite adapter resource,
and a client that half-registers one leaks it. Five failed starts can exhaust
an adapter, after which nothing on the host can advertise until `systemctl
restart bluetooth`. `free_instance()` reports that state by name rather than
letting it look like a fresh failure.

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| `no BLE peripheral advertising the name ... was found` | Device powered off, out of range, or already connected to something else (it stops advertising) |
| `could not connect to ...` | Advertising but refusing connections, or another host connected between the scan and the connect |
| `peripheral does not expose service ...` | Wrong UUID, or the device only exposes it after pairing/bonding |
| `characteristic ... exists in more than one service` | Add a `service` to say which one you mean |
| `write to characteristic ... failed` | Characteristic is not writable, needs pairing, or the value is the wrong length for it — check `services` output for its properties |
| Scan finds nothing at all | `bluetooth.service` down, adapter blocked (`rfkill list`), or no permission to use it |
| `the Bluetooth management socket is not available in this network namespace` | The container is on a bridge network. Bluetooth sockets are namespace-scoped: run it with `--network host` (the deploy script now does) |
| `not permitted to open the Bluetooth management socket` | Needs `CAP_NET_ADMIN` — run as root or add `--cap-add=NET_ADMIN` |
| `all N advertising slot(s) on this adapter are in use` | Another program is advertising, or a previous one leaked a slot. `systemctl restart bluetooth` on the host reclaims them |
| `AdvertisingDataTooLarge` | Shorten the advertised name (22 characters with one 16-bit service) or advertise fewer services |
| DUT never connects to the peripheral | It may require the name *and* the service UUID in one PDU — check both are in the advertisement, not split into the scan response, with `sudo btmon` |
