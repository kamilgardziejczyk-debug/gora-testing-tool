# Gora Testing Tool

An automated, YAML-driven test execution and hardware control tool designed to parse test scenarios, toggle GPIOs (e.g. on a Raspberry Pi), manipulate USB switches, run terminal commands, simulate sub-GHz sensors, interact with Bluetooth LE devices over GATT, listen to messages published to AWS IoT Core, and flash device microcontrollers using both `esptool` and SEGGER `J-Link`.

---

## 1. Setup and Installation

### Prerequisites
*   Python 3.10 or higher. (The codebase uses PEP 604 unions such as `-> Wrapper | None` in function signatures, which are evaluated at import time, so 3.9 and earlier fail immediately with a `TypeError`.)
*   (Optional but recommended) SEGGER J-Link Software and Documentation Pack installed (adds `JLinkExe` / `JLink.exe` to your PATH).

### Installation Steps
1.  **Clone the repository** and navigate to the project directory:
    ```bash
    cd gora-testing-tool
    ```
2.  **Create and activate a virtual environment**:
    ```bash
    python3 -m venv .venv
    source .venv/bin/activate
    ```
3.  **Install the required dependencies**:
    ```bash
    pip install -r requirements.txt
    ```
    On a Raspberry Pi (or anywhere you want `!GpioControl` to drive real pins instead of falling back to simulation), also install:
    ```bash
    pip install -r requirements-rpi.txt
    ```

### Running in Docker on a Raspberry Pi test node

A `Dockerfile` is provided so a new HIL test node can be provisioned without
installing Python packages on the Pi itself. **This layer covers GPIO,
serial and BLE scenarios** — J-Link and MQTT need additional device access
that later steps add.

Build the image on the Pi (native build, no cross-compilation needed):

```bash
docker build -t gora-testing-tool .
```

Run a scenario, passing through the GPIO and serial devices:

```bash
docker run --rm \
  --device /dev/gpiomem \
  --device /dev/ttyUSB0 \
  -e TZ=Europe/Dublin \
  -v "$PWD/firmware:/app/firmware:ro" \
  -v "$PWD/results:/app/results" \
  gora-testing-tool \
  -t scenarios/test.yml -p /dev/ttyUSB0
```

Notes on this invocation:

*   Arguments after the image name go straight to `main.py`, so every flag in
    section 2 works unchanged.
*   `/dev/gpiomem` is what `RPi.GPIO` memory-maps to drive pins. Without it,
    `!GpioControl` falls back to logging the pin change instead of performing
    it, and the scenario still passes — so an absent device is easy to miss.
    Check the logs for `RPi.GPIO is not available` to confirm you are driving
    real hardware.
*   `results/` must be bind-mounted or the HTML report is written inside the
    container and lost when it exits.
*   `firmware/` is mounted read-only; it is `.gitignore`d and therefore not
    part of the image.
*   Certificates are deliberately **not** baked into the image (see
    `.dockerignore`); they are mounted when MQTT support is added.
*   `--device` bindings are resolved once at container start. If a DUT
    power-cycles mid-scenario and re-enumerates, the node disappears from the
    container. Scenarios that reset the device need `--privileged -v /dev:/dev`
    instead; this is covered in a later step along with stable udev symlinks.
*   The container runs as root so it does not have to match the host's
    `dialout` and `gpio` group IDs, which differ across Raspberry Pi OS
    releases.

#### Running `!BleCentral` scenarios in Docker

No extra image layer is needed for BLE: `tools/ble_gatt` depends only on
`bleak>=3.0`, already installed via the image's `-r requirements.txt` chain.
On Linux, bleak talks to the **host's** `bluetoothd` over D-Bus rather than
touching `/dev` directly, so this needs a D-Bus socket, not a device
passthrough:

```bash
docker run --rm \
  -v /var/run/dbus:/var/run/dbus \
  -e TZ=Europe/Dublin \
  -v "$PWD/results:/app/results" \
  gora-testing-tool \
  -t scenarios/gateway.yml
```

*   `bluetooth.service` must be running on the **Pi itself**, not the
    container — BlueZ owns the adapter; the container only ever talks to it
    over D-Bus.
*   No `--device` or `--privileged` is needed for the adapter, because the
    container never opens the HCI device directly. The container already
    runs as root, which is what lets the D-Bus connection satisfy BlueZ's
    default policy without extra grants.
