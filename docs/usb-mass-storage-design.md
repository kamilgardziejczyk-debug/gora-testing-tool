# Design: reading the DUT's SD card over USB mass storage

Status: **proposed**, not implemented.
Depends on: the `validation` expression language (`wrappers/expression.py`), already in place.

## 1. Goal

The tracker carries an SD card that it exposes to a host as a USB mass storage
device. A test node can already switch that connection on and off — the hub
port is wired and named in `scenarios/tracker.yml`:

```yaml
usb_hub:
  ports:
    dut_storage: 1
```

What is missing is the ability to do anything with the card once it appears:
list what the firmware wrote, copy logs off it for the report, and assert on
their contents. This design adds a `!DutStorage` tag for that.

It also has a second purpose, which turns out to be the more valuable one: the
firmware's hand-off of the card between itself and the host is a real state
machine with a two-phase teardown, and mounting the card the way an ordinary
host does is the only way to exercise it.

## 2. What the firmware does

From `propadz/components/tekpadz_usb_storage/tekpadz_usb_storage_task.c`.

The device runs an FSM — `INACTIVE → MOUNTING → ACTIVE` — that hands the card
to the host by unmounting it from the ESP32's own VFS and exposing it raw over
SCSI (`tinyusb_msc_storage_init_sdmmc`, then `tinyusb_msc_storage_unmount()`).

Teardown happens in **two separate phases**:

| Phase | Trigger | Firmware log |
| --- | --- | --- |
| Card handed to host | port powered, enumeration | `USB Storage is now ACTIVE - host can access SD card` |
| Host ejects | SCSI `START_STOP_UNIT` (LoEj=1) | `TinyUSB: Storage control returned to ESP32` |
| | | `Host ejected drive. Waiting for physical disconnect to shutdown USB storage.` |
| VBUS drops | `!UsbSwitch state: 0` | `Exiting ACTIVE state - cleaning up USB` |
| | | `Remounting SD card normally for application...` |

The split is deliberate. The comment at `tekpadz_usb_storage_task.c:210`
records why: sending `DEINIT` at eject time uninstalls the USB driver while
the host is still finishing the eject transaction, which produced *"An error
occurred while ejecting"* on Windows. So the firmware acknowledges the eject
and then waits for the cable to actually go away.

Three consequences for this design:

*   **`umount` alone does not reach the firmware.** Linux `umount` flushes and
    detaches the filesystem; it never sends `START_STOP_UNIT`. Only an
    explicit eject trips `storage_mount_changed_cb`. Unmount and eject
    therefore have to be separate scenario steps — which is also useful,
    because "unmount, then cut VBUS without ejecting" is the cable-yank case
    and a distinct path through the FSM.
*   **A raw-block reader would test none of this.** Reading the card with
    `mtools` or a userspace FAT library never enumerates as a host, never
    ejects, and never triggers either teardown phase. It was the first thing
    considered and is rejected for exactly that reason.
*   **The device is full-speed** (`CONFIG_TINYUSB_RHPORT_FS=y`), so it always
    enumerates on the MEGA4's USB2 half. The USB3 companion-hub complication
    that `tools/usb_hub/hub.py` deals with does not arise here.

There is no console command for the USB storage state (`tekpadz_cmd.c`
registers `factory`, `application`, `imu`, `gnss`, `reset` only), so scenario
assertions about the firmware's side go through `!DutLogExpect` against the
console `tracker.yml` already captures.

## 3. Approach

### 3.1 Find the block device by USB port, never by scanning

The `Switchboard` already resolves `dut_storage` to a port number, and
`UsbHub.location` gives the hub's USB2 location (e.g. `1-1.2`). Together those
name a sysfs path:

```
/sys/bus/usb/devices/<hub_location>.<port>/*/host*/target*/*:*:*:*/block/*
```

That is the deterministic link from "the port we powered" to "the block device
that appeared". The alternative — taking whichever `/dev/sd*` is new — is how
a test rig eventually mounts the Pi's own disk read-write, and enumeration
order is not stable across runs.

Enumeration plus the SCSI probe takes a second or two after power-on, so
discovery polls until the node appears or a budget expires. That budget is
named `settle_timeout_s` rather than `timeout_s` deliberately: it genuinely is
a poll budget, which is the opposite of what `timeout_s` means on `!DutCli`,
and reusing the name would entrench the confusion.

### 3.2 Mount, unmount and eject as explicit scenario steps

Mount lifecycle is the thing under test, not an implementation detail to hide,
so each phase is its own command:

*   `mount` — `mount -t vfat -o ro,noatime <device> /run/gora/<alias>`.
    Read-only unless `mode: rw`.
*   `unmount` — `sync`, then `umount`.
*   `eject` — `sg_start --stop --loej <device>` (sg3-utils).

`sg_start` rather than `eject(1)`: `eject` unmounts as a side effect, which
would blur the two steps this design is specifically trying to keep apart.

### 3.3 The hung-mount hazard

Cutting VBUS while the filesystem is mounted leaves the kernel with a mount
backed by a device that no longer exists. Processes touching it block in
uninterruptible sleep; inside a container that usually ends the run
unrecoverably and takes the next run with it. Three mitigations, all required:

1.  Track the active mount at module level, exactly as
    `wrappers/usb_switch_wrapper.py` tracks `_powered_off`.
2.  Register a `restore_all`-style cleanup called from the runner's `finally`,
    which syncs and unmounts anything still mounted.
3.  Have that cleanup fall back to `umount -l` so a wedged mount cannot
    outlive the scenario.

