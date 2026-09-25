# Gora Testing Tool

An automated, YAML-driven test execution and hardware control tool designed to parse test scenarios, control relays (e.g. on a Raspberry Pi), manipulate USB switches, run terminal commands, simulate sub-GHz sensors, interact with Bluetooth LE devices over GATT, simulate a Bluetooth LE heart rate sensor for a device to connect to, listen to messages published to AWS IoT Core, drive a device's Zephyr shell over UART, mount and inspect a device's SD card exposed over USB mass storage, and flash device microcontrollers using both `esptool` and SEGGER `J-Link`.

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
*   The **peripheral** role (`!BleHrvSim*`, this node advertising a simulated
    sensor for a DUT to connect to) needs one thing more: **`--network host`**.
    Its GATT server uses the same D-Bus socket, but its advertisement goes
    straight to the kernel over a Bluetooth management socket, and Bluetooth
    sockets are scoped to a network namespace — from the default bridge network
    the adapter is not merely unusable but invisible, and the tag fails with
    `the Bluetooth management socket is not available in this network
    namespace`. `deploy_docker_to_rpis.sh` passes `--network host` for this
    reason; a hand-rolled `docker run` needs it too. (BlueZ is deliberately not
    asked to advertise — see [`tools/ble_gatt/README.md`](tools/ble_gatt/README.md).)
*   Advertising also needs a free advertising slot on the adapter, so a node
    that both advertises a simulated sensor and scans as a central at the same
    time is worth giving a second USB BLE dongle.
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

#### Running `!DutStorage` scenarios in Docker

Mounting the DUT's SD card needs more than the USB passthrough above, because
a mount is a kernel operation on a block device that does not exist yet when
the container starts:

```bash
docker run --rm \
  --privileged \
  -v /dev:/dev \
  -e TZ=Europe/Dublin \
  -v "$PWD/results:/app/results" \
  gora-testing-tool \
  -t scenarios/tracker.yml
```

*   **`-v /dev:/dev`, not `--device`.** `--device` bindings are resolved once
    at container start, and the card's block device only appears when the port
    is powered mid-scenario. This is the same reason the J-Link notes give for
    a DUT that re-enumerates.
*   **`--privileged`** supplies `CAP_SYS_ADMIN`, without which `mount` is
    refused. The image runs as root already, which is necessary but not
    sufficient on its own.
*   The image bundles `sg3-utils` for `sg_start`, which is how the card is
    ejected. Without it the eject step fails naming the missing binary — and
    it matters, because the eject is the only part of the sequence the DUT
    firmware actually observes.
*   Mounts are made under `/run/gora/`, inside the container's own mount
    namespace. The host does not see them, which is what you want: a mount
    that leaked into the host would outlive the run.

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
            -f "$GITHUB_WORKSPACE/firmware" -r /app/results --clean-results
```

`--clean-results` matters specifically for this runner-mode setup: the container is long-lived (`--restart unless-stopped`), so `/app/results` is not a fresh directory each job the way a one-shot `docker run` would give you - without it, every report, log and `!DutStorage` copy from every past job stays on the node's disk forever. It empties `/app/results` before this job's own report is written, leaving the directory itself (a host bind mount) in place. Off by default for local/interactive use, where keeping run history is normally what you want.

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
*   It installs a udev rule (`/etc/udev/rules.d/99-gora-dut-no-automount.rules`)
    that keeps the desktop automounter (udisks2) off cards a DUT exposes (USB
    vendor `303a`). Otherwise the desktop mounts the card next to `!DutStorage`,
    and FAT updates still pending on that mount are lost when the card is
    ejected — the kernel logs `lost async page write` and every later mount
    reports the volume as not properly unmounted.
*   The runner in the image does not update itself (`--disableupdate`): its
    self-update failed on the nodes and removed its own binaries, leaving the
    container restarting. Bump `RUNNER_VERSION` in the Dockerfile to a current
    release instead — GitHub stops sending jobs to a runner that falls too far
    behind.
*   `EXTRA_DOCKER_RUN_ARGS` (optional): flags appended to every node's
    `docker run` for anything that *does* vary per node, e.g.
    `EXTRA_DOCKER_RUN_ARGS='--device /dev/ttyUSB0'` for serial scenarios, or
    `'--privileged -v /dev/bus/usb:/dev/bus/usb'` on a node with a MEGA4 hub —
    it applies the same to every target in one invocation, so group nodes
    with matching extra hardware into separate script runs if they differ.
*   At the end, it prints each node's container IP (from `docker inspect`
    on that node) alongside its SSH target.

#### Pulling results back from a node

`copy_results_from_rpi.sh` fetches a node's HTML reports, per-run
`.tool`/`.device`/`.mqtt`/`.cli`/`.combined` logs, and any `!DutStorage`
`copy_from` output back to this machine — everything
[`analysis/analyze.py`](#analysis) needs:

```bash
./copy_results_from_rpi.sh rpi1@192.168.1.42 [local_dest]
```

`local_dest` defaults to `./results`. It goes through `docker cp` first —
`gora-node:/app/results` on the Pi into a `/tmp` staging dir there — and
only then `rsync`s that staging dir down to `local_dest`. Reading straight
from the container rather than assuming a host path means it works
regardless of where a node's `results/` bind mount actually lives, and
`rsync` means a repeat run only transfers what changed since the last one.
Nothing is removed from the container itself; that's what `--clean-results`
on `main.py` is for (see above).

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

#### Running a one-shot scenario against an already-deployed node

A node normally sits with `gora-node` running long-lived, in runner mode,
waiting for a workflow job (see above). To run a scenario against its
hardware right now, without going through a workflow dispatch, start a
second, ordinary one-shot container over SSH — it shares the same image and
the same bind-mounted `firmware/`/`results/`, and doesn't touch `gora-node`:

```bash
ssh rpi1@192.168.1.42 \
  "docker run -d --name gora-usb-stress \
     --privileged \
     -v /dev:/dev \
     -e TZ=Europe/Warsaw \
     -v /home/rpi1/gora-testing-tool/firmware:/app/firmware:ro \
     -v /home/rpi1/gora-testing-tool/results:/app/results \
     gora-testing-tool:<tag> \
     -t scenarios/tracker_usb_cycle.yml -f /app/firmware"