*   If a `!BleCentral` command times out scanning/connecting from inside the
    container even though `bluetoothctl` works fine on the host, try
    `--net=host` as a fallback — it should not normally be required.
*   `adapter:` in the YAML (e.g. `hci0`) still refers to the host's adapter
    name, unchanged from running outside Docker.

#### Running as a GitHub Actions self-hosted runner

`entrypoint.sh` (the image's `ENTRYPOINT`) has two modes, chosen by whether
`GH_PAT` and `GH_REPO` are set at `docker run` time:

*   **Neither set (default):** unchanged one-shot behavior — `docker run
    gora-testing-tool -t scenarios/gateway.yml` runs that scenario and
    exits, exactly as in every example above.
*   **Both set:** the container registers itself as a GitHub Actions
    self-hosted runner for `GH_REPO` and runs in the foreground instead.
    Workflow job steps (e.g. `run: python main.py -t scenarios/gateway.yml`)
    then execute *inside this same container* — that's how a self-hosted
    runner works, so no `docker exec` or extra plumbing is needed. On
    `docker stop` it deregisters itself before exiting.

```bash
docker run -d --name gora-node --restart unless-stopped \
  -e GH_PAT=ghp_xxx \
  -e GH_REPO=owner/repo \
  -e RUNNER_NAME=rpi1 \
  -e RUNNER_LABELS=rpi1 \
  -v /var/run/dbus:/var/run/dbus \
  -v "$PWD/firmware:/app/firmware:ro" \
  -v "$PWD/results:/app/results" \
  gora-testing-tool
```

*   `GH_PAT`: a GitHub PAT with "Administration" write access on `GH_REPO`
    (fine-grained) or the classic `repo` scope — used to mint a fresh
    registration/removal token from the GitHub API each time, since a
    manually-generated registration token expires after about an hour.
    Visible via `docker inspect` on whatever host runs the container, so
    scope it tightly.
*   `RUNNER_LABELS` lets a workflow target one specific node's hardware,
    e.g. `runs-on: [self-hosted, rpi1]`.
*   Add `--device`/other flags here the same way the sections above do, for
    whichever scenario tags this node's workflows actually exercise.

#### Deploying the image to multiple Raspberry Pi nodes

Building natively on every single Pi does not scale once you have a fleet
of them. `deploy_docker_to_rpis.sh` cross-builds the image once, on your
(x86_64) PC, using Docker Buildx with QEMU emulation for `linux/arm64`,
streams it into `docker load` on every target Pi over a single SSH pipe
per target, then starts (or replaces) each one as a self-hosted runner per
the section above — no registry, no local tarball:

```bash
GH_PAT=ghp_xxx ./deploy_docker_to_rpis.sh rpi1@192.168.1.42 rpi2@192.168.1.43
```

*   `GH_PAT` (required): as described above. `GH_REPO` defaults to this
    repo's own `origin` remote; set it explicitly to target a different one.
*   Each node's runner name/label defaults to the part before `@` in its SSH
    target (`rpi1@...` → label `rpi1`), so a workflow can target one
    specific Pi with `runs-on: [self-hosted, rpi1]`. Give it an explicit
    name instead with `target:name`, e.g.
    `rpi@192.168.1.42:rpi1 rpi@192.168.1.43:rpi2` — needed whenever more
    than one node logs in as the same SSH user (a common Pi default), since
    otherwise they'd all derive the same label and fight over it.
*   GPIO (`/dev/gpiomem`) and BLE (the D-Bus socket) are passed to every
    node by default, since every Pi 4 test node has both.
*   `EXTRA_DOCKER_RUN_ARGS` (optional): flags appended to every node's
    `docker run` for anything that *does* vary per node, e.g.
    `EXTRA_DOCKER_RUN_ARGS='--device /dev/ttyUSB0'` for serial scenarios —
    it applies the same to every target in one invocation, so group nodes
    with matching extra hardware into separate script runs if they differ.
*   At the end, it prints each node's container IP (from `docker inspect`
    on that node) alongside its SSH target.

One-time setup on a plain Linux Docker install (Docker Desktop on Mac/Windows
already bundles this):

```bash
docker run --privileged --rm tonistiigi/binfmt --install arm64
```

