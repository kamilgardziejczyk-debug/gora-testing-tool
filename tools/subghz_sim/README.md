# subghz_sim

Pretends to be a set of sub-GHz sensors talking to the gateway over a serial
link, so gateway behaviour can be tested without real sensors: heat, smoke and
CO alarms (online/alarm state) and a temp/humidity sensor (drifting or pinned
values).

Usable two ways: as an interactive REPL, and as a Python API that the
`!SubghzSim` scenario wrapper drives.

## How it reports

Adding or changing a sensor sends a frame immediately, and a background
heartbeat re-sends **every** live sensor's current state every `interval_s`
seconds (5 by default). A gateway that expects periodic uplinks therefore keeps
seeing a sensor for as long as the session is open, and stops seeing it the
moment it is removed.

`temp_hum` sensors drift by a small random step on each heartbeat until a value
is set explicitly; from then on the value is pinned and reported unchanged
(shown as `(manual)`).

## Install

```bash
pip install -r tools/subghz_sim/requirements.txt   # pyserial
```

## Command line

```bash
python tools/subghz_sim/subghz_sim.py --port /dev/ttyUSB0 --baud 115200 --interval 5
```

```
Sub-GHz sensor simulator. Type 'help' for commands, 'quit' to exit.
subghz> add temp_hum
added #1 temp_hum online=True temp_c=22.6 humidity=43
subghz> add co
added #2 co online=True alarm=False
subghz> set 1 temp 30.5 humidity 70
#1 temp_hum online=True temp_c=30.5 humidity=70 (manual)
subghz> set 2 alarm on
#2 co online=True alarm=True
subghz> del 2
removed #2
subghz> quit
```

Exit codes: `0` clean exit, `2` the serial port could not be opened.

### Commands

| Command | Notes |
| --- | --- |
| `add <heat\|smoke\|co\|temp_hum>` | Adds a sensor, prints the id it was given. `temp` is an alias for `temp_hum` |
| `del <sensor_id>` | Stops reporting that sensor |
| `list` | Every live sensor and its current state |
| `set <sensor_id> <field> <value> ...` | Updates a sensor and sends it at once |
| `quit` | Closes the link (Ctrl-D also works) |

Fields accepted by `set`, per sensor type:

| Type | Fields |
| --- | --- |
| `heat`, `smoke`, `co` | `status online\|offline`, `alarm on\|off` |
| `temp_hum` | `status online\|offline`, `temp <°C>`, `humidity <%>` |

Several pairs can be combined: `set 1 temp 22.5 humidity 48 status online`.

## Python API

```python
from tools.subghz_sim import SubghzSimulator

simulator = SubghzSimulator("/dev/ttyUSB0", baud=115200, interval_s=5.0)

simulator.open()                                   # raises ConnectionError

sensor = simulator.add_sensor("temp_hum")           # sent immediately, id == 1
simulator.update_sensor(sensor.sensor_id, [("temp", "30.5"), ("humidity", "70")])
simulator.remove_sensor(sensor.sensor_id)

simulator.close()
```

`SubghzSimulator` is also a context manager, which closes the link on exit:

```python
with SubghzSimulator("/dev/ttyUSB0") as simulator:
    simulator.open()
    simulator.add_sensor("co")
    ...
```

### Reference

**`SubghzSimulator(port, baud=115200, interval_s=5.0)`**

| Method | Behaviour |
| --- | --- |
| `open()` | Open the port, start the heartbeat. Raises `ConnectionError` |
| `close()` | Stop the heartbeat and close the port. Idempotent |
| `add_sensor(type_name)` | Add a sensor, send it, return it. Raises `UnknownSensorType` |
| `remove_sensor(sensor_id)` | Stop reporting it. Raises `UnknownSensorId` |
| `update_sensor(sensor_id, fields)` | Apply `(field, value)` pairs and send. Raises `UnknownSensorId`, `ValueError` |
| `get_sensor(sensor_id)` | The live `Sensor`. Raises `UnknownSensorId` |
| `list_sensors()` | Every live sensor, ordered by id |

Any command issued before `open()` or after `close()` raises `RuntimeError`,
and nothing is registered — a sensor that could not be announced is never left
behind for the heartbeat to report.

**`parse_field_pairs(tokens)`** groups a flat `["temp", "22.5", "humidity",
"48"]` token list into the pairs `update_sensor()` takes, raising `ValueError`
if they do not pair up. Both front ends use it so a malformed `set` is rejected
identically.

### Semantics worth knowing

- **Values are text.** `update_sensor()` takes `(field, value)` string pairs,
  because both front ends receive text (a REPL line, a YAML scalar). The
  coercion and validation live in `sensors.apply_fields()`, once.
- **An update is validated before it is applied.** A pair list with one bad
  entry changes nothing, so a rejected update never leaves a sensor reporting a
  state no test asked for.
- **Ids are per session** and start at 1. A new `SubghzSimulator` always numbers
  its first sensor `#1`; ids are not reused after a `del`.
- **The registry is thread-safe**, the serial port is not. State changes are
  sent from the calling thread and the heartbeat sends from its own, so drive one
  simulator from one thread.

## Wire format

Frames match the RW612 gateway's parser (`src/gora_subghz/gora_subghz_task.c`):

```
SOF(0xA5) VER(1) TYPE(1) SENSOR_ID(1) STATUS(1) LEN(1) PAYLOAD(0-3) CRC8(1)
```

`TYPE` is 0 heat, 1 smoke, 2 CO, 3 temp/hum. `STATUS` bit0 is online, bit1 is
alarm active (alarm types only). The temp/hum payload is an int16 LE of tenths
of a degree Celsius plus a uint8 humidity percentage. `CRC8` covers everything
between `SOF` and the checksum, poly `0x07`, init `0x00` — matching Zephyr's
`crc8()`.

## Testing without hardware

A pty pair is enough to see exactly what the simulator puts on the wire:

```bash
socat -d -d pty,raw,echo=0 pty,raw,echo=0     # prints two /dev/pts/N names
python tools/subghz_sim/subghz_sim.py --port /dev/pts/3 --interval 1
xxd < /dev/pts/4                              # frames appear as they are sent
```

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| `could not open serial port …` | Wrong device path, or the port is held by another process (a REPL still running, a serial monitor) |
| Nothing arrives at the gateway | Baud mismatch, or TX/RX not crossed |
| Gateway sees a sensor appear and vanish | The scenario's `duration_s` elapsed and the link closed, stopping the heartbeat |
| A `temp_hum` value never changes | It was set explicitly, which pins it — `list` shows `(manual)` |