```

*   Safe to run alongside an idle `gora-node`: the runner only touches the
    DUT/hub while a job is actually executing, not while it's waiting for
    one — two containers can both see `/dev` without conflict as long as
    only one of them is mid-scenario at a time.
*   Use `docker images` on the node to see which `<tag>`s are already
    loaded (see "Pulling results back from a node" above for how a tag maps
    to a commit).
*   **If the scenario was added after the loaded image's commit**, check
    what that commit touched (`git show --stat <commit>`) before assuming a
    rebuild is needed. A scenario-only change (a new/edited `.yml`, no
    `main.py` or command-parser changes) can run against an older image by
    bind-mounting the file over the baked-in one instead of rebuilding:
    add `-v /path/to/scenarios/tracker_usb_cycle.yml:/app/scenarios/tracker_usb_cycle.yml:ro`
    to the command above. A scenario that needs a command type or option the
    loaded image doesn't have yet still needs the full rebuild + redeploy.
*   Flags needed follow the same rules as everywhere else in this doc —
    `--privileged -v /dev:/dev` here because `tracker_usb_cycle.yml` uses
    `!DutStorage` (see "Running `!DutStorage` scenarios in Docker" above).
*   `docker logs -f gora-usb-stress` to watch it, `docker wait
    gora-usb-stress` to block until it exits, `docker rm gora-usb-stress`
    once done (it isn't `--rm` here so logs survive a crash for inspection).

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
*   `--clean-results` (Optional): Empty the report's directory before this run, so old reports, logs and `!DutStorage` copies don't accumulate forever. Off by default — local/interactive use generally wants to keep run history; a long-lived self-hosted runner container generally does not. See the [runner-mode workflow example](#running-as-a-github-actions-self-hosted-runner).
*   `--dut-log` (Optional): Serial port carrying the DUT's own console (e.g. `/dev/ttyACM0`), captured for the whole run into the log files below. Overrides the scenario's `dut_log` block, since the console's device path is a property of the test *node*, not the test.
*   `--dut-log-baud` (Optional): Baud rate for `--dut-log`. Defaults to the scenario's value, else `115200`.
*   `--dut-cli` (Optional): Serial port carrying the DUT's *shell* (e.g. `/dev/ttyACM1`), which [`!DutCli`](#dutcli) commands send to. Overrides the scenario's `dut_cli` block, for the same reason `--dut-log` overrides `dut_log`. Must be a different port from `--dut-log`: one process reading a port is what makes framing a shell response possible at all.
*   `--dut-cli-baud` (Optional): Baud rate for `--dut-cli`. Defaults to the scenario's value, else `115200`.

### Test Report

Every run writes an HTML report once it finishes, whether every command passed or a command failed and stopped the scenario early — the report always reflects whatever actually ran. It contains:
*   A masthead with the scenario's name, when the run started, the elapsed time, the number of checks, and links to the log files below.
*   A pass tally — `passed / total` with a progress meter, coloured green when the run is clean and red when anything failed.
*   One row per executed command: its `name`, its tag (click to expand its exact YAML source), the `validation` expression it was checked against and what was actually observed (blank for commands with no assertion of their own, such as `!RelayControl` or `!UsbSwitch`), how long it took, and a PASS/FAIL chip (with the error message, if it failed). Failed rows are tinted so they stand out when scanning.
*   A heading band above each [`!Group`](#group)'s rows, naming the group and carrying its own `passed / total` tally — coloured red when anything inside it failed, so a failing stage is findable without reading every row. A scenario using a plain `commands:` list has no bands at all and looks exactly as it did before.
*   The total wall-clock time for the run, under the table.

#### Naming the run (`name`)

The masthead's heading comes from an optional **top-level `name:`** in the scenario — a title for the run, in place of the file it happens to live in:

```yaml
name: "ESP32 Tracker - flash, GNSS recording and SD card readout"

dut_log:
  port: "/dev/ttyUSB0"
  baud: 115200

groups:
  - !Group
    ...
```

*   Optional. A scenario declaring no `name:` is titled with its filename, as before — `tracker.yml`.
*   The same label is used for the combined log's `SCENARIO START` marker, so the report and the logs agree on what the run was called.
*   Purely a label: it changes nothing about what the scenario runs, and it does not affect the report's *filename*, which stays `<scenario-file-stem>_<timestamp>.html` so repeated runs still sort by scenario and time.
*   A present-but-unusable value (blank, or a mapping rather than a string) is warned about in the tool log and the filename is used instead — a bad title is not a reason to refuse to run the bench.

The report is a single self-contained file with no external assets, and follows the light/dark preference of whatever opens it.

A command that fails stops the scenario at that point, same as before this existed — the report is generated either way, so a partial run still leaves a record of what happened.

### Log Files

Alongside the report, a run writes up to five logs sharing its name — so `results/gateway_20260730_143322.html` comes with:

| File | Written when | Contents |
| --- | --- | --- |
| `gateway_20260730_143322.tool.log` | always | The tool's own log output, timestamped |
| `gateway_20260730_143322.device.log` | a DUT console is captured | The DUT's serial console, timestamped |
| `gateway_20260730_143322.mqtt.log` | the scenario opens a broker session | Every MQTT message received, plus each session's connect/subscribe/disconnect, timestamped |
| `gateway_20260730_143322.cli.log` | the scenario sends shell commands | Every `!DutCli` shell transaction: the command sent (`->`), each reply line (`<-`), and log output that arrived while it ran (`<~`) |
| `gateway_20260730_143322.combined.log` | always | All of the above interleaved, plus per-command `START`/`END` markers carrying PASS/FAIL |

**Only the logs a run actually used are written and linked from its report.** A scenario with no `!MqttSubscribe` and no `!DutCli` produces no `mqtt.log` and no `cli.log` at all, rather than empty ones — an empty artefact reads as a capture having failed, when the run simply never asked for it. The DUT log is keyed on the console being *configured*, not on the DUT having said anything: a board that was supposed to be talking and stayed silent leaves an empty `device.log`, and that emptiness is itself evidence.

A run with no DUT console still leaves a `device.log` holding a single `[no-dut]` note saying why there was nothing to capture. It is deliberately not linked from the report, so it cannot be mistaken for a capture.

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

### Analysis

Some questions a run raises are not lines a regex could match: *why did the GNSS fix drop between sessions*, *does the battery curve explain the reset*, *is anything anomalous across this run that no check asked about*. Those are correlations across a log and the card's contents.

`analysis/analyze.py` asks them. You give it a YAML file of questions and the files they are about; it uploads the files, asks one request per task, and writes the answers as markdown:

```bash
python analysis/analyze.py --tasks scenarios/tracker.analysis.yml
python analysis/analyze.py --tasks questions.yml --files results/*.device.log
```

```yaml
# scenarios/tracker.analysis.yml
tasks:
  - name: "Session gaps"
    files:
      - "results/*.device.log"
      - "results/sd/session_*/*.csv"
    questions:
      - "Correlate GNSS fix loss with power rail dips in the session CSVs."
      - "Does the battery curve explain any reset or gap in the recording?"
