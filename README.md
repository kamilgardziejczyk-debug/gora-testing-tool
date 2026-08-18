# Gora Testing Tool

An automated, YAML-driven test execution and hardware control tool designed to parse test scenarios, control relays (e.g. on a Raspberry Pi), manipulate USB switches, run terminal commands, simulate sub-GHz sensors, interact with Bluetooth LE devices over GATT, listen to messages published to AWS IoT Core, drive a device's Zephyr shell over UART, and flash device microcontrollers using both `esptool` and SEGGER `J-Link`.

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
    On a Raspberry Pi (or anywhere you want `!RelayControl` to drive real pins instead of falling back to simulation), also install:
    ```bash
    pip install -r requirements-rpi.txt
    ```

### Running in Docker on a Raspberry Pi test node

A `Dockerfile` is provided so a new HIL test node can be provisioned without
installing Python packages on the Pi itself. **This layer covers relay,
serial and BLE scenarios** — J-Link and MQTT need additional device access
that later steps add.

Build the image on the Pi (native build, no cross-compilation needed):

```bash
docker build -t gora-testing-tool .
```

Run a scenario, passing through the serial device it needs:

```bash
docker run --rm \
  --device /dev/ttyUSB0 \
  -e TZ=Europe/Dublin \
  -v "$PWD/firmware:/app/firmware:ro" \
  -v "$PWD/results:/app/results" \
  gora-testing-tool \
  -t scenarios/jlink_test.yml
```

Notes on this invocation:

*   Arguments after the image name go straight to `main.py`, so every flag in
    section 2 works unchanged.
