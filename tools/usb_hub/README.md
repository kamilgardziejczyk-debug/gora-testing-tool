# usb_hub

Per-port USB power control for the **UUGear MEGA4**, a 4-port USB 3.1 Gen1 hub
board the size of a Pi 4B.

The MEGA4 implements the USB spec's per-port power switching (PPPS) with real
**AP2511** load switches, one per port. That distinction matters: plenty of hubs
advertise PPPS but only gate the data lines, leaving VBUS live so the device
keeps running (or at least charging) while nominally "off". On the MEGA4 the
power genuinely drops, which is what makes it usable for hard power-cycling a
DUT from a scenario.

Ports are addressed **1..4**, matching the silkscreen.

Scenarios drive this through the `!UsbSwitch` tag; see the root README for that
tag's fields and the `usb_hub:` block that names ports. This document covers the
hardware, the setup it needs, and the standalone CLI.

## How it works

Everything UUGear ships for the board — the interactive `mega4.sh`, the UWI web
page — is a wrapper around [`uhubctl`](https://github.com/mvp/uhubctl), and so
is this module. There is no I²C link, no vendor MCU and no custom protocol: the
switch is a standard `SET_FEATURE(PORT_POWER)` control transfer to the hub, and
`uhubctl` is the tool that issues it.

Two pieces of hardware/kernel awkwardness are worth knowing about, because they
shape the API:

*   **A USB 3 hub enumerates twice.** The VL817 controller appears as a USB2
    hub (`2109:2817`) *and* a USB3 hub (`2109:0817`), at unrelated locations.
    Both halves have to be switched for the change to take effect; `uhubctl`
    pairs them automatically, which is why this module never passes its `-e`
    flag.
*   **The kernel fights you on power-off.** When a device disappears from a
    port, Linux re-powers the port to try to bring it back. `uhubctl -r` keeps
    re-issuing the request until the kernel gives up. This module passes
    `-r 200` (the value UUGear's own script uses) on every off, with the direct
    consequence that **powering off takes seconds while powering on is
    instant** — do not budget the two directions equally.

## Install

`uhubctl` must be on `PATH`:

```bash
sudo apt install uhubctl        # Debian / Raspberry Pi OS
```

It needs raw USB access, so run as root, or install a udev rule for the hub's
vendor ID:

```
# /etc/udev/rules.d/52-usb.rules
SUBSYSTEM=="usb", DRIVER=="hub|usb", MODE="0664", GROUP="dialout", ATTR{idVendor}=="2109"
```

```bash
sudo udevadm trigger --attr-match=subsystem=usb
```

Also disable USB autosuspend, or ports come back in unpredictable states — add
`usbcore.autosuspend=-1` to `/boot/cmdline.txt` and reboot. (UUGear's own
installer patches the same file for the same reason.)

If `uhubctl` is **not** installed, `UsbHub` falls back to simulating the board
and logs a warning per call, so scenarios and the CLI stay runnable on a
developer PC. A hub that is genuinely missing or unreachable on a machine that
*does* have `uhubctl` raises `HubNotFoundError` instead — a rig with an
unplugged hub should fail its run, not silently pass it.

## In Docker

The container needs the host's USB devices and privileges to talk to them —
the same passthrough the J-Link tooling already uses:

```bash
docker run --privileged -v /dev/bus/usb:/dev/bus/usb ...
```

`uhubctl` and `libusb-1.0-0` are installed in the image's runtime layer, so
nothing extra has to be present on the node itself. The `usbcore.autosuspend=-1`
kernel argument does have to be set on the host, though — a container cannot
change it.

## Command line

```bash
python tools/usb_hub/usb_hub.py <command>
```

Every port argument takes a spec: a number (`3`), a list (`1,3`) or a range
(`1-3`). Multi-port commands are issued as a single `uhubctl` call, which on the
off path matters a lot — one retry storm for the set instead of one per port.

| Command | Does |
| --- | --- |
| `on <ports>` | Power the ports on. |
| `off <ports>` | Power the ports off. Takes seconds, not milliseconds. |
| `toggle <ports>` | Invert each port (necessarily one call per port — each needs its own current state). |
| `cycle <ports> [--delay S]` | Off, wait `S` seconds (default 1.0), on. |
| `all-on` / `all-off` | Every port, one call. |
| `status` | Power state and occupancy of all four ports. |
| `discover` | Locations of every attached MEGA4, one per line. |
| `repl` | Interactive shell — the default when no command is given. |

Each state-changing command prints the resulting status, so a one-shot run shows
you what it did.

```bash
python tools/usb_hub/usb_hub.py discover
python tools/usb_hub/usb_hub.py off 3
python tools/usb_hub/usb_hub.py -l 1-1.2 cycle 1-2 --delay 3
```

### Global flags

| Flag | Default | Meaning |
| --- | --- | --- |
| `-l`, `--location` | discovered | Hub location, e.g. `1-1.2`. Required once more than one MEGA4 is attached. |
| `--uhubctl` | `uhubctl` | Binary to drive the hub with. |
| `--off-retries` | `200` | `uhubctl -r` value used on power-off. |
| `--timeout` | `30.0` | Seconds allowed per `uhubctl` call. |
| `-v`, `--verbose` | off | Log each hub call. |

### Exit codes

`0` on success, `2` for a bad argument (unknown port, malformed spec), and `1`
for a hardware failure — no hub, no permission, a stale location, a timeout.
Anything scripting this can tell "you asked wrong" apart from "the rig is
broken" without parsing messages.

### Interactive shell

```
$ python tools/usb_hub/usb_hub.py
MEGA4 USB hub. Type 'help' for commands, 'quit' to exit.
Ports keep whatever power state you leave them in on exit.
Powering a port off takes a few seconds - the kernel has to be retried out.
usb> status
MEGA4 at 1-1.2
port 1  ON   device
port 2  ON   device
port 3  off  -
port 4  ON   -
usb> cycle 1 2
port(s) 1 cycled with a 2.0s gap
usb> quit
```

Commands are `on`, `off`, `toggle`, `cycle <ports> [seconds]`, `all <on|off>`,
`status` and `quit`. Hardware errors are printed rather than raised, so a
mistake during bring-up does not drop you out of the shell.

## Port names

`Switchboard` pairs a `UsbHub` with the scenario's `usb_hub.ports` mapping, so
a caller can address a port by what it *is* rather than where it is plugged in.
It is what `!UsbSwitch` actually holds; `main.py` builds one per run so hub
discovery is paid for once rather than per command.

```python
from tools.usb_hub import Switchboard, UsbHub

board = Switchboard(UsbHub(), {"dut_power": 3, "debugger": 1})
board.resolve("dut_power")      # -> 3
board.resolve("2")              # -> 2   (numbers still work)
board.set("dut_power", False)
board.cycle("dut_power", 2.0)
board.describe_target("dut_power")   # -> 'dut_power (port 3)', for log lines
```

An unknown name and an out-of-range number raise different messages on purpose:
they need different fixes, and the first lists the names that *are* defined.

## Python API

```python
from tools.usb_hub import UsbHub

hub = UsbHub()              # discovers the only attached MEGA4
hub.off(3)                  # drop VBUS on port 3 (slow — retries the kernel out)
hub.on(3)                   # restore it
hub.set_ports("1-3", False) # several ports, one uhubctl call
hub.cycle(3, delay_s=2.0)   # off, wait, on — restores power even on Ctrl-C
hub.toggle(3)               # returns the new state
hub.set_all(False)          # all four ports, one uhubctl call

hub.state(3)                # -> bool
hub.status()                # -> [PortStatus(port, powered, connected), ...]
print(hub.describe())       # one line per port
```

`set_ports` and `cycle` accept a spec string (`"1,3"`, `"1-3"`), a sequence of
port numbers, or a single `int`.

### Hub location

`uhubctl` addresses a hub by a location string such as `1-1.2`, which describes
its position in the USB topology, **not** its identity — re-plug the hub into a
different upstream port and the string changes. So `UsbHub()` discovers the
attached MEGA4 on first use. Once more than one MEGA4 is in the rig (they can
be daisy-chained), discovery becomes ambiguous and raises; pin one explicitly:

```python
UsbHub.discover()           # -> ['1-1.2', '1-1.3']
hub = UsbHub(location="1-1.2")
```

Note that on a daisy chain, the port feeding a downstream MEGA4 cannot be
powered off — doing so would cut the hub issuing the command.

### Constructor options

| Argument | Default | Meaning |
| --- | --- | --- |
| `location` | discovered | USB2 location of the hub, e.g. `1-1.2`. |
| `uhubctl_bin` | `uhubctl` | Binary to invoke; also what the simulation fallback keys off. |
| `off_retries` | `200` | `uhubctl -r` value used on power-off. `0` disables it — the port will very likely come straight back on. |
| `timeout_s` | `30.0` | Per-invocation subprocess timeout. Must comfortably exceed a full retry storm. |

### Errors

All inherit `UsbHubError`:

*   `HubNotFoundError` — no MEGA4 attached, several attached with no location
    given, or a pinned location that no longer exists.
*   `UhubctlNotFoundError` — the binary vanished between the simulation check
    and the call.
*   `UhubctlCommandError` — `uhubctl` exited non-zero (most often a permissions
    problem: no root, no udev rule, or no `/dev/bus/usb` in the container) or
    exceeded `timeout_s`.

Bad port numbers and malformed port specs raise plain `ValueError`.

## Current limits

*   Bus-powered from the Pi 4B: **1.2 A total** across all four ports.
*   With a 5 V supply on the USB-C connector: **5 A total, 2.5 A per port**.

The board does not back-feed power to the Pi (an AO4447 ideal-diode MOSFET
blocks it), so the supplemental supply is safe to leave connected.