```

*   `name`: (Optional) What the answer is filed under. Defaults to `Task N`.
*   `questions`: (Required) What to ask about these files. A single question may be written as a plain string.
*   `files`: (Optional) Globs naming the evidence, resolved against the directory you run the command from. A task that declares none is given whatever `--files` named, so a one-off question needs no edit to the file.

Options:

*   `--tasks <file>` (Required): The YAML above.
*   `--files <glob> ...`: Evidence for every task that names none of its own.
*   `--out <file>`: Where the markdown answers go. Defaults to `analysis.md`; they are printed as well.
*   `--dry-run`: Print what would be sent — every question, every file, and its size — and stop. Sends nothing and needs neither the SDK nor a credential, so it is the cheap way to check a glob actually names the log you meant.
*   `--model`, `--effort`: Which model, and how hard it works (`low` … `max`, default `high`).
*   `--max-file-mb`: Refuse any single file larger than this (default 32). A sanity cap on a capture that ran away, not a budget.
*   `--keep-uploads`: Leave the uploaded files on the account. They are deleted once the answers are in otherwise.

Notes:

*   **Nothing here runs on the bench.** The scenario writes logs into `results/` and a [`!DutStorage`](#dutstorage) `copy_from` pulls the card's session directories into `results/sd/`; the questions are asked afterwards, on a machine that has a credential. A firmware regression suite must not be able to fail because an API call timed out, and the node needs no network, no SDK and no key.
*   **A results directory can be asked anything, whenever.** The questions are not recorded during the run, so rewording one costs a re-read of files already on disk — not a re-flash and another wait for a GNSS fix. Last month's results answer a question written today.
*   **A glob that matches nothing is a warning, not a failure** — a question about a log this run never captured is still worth asking about the logs it did. The manifest printed before anything is sent names exactly what each task got.
*   **The files are data, not instructions.** The system prompt says so: firmware can print anything, including text shaped like a request.
*   **Install separately**: `pip install -r analysis/requirements.txt`. Deliberately not in the root `requirements.txt`, so the node's Docker image does not carry an API SDK it never uses.
*   **The credential comes from the environment** — `ANTHROPIC_API_KEY`, or an `ant auth login` profile. Never from a scenario or `config.json`: both are committed, and the Dockerfile bakes `scenarios/` into the image. Under CI it belongs in the analysis step's `env:`, in a job that runs after the bench job and needs no hardware.
*   **Privacy**: this sends logs and card contents to an external API. Fine for firmware output; worth a deliberate decision if a scenario ever captures something that should not leave the bench. There is no redaction step.

### Execution Examples

#### 1. Running the NXP FRDM-RW612 J-Link flashing scenario:
```bash
python main.py -t scenarios/jlink_test.yml -f /path/to/my/nxp/firmware
```

#### 2. Checking the DUT's card with `fsck`:
```bash
python main.py -t scenarios/card_fsck.yml
```
The scenario only hands the card to the host and holds it there for three minutes, so
`fsck.vfat -a /dev/sdX` can be run against the block device meanwhile - it mounts nothing,
because fsck needs the device to itself. It then ejects the card and leaves USB power and the
DUT on, since the same port charges the board.

#### 3. Stress-testing recording stops (`tracker_usb_cycle.yml`):
```bash
python main.py -t scenarios/tracker_usb_cycle.yml -f firmware
```
Thirty cycles of recording on battery, then connecting USB, which stops the recording.
A crash at the stop usually reboots straight into storage mode and passes every check,
so read the verdict from `device.log`: no `assert failed`, no
`Boot reset reason: PANIC`, and one `APP_RECORDING -> APP_WAIT_SD_UNMOUNT` per cycle.

#### 4. Holding the DUT powered (`power_hold.yml`):
```bash
python main.py -t scenarios/power_hold.yml
```
Switches the storage USB port and the DUT's relay on and holds them for an hour, for charging a
flat battery or working on the board by hand. Stop it early with Ctrl-C (or
`docker kill --signal=SIGINT`); the run's cleanup then releases the relay.

#### 5. Stress-testing power-loss recovery (`power_cycle_stress.yml`):
```bash
python main.py -t scenarios/power_cycle_stress.yml -f firmware
```
Twenty cycles of recording on battery, then cutting relay power outright — no unmount, no
warning — and restoring it. Each cycle only checks that the board reboots at all; the real
verdict is `device.log` afterwards (no `assert failed`, no `Boot reset reason: PANIC` outside a
clean power-on reset) plus the final card mount succeeding, which proves the FAT survived every
cut without needing an external `fsck`.

#### 6. Stress-testing BLE reconnects (`ble_hrv_reconnect_stress.yml`):
```bash
python main.py -t scenarios/ble_hrv_reconnect_stress.yml -f firmware
```
Fifteen cycles of dropping the simulated heart-rate sensor off the air and back
(`!BleHrvSimSet`'s `bounce` action) while the tracker records, so it has to scan, connect,
discover and subscribe again from nothing each time. PREREQUISITE, same as `tracker_hrv.yml`:
the card must already carry a `configuration.json` whose `ble.hr_sensor_name` is exactly
`"GoraHRV_01"`. Every check in the loop verdict comes from the DUT's own console, scoped
`since: command` so a stale line from an earlier reconnect can't pass a later one.

#### 7. Calibration state (`calibration.yml`):
```bash
python main.py -t scenarios/calibration.yml -f firmware
```
Flashes the tracker, lets it record, then types `app calibrate start` with
[`!DutLogSend`](#dutlogsend). Checks that `pot` is refused while recording, that the tracker
goes `APP_RECORDING -> APP_WAIT_SD_UNMOUNT -> APP_CALIBRATION`, that `pot` and `adc` then work,
and that `app calibrate stop` restarts it into recording. Every check reads the DUT's console,
scoped `since: group`. Not yet run on the bench.

---

## 3. Supported Scenario Tags

You can design custom test scenarios under `scenarios/` using the following YAML tags:

### Validation expressions

Every tag that asserts something — `!DutLogExpect`, `!DutCli`, `!MqttExpect`, and `!BleCentral`'s `read`/`notify` — states its condition in a `validation` field, written as a **Python expression**. The values the tag measured are named in braces:

```yaml
validation: "{count} == 192"
validation: 'matches({line}, r"unix=[0-9]+")'
validation: "len({files}) == 24 and 'BOOT.CFG' not in {files}"
```

Braces are what make a scenario readable at a glance: `{count}` is plainly the thing the tag measured, where a bare `count` could be anything. They also make a typo fail **when the scenario loads**, before any hardware is touched — every bare name is rejected, so both `{cont}` and `count` are caught while the file is being read rather than at the moment the DUT is finally in the right state to be checked.

Which variables exist depends on the tag, and each tag's section below lists its own. What every expression may use:

*   **Operators**: `==`, `!=`, `<`, `<=`, `>`, `>=`, `in`, `not in`, `and`, `or`, `not`, and arithmetic.
*   **Functions**: `len`, `any`, `all`, `sorted`, `sum`, `min`, `max`, `abs`, `int`, `str`, `float`, `bool`, `set`, `matches(text, pattern)` and `matching(pattern, items)`.
*   **String methods**: `.startswith()`, `.endswith()`, `.lower()`, `.upper()`, `.strip()`, `.lstrip()`, `.rstrip()`, `.split()`, `.replace()`, `.find()`, `.count()`.
*   **List literals and comprehensions**: `{count} in [1, 2, 3]`, `any(f.endswith(".log") for f in {files})`.

Anything else — an import, a lambda, an assignment, an attribute outside that method list — is rejected when the scenario loads, naming what was disallowed. This is a guard against typos and accidents, not a security boundary: `!ExecuteCommand` already runs arbitrary shell, so it makes no attempt to contain a scenario author who means harm.

#### Globs: `matching(pattern, items)`

`in` is **exact membership**, not pattern matching. `'session_*' in {dirs}` asks whether a directory is literally named `session_*`, which nothing ever is — so it is always false, even on a card full of `session_54`, `session_55` and so on. This is an easy trap because the `path` field of the same tags *does* take globs.

`matching(pattern, items)` is the glob:

```yaml
validation: "matching('session_*', {dirs})"                # at least one
validation: "len(matching('session_*', {dirs})) >= 10"     # how many
validation: "matching('*.log', {files}) == ['boot.log']"   # exactly which
```

It returns the matching items rather than a bool, so it composes. An empty list is falsey, which is what makes the bare form read as "at least one". Shell-style wildcards (`*`, `?`, `[seq]`) and case-sensitive, matching the `path` globs.

#### Regular expressions

`matches(text, pattern)` is `re.search`: true when `pattern` is found anywhere in `text`. It is how the two log-matching tags express what used to be a bare pattern.

Quoting needs care, because three languages stack up in one line. The house pattern is **single-quoted YAML on the outside, a raw Python string on the inside**:

```yaml
validation: 'matches({line}, r"Wall clock set from \S+: unix=[0-9]+")'
```

*   **Single-quoted YAML outside.** A backslash in a double-quoted YAML scalar (`"...\S+..."`) is a YAML *scanner error*. Single-quoted YAML passes backslashes through untouched.
*   **A raw Python string inside.** `"\S+"` is a deprecated escape in Python and an error in future versions; `r"\S+"` delivers the backslash to the regex intact. A pattern written without the `r` is rejected when the scenario loads, with a message saying so.
*   **Braces inside a string are left alone.** `r"\d{2,4}"` is an ordinary quantifier, not a variable — substitution runs over Python tokens, so only a `{name}` outside a string literal is treated as one.

#### Migrating from the older syntax

`validation` used to mean four different things depending on the tag: a bare regex for `!DutLogExpect` and `!DutCli`, `count <op> <n>` for `!MqttExpect`, and `value <op> <literal>` for `!BleCentral`. A scenario still using any of those is **rejected when it loads**, with a message naming the replacement — it is never silently misread.

| Tag | Was | Now |
| --- | --- | --- |
| `!DutLogExpect` | `'Wall clock set from \S+'` | `'matches({line}, r"Wall clock set from \S+")'` |
| `!DutCli` | `'Journal cleared'` | `'matches({reply}, "Journal cleared")'` |
| `!MqttExpect` | `"count == 192"` | `"{count} == 192"` |
| `!BleCentral` | `"value == 01"` | `'{value} == "01"'` |

`!BleCentral` is the one change that is more than syntax: `read` and `notify` no longer take an `encoding` field, because the expression now names which reading of the bytes it means (`{value}`, `{text}`, `{number}`, `{size}`). `encoding` remains on `write`, where it still decides how the literal being written is encoded.

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
*   `address`: (Optional) Load address for `firmware`. Defaults to `0x10000`, the app offset of ESP-IDF's single-app partition table. OTA tables usually move the app (e.g. to `0x20000`).
*   `bootloader`: (Optional) Bootloader filename. Omit it to leave the bootloader already on the chip untouched.
*   `bootloader_address`: (Optional) Defaults to `0x0` (ESP32-S3/C3; the original ESP32 uses `0x1000`).
*   `partition_table`: (Optional) Partition table filename. Omit it to leave the table already on the chip untouched.
*   `partition_table_address`: (Optional) Defaults to `0x8000`.
*   `ota_data`: (Optional) OTA data image (`ota_data_initial.bin`). Resets the boot selection to `ota_0`, so the fresh app boots even if the device last switched to `ota_1`.
*   `ota_data_address`: (Required with `ota_data`) The `otadata` offset from the partition table. There is no default.
*   `timeout_s`: (Optional) Fail the step if flashing doesn't finish within this many seconds. Defaults to no timeout. Note the difference from `!ProgramJlink`: esptool runs in-process rather than as a subprocess, so the timeout fails the command (stopping the scenario) but cannot interrupt a write already in progress.

If the DUT's console is being captured on the same port this flashes (the usual ESP32 case, where both are `/dev/ttyUSB0`), bracket this command with [`!DutLogControl`](#dutlogcontrol) so the two do not read the port at once. A scenario that does not is rejected before the first command runs.

Giving only `firmware` (plus `port`/`firmware_dir`) flashes the app alone — the quick edit-flash-test loop, matching `!ProgramJlink`'s single-binary form. Adding `bootloader`, `partition_table` and, for an OTA partition table, `ota_data` performs a full flash. Images are written in ascending address order; overlapping images are rejected before connecting. The addresses to use are listed under `flash_files` in the build's `build/flasher_args.json`:

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

  - !ProgramEsptool:
    name: "Full Flash With An OTA Partition Table"
    port: "/dev/ttyUSB0"
    bootloader: "bootloader.bin"
    partition_table: "partition-table.bin"
    ota_data: "ota_data_initial.bin"
    ota_data_address: 0x10000
    firmware: "tracker.bin"
    address: 0x20000
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

### `!DutStorage`

Mounts the DUT's SD card, exposed over USB mass storage, and asserts on what is on it. Needs the card's hub port to be named in the scenario's `usb_hub` block, the same one `!UsbSwitch` powers.

The card is mounted the way any host would mount it, rather than read as raw blocks, because **the firmware's hand-off of the card is itself under test**. The tracker gives the card to the host by unmounting it from its own filesystem, and takes it back in two distinct steps: an eject returns control to the ESP32, and only the later loss of VBUS makes it re-mount the card for its own use. A raw-block reader would trigger neither, and would report a passing test against a firmware whose hand-off was broken.

That is why `mount`, `unmount` and `eject` are separate commands rather than something the file actions do implicitly — they are the steps being tested.

```yaml
  - !DutStorage:
    name: "Mount The Card"
    port: dut_storage
    action: mount

  - !DutStorage:
    name: "Card Holds A Log Directory"
    action: list
    path: "/"
    validation: "'logs' in {dirs}"