This is the cost of mounting rather than reading raw blocks, and it is
accepted knowingly: the firmware behaviour under test is only reachable this
way.

## 4. Tag design

```yaml
  - !UsbSwitch
    name: "Connect storage USB"
    port: dut_storage
    state: 1

  - !DutLogExpect:
    name: "Device Hands Card To Host"
    validation: 'matches({line}, "USB Storage is now ACTIVE")'
    timeout_s: 20

  - !DutStorage:
    name: "Mount The Card"
    port: dut_storage
    action: mount
    mode: ro
    settle_timeout_s: 15

  - !DutStorage:
    name: "Card Holds Today's Log"
    action: list
    path: "/logs"
    validation: "'tracker-2026-08-18.log' in {files}"

  - !DutStorage:
    name: "Log Records No Mount Failure"
    action: read
    path: "/logs/tracker-2026-08-18.log"
    validation: "'SD_MOUNT_FAILED' not in {content} and {size} > 0"

  - !DutStorage:
    name: "Pull The Logs Off"
    action: copy_from
    path: "/logs/*.log"
    dest: "results/sd/"

  - !DutStorage:
    name: "Unmount"
    action: unmount

  - !DutStorage:
    name: "Eject The Drive"
    action: eject

  - !DutLogExpect:
    name: "Device Reclaims Card On Eject"
    validation: 'matches({line}, "Storage control returned to ESP32")'
    timeout_s: 10

  - !UsbSwitch
    name: "Disconnect storage USB"
    port: dut_storage
    state: 0

  - !DutLogExpect:
    name: "Device Remounts Card For Itself"
    validation: 'matches({line}, "Remounting SD card normally for application")'
    timeout_s: 15
```

### 4.1 Validation variables

Per action, in the brace syntax the other tags now use:

**`list`** — names directly under `path`, not recursive:

| Variable | Type | Holds |
| --- | --- | --- |
| `{files}` | `list[str]` | file names, sorted |
| `{dirs}` | `list[str]` | subdirectory names, sorted |
| `{entries}` | `list[str]` | both, sorted |
| `{count}` | `int` | `len({entries})` |

**`read`** — the single file at `path`:

| Variable | Type | Holds |
| --- | --- | --- |
| `{content}` | `str` | decoded file text |
| `{lines}` | `list[str]` | `{content}` split on newlines |
| `{size}` | `int` | file size in bytes |

**`copy_from`** — what was pulled into `dest`:

| Variable | Type | Holds |
| --- | --- | --- |
| `{copied}` | `list[str]` | card-relative paths copied, sorted |
| `{count}` | `int` | `len({copied})` |

`mount`, `unmount` and `eject` bind no variables and take no `validation`:
they are lifecycle steps, and what the firmware made of them is visible on its
console rather than in anything readable from the host.

### 4.2 Other fields

*   `port` — a `usb_hub.ports` alias or a port number, resolved by the same
    `Switchboard` as `!UsbSwitch`. Required on `mount`; the later actions use
    the mount the scenario already established.
*   `mode` — `ro` (default) or `rw`. Opt-in per command, never a scenario-level
    default: a wrong path under `rw` writes to the DUT's card.
*   `dest` — resolved against `scenario_dir`, per the `Wrapper` base class.

## 5. Docker

*   `--privileged` (CAP_SYS_ADMIN for `mount`) and `-v /dev:/dev` rather than
    `--device`. README section 1 already explains why `--device` cannot work
    for a re-enumerating DUT; a block device that only appears mid-run is the
    same problem.
*   Add `sg3-utils` and `dosfstools` to the apt layer at `Dockerfile:81`,
    alongside `uhubctl`.

## 6. Development steps

1.  `tools/mass_storage/device.py` — port-anchored sysfs discovery.
    `find_block_device(hub_location, port, settle_timeout_s)`, returning the
    partition node (whole-disk fallback for a superfloppy), with an error
    naming the port when nothing appears.
2.  `tools/mass_storage/mount.py` — `mount` / `unmount` / `eject`, with the
    error classes `tools/usb_hub/hub.py` uses.
3.  `tools/mass_storage/cli.py` + `mass_storage.py` — standalone CLI mirroring
    `tools/usb_hub/cli.py`. This is where the eject is verified to trip
    `storage_mount_changed_cb` on real hardware, with the console open, before
    any YAML exists.
4.  `wrappers/dut_storage_wrapper.py` — mount tracking, `restore_all` cleanup,
    per-action variable binding, `validation_expected` / `validation_actual`.
5.  Register in `parser/parser.py` `WRAPPER_BY_TAG` and `wrappers/__init__.py`;
    call the new `restore_all` alongside the existing one in `main.py`.
6.  Dockerfile packages and run-flag documentation.
7.  Extend `scenarios/tracker.yml` with the sequence in section 4; update
    `README.md`.

## 7. Open questions

*   **Throughput.** Full-speed USB caps around 1 MB/s, so a whole-card
    `copy_from` is slow. Scenario copies should stay scoped to a
    subdirectory, and `copy_from` needs a generous timeout of its own.
*   **Filesystem.** `CONFIG_TINYUSB_FAT_FORMAT_ANY=y` and the card is expected
    to be FAT32. Worth confirming with `lsblk -o NAME,FSTYPE,LABEL` on a
    powered port before step 1, since an exFAT card needs `exfatprogs` in the
    image.
*   **`copy_to`.** Deliberately not in this design. Writing to the DUT's card
    is a different risk profile and can be added once reading is trusted.