If a target Pi doesn't have Docker yet (e.g. a freshly imaged SD card), the
script installs it automatically via the official `get.docker.com` script
and adds the SSH user to the `docker` group — this needs passwordless
`sudo` on that account (the default on a Pi set up through Raspberry Pi
Imager); otherwise it stops with instructions to install Docker manually.

The image is tagged with the local `git rev-parse --short HEAD`, so
`docker images` on any node shows exactly which commit it is running. The
emulated build is noticeably slower than a native one — expect it to take
longer than building the same image directly on a Pi.

`scenarios/` is baked into the image, so editing a scenario file needs a
rebuild + redeploy, not a file copy; `firmware/` and `results/` are
bind-mounted from `~/gora-testing-tool/` on each node (created
automatically) and are not touched by rebuilds.

##### Troubleshooting runner registration

If a node comes up but the runner never appears under the repo's
Settings → Actions → Runners, check what actually happened inside the
container — with `--restart unless-stopped`, a registration failure just
crash-loops silently instead of surfacing in the deploy script's output:

```bash
ssh <target> docker logs --tail 50 gora-node
```

*   **`GH_REPO` must be `owner/repo`**, e.g. `kamilgardziejczyk-debug/gora-testing-tool`
    — not a full URL. The entrypoint builds `https://github.com/${GH_REPO}`
    itself, so a URL value doubles up wrong.
*   **A `curl ... 404` fetching the registration token** almost always means
    the PAT can't see that repo, not that the repo doesn't exist — GitHub
    returns 404 rather than 403 for a repo a token has no access to, to
    avoid confirming it exists. A fine-grained PAT only covers the
    repositories explicitly picked under "Repository access" *when it was
    created*, each with its own permissions — pointing `GH_REPO` at a repo
    the PAT wasn't scoped to (or was scoped to without "Administration:
    Read and write") reproduces this exactly. Fix it under
    `https://github.com/settings/personal-access-tokens`: add the repo to
    the token's access list (or generate a new token scoped to it) with
    "Administration: Read and write", then redeploy.

---

## 2. Running Scenarios

The tool is executed using `main.py`. You specify the path to a scenario YAML file and, optionally, override the target serial port or firmware directory via the command line.

### Command-Line Arguments
*   `-t, --test` (Required): Path to the YAML test scenario file.
*   `-p, --port` (Optional): Serial port for flashing (e.g. `/dev/ttyUSB0`). Overrides the port specified inside the YAML file for all `!ProgramEsptool` commands. Not applicable to `!SubghzSim`, which uses a different device/port and is always configured via its own `port` field in the YAML — see below.
*   `-f, --firmware` (Optional): Path to the directory containing firmware binaries (such as `.bin`, `.hex`, or `.elf`). Overrides the directory for all `!ProgramEsptool` and `!ProgramJlink` commands.
*   `-r, --report` (Optional): Path to write the HTML test report to. A directory (existing, ending in `/`, or just a bare name with no `.html` suffix like `reports`) gets a default-named report file written inside it, rather than becoming the report file itself. Defaults to `results/<scenario>_<timestamp>.html`.

### Test Report

Every run writes an HTML report once it finishes, whether every command passed or a command failed and stopped the scenario early — the report always reflects whatever actually ran. It contains:
*   The scenario file name and when the run started.
*   One row per executed command: its `name`, its tag (click to expand its exact YAML source), the `validation` expression it was checked against and what was actually observed (blank for commands with no assertion of their own, e.g. anything other than `!MqttExpect`), how long it took, and PASS/FAIL (with the error message, if it failed).
*   The total wall-clock time for the run, under the table.

A command that fails stops the scenario at that point, same as before this existed — the report is generated either way, so a partial run still leaves a record of what happened.

### Execution Examples

#### 1. Running the Standard ESP32 flasher & GPIO Loop scenario:
```bash
python main.py -t scenarios/test.yml -p /dev/ttyUSB0 -f /path/to/my/firmware/binaries
```

#### 2. Running the NXP FRDM-RW612 J-Link flashing scenario:
```bash
python main.py -t scenarios/jlink_test.yml -f /path/to/my/nxp/firmware
```

---

## 3. Supported Scenario Tags

You can design custom test scenarios under `scenarios/` using the following YAML tags:

### `!ProgramJlink`
Programs a microcontroller using SEGGER J-Link Commander (`JLinkExe`/`JLink.exe`).
*   `name`: (Optional) Descriptive log name.
*   `device`: (Required) MCU device name (e.g. `RW612` for NXP RW612, `STM32F407VE`).
*   `interface`: (Optional) Debug interface (`SWD`, `JTAG`). Defaults to `SWD`.
*   `speed`: (Optional) Connection clock speed in kHz. Defaults to `4000`.
*   `firmware_dir`: (Required if not overridden via `-f` / `--firmware`) Directory containing the firmware binary. A relative path resolves against the scenario file's directory, so a scenario and its firmware can be moved together as one portable tree. A CLI `-f`/`--firmware` value overrides this entirely and is used as-is (relative to the shell's working directory, like any other CLI argument).
*   `firmware`: (Required) Filename of the binary to flash.
*   `address`: (Required for `.bin` / raw files) The load address (e.g., `0x18000000`). Automatically omitted for `.hex` and `.elf` files since J-Link automatically parses internal addresses.
*   `timeout_s`: (Optional) Kill `JLinkExe`/`JLink.exe` and fail the step if it doesn't finish within this many seconds. Defaults to no timeout.

### `!ProgramEsptool`
Flashes an ESP32 microcontroller using the `esptool` library.
*   `name`: (Optional) Descriptive log name.
*   `port`: (Required if not overridden via `-p` / `--port`) Destination serial port.
*   `baudrate`: (Optional) Upload baudrate. Defaults to `460800`.
*   `firmware_dir`: (Required if not overridden via `-f` / `--firmware`) Directory containing the firmware binaries. A relative path resolves against the scenario file's directory, so a scenario and its firmware can be moved together as one portable tree. A CLI `-f`/`--firmware` value overrides this entirely and is used as-is (relative to the shell's working directory, like any other CLI argument).
*   `bootloader`: (Required) Bootloader filename.
*   `partition_table`: (Required) Partition table filename.
*   `firmware`: (Required) App firmware filename.

### `!GpioControl`
Toggles Raspberry Pi GPIO pins (requires `RPi.GPIO`).
*   `name`: (Optional) Descriptive log name.
*   `pin`: (Required) BCM pin number.
*   `state`: (Required) `true` (HIGH) or `false` (LOW).
*   `wait_after_s`: (Optional) Time in seconds to sleep after executing the pin change.

### `!UsbSwitch`
**No-op stub** — `execute()` only logs a warning; no USB switch hardware is actually controlled yet. Implement `UsbSwitchWrapper.execute()` before relying on this in a real scenario.
*   `name`: (Optional) Descriptive log name.
*   `state`: (Required) `true` (enabled) or `false` (disabled).
*   `wait_after_s`: (Optional) Time in seconds to wait.

### `!ExecuteCommand` (or `!ExecuteCommand:`)
Runs a host terminal command using shell execution.
*   `name`: (Optional) Descriptive log name.
*   `command`: (Required) The bash command string.
*   `timeout_s`: (Optional) Kill the command and fail the step if it doesn't finish within this many seconds. Defaults to no timeout.
*   `wait_after_s`: (Optional) Wait time in seconds after command execution.

### `!SubghzSim`
Runs a scripted sub-GHz simulator session over a serial link: opens the port, applies a sequence of sensor actions with waits in between, keeps the simulator reporting for `duration_s`, then closes the link. Wraps `tools/subghz_sim` — see [its README](tools/subghz_sim/README.md) for the standalone REPL, the wire format, and the Python API.
*   `name`: (Optional) Descriptive log name.
*   `port`: (Required) Serial port the simulator connects to. Set directly in the YAML — not overridable via `-p` / `--port`, since a scenario may also flash a device (e.g. `!ProgramEsptool`) on a different port at the same time. An OS device path (e.g. `/dev/ttyUSB0`), not a file, so unlike `firmware_dir` it is never resolved relative to the scenario file.
*   `baud`: (Optional) Baud rate. Defaults to `115200`.
*   `interval_s`: (Optional) Heartbeat interval in seconds — how often every live sensor's current state is re-sent. Defaults to `5`.
*   `duration_s`: (Optional) Total time in seconds to keep the simulator active, measured from when the port is opened. If the scripted `actions` finish before `duration_s` elapses, the simulator keeps running (still sending its periodic heartbeat) for the remaining time before it's closed. Has no effect if the actions already take longer than `duration_s`.
*   `actions`: (Optional) A list of simulator commands to run in order. Each entry has exactly one verb key (`add`, `set`, `del`, or `list`) plus an optional `wait_after_ms`:
    ```yaml
    actions:
      - add: temp_hum          # add <heat|smoke|co|temp_hum>
        wait_after_ms: 1000
      - set: "1 temp 30 humidity 70"   # set <sensor_id> <field> <value> ...
        wait_after_ms: 5000
      - del: 1                 # del <sensor_id>
        wait_after_ms: 1000
    ```
    Sensor ids are assigned per command, starting at `1` in the order the `add` actions run — so the first `add` above is `#1`. Each `!SubghzSim` command starts with an empty sensor list; ids from an earlier command are gone.

    A bad action **fails the scenario** rather than being skipped: an unknown sensor type, a non-numeric id or a malformed `set` is rejected while parsing the file, before any hardware is touched, and an unknown sensor id or a field the sensor's type does not have fails when the action runs.