```

*   `name`: (Optional) Descriptive log name.
*   `action`: (Required) One of `mount`, `unmount`, `eject`, `list`, `read`, `copy_from`, `delete`.
*   `port`: (Required for `mount` and `eject`) A port number, or a name from the scenario's `usb_hub.ports` block.
*   `path`: (Required for `read`, `copy_from` and `delete`; optional for `list`, default `/`) A path **on the card**, not on the host. Resolved and then checked to still be inside the mount, so a `path` of `../../etc` is rejected rather than quietly reading the test node's own filesystem.
*   `dest`: (Required for `copy_from`) Host directory to copy into, relative to the **scenario file's** directory.
*   `mode`: (Optional, `mount` only) `ro` (default) or `rw`. Opt-in per command rather than a scenario-level default — a wrong `path` under `rw` writes to the DUT's card. `delete` needs the card mounted `rw`; everything else works read-only.
*   `settle_timeout_s`: (Optional, `mount`/`eject`) Seconds to wait for the card to enumerate after the port is powered. Defaults to `15`. Named distinctly from `timeout_s` because it genuinely is a poll budget, which is the opposite of what `timeout_s` means on `!DutCli`.
*   `validation`: (Optional) A [validation expression](#validation-expressions). Variables depend on the action:

    | Action | Variables |
    | --- | --- |
    | `list` | `{files}` `list[str]`, `{dirs}` `list[str]`, `{entries}` `list[str]` — all sorted, not recursive. No `{count}`: with both lists in scope it could only be ambiguous, so write `len({dirs})` or `len(matching('session_*', {dirs}))` |
    | `read` | `{content}` `str`, `{lines}` `list[str]`, `{size}` `int` (bytes, not decoded characters) |
    | `copy_from` | `{copied}` `list[str]` card-relative paths, `{count}` `int` |
    | `delete` | `{deleted}` `list[str]` card-relative paths, `{count}` `int` |
    | `mount`, `unmount`, `eject` | none — a `validation` on these is rejected when the scenario loads |

Things to know:

*   **`umount` alone does not reach the firmware.** Linux `umount` flushes and detaches the filesystem; it never sends SCSI `START_STOP_UNIT`. Only `eject` does. Keeping them apart is also what lets a scenario test the cable-yank path — unmount, then cut power without ejecting — as deliberately as the clean one.
*   **The block device is found by hub port, never by scanning `/dev`.** `/dev/sd*` ordering is not stable across runs, and a rig that guesses wrong writes to whichever disk it picked — on a Raspberry Pi node, quite possibly its own. Discovery walks sysfs from the port, so a wrong answer is an error rather than a wrong disk.
*   **The runner unmounts anything still mounted when the scenario ends**, before it restores USB port power. A card left mounted when `!UsbSwitch` cuts VBUS leaves the kernel with a filesystem whose device has vanished; anything touching it blocks uninterruptibly, which outlives the run and takes the next one with it. `eject` refuses while the card is still mounted for the same reason.
*   **Assert on the firmware's side with `!DutLogExpect`.** None of the device's state machine is visible from the host. The lines worth checking are `USB Storage is now ACTIVE` (card handed over), `Storage control returned to ESP32` (eject received), and `Remounting SD card normally for application` (card reclaimed after VBUS drops). Leave those checks on the default `since: scenario` — the line arrives while the previous command is still finishing, so a check looking only forward from its own start can miss it. When a scenario hands the card over more than once, put each hand-off in its own `!Group` and use `since: group`, so a later check cannot be satisfied by the first hand-off's lines.
*   **Throughput is limited.** The tracker is a full-speed USB device, so expect around 1 MB/s. Keep `copy_from` scoped to a subdirectory rather than the whole card.
*   **Scope `path` to what the firmware actually wrote, not `/*`.** A card that has ever been mounted on a desktop can carry `.Trash-1000` or other metadata that is neither the DUT's output nor guaranteed to be readable — an SD card's bad sectors surface as I/O errors on exactly this kind of leftover. `copy_from` copies each match independently and does not let one bad item block the ones after it, but a narrower `path` (e.g. `/session_*`) means there is nothing irrelevant to fail on in the first place.
*   **`copy_from` logs as it goes**, not only at the end: one line before the first item starts, and one per item as it completes. On a full-speed link, several directories' worth of data can take minutes with nothing else to show for it — check the tool log if a run looks stuck; a copy still climbing through `(12/26)` is working, not hung.

#### Clearing the card (`delete`)

A scenario that asserts on the logs a firmware wrote wants to know they are *this* run's logs. The cheapest way to be sure is to start from a card with none:

```yaml
  - !DutStorage:
    name: "Mount The Card For Writing"
    port: dut_storage
    action: mount
    mode: rw

  - !DutStorage:
    name: "Clear Last Run's Logs"
    action: delete
    path: "/logs/*"

  - !DutStorage:
    name: "Clear Every Session Directory"
    action: delete
    path: "**/session-*"