*   Add `--device /dev/gpiomem` too for a scenario that also uses
    `!RelayControl` — that's what `RPi.GPIO` memory-maps to drive pins.
    Without it, `!RelayControl` falls back to logging the pin change instead
    of performing it, and the scenario still passes — so an absent device is
    easy to miss. Check the logs for `RPi.GPIO is not available` to confirm
    you are driving real hardware.
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
*   The adapter must be **powered on** on the host, or `!BleCentral` fails
    fast with `No powered Bluetooth adapters found` before it ever scans.
    `deploy_docker_to_rpis.sh` provisions each node so this survives reboots
    (`AutoEnable=true` in `/etc/bluetooth/main.conf`, `rfkill unblock
    bluetooth`), and the container also tries `bluetoothctl power on` itself
    at startup as a best-effort fallback — but that fallback can't reach an
    `rfkill`-blocked adapter (that needs host privileges the container
    doesn't have), so a node set up outside that script may still need a
    manual `rfkill unblock bluetooth && bluetoothctl power on` once.

#### Running `!ProgramJlink` scenarios in Docker

The image bundles SEGGER's J-Link tools (`JLinkExe`), fetched from SEGGER's
download server at build time — no separate install step on the Pi. Unlike
GPIO/serial's fixed device paths, a J-Link probe enumerates as a USB device
that can renumber, so it needs the whole USB bus rather than one `--device`:

```bash
docker run --rm \
  --privileged \
  -v /dev/bus/usb:/dev/bus/usb \
  -e TZ=Europe/Dublin \
  -v "$PWD/firmware:/app/firmware:ro" \
  -v "$PWD/results:/app/results" \
  gora-testing-tool \
  -t scenarios/jlink_test.yml
```

*   `--privileged -v /dev/bus/usb:/dev/bus/usb` grants access to the whole
    USB bus rather than one node, since the probe can enumerate under a
    different `/dev/bus/usb/<bus>/<device>` path each time it's plugged in
    or power-cycled — a single `--device` binding would need updating to
    match.
*   The image installs the J-Link `.deb` unversioned, from SEGGER's own
    "latest" URL (there's no stable versioned URL for arm64) — a rebuild can
    therefore pick up a newer J-Link release; check `dpkg -s jlink` inside
    the container if you need to know exactly which one landed.
*   The `.deb`'s installer normally reloads udev rules for already-connected
    probes; the image stubs that out since there's no udev daemon in a
    container and devices instead reach it via the bind-mount above, so
    nothing depends on that step actually running.

#### Running `!UsbSwitch` scenarios in Docker

The image bundles `uhubctl`, which is what switches power on an individual
MEGA4 hub port. It talks to the hub over raw USB, so it needs exactly the same
passthrough as the J-Link probe above:

```bash
docker run --rm \
  --privileged \
  -v /dev/bus/usb:/dev/bus/usb \
  -e TZ=Europe/Dublin \
  -v "$PWD/results:/app/results" \
  gora-testing-tool \
  -t scenarios/power_cycle.yml
```

*   Without the passthrough, `tools/usb_hub` cannot reach the hub and the
    scenario **fails** with a message naming the missing access — deliberately
    unlike `!RelayControl`, which silently simulates. The one case that does
    simulate is `uhubctl` being absent from the image entirely, which only
    happens on an image built before it was added; the log line to look for is
    `not installed on this platform. Simulating`.
*   On the **host**, disable USB autosuspend or ports come back in
    unpredictable states: add `usbcore.autosuspend=-1` to `/boot/cmdline.txt`
    and reboot. This is a one-off per node and cannot be done from inside the
    container.
*   Running the standalone CLI on the host as a non-root user needs a udev rule
    for the hub's vendor ID — see the [tool README](tools/usb_hub/README.md).
    Inside the container it is not needed, since `--privileged` already implies
    root access to the bus.
*   `firmware:` values in the YAML (e.g. `zephyr.hex`) resolve the same as
    outside Docker, against whatever `firmware_dir`/`--firmware` gives —
    typically the mounted `/app/firmware`.

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

The workflow that runs a scenario belongs in **the repo whose commits should
trigger it** — e.g. `scenarios/gateway.yml` exercises the gateway firmware,
so its workflow lives in the `gora-gateway` repo, not here. `GH_REPO` (above)
must point at that same repo for the runner to pick the job up at all — a
runner only ever sees workflows defined in the repo it's registered against.
This repo just supplies the image `/app/main.py` and `scenarios/` run from;
`gora-gateway`'s own `.github/workflows/gateway.yml` would look like:

```yaml
name: Gateway Scenario

on:
  workflow_dispatch:

jobs:
  gateway:
    runs-on: [self-hosted, rpi]
    steps:
      - name: Download firmware artifact
        uses: actions/download-artifact@v4
        with:
          name: gora-gateway-${{ github.sha }}
          path: firmware

      - name: Run gateway scenario
        run: |
          python /app/main.py -t /app/scenarios/gateway.yml \
            -f "$GITHUB_WORKSPACE/firmware" -r /app/results
```

It's `workflow_dispatch`-only (no push/PR trigger) since the scenario
flashes real firmware and drives real BLE/MQTT/sub-GHz hardware. No checkout
of `gora-gateway` (or of this repo) is needed: `main.py` and `scenarios/`
already live at `/app`, baked into the image at build time, so the step
invokes them by absolute path rather than relying on `working-directory`.
That leaves `$GITHUB_WORKSPACE` (the runner's own per-job workspace, unrelated
to `/app`) free for `download-artifact` to drop the firmware built by an
earlier job into, which `-f` then points `!ProgramJlink` at — overriding the
`firmware:` value baked into the YAML — and `-r /app/results` pins the report
to the bind-mounted, persisted results directory rather than the ephemeral
job workspace.

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
    repo's own `origin` remote, but in practice you almost always want it
    set explicitly to whichever repo's workflows should trigger a run (e.g.
    `gora-gateway`) — see the note above on where that workflow file lives.
*   Each node's runner name/label defaults to the part before `@` in its SSH
    target (`rpi1@...` → label `rpi1`), so a workflow can target one
    specific Pi with `runs-on: [self-hosted, rpi1]`. Give it an explicit
    name instead with `target:name`, e.g.
    `rpi@192.168.1.42:rpi1 rpi@192.168.1.43:rpi2` — needed whenever more
    than one node logs in as the same SSH user (a common Pi default), since
    otherwise they'd all derive the same label and fight over it.
*   GPIO (`/dev/gpiomem`) and BLE (the D-Bus socket) are passed to every
    node by default, since every Pi 4 test node has both.
*   It also provisions each node's Bluetooth adapter to auto-power on every
    boot (skipped with a warning on a node with no `bluetoothctl` at all) —
    see the note in the `!BleCentral` Docker section above.
*   `EXTRA_DOCKER_RUN_ARGS` (optional): flags appended to every node's
    `docker run` for anything that *does* vary per node, e.g.
    `EXTRA_DOCKER_RUN_ARGS='--device /dev/ttyUSB0'` for serial scenarios, or
    `'--privileged -v /dev/bus/usb:/dev/bus/usb'` on a node with a MEGA4 hub —
    it applies the same to every target in one invocation, so group nodes
    with matching extra hardware into separate script runs if they differ.
*   At the end, it prints each node's container IP (from `docker inspect`
    on that node) alongside its SSH target.

