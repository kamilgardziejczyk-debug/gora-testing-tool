# dut_logger

Captures the DUT's serial console for the whole of a scenario run and saves it
beside the HTML report, so a failed test comes with the device's own account of
what happened.

Usable two ways: as a standalone CLI (a bench check — "is this DUT talking at
all?"), and as the API `main.py` uses to log a real run.

## What a run produces

Four artefacts sharing one name, so a run's output stays together:

| File | Contents |
| --- | --- |
| `<stem>.html` | The test report (as before) |
| `<stem>.tool.log` | The testing tool's own log output, timestamped |
| `<stem>.device.log` | The DUT's serial output, timestamped |
| `<stem>.combined.log` | Both of the above interleaved, **plus test markers** |

`<stem>` comes from the report path, e.g. `results/gateway_20260730_143322`.
`*.log` is already `.gitignore`d.

The two single-source logs stay faithful to their one source; the combined log
is the one that gains structure:

```
[14:33:22.819] tool | INFO __main__: YAML validation successful
[14:33:22.819] --- SCENARIO START: gateway.yml (8 commands) ---
[14:33:22.819] --- CMD 1/8 START: Flash The Device (!ProgramJlink) ---
[14:33:22.862] dut  | boot: RW612 rev A2
[14:33:23.012] dut  | wifi: connecting
[14:33:23.219] --- CMD 1/8 END: PASS (0.40s) ---
[14:33:23.220] --- CMD 2/8 START: Set Wifi Credentials (!BleCentral) ---
[14:33:23.313] dut  | mqtt: connected
[14:33:23.520] --- CMD 2/8 END: FAIL (0.30s): no BLE peripheral advertising ... ---
[14:33:23.529] --- SCENARIO END: 1/2 passed in 0.70s ---
```

A `FAIL` marker carries the error, so you can find the DUT output immediately
before a failure without cross-referencing the report.

## Surviving a DUT reset

A scenario that flashes or reboots the DUT (`!ProgramJlink`, a BLE write that
resets it) makes a USB CDC console such as `/dev/ttyACM0` **disappear and
re-enumerate** part-way through the run. That is expected, so the reader waits
it out and reattaches, recording both events in the combined log:

```
[14:34:01.575] --- DUT LOG PORT LOST: /dev/ttyACM0 (device disconnected...) ---
[14:34:03.076] --- DUT LOG PORT REATTACHED: /dev/ttyACM0 ---
```

Two consequences worth knowing:

*   **Output emitted while the port was down is gone.** Nothing is buffered on
    the device's side, so the very first boot lines after a reset can be lost
    if the console reappears slightly later than the DUT starts printing.
*   **A reset on a bench with more than one CDC device may come back on a
    different path** (`ttyACM0` → `ttyACM1`), which the reader will not follow.
    Use a stable `by-id` path or a udev symlink if that applies:
    `/dev/serial/by-id/usb-...-if00`.

The one case treated as fatal is the **first** open: if the console can't be
opened when the run starts, the scenario aborts before any command executes,
rather than completing with a convincing but empty device log. The reason is
written to the logs before the process exits.

## Install

Needs `pyserial`, already pulled in via the repo's requirements chain:

```bash
pip install -r requirements.txt
```

## Use in a scenario

Either declare it in the scenario:

```yaml
dut_log:
  port: "/dev/ttyACM0"
  baud: 115200      # optional, defaults to 115200
commands:
  - ...
```

or pass it per run, which overrides the scenario's value:

```bash
python main.py -t scenarios/gateway.yml --dut-log /dev/ttyACM0
python main.py -t scenarios/gateway.yml --dut-log /dev/ttyACM0 --dut-log-baud 921600
```

The CLI flag exists because the console's device path is a property of the test
*node*, not the test — a scenario shared across benches shouldn't have one
node's path baked in. With neither set, the run still produces all three logs;
`device.log` just says no console was configured, so an empty one is never
ambiguous.

## Standalone capture

For checking a bench before involving a scenario:

```bash
python tools/dut_logger/dut_logger.py --port /dev/ttyACM0 --duration 10
python tools/dut_logger/dut_logger.py --port /dev/ttyACM0            # until Ctrl-C
```

Writes the same three log files (derived from `--out`, default
`results/dut_capture.html`; the `.html` itself is not written) and prints where
they went. Exit codes: `0` clean, `2` the port could not be opened.

## In Docker

Pass the console through like any other serial device:

```bash
docker run --rm --device /dev/ttyACM0 \
  -v "$PWD/results:/app/results" \
  gora-testing-tool -t scenarios/gateway.yml --dut-log /dev/ttyACM0
```

`results/` must be bind-mounted or the logs are written inside the container
and lost with it. For a node whose DUT re-enumerates mid-scenario, prefer
`--privileged -v /dev:/dev` over a single `--device` binding — a `--device`
mapping is resolved once at container start, so the re-enumerated device would
not appear inside the container and the reader would retry forever.

## Python API

```python
from pathlib import Path
from tools.dut_logger import LogSession, DutLogger, attach, detach

session = LogSession(Path("results/run.html"))   # log names derived from this
session.open()
handler = attach(session)          # tool logging now also goes to the session

logger = DutLogger(session, port="/dev/ttyACM0", baud=115200)
logger.start()                     # raises ConnectionError if it can't open

session.write_marker("CMD 1/2 START: Flash (!ProgramJlink)")
session.write_device("a line as if from the DUT")
session.write_tool("a line as if from the tool")
session.write_device_note("why the device log is empty")

logger.stop()
detach(handler)
session.close()

session.tool_path, session.device_path, session.combined_path
```

`LogSession` is safe to write from any thread — the reader thread and the
scenario runner both write to the combined log — and every line is flushed as
it is written, so a run killed mid-scenario still leaves a usable log.