```

`path` takes the same globs the other actions do, including `**` to match at any depth — which is how you delete every directory of a given name without knowing where they are. Note that `**` walks the whole card, and the card is on a full-speed link.

Three things behave differently from the read-only actions:

*   **Matching nothing is a success, not a failure.** This is the opposite of `copy_from`, which fails when it collects nothing. Clearing a card has to be idempotent: the same scenario run twice would otherwise fail the second time precisely because the first one worked. Assert on `{count}` if you do care that something was there.
*   **A directory is deleted with everything under it.** That is the point — a firmware writing one directory per session leaves exactly that to clear — but it means a too-broad glob is not recoverable. There is no undo and no trash.
*   **It refuses on a read-only card**, naming the fix, rather than surfacing a read-only-filesystem errno from inside the copy machinery.

The card root itself (`path: "/"`) is rejected, and a path or glob resolving outside the mount is rejected — `"/../*"` reaches the test node's own filesystem otherwise, which is a check that genuinely fires rather than a theoretical one.

`scenarios/tracker.yml` runs the full sequence end to end. The standalone CLI (`python3 tools/mass_storage/mass_storage.py -l 1-1.2 -p 1 discover`) drives the same code by hand, which is the way to confirm an eject reaches the firmware before a scenario depends on it.

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
        *   `validation`: (Optional) An expression over the value read — see below. Omit to just read and log the value without asserting anything about it.
        *   `service`, `wait_after_ms`: (Optional) Same as `write`.
    *   `notify`: Wait for a `validation` expression to be satisfied by a **pushed** notification. Ignores values that don't satisfy it and keeps waiting, rather than failing on the first mismatch — a device reporting an intermediate state (e.g. "booting") before the expected one is normal. A real push notification can be missed in a narrow window right after reconnecting (e.g. following a device reset); `read` with `attempts` (below) is the reliable alternative for that case.
        *   `uuid`: (Required) Characteristic UUID to subscribe to.
        *   `validation`: (Required) See below.
        *   `service`, `wait_after_ms`: (Optional) Same as `write`.
        *   `timeout_s`: (Optional) Seconds to wait before failing the command. Defaults to `30`.
    *   `attempts`: (Optional, `read`/`notify` only) Retries this one action, on the same connection, up to this many times before giving up. Defaults to `1` (no retry). The general way to poll: a `read` with `validation` fails whenever the value doesn't satisfy it yet, so giving it `attempts` repeats the read until it does — replacing what would otherwise need a hand-written retry loop. If a failed attempt finds the link itself has dropped, the next attempt reconnects first rather than retrying against a dead connection; if that reconnect also fails, the command fails immediately instead of exhausting the remaining attempts.
    *   `retry_wait_ms`: (Optional, any verb) Pause between a failed attempt and the next one. Defaults to `1000`. Distinct from `wait_after_ms`, which only applies once the action has succeeded.
*   `adapter`: (Optional) Bluetooth adapter to use, e.g. `hci0`. Defaults to the system default.
*   `scan_timeout_s`: (Optional) Seconds to scan when resolving `device` by name. Defaults to `8`. Raise this on a command that reconnects right after a device reboot, since it needs time to start advertising again before a scan will find it.
*   `connect_timeout_s`: (Optional) Seconds to wait for the connection itself. Defaults to `15`.

`read` and `notify` take a [validation expression](#validation-expressions) over the characteristic's value, offered in four readings:

| Variable | Type | Holds |
| --- | --- | --- |
| `{value}` | `str` | the bytes as lowercase hex, e.g. `"01ff"` |
| `{text}` | `str` | the bytes decoded as UTF-8, undecodable bytes replaced |
| `{number}` | `int` | the bytes as a little-endian unsigned integer |
| `{size}` | `int` | how many bytes arrived |

Examples: `'{value} == "01"'`, `'{value} != "00"'`, `"{number} >= 10"`, `'{text}.startswith("OK")'`, `"{size} == 4"`.

Which reading is meaningful belongs to the characteristic, so the expression names it directly rather than an `encoding` field deciding how a literal is interpreted — `"{number} == 1"` where a scenario used to say `encoding: uint8` plus `value == 1`. Little-endian, matching the Bluetooth spec's own numeric fields. `encoding` still applies to `write`, which has a literal to encode.

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
          validation: '{value} == "01"'
          attempts: 15
          retry_wait_ms: 2000
```

A bad UUID, an unknown encoding on a `write`, or a malformed `validation` expression **fails while parsing the file**, before the radio is touched — so a malformed scenario cannot leave a device half-configured. A missing device, a service or characteristic the peripheral doesn't expose, a rejected write, or a `read`/`notify` assertion that isn't satisfied fails when the command runs — after exhausting `attempts`, if given one greater than `1`.