**QEMU emulation is registered for you.** Before building, the script checks
whether the active buildx builder reports `linux/arm64` and, if it doesn't,
runs the privileged `tonistiigi/binfmt` container to register the handlers:

```bash
docker run --privileged --rm tonistiigi/binfmt --install arm64
```

This reads like one-time setup but isn't — the handlers live in the kernel's
`binfmt_misc`, which is cleared by every reboot, so on a plain Linux Docker
install it typically re-runs after each one. Without it the build fails
several minutes in, at the first `RUN` step, with a bare
`exec /bin/sh: exec format error` — every preceding layer comes from cache, so
it looks like the Dockerfile broke rather than the emulation. Docker Desktop
(Mac/Windows) bundles the emulation, so the step never triggers there.

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
*   `--dut-log` (Optional): Serial port carrying the DUT's own console (e.g. `/dev/ttyACM0`), captured for the whole run into the log files below. Overrides the scenario's `dut_log` block, since the console's device path is a property of the test *node*, not the test.
*   `--dut-log-baud` (Optional): Baud rate for `--dut-log`. Defaults to the scenario's value, else `115200`.
*   `--dut-cli` (Optional): Serial port carrying the DUT's *shell* (e.g. `/dev/ttyACM1`), which [`!DutCli`](#dutcli) commands send to. Overrides the scenario's `dut_cli` block, for the same reason `--dut-log` overrides `dut_log`. Must be a different port from `--dut-log`: one process reading a port is what makes framing a shell response possible at all.
*   `--dut-cli-baud` (Optional): Baud rate for `--dut-cli`. Defaults to the scenario's value, else `115200`.

### Test Report

Every run writes an HTML report once it finishes, whether every command passed or a command failed and stopped the scenario early — the report always reflects whatever actually ran. It contains:
*   A masthead with the scenario file name, when the run started, the elapsed time, the number of checks, and links to the log files below.
*   A pass tally — `passed / total` with a progress meter, coloured green when the run is clean and red when anything failed.
*   One row per executed command: its `name`, its tag (click to expand its exact YAML source), the `validation` expression it was checked against and what was actually observed (blank for commands with no assertion of their own, e.g. anything other than `!MqttExpect`), how long it took, and a PASS/FAIL chip (with the error message, if it failed). Failed rows are tinted so they stand out when scanning.
*   The total wall-clock time for the run, under the table.

The report is a single self-contained file with no external assets, and follows the light/dark preference of whatever opens it.

A command that fails stops the scenario at that point, same as before this existed — the report is generated either way, so a partial run still leaves a record of what happened.

### Log Files

Alongside the report, every run writes five logs sharing its name — so `results/gateway_20260730_143322.html` comes with:

| File | Contents |
| --- | --- |
| `gateway_20260730_143322.tool.log` | The tool's own log output, timestamped |
| `gateway_20260730_143322.device.log` | The DUT's serial console, timestamped |
| `gateway_20260730_143322.mqtt.log` | Every MQTT message received, plus each session's connect/subscribe/disconnect, timestamped |
| `gateway_20260730_143322.cli.log` | Every `!DutCli` shell transaction: the command sent (`->`), each reply line (`<-`), and log output that arrived while it ran (`<~`) |
| `gateway_20260730_143322.combined.log` | All four interleaved, plus per-command `START`/`END` markers carrying PASS/FAIL |

The combined log is the one to read when a test fails: it shows what the DUT was saying and what the broker carried at the moment a command failed, without cross-referencing timestamps by hand. Each line is tagged with the source it came from:

```
[14:33:41.204] --- CMD 7/9 START: shadow update arrives (!MqttExpect) ---
[14:33:41.318] dut  | gora: publishing shadow update
[14:33:41.492] mqtt | gateway-01-listener  rx       <- $aws/things/gateway-01/shadow/update  {"state":{"reported":{"rssi":-31}}}
[14:33:41.494] tool | INFO wrappers.mqtt_expect_wrapper: MqttExpect: session 'iot' topic '$aws/things/gateway-01/shadow/update' satisfied 'count == 1' (got 1)
[14:33:41.495] --- CMD 7/9 END: PASS (0.29s) ---
```

Capturing the DUT console needs either `--dut-log` (above) or a top-level `dut_log` block in the scenario. Driving the DUT's shell with [`!DutCli`](#dutcli) needs the same for its own UART, as `--dut-cli` or a top-level `dut_cli` block — the two are configured identically, and a scenario may declare either, both, or neither:

```yaml
dut_log:
  port: "/dev/ttyACM0"   # the console the DUT prints to
  baud: 115200           # optional, defaults to 115200
dut_cli:
  port: "/dev/ttyACM1"   # the shell the DUT accepts commands on
  baud: 115200           # optional, defaults to 115200
commands:
  - ...
```

Notes:
*   The tool log starts before the scenario is parsed, so a scenario that fails to load still leaves a log explaining why — as does a run that dies part-way, since every line is flushed as it is written.
*   A DUT that resets mid-scenario (`!ProgramJlink`, a BLE write that reboots it) makes its USB console disappear and re-enumerate. That is handled: the reader reattaches and notes both events in the combined log. Output emitted while the port was down is lost, and a bench with several CDC devices may need a stable `/dev/serial/by-id/...` path.
*   If the console **cannot be opened when the run starts**, the scenario aborts before any command executes rather than finishing with a convincing but empty device log.
*   A board whose **console is also its programming port** (an ESP32 on `/dev/ttyUSB0`) cannot be captured and flashed at the same time — that port belongs to the USB-UART bridge, so it never disappears and two readers simply split its bytes between them. Hand it over around the flash with [`!DutLogControl`](#dutlogcontrol); a scenario that forgets to is rejected at load rather than failing its flash at random.
*   The console and the shell (`--dut-cli`) must be **different ports**, and a scenario configuring both on one is rejected at load: one process reading a port is what makes framing a shell response possible at all.
*   With no DUT console configured, all five logs are still written; `device.log` says so explicitly, so an empty one is never ambiguous. A run with no [`!MqttSubscribe`](#mqttsubscribe) simply leaves `mqtt.log` empty, and one with no [`!DutCli`](#dutcli) leaves `cli.log` empty.
*   `mqtt.log` records every message the broker delivered, written as it arrives and *before* it is buffered for a check — so it stays a complete record whether or not an [`!MqttExpect`](#mqttexpect) consumed the message, and even for traffic no check ever looked at. That is what separates "the gateway never published" from "it published, but a later check was looking at the wrong topic". Payload line breaks are escaped as `\n` to keep one message per line.
*   Every `mqtt.log` line carries the session's `client_id`, since a scenario can hold several broker sessions open at once and they all share the one file.

*   Captured device lines are also kept in memory (the last 5000) as well as written to disk, so a scenario can assert on what the DUT said with [`!DutLogExpect`](#dutlogexpect) instead of only reading the log afterwards.

See [`tools/dut_logger`](tools/dut_logger/README.md) for the marker format, the standalone bench-check CLI, Docker notes, and the Python API — including reading captured device lines.

### Execution Examples

#### 1. Running the NXP FRDM-RW612 J-Link flashing scenario:
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
*   `firmware`: (Required) App firmware filename.
*   `address`: (Optional) Load address for `firmware` (e.g. `0x20000`). Defaults to `0x10000`, the standard ESP-IDF app partition offset.
*   `bootloader`: (Optional) Bootloader filename, flashed at `0x0000`. Omit it to leave the bootloader already on the chip untouched.
*   `partition_table`: (Optional) Partition table filename, flashed at `0x8000`. Omit it to leave the table already on the chip untouched.
*   `timeout_s`: (Optional) Fail the step if flashing doesn't finish within this many seconds. Defaults to no timeout. Note the difference from `!ProgramJlink`: esptool runs in-process rather than as a subprocess, so the timeout fails the command (stopping the scenario) but cannot interrupt a write already in progress.

If the DUT's console is being captured on the same port this flashes (the usual ESP32 case, where both are `/dev/ttyUSB0`), bracket this command with [`!DutLogControl`](#dutlogcontrol) so the two do not read the port at once. A scenario that does not is rejected before the first command runs.

Giving only `firmware` (plus `port`/`firmware_dir`) flashes the app alone — the quick edit-flash-test loop, matching `!ProgramJlink`'s single-binary form. Adding `bootloader` and `partition_table` performs the full three-image flash, in ascending address order:

```yaml
  - !ProgramEsptool:
    name: "Flash The Tracker App"
    port: "/dev/ttyUSB0"
    firmware: "tracker.bin"
    timeout_s: 120

  - !ProgramEsptool:
    name: "Full Flash From Scratch"
    port: "/dev/ttyUSB0"
    bootloader: "bootloader.bin"
    partition_table: "partition-table.bin"
    firmware: "tracker.bin"
```

### `!RelayControl`
Energizes or de-energizes one channel of an 8-channel relay board over the Raspberry Pi GPIO header (requires `RPi.GPIO`). Wraps `tools/relay_board` — see [its README](tools/relay_board/README.md) for wiring, power supply notes, active-low/active-high polarity, and the standalone CLI/REPL.
*   `name`: (Optional) Descriptive log name.
*   `relay`: (Required) Relay number, 1-8.
*   `state`: (Required unless `pulse_s` is given) `1` (energized) or `0` (de-energized). Leaves the relay in that state after the command returns.
*   `pulse_s`: (Required unless `state` is given) Energizes the relay, waits this many seconds, then de-energizes it again — one command, for simulating a momentary button push. Mutually exclusive with `state`.
*   `wait_after_s`: (Optional) Time in seconds to sleep after executing the change.

### `!UsbSwitch`
Switches power on one port of a UUGear MEGA4 USB hub, cutting VBUS to whatever is plugged into it — a hard power cycle of the device, not a soft reset. Wraps `tools/usb_hub` — see [its README](tools/usb_hub/README.md) for the `uhubctl`/udev setup, the Docker passthrough, and the standalone CLI/REPL.
*   `name`: (Optional) Descriptive log name.
*   `port`: (Required) Port number `1`-`4`, or a name defined in the scenario's `usb_hub.ports` block.
*   `state`: (Required unless `cycle_s` is given) `1`/`true` (powered) or `0`/`false` (unpowered). Leaves the port in that state after the command returns.
*   `cycle_s`: (Required unless `state` is given) Cuts power, waits this many seconds, then restores it — one command, for power-cycling a DUT. Mutually exclusive with `state`.
*   `wait_after_s`: (Optional) Time in seconds to sleep after executing the change.

Two behaviours worth knowing before you time a scenario around this tag:

*   **Powering a port off takes seconds, powering on is instant.** The kernel re-powers a port the moment a device disappears from it, so the request has to be retried until it gives up. Budget for it, or a following `!DutLogExpect` will start its timeout while the DUT is still on its way down.
*   **Ports switched off are powered back on when the scenario ends**, including when it ends by failing — mirroring how relays are released. A run that dies between a `state: 0` and its matching `state: 1` would otherwise leave the DUT dark, and the *next* run would fail for a reason that has nothing to do with what it was testing. A scenario that deliberately ends with a port off does not get to keep it off.

Ports can be given names in a top-level `usb_hub` block, so a scenario says what it is switching rather than where it happens to be plugged in:

```yaml
usb_hub:
  location: "1-1.2"     # optional: which MEGA4, for a bench with more than one
  ports:
    dut_power: 3
    debugger: 1

commands:
  - !UsbSwitch
    name: "Power-cycle the DUT"
    port: dut_power
    cycle_s: 2.0
    wait_after_s: 5
```

Both the block and each of its fields are optional: with no block at all, the only attached MEGA4 is discovered automatically and ports are addressed by number. Names are resolved *before the first command runs*, so a typo fails the scenario immediately rather than half way through a run that has already flashed the DUT.

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
      - add: co                # add 3 more of the same type in one action
        count: 3
        wait_after_ms: 1000
      - set: "1 temp 30 humidity 70"   # set <sensor_id> <field> <value> ...
        wait_after_ms: 5000
      - del: 1                 # del <sensor_id>
        wait_after_ms: 1000
    ```
    Sensor ids are assigned per command, starting at `1` in the order the `add` actions run — so the first `add` above is `#1`. Each `!SubghzSim` command starts with an empty sensor list; ids from an earlier command are gone.

    `count`: (Optional, `add` only) Adds this many sensors of the same type in one action, e.g. `count: 3` on an `add: co` assigns them the next 3 free ids in order. `wait_after_ms` still applies once, after all of them are added, not between each. Defaults to `1`. Using it on any other verb is rejected at parse time.

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

Everything the session receives goes to the run's [`mqtt.log` and `combined.log`](#log-files) as it arrives, whether or not a later `!MqttExpect` counts it.

### `!MqttExpect`
Asserts a message-count expression against one `topic` filter within a `!MqttSubscribe` session, e.g. `validation: "count == 2"`. Place it **after** the command that triggers the device, so the assertion covers what that action actually produced.

Since a session can carry more than one topic, this only counts messages whose topic matches `topic` — matched the same way a broker matches a subscription filter against a concrete topic, so `topic` can itself use `+`/`#` wildcards. Anything read off the session that doesn't match is put back for a later command to see, so a second `!MqttExpect` on a different topic within the same session still sees its own traffic.

MQTT delivery has no "no more messages coming" signal, so this generally waits out the full `timeout_s` window rather than stopping as soon as the count looks right — a straggler arriving just after would otherwise go unnoticed. The exception is when the running count already makes the final verdict certain before the window ends (e.g. `count == 2` can no longer pass once a 3rd message has arrived, and `count >= 2` can no longer fail once the 2nd has); in that case it stops waiting immediately instead of running out the clock.

Does not close the session, so a scenario can `!MqttExpect` more than once against the same session — for example, once per topic. The runner closes any session still open once the scenario ends, including after a failure.
*   `name`: (Optional) Descriptive log name.
*   `session`: (Required) Session name given to `!MqttSubscribe`.
*   `topic`: (Required) Which topic filter (out of the session's `topics`) to count messages on.
*   `count_by`: (Optional) A field name in the payload. Counts *distinct values of that field* instead of messages — see below. Omit it to count messages, as before.
*   `validation`: (Required) A `"count <op> <n>"` expression, where `<op>` is one of `==`, `!=`, `>=`, `<=`, `>`, `<` and `<n>` is a non-negative integer. Examples: `"count == 2"`, `"count >= 1"`, `"count < 5"`.
*   `timeout_s`: (Optional) Seconds to wait for messages to arrive. Defaults to `10`.

#### Counting samples instead of messages (`count_by`)

A device that batches makes the message count meaningless. The gateway's journal uploads everything it has accumulated on a fixed 120s timer, so the same 192 sub-GHz samples might arrive as one publish or five — the split is a property of that timer, not of the gateway forwarding correctly. `count_by` moves the count onto the payload's own items:

```yaml
  - !MqttExpect:
    name: "Collect Simulated Sensors Messages"
    session: "iot"
    topic: "gora/gateway-01/journal"
    count_by: "seq"          # count distinct 'seq' values, not messages
    validation: "count == 192"
    timeout_s: 150           # must clear a whole 120s upload period
```

Each payload is parsed as JSON and may be a **single object or an array of them**, so the batch size never matters. Counting *distinct* values has a second benefit: QoS 1 is at-least-once, so a redelivered batch would otherwise inflate the count and let a broken run pass. Notes:

*   Pick a field that is **unique per item** — a journal `seq` is ideal. A field with repeats (`type`, `id`) would collapse them and count far fewer than arrived.
*   A payload that is not JSON, an item without the field, or a field holding an object/array **fails the check with that payload quoted**. It deliberately does not count zero: a silent zero is indistinguishable from "the device published nothing", which is the one wrong conclusion this check must never invite.
*   When every counted value is an integer, a failure also reports the range that arrived and how many values are **missing inside it** — with a monotonic counter that says *which* samples were dropped, not just how many.
*   `timeout_s` has to cover the device's whole publish period, not just its transfer time. Samples can be stored moments after an upload fires, leaving a full period's wait for the next one.

A failed assertion **fails the scenario**, logging every message counted for this check, plus (if it differs) everything else buffered on the session across every topic, to make it debuggable without touching the broker directly. Quoted payloads are abbreviated past 300 characters, since a few batches of 48 samples would otherwise bury the failure itself; `mqtt.log` holds them in full, alongside timestamps to compare against the DUT's own output in `combined.log`.

### `!MqttDisconnect`
Closes a session opened by `!MqttSubscribe`. Optional — the runner closes any session still open when the scenario ends, including after a failure. Use it to free a client id partway through a scenario, for example so the device can reconnect with it.
*   `name`: (Optional) Descriptive log name.
*   `session`: (Required) Session name given to `!MqttSubscribe`. An unknown name logs a warning rather than failing the scenario.

### `!DutLogExpect`
Asserts that the DUT's serial console emitted a line satisfying `validation`, failing the scenario if it never does. For the things only the device can tell you — whether its wall clock was set, whether a subsystem came up — rather than reading `device.log` by eye after the run.

Needs a DUT console to be captured (`--dut-log`, or a `dut_log` block). A scenario using this tag without one is **rejected before the first command runs**, since the check cannot pass and finding out early saves a flash and a full provisioning cycle.

```yaml
  - !DutLogExpect:
    name: "Wall Clock Set From NTP"
    validation: 'Wall clock set from \S+: unix=[0-9]+'
    timeout_s: 60
```

*   `name`: (Optional) Descriptive log name.
*   `validation`: (Required) A Python regular expression, *searched* against each captured line (it need not match the whole line).
*   `since`: (Optional) How much of the capture to search. `scenario` (default) searches the whole run, including output from before this command. `command` searches only from this command onwards.
*   `timeout_s`: (Optional) Seconds to wait for a matching line. Defaults to `30`.

Three things to know:

*   **Use single quotes around the validation.** A regex in double quotes (`"...\S+..."`) is a YAML *scanner error*, because YAML treats `\S` as an invalid escape — unlike every other field in these scenarios, which are conventionally double-quoted. Single-quoted YAML passes backslashes through untouched, so `'\S+'` and `'\d{4}'` work as written.
*   **It searches output captured before it runs**, so it can be placed anywhere after the action that provokes the line. A DUT does not wait to be asked: the gateway sets its clock about 16 s into boot, which on a scenario that resets it early is several commands before anything reads for it. This is why the default `since` is `scenario` — a wait-only check would sit out its whole timeout while the line it wanted was already captured. Use `since: command` when a match from *before* an action would be a false pass, such as re-checking a sync after a deliberate reset.
*   **A retry is not a failure.** Assert that a line eventually appears; don't try to assert a warning never did. The gateway's first NTP query routinely fails with `-11` (`EAGAIN` — DNS isn't usable in the instant after DHCP) and the next attempt succeeds, so a "no NTP errors" check would fail every healthy run. Bound how long a retry may take with `timeout_s` instead.

A failed match **fails the scenario**, logging how many lines were examined and the last 15 the DUT emitted, so the report shows what it *was* saying. If lines have been evicted from the in-memory buffer (over 5000 captured), the failure says so rather than implying the DUT definitely never emitted the line — `device.log` remains complete either way.

Matching is against the line **as the firmware emitted it**; the `[HH:MM:SS.mmm]` prefix in the log files is added by this tool and is not part of what the regex sees. A timestamp the firmware prints itself — such as the gateway's own `[2026-08-04T06:25:01,707000Z]` — *is* matchable, which makes `validation: '^\[19[0-9]{2}-'` a way to spot a device still running on an unsynced 1970 clock.

### `!DutLogControl`
Stops and starts the run's DUT console capture, for the board whose console **is** its programming port. An ESP32 behind a USB-UART bridge logs on `/dev/ttyUSB0` and is flashed on `/dev/ttyUSB0`, and both cannot read it at once: the kernel gives each byte to whichever reader asks first, so a capture left running through a flash quietly eats parts of esptool's handshake and the flash fails in ways that look random. Bracket the flash to hand the port over explicitly:

```yaml
  - !DutLogControl
    name: "Release The Console For Flashing"
    state: 0

  - !ProgramEsptool:
    name: "Flash The Tracker"
    port: "/dev/ttyUSB0"
    firmware: "tekpadz.bin"
    timeout_s: 180

  - !DutLogControl
    name: "Capture The Console Again"
    state: 1
    wait_after_s: 120
```

*   `name`: (Optional) Descriptive log name.
*   `state`: (Required) `1` captures, `0` stops capturing — spelled like [`!RelayControl`](#relaycontrol)'s `state`.
*   `reset_dut`: (Optional) Whether taking the console back may reset the device. Defaults to `0`.

Needs a DUT console to be captured (`--dut-log`, or a `dut_log` block), and a scenario using this tag without one is **rejected before the first command runs**, exactly as [`!DutLogExpect`](#dutlogexpect) is.

Four things to know:

*   **A forgotten bracket is caught before the bench is touched.** A scenario that opens the console's port while capture is still holding it is rejected at load, naming the command and what to add — as is a [`!DutLogExpect`](#dutlogexpect) scheduled at a point where capture is stopped, which could only ever find nothing. This is a static walk over the commands in order, so it costs nothing at runtime and does not depend on the flash actually failing to reveal the mistake.
*   **`state: 0` returns only once the port is genuinely closed**, not once the reader has been asked to close it — otherwise the overlap it exists to prevent would survive it. `state: 1` likewise opens the port itself, so a console that cannot be recovered fails that command rather than silently logging nothing for the rest of the run.
*   **`reset_dut` defaults to `0` because DTR and RTS are wired to reset.** On these boards, opening the port reboots the chip through the usual auto-reset transistor pair. A `state: 1` after flashing is normally there to capture the boot esptool has just started, and resetting would throw away exactly that. The choice sticks for later reconnects too, since the wiring doesn't change mid-run.
*   **Put the post-flash wait on the resume, not on the flash.** `wait_after_s` sleeps *after* a command, so leaving it on `!ProgramEsptool` sleeps through the boot with nothing listening. Moving it to the `state: 1` command means the same wait happens with capture running, and the whole boot lands in `device.log`.

Output the DUT emitted **while capture was stopped is gone**, not replayed — the input buffer is cleared as the port reopens, which on a shared port is what keeps the programmer's own traffic from being decoded as console lines. Keep the `state: 1` immediately after the flash so the window is as small as possible; that is also what puts the boot itself inside the capture.

Both states are idempotent: stopping twice, or resuming something already running, is not an error. The tag states what the capture's state should be, not a transition it must be correctly positioned to perform. Each handover is marked in the combined log (`DUT LOG PORT RELEASED` / `DUT LOG PORT RECLAIMED`), so the gap in the device log is explained rather than looking like a silent DUT.

A board with a *separate* console — the RW612 in `scenarios/gateway.yml`, which logs on `/dev/ttyACM0` while being flashed over SWD — never needs this tag.

### `!DutCli`
Sends one command to the DUT's Zephyr shell over UART and, optionally, asserts on the reply. The other side of [`!DutLogExpect`](#dutlogexpect): rather than waiting for the DUT to volunteer something on its console, this *asks* it and checks the answer — for state the device will only report when queried (`gora status`), and for driving it (provisioning, resets) without a BLE or MQTT round trip. Wraps `tools/dut_cli`, which is also runnable standalone for poking at a device by hand:

```bash
python tools/dut_cli/dut_cli.py --port /dev/ttyACM1 "gora status" -c "kernel version"
```

Needs a shell UART to be configured (`--dut-cli`, or a `dut_cli` block). A scenario using this tag without one is **rejected before the first command runs**, exactly as `!DutLogExpect` is without a console.

```yaml
  - !DutCli:
    name: "Gateway Reports Itself Online"
    command: "gora status"
    validation: 'state:\s*connected'
    timeout_s: 5
```

*   `name`: (Optional) Descriptive log name.
*   `command`: (Required) The line typed at the shell, e.g. `"gora status"`.
*   `validation`: (Optional) A Python regular expression, *searched* against the reply (it need not match the whole reply, and may span lines with an explicit `\n`). Omit it to run a command for its effect and just log what came back.
*   `timeout_s`: (Optional) Seconds to wait for the DUT's reply. Defaults to `3`. Raise it for a command the device takes real time over (clearing a journal, a flash erase) — the command is sent once and this is how long its answer is waited for, never a budget for re-sending it.

Five things to know:

*   **Use single quotes around the validation**, for the same YAML reason as `!DutLogExpect`: `"...\s..."` is a scanner error, `'...\s...'` passes the backslash through untouched.
*   **The reply is framed from the command's own echo**, and ends at the following prompt. Requiring the echo is what makes `timeout_s` mean anything: a prompt the DUT had already sent — it was sitting at one before the port was opened, or the shell's sync answer arrived as two — lands just after the command goes out and would otherwise be read as that command's terminator, returning an empty reply in ~0s with the timeout never spent. This assumes the firmware echoes, i.e. Zephyr's default `CONFIG_SHELL_ECHO=y`; with echo disabled every command fails with "the DUT never echoed ...". The port is also drained to silence before each command, so a reply always starts from an empty wire.
*   **The reply is matched, not the DUT's log output.** Zephyr's logging backend usually shares the shell UART, so `<inf>` lines can land in the middle of a response; they are separated out and never matched against (a failure quotes them separately, since they often explain the reply). The command's own echo and the trailing prompt are stripped too, so a pattern is written against what the command actually printed.
*   **One shell serves the whole run.** It is opened by the first `!DutCli` — not at start-up, so a scenario may flash the DUT first — and closed when the run ends. If the port has gone when a command is sent (a DUT that reset since the last one), it is reopened once and the command retried.
*   **A command the shell refuses fails the scenario** regardless of `validation` — `command not found`, `wrong parameter count` and friends mean the scenario is written against a firmware that does not have this command, which no pattern could sensibly assert against. A command that *ran* and returned news the test dislikes is an ordinary `validation` failure instead.

A failed match **fails the scenario**, quoting the reply and any log output that arrived while the command ran. A reply that never comes fails differently, and says which half broke: "the DUT never echoed *x*" (it may not have received the command at all) versus "*x* was echoed but no prompt followed" (it is still working on it), the latter quoting however much of the reply did arrive.

Unlike `!DutLogExpect`, `timeout_s` is not a poll budget — the command is sent exactly once and the reply waited for, since a shell command may have side effects and re-issuing `gora reset` or a provisioning write would execute it twice. A state that has not settled yet is therefore a mismatch, not something to wait out. Poll for one with the DUT console (`!DutLogExpect`) or a `!Loop`, or bound it by placing the check after a `wait_after_s`.

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