### `!BleCentral`
Acts as a Bluetooth LE central: connects to a peripheral by advertised name (or address), runs a sequence of `actions`, then disconnects. Wraps `tools/ble_gatt` — see [its README](tools/ble_gatt/README.md) for the standalone REPL, UUID/value notation, and troubleshooting.

Self-contained like `!SubghzSim`: the connection lives for this command only, so nothing is left holding the adapter (and blocking the device from advertising) afterwards. This also makes it the right tool for waiting on something *after* a device reset that drops the BLE link: reconnecting is a fresh `!BleCentral` command, not something the command that triggered the reset stays open for.
*   `name`: (Optional) Descriptive log name.
*   `device`: (Required) The peripheral's advertised name (e.g. `GoraGateway_01B4EE`), or its Bluetooth address (`AA:BB:CC:DD:EE:FF`). A name is resolved by scanning; an address connects directly, skipping the scan.
*   `service`: (Optional) Default service UUID for every action below. Each can override it with its own `service`.
*   `actions`: (Required) A non-empty list run in order, on the one connection. Each entry has exactly one verb key. `read` and `notify` also accept `attempts` and `retry_wait_ms` (below), so either can retry itself without a separate wrapping construct.
    *   `write`: Write a characteristic.
        *   `uuid`: (Required) Characteristic UUID — 16-bit shorthand (`2a00`) or full 128-bit.
        *   `value`: (Required) The value to write, interpreted per `encoding`. An empty string (`""`) is a legal zero-length write with `hex` or `utf8` (e.g. clearing a credential characteristic) — but not with `uint8`/`uint16`/`uint32`, which are always their fixed width and have no empty form.
        *   `encoding`: (Optional) `hex` (default), `utf8`, `uint8`, `uint16`, or `uint32`. Integers are little-endian, matching the Bluetooth spec's own numeric fields.
        *   `service`: (Optional) Overrides the command-level `service`.
        *   `response`: (Optional) `true` (default) waits for the device to acknowledge the write, so a rejection fails the test; `false` is fire-and-forget.
        *   `wait_after_ms`: (Optional) Pause after this action succeeds, before the next one in `actions` runs.
        *   `attempts`: Must be `1` (or omitted) — writes cannot be retried safely. Retrying a *read* is idempotent; retrying a *write* is not (e.g. a reset-trigger characteristic could fire twice if the first write's acknowledgement times out). Setting `attempts > 1` on a write fails while parsing the file. Use `read`/`notify` where a retry is needed.
    *   `read`: Read a characteristic, optionally asserting on its value.
        *   `uuid`: (Required) Characteristic UUID to read.
        *   `validation`: (Optional) A `"value <op> <literal>"` expression — see below. Omit to just read and log the value without asserting anything about it.
        *   `encoding`, `service`, `wait_after_ms`: (Optional) Same as `write`.
    *   `notify`: Wait for a `validation` expression to be satisfied by a **pushed** notification. Ignores values that don't satisfy it and keeps waiting, rather than failing on the first mismatch — a device reporting an intermediate state (e.g. "booting") before the expected one is normal. A real push notification can be missed in a narrow window right after reconnecting (e.g. following a device reset); `read` with `attempts` (below) is the reliable alternative for that case.
        *   `uuid`: (Required) Characteristic UUID to subscribe to.
        *   `validation`: (Required) See below.
        *   `encoding`, `service`, `wait_after_ms`: (Optional) Same as `write`.
        *   `timeout_s`: (Optional) Seconds to wait before failing the command. Defaults to `30`.
    *   `attempts`: (Optional, `read`/`notify` only) Retries this one action, on the same connection, up to this many times before giving up. Defaults to `1` (no retry). The general way to poll: a `read` with `validation` fails whenever the value doesn't satisfy it yet, so giving it `attempts` repeats the read until it does — replacing what would otherwise need a hand-written retry loop. If a failed attempt finds the link itself has dropped, the next attempt reconnects first rather than retrying against a dead connection; if that reconnect also fails, the command fails immediately instead of exhausting the remaining attempts.
    *   `retry_wait_ms`: (Optional, any verb) Pause between a failed attempt and the next one. Defaults to `1000`. Distinct from `wait_after_ms`, which only applies once the action has succeeded.
*   `adapter`: (Optional) Bluetooth adapter to use, e.g. `hci0`. Defaults to the system default.
*   `scan_timeout_s`: (Optional) Seconds to scan when resolving `device` by name. Defaults to `8`. Raise this on a command that reconnects right after a device reboot, since it needs time to start advertising again before a scan will find it.
*   `connect_timeout_s`: (Optional) Seconds to wait for the connection itself. Defaults to `15`.

`read` and `notify`'s `validation` is a `"value <op> <literal>"` expression, where `<op>` is `==` or `!=` and `<literal>` is interpreted per `encoding`. Examples: `"value == 01"`, `"value != 00"`. Unlike `!MqttExpect`'s count, ordering operators (`>=`, `<=`, `>`, `<`) aren't supported — a GATT payload has no general ordering once encodings other than a fixed-width integer are allowed.

```yaml
  - !BleCentral:
    name: "Provision the gateway over BLE"
    device: "GoraGateway_01B4EE"
    service: "0000ffe0-0000-1000-8000-00805f9b34fb"
    actions:
      - write:
          uuid: "0000ffe1-0000-1000-8000-00805f9b34fb"
          value: "MyWifiSSID"
          encoding: utf8
          wait_after_ms: 200
      - write:
          uuid: "0000ffe2-0000-1000-8000-00805f9b34fb"
          value: "01"

  # A device reset drops the BLE link, so this is a second, independent
  # command rather than something the one above stays connected for.
  - !BleCentral:
    name: "Wait for the gateway to come back online"
    device: "GoraGateway_01B4EE"
    scan_timeout_s: 30
    actions:
      - read:
          uuid: "0000ffe3-0000-1000-8000-00805f9b34fb"
          validation: "value == 01"
          attempts: 15
          retry_wait_ms: 2000
```

A bad UUID, an unknown encoding, a malformed `validation` expression, or a value that doesn't fit it **fails while parsing the file**, before the radio is touched — so a malformed scenario cannot leave a device half-configured. A missing device, a service or characteristic the peripheral doesn't expose, a rejected write, or a `read`/`notify` assertion that isn't satisfied fails when the command runs — after exhausting `attempts`, if given one greater than `1`.

> Note: only the central role exists. A peripheral role (this host advertising its own GATT server) is not implemented yet.

### `!MqttSubscribe`
Opens a connection to an MQTT broker (built for AWS IoT Core) over mutual TLS and starts buffering messages from one or more topics. Wraps `tools/mqtt_listener` — see [its README](tools/mqtt_listener/README.md) for the standalone tool, certificate setup, and troubleshooting.

Non-blocking: it returns as soon as the broker confirms every subscription, then buffers in the background. Place it **before** the command that makes the device publish, so a gateway that forwards a message within milliseconds cannot publish before anything is listening.
*   `name`: (Optional) Descriptive log name.
*   `session`: (Required) Name to register this session under, referenced by `!MqttExpect` and `!MqttDisconnect`.
*   `endpoint`: (Required) Broker hostname (e.g. `xxxx-ats.iot.us-east-1.amazonaws.com`).
*   `client_id`: (Required) MQTT client id. Must be permitted by the IoT policy, and must differ from the device's own id — a broker allows one connection per client id, so a collision makes the listener and the device evict each other in a loop. Since a broker allows only one connection per client id, subscribing to more than one topic that shares a client id belongs in a **single** `!MqttSubscribe`'s `topics` list, not two separate `!MqttSubscribe` commands.
*   `cert`: (Required) Device certificate (PEM). Relative paths resolve against the scenario file's directory.
*   `private_key`: (Required) Private key (PEM).
*   `root_ca`: (Required) Root CA certificate (PEM), e.g. `AmazonRootCA1.pem`.
*   `topics`: (Required) A list of topic filters to subscribe to; `+` and `#` wildcards allowed. Example: `topics: ["gora/gateway-01/subghz/#", "$aws/things/gateway-01/shadow/update"]`.
*   `port`: (Optional) Broker port. Defaults to `8883`.
*   `qos`: (Optional) `0` or `1`, applied to every topic in `topics`. Defaults to `1`. IoT Core does not support QoS 2.
*   `connect_timeout_s`: (Optional) Seconds to wait for the broker's connection acknowledgement. Defaults to `10`.

### `!MqttExpect`
Asserts a message-count expression against one `topic` filter within a `!MqttSubscribe` session, e.g. `validation: "count == 2"`. Place it **after** the command that triggers the device, so the assertion covers what that action actually produced.

Since a session can carry more than one topic, this only counts messages whose topic matches `topic` — matched the same way a broker matches a subscription filter against a concrete topic, so `topic` can itself use `+`/`#` wildcards. Anything read off the session that doesn't match is put back for a later command to see, so a second `!MqttExpect` on a different topic within the same session still sees its own traffic.

MQTT delivery has no "no more messages coming" signal, so this generally waits out the full `timeout_s` window rather than stopping as soon as the count looks right — a straggler arriving just after would otherwise go unnoticed. The exception is when the running count already makes the final verdict certain before the window ends (e.g. `count == 2` can no longer pass once a 3rd message has arrived, and `count >= 2` can no longer fail once the 2nd has); in that case it stops waiting immediately instead of running out the clock.

Does not close the session, so a scenario can `!MqttExpect` more than once against the same session — for example, once per topic. The runner closes any session still open once the scenario ends, including after a failure.
*   `name`: (Optional) Descriptive log name.
*   `session`: (Required) Session name given to `!MqttSubscribe`.
*   `topic`: (Required) Which topic filter (out of the session's `topics`) to count messages on.
*   `validation`: (Required) A `"count <op> <n>"` expression, where `<op>` is one of `==`, `!=`, `>=`, `<=`, `>`, `<` and `<n>` is a non-negative integer. Examples: `"count == 2"`, `"count >= 1"`, `"count < 5"`.
*   `timeout_s`: (Optional) Seconds to wait for messages to arrive. Defaults to `10`.

A failed assertion **fails the scenario**, logging every message counted for this check, plus (if it differs) everything else buffered on the session across every topic, to make it debuggable without touching the broker directly.

### `!MqttDisconnect`
Closes a session opened by `!MqttSubscribe`. Optional — the runner closes any session still open when the scenario ends, including after a failure. Use it to free a client id partway through a scenario, for example so the device can reconnect with it.
*   `name`: (Optional) Descriptive log name.
*   `session`: (Required) Session name given to `!MqttSubscribe`. An unknown name logs a warning rather than failing the scenario.

### `!Loop`
Runs nested scenario commands sequentially multiple times.
*   `name`: (Optional) Descriptive log name.
*   `iterations`: (Required) Number of loop iterations.
*   `commands`: (Required) A list of nested scenario commands.

---

## 4. TODO — Known Issues and Cleanup

Findings from a source scan of the whole codebase. Ordered by severity; each entry names the file so it can be picked up independently. Nothing here is fixed yet.

### 4.1 Missing infrastructure

*   **No automated tests.** Everything so far has been verified with throwaway scripts against stubs and pty pairs. The pure logic is easy to cover now and would have caught several items above: `parser` tag resolution and `!Loop` expansion, `values.encode_value` / `uuids.normalize_uuid`, `subghz_sim.frame` CRC and encoding, `mqtt_expect` operator/early-exit table, and the BLE `attempts` retry loop — all with no hardware.
*   **No linter or formatter config.** No `ruff`/`flake8`/`black` setup, so the project's ≤50-line-function and PEP 8 standards are unenforced.
*   **No CI.** Nothing runs the above on a push.