> Note: only the central role exists. A peripheral role (this host advertising its own GATT server) is not implemented yet.

### `!BleHrvSimStart` / `!BleHrvSimSet` / `!BleHrvSimStop`

Simulates a Bluetooth LE **heart rate sensor** for a DUT to connect to — the mirror image of `!BleCentral`. Where that tag connects *to* a peripheral, these advertise one: a standard Heart Rate Service (`0x180D`) streaming measurements with the RR intervals that carry the HRV, plus Battery and Device Information. Wraps `tools/ble_gatt`'s peripheral role — see [its README](tools/ble_gatt/README.md) for the standalone REPL (`--peripheral`) and the advertising rules.

Unlike `!BleCentral`, this is a **session**, in the same shape as `!MqttSubscribe`/`!MqttDisconnect`: the sensor must keep advertising and streaming *while other commands run*, because a DUT records from it across a whole test. `!BleHrvSimStart` puts it on the air under a `session` name; `!BleHrvSimSet` drives it; `!BleHrvSimStop` ends it. The runner stops any session still running when the scenario ends, so a scenario that fails part-way still frees the adapter.

**Nothing is streamed until a central subscribes.** Beats generated with nobody listening would be counted but never sent, which would make the totals `!BleHrvSimStop` asserts on meaningless.

A scenario waits for that moment **on the DUT's own console**, not here: the first measurement the DUT logs cannot appear unless it wrote the CCCD, so a `!DutLogExpect` on that line proves the subscription *and* proves the DUT parsed what arrived — which the sensor's own view of its subscription does not. Give it a `timeout_s` generous enough to cover the whole scan, connect, discover and subscribe sequence. The rule this follows is worth keeping in mind generally: **assert on the DUT, not on the simulator.** What the sensor sent is evidence about the sensor; only the DUT's log and its card are evidence about the thing under test.

```yaml
  - !BleHrvSimStart:
    name: "Advertise A Heart Rate Sensor"
    session: hrv
    device: "GoraHRV_01"
    bpm: 60
    jitter_ms: 25
    seed: 1

  # Proof the tracker subscribed - and parsed what arrived.
  - !DutLogExpect:
    name: "Tracker Logs A Measurement"
    validation: 'matches({line}, r"HR: \d+ bpm")'
    timeout_s: 60

  - !BleHrvSimSet:
    name: "Raise The Pulse, Then Go Quiet"
    session: hrv
    actions:
      - bpm: 120
        wait_after_ms: 3000
      - stall: 10

  - !BleHrvSimStop:
    name: "Stop The Sensor"
    session: hrv
    validation: "{subscribed} and {rr_intervals} >= 60"
```

#### `!BleHrvSimStart`

*   `name`: (Optional) Descriptive log name.
*   `session`: (Required) Name later `!BleHrvSim*` commands use to reach this sensor.
*   `device`: (Required) The name the sensor **advertises**. The DUT must be configured to look for exactly this name — the tracker compares it with `strcmp`. At most **22 characters**: one advertising PDU holds 31 bytes and the name must share it with the `0x180D` service UUID. A longer name is rejected when the scenario loads, because the alternative is worse than an error — BlueZ would move it into the scan response, and a DUT parsing each PDU separately would never see the name and the service together, so it would simply never connect, with nothing logged at either end.
*   `bpm`: (Optional) Starting pulse, 20–250. Defaults to `60`.
*   `jitter_ms`: (Optional) Beat-to-beat variability — this *is* the HRV. Defaults to `25`. Set `0` for a metronome, which is the way to prove a consumer is reading real variability rather than deriving it from the pulse.
*   `drift_bpm_per_min`: (Optional) Pulse change per minute, e.g. `10` to ramp up. Defaults to `0`.
*   `interval_s`: (Optional) Seconds between notifications. Defaults to `1`.
*   `seed`: (Optional) Seeds the beat generator so a run replays exactly. Without it every run differs, which makes a failure hard to reproduce.
*   `battery_pct`: (Optional) Initial battery level, 0–100. Defaults to `100`.
*   `location`: (Optional) Body sensor location: `chest` (default), `wrist`, `finger`, `hand`, `ear-lobe`, `foot`, `other`.
*   `contact`: (Optional) `yes` (default), `no`, or `none`. Three states, not a boolean: `none` means the sensor does not report contact at all, which is a different thing from reporting that it has none.
*   `adapter`: (Optional) Bluetooth adapter, e.g. `hci0`. Defaults to `hci0`. A node that both advertises a sensor and scans as a central wants a second dongle — see the Docker BLE notes above.

#### `!BleHrvSimSet`

*   `session`: (Required) A session opened by `!BleHrvSimStart`.
*   `actions`: (Required) A non-empty list, run in order. Any action takes `wait_after_ms` to pause before the next.

    | Action | Does |
    | --- | --- |
    | `bpm: <n>` | Change the pulse from the next beat on |
    | `contact: <yes\|no\|none>` | Skin contact state |
    | `battery: <percent>` | Set and notify the battery level |
    | `energy: <kJ\|off>` | Report energy expended in every frame, or stop. `off` removes the field; `0` reports zero, which is a different frame |
    | `burst: <count>` | Send that many RR intervals in one frame, to exceed what the central budgeted for |
    | `stall: <seconds>` | Stay connected but send nothing. Returns at once, so the gap runs while later commands do |
    | `resume` | End a stall early |
    | `bounce` | Drop off the air and come back, forcing the central to reconnect |

    `resume` and `bounce` take no argument, so they are written as bare entries (`- resume`).

#### `!BleHrvSimStop`

