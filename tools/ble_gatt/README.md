# ble_gatt

Talks to Bluetooth Low Energy devices that expose GATT services: scans for
peripherals, connects to one, and reads or writes its characteristics.

Usable two ways: as an interactive REPL for poking at a device by hand, and as
a Python API that the `!BleCentral` scenario wrapper drives.

**Central role only.** A peripheral role (this host *advertising* a GATT server
for something else to connect to) is not implemented yet. When it is, it belongs
in its own `peripheral.py` beside `central.py` and can reuse `loop.py`,
`uuids.py` and `values.py` unchanged.

## Install

```bash
pip install -r tools/ble_gatt/requirements.txt   # bleak>=3.0
```

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
- **The event loop is a background thread.** bleak is async-only and a
  `BleakClient` is bound to the loop that created it, so one long-lived loop
  runs per `BleCentral`, which is what lets a connection survive across
  several synchronous calls. Drive one `BleCentral` from one thread.

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| `no BLE peripheral advertising the name ... was found` | Device powered off, out of range, or already connected to something else (it stops advertising) |
| `could not connect to ...` | Advertising but refusing connections, or another host connected between the scan and the connect |
| `peripheral does not expose service ...` | Wrong UUID, or the device only exposes it after pairing/bonding |
| `characteristic ... exists in more than one service` | Add a `service` to say which one you mean |
| `write to characteristic ... failed` | Characteristic is not writable, needs pairing, or the value is the wrong length for it — check `services` output for its properties |
| Scan finds nothing at all | `bluetooth.service` down, adapter blocked (`rfkill list`), or no permission to use it |