*   `session`: (Required) A session opened by `!BleHrvSimStart`.
*   `validation`: (Optional) A [validation expression](#validation-expressions) over the variables below, evaluated once. The counters are read *before* the sensor leaves the air — `{subscribed}` is false the moment it stops advertising, so asking afterwards would report that no central was ever there. The sensor is then stopped either way, including when the assertion fails, so a failed check never leaves the adapter advertising. Without a `validation` this just stops the sensor.

| Variable | Type | Holds |
| --- | --- | --- |
| `{subscribed}` | `bool` | whether a central is subscribed to measurements right now |
| `{notifications}` | `int` | measurements actually sent — not generated |
| `{rr_intervals}` | `int` | RR intervals actually sent |
| `{bpm}` | `int` | the sensor's current pulse |
| `{writes}` | `int` | writes the central made to the control point (`0x2A39`) |

`{rr_intervals}` is the one worth asserting on: a DUT logging one row per RR interval should hold exactly this many rows. No tag can compare the two directly — there is no variable passing between commands — but both numbers land in the report, so the comparison is one glance rather than a guess.

Asserting on `{bpm}` is pointless: a scenario that just set it is asserting on itself.

See `scenarios/tracker_hrv.yml` for the whole flow, including the fault cases.

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
Asserts a message-count expression against one `topic` filter within a `!MqttSubscribe` session, e.g. `validation: "{count} == 2"`. Place it **after** the command that triggers the device, so the assertion covers what that action actually produced.

Since a session can carry more than one topic, this only counts messages whose topic matches `topic` — matched the same way a broker matches a subscription filter against a concrete topic, so `topic` can itself use `+`/`#` wildcards. Anything read off the session that doesn't match is put back for a later command to see, so a second `!MqttExpect` on a different topic within the same session still sees its own traffic.

MQTT delivery has no "no more messages coming" signal, so this generally waits out the full `timeout_s` window rather than stopping as soon as the count looks right — a straggler arriving just after would otherwise go unnoticed. The exception is when the running count already makes the final verdict certain before the window ends (e.g. `{count} == 2` can no longer pass once a 3rd message has arrived, and `{count} >= 2` can no longer fail once the 2nd has); in that case it stops waiting immediately instead of running out the clock.

That shortcut only applies to a validation that is exactly `{count} <op> <n>`. A richer expression — one touching `{payloads}`, or combining conditions — cannot be reasoned about that way and always waits out the full `timeout_s`. Worth knowing before putting one on a check with a long window: it costs the whole window on failure rather than stopping early.

Does not close the session, so a scenario can `!MqttExpect` more than once against the same session — for example, once per topic. The runner closes any session still open once the scenario ends, including after a failure.
*   `name`: (Optional) Descriptive log name.
*   `session`: (Required) Session name given to `!MqttSubscribe`.
*   `topic`: (Required) Which topic filter (out of the session's `topics`) to count messages on.
*   `count_by`: (Optional) A field name in the payload. Counts *distinct values of that field* instead of messages — see below. Omit it to count messages, as before.
*   `validation`: (Required) A [validation expression](#validation-expressions) over what arrived:

    | Variable | Type | Holds |
    | --- | --- | --- |
    | `{count}` | `int` | messages matched, or distinct `count_by` values when that is set |
    | `{payloads}` | `list[str]` | the matched messages' payloads, in arrival order |
    | `{values}` | `list` | the distinct `count_by` values seen, or `[]` |

    Examples: `"{count} == 2"`, `"{count} >= 1"`, `'any("error" in p for p in {payloads})'`.
*   `timeout_s`: (Optional) Seconds to wait for messages to arrive. Defaults to `10`.

#### Counting samples instead of messages (`count_by`)

A device that batches makes the message count meaningless. The gateway's journal uploads everything it has accumulated on a fixed 120s timer, so the same 192 sub-GHz samples might arrive as one publish or five — the split is a property of that timer, not of the gateway forwarding correctly. `count_by` moves the count onto the payload's own items:

```yaml
  - !MqttExpect:
    name: "Collect Simulated Sensors Messages"
    session: "iot"
    topic: "gora/gateway-01/journal"
    count_by: "seq"          # count distinct 'seq' values, not messages
    validation: "{count} == 192"
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
    validation: 'matches({line}, r"Wall clock set from \S+: unix=[0-9]+")'
    timeout_s: 60
```

*   `name`: (Optional) Descriptive log name.
*   `validation`: (Required) A [validation expression](#validation-expressions), evaluated once per captured line against:

    | Variable | Type | Holds |
    | --- | --- | --- |
    | `{line}` | `str` | the line that just arrived |
    | `{log}` | `str` | every line considered so far, newline-separated |
    | `{lines}` | `list[str]` | the same, as a list |

    `{line}` is the one to reach for — the check passes as soon as any single line satisfies the expression, which is what "the DUT said this" means. `{log}` and `{lines}` are for a condition spanning several lines, e.g. `"len({lines}) > 5 and matches({log}, r'done')"`.
*   `since`: (Optional) How much of the capture to search. `scenario` (default) searches the whole run, including output from before this command. `command` searches only from this command onwards. `group` searches from the start of this command's `!Group`, for a line the scenario provokes more than once: a later check cannot be satisfied by an earlier occurrence, yet still sees a line that arrived while the previous command was finishing. Rejected outside a `!Group`.
*   `timeout_s`: (Optional) Seconds to wait for a matching line. Defaults to `30`.

Three things to know:

*   **Quote it single-outside, raw-inside**: `'matches({line}, r"\S+")'`. See [Regular expressions](#regular-expressions) for why each layer is needed.
*   **It searches output captured before it runs**, so it can be placed anywhere after the action that provokes the line. A DUT does not wait to be asked: the gateway sets its clock about 16 s into boot, which on a scenario that resets it early is several commands before anything reads for it. This is why the default `since` is `scenario` — a wait-only check would sit out its whole timeout while the line it wanted was already captured. Use `since: command` when a match from *before* an action would be a false pass, such as re-checking a sync after a deliberate reset.
*   **A retry is not a failure.** Assert that a line eventually appears; don't try to assert a warning never did. The gateway's first NTP query routinely fails with `-11` (`EAGAIN` — DNS isn't usable in the instant after DHCP) and the next attempt succeeds, so a "no NTP errors" check would fail every healthy run. Bound how long a retry may take with `timeout_s` instead.

    The expression syntax now lets you *write* that mistake, so it is worth stating plainly: this tag waits for its expression to become **true**, so a negative one — `'not matches({log}, r"PANIC")'` — is already true before the DUT has said anything and passes instantly, testing nothing. Assert what the DUT must say, not what it must not.

**Waiting for something physical is the same tag with a longer `timeout_s`.** `scenarios/tracker.yml` waits for the tracker to catch a GNSS fix this way — the receiver reports its own progress, so no separate poll or CLI query is needed:

```yaml
  - !DutLogExpect:
    name: "GNSS Positioning Starts"
    validation: 'matches({line}, r"GNSS positioning started")'
    timeout_s: 30

  - !DutLogExpect:
    name: "Device Acquires A GNSS Fix"
    validation: 'matches({line}, r"GNSS FIX ACQUIRED with [0-9]+ satellites")'
    timeout_s: 300

  - !DutLogExpect:
    name: "Tracker Enters The Active State"
    validation: 'matches({line}, r"GNSS: GNSS_WAITING_FIX -> GNSS_ACTIVE")'
    timeout_s: 10
```

Three habits worth copying from it. **Check that the subsystem started before waiting on its result** — a receiver that failed to start (`Failed to start GNSS positioning`) would otherwise sit out the full five minutes below, reported as "no fix" when the real fault was bring-up. **Size the timeout to the physics, not to the rest of the scenario** — a cold start with no almanac takes 30 s under open sky and minutes through a window, so 300 s fails a tracker that cannot see the sky without failing one that is merely slow; a node with an indoor antenna needs that number raised, not the check dropped. And **assert the state change, not only the log line** — the fix line and the FSM transition it triggers are separate events, and it is the transition that starts SD recording, so a later card check failing for want of it would look unrelated to GNSS entirely.

A failed match **fails the scenario**, logging how many lines were examined and the last 15 the DUT emitted, so the report shows what it *was* saying. If lines have been evicted from the in-memory buffer (over 5000 captured), the failure says so rather than implying the DUT definitely never emitted the line — `device.log` remains complete either way.

Matching is against the line **as the firmware emitted it**; the `[HH:MM:SS.mmm]` prefix in the log files is added by this tool and is not part of what the expression sees. A timestamp the firmware prints itself — such as the gateway's own `[2026-08-04T06:25:01,707000Z]` — *is* matchable, which makes `validation: 'matches({line}, r"^\[19[0-9]{2}-")'` a way to spot a device still running on an unsynced 1970 clock.

### `!DutLogSend`
Types one line on the DUT's console, for a board whose console is also its input — the ESP32 `tekpadz>` console shares one wire with its log output. The line goes out on the port the capture already holds, so no second opener is needed (which is also why [`!DutCli`](#dutcli), needing its own UART, does not fit). What the DUT does with it is checked with [`!DutLogExpect`](#dutlogexpect).

```yaml
  - !DutLogSend
    name: "Enter Calibration"
    command: "app calibrate start"
```

*   `name`: (Optional) Descriptive log name.
*   `command`: (Required) The line to type; a newline is appended.

Needs a DUT console to be captured, and is rejected at load if capture is stopped at that point (see [`!DutLogControl`](#dutlogcontrol)). A send while the port is down fails the command rather than being silently lost. Each send is marked in the combined log (`DUT LOG PORT SENT`).

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
    validation: 'matches({reply}, r"state:\s*connected")'
    timeout_s: 5
```

*   `name`: (Optional) Descriptive log name.
*   `command`: (Required) The line typed at the shell, e.g. `"gora status"`.
*   `validation`: (Optional) A [validation expression](#validation-expressions) over the reply. Omit it to run a command for its effect and just log what came back.

    | Variable | Type | Holds |
    | --- | --- | --- |
    | `{reply}` | `str` | the whole reply, newline-separated |
    | `{lines}` | `list[str]` | the reply split into lines, without line endings |

    A reply is not a device log: output the DUT volunteered while the command ran is captured separately and is never part of `{reply}`. Assert on that with `!DutLogExpect` instead.
*   `timeout_s`: (Optional) Seconds to wait for the DUT's reply. Defaults to `3`. Raise it for a command the device takes real time over (clearing a journal, a flash erase) — the command is sent once and this is how long its answer is waited for, never a budget for re-sending it.

Five things to know:

*   **Quote it single-outside, raw-inside**, for the same reasons as `!DutLogExpect`: `'matches({reply}, r"\s")'`. See [Regular expressions](#regular-expressions).
*   **The reply is framed from the command's own echo**, and ends at the following prompt. Requiring the echo is what makes `timeout_s` mean anything: a prompt the DUT had already sent — it was sitting at one before the port was opened, or the shell's sync answer arrived as two — lands just after the command goes out and would otherwise be read as that command's terminator, returning an empty reply in ~0s with the timeout never spent. This assumes the firmware echoes, i.e. Zephyr's default `CONFIG_SHELL_ECHO=y`; with echo disabled every command fails with "the DUT never echoed ...". The port is also drained to silence before each command, so a reply always starts from an empty wire.
*   **The reply is matched, not the DUT's log output.** Zephyr's logging backend usually shares the shell UART, so `<inf>` lines can land in the middle of a response; they are separated out and never matched against (a failure quotes them separately, since they often explain the reply). The command's own echo and the trailing prompt are stripped too, so a pattern is written against what the command actually printed.
*   **One shell serves the whole run.** It is opened by the first `!DutCli` — not at start-up, so a scenario may flash the DUT first — and closed when the run ends. If the port has gone when a command is sent (a DUT that reset since the last one), it is reopened once and the command retried.
*   **A command the shell refuses fails the scenario** regardless of `validation` — `command not found`, `wrong parameter count` and friends mean the scenario is written against a firmware that does not have this command, which no expression could sensibly assert against. A command that *ran* and returned news the test dislikes is an ordinary `validation` failure instead.

A failed match **fails the scenario**, quoting the reply and any log output that arrived while the command ran. A reply that never comes fails differently, and says which half broke: "the DUT never echoed *x*" (it may not have received the command at all) versus "*x* was echoed but no prompt followed" (it is still working on it), the latter quoting however much of the reply did arrive.

Unlike `!DutLogExpect`, `timeout_s` is not a poll budget — the command is sent exactly once and the reply waited for, since a shell command may have side effects and re-issuing `gora reset` or a provisioning write would execute it twice. A state that has not settled yet is therefore a mismatch, not something to wait out. Poll for one with the DUT console (`!DutLogExpect`) or a `!Loop`, or bound it by placing the check after a `wait_after_s`.

### `!Loop`
Runs nested scenario commands sequentially multiple times.
*   `name`: (Optional) Descriptive log name.
*   `iterations`: (Required) Number of loop iterations.
*   `commands`: (Required) A list of nested scenario commands.

### `!Group`
Splits a scenario into named stages, so the [test report](#test-report) shows each under a heading with its own pass tally instead of as one flat list of every command in the scenario.
*   `name`: (Required) The stage's name, shown as the report's heading band.
*   `commands`: (Required) The commands making up this stage.

Groups live at the **top level** of a scenario, in a `groups:` list that replaces the usual `commands:` one. **Commands nest inside groups, never the other way round** — a `!Group` inside a `commands:` list is rejected.

```yaml
dut_log:
  port: "/dev/ttyUSB0"
  baud: 115200

groups:
  - !Group
    name: "Initialization"
    commands:
      - !RelayControl
        name: "Turn on the device"
        relay: 1
        state: 1

      - !UsbSwitch
        name: "Connect storage USB"
        port: dut_storage
        state: 1

  - !Group
    name: "Flashing"
    commands:
      - !ProgramEsptool
        name: "Flash The Tracker"
        firmware: "tekpadz.bin"
```

A scenario declares **either `commands:` or `groups:`, not both** — it is wholly ungrouped or wholly grouped. A plain `commands:` list keeps working untouched; grouping such a scenario means wrapping its commands in `!Group` blocks and indenting them, changing nothing about what it runs. Both scenarios shipped here are grouped — `tracker.yml` by bench stage, `gateway.yml` by *Flashing / Provisioning / Cloud Connection / Sub-GHz Uplink / Teardown*.

A group is **purely a label**. It is expanded away when the scenario is parsed, exactly like `!Loop`: the commands inside it run in order, in place, with no setup, teardown or isolation of any kind, and a failure inside one still stops the whole scenario rather than just the group. How a scenario is grouped therefore cannot change what it does — only how its results read.

Four more things:

*   **Groups do not nest.** A `!Group` inside another group's `commands:` is rejected, with the same error as one inside a top-level `commands:` list — the parser would otherwise walk straight past it and silently drop every command under it from the run.
*   **A `!Loop` inside a group stays in that group.** All its iterations report under the one heading; a loop cannot split its commands across sections.
*   **Two groups may share a name** and still report as two separate bands, since a group is identified by its position in `groups:` rather than by what it is called.
*   **`name` is required and must not be blank**, and an entry in `groups:` must actually be tagged `!Group` — a missing `!` is rejected rather than skipped, since that too would drop a whole stage. A group with no `commands` is skipped with a warning, matching how an empty `!Loop` behaves.

Groups also appear in the combined log's command markers (`CMD 3/12 START: Flash The Tracker (!ProgramEsptool) [Flashing]`), so a stage is greppable in the logs and not only visible in the HTML.

---

## 4. TODO — Known Issues and Cleanup

Findings from a source scan of the whole codebase. Ordered by severity; each entry names the file so it can be picked up independently. Nothing here is fixed yet.

### 4.1 Missing infrastructure

*   **No automated tests.** Everything so far has been verified with throwaway scripts against stubs and pty pairs. The pure logic is easy to cover now and would have caught several items above: `parser` tag resolution and `!Loop` expansion, `values.encode_value` / `uuids.normalize_uuid`, `subghz_sim.frame` CRC and encoding, `mqtt_expect` operator/early-exit table, and the BLE `attempts` retry loop — all with no hardware.
*   **No linter or formatter config.** No `ruff`/`flake8`/`black` setup, so the project's ≤50-line-function and PEP 8 standards are unenforced.
*   **No CI.** Nothing runs the above on a push.
