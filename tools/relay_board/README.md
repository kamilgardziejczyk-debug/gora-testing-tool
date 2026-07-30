# relay_board

Drives an 8-channel relay board from the Raspberry Pi's GPIO header, for
switching power and signal lines to a DUT during a test.

Usable two ways: as a CLI (one-shot commands and an interactive shell), and as
a Python API that the `!RelayControl` scenario wrapper drives.

Relays are addressed **1–8**, matching the `IN1`–`IN8` silkscreen on the board.
You never deal with electrical levels — you say "energize", and the active-low
vs active-high mapping is handled for you.

## Wiring to a Raspberry Pi 4B

The board has 10 inputs: `GND`, `VCC`, and `IN1`–`IN8`.

| Board pin | Goes to | Pi physical pin |
| --- | --- | --- |
| `VCC` | **external 5 V supply (+)** — see below | *not the Pi* |
| `GND` | external supply (−) **and** a Pi ground | 39 |
| `IN1` | BCM 5 | 29 |
| `IN2` | BCM 6 | 31 |
| `IN3` | BCM 13 | 33 |
| `IN4` | BCM 19 | 35 |
| `IN5` | BCM 16 | 36 |
| `IN6` | BCM 26 | 37 |
| `IN7` | BCM 20 | 38 |
| `IN8` | BCM 21 | 40 |

These pins are the default because they are all general-purpose (no I²C, UART
or SPI function), none has a boot-time pull-up, and they sit together at the
bottom of the header so the 10-wire run stays contiguous. Override them with
`--pins` if your bench needs something else.

### Power: do not run 8 relays off the Pi's 5 V pin

Each energized relay coil draws roughly 70–80 mA, so all eight at once is
~600 mA on top of whatever the Pi itself is using. The Pi's 5 V header pins
feed straight off the USB-C input with no protection, and the inrush when
several coils pull in together is enough to brown out the Pi and reboot it
mid-test.

Use a **separate 5 V supply** (1 A minimum, 2 A comfortable) for `VCC`, and tie
its ground to both the board's `GND` **and** a Pi ground pin. The shared ground
is not optional — without a common reference the `IN` signals mean nothing to
the board.

If your board has a `JD-VCC` jumper it can take coil power separately from
opto/logic power, which isolates the Pi further; a plain `GND`/`VCC`/8×`IN`
board like this one does not.

### Using just one relay

Nothing needs configuring for this — wire only the channel you want and use
it. With a single relay the hookup is three wires:

| Board pin | Pi physical pin |
| --- | --- |
| `IN1` (BCM 5) | 29 |
| `GND` | 39 |
| `VCC` | 2 or 4 (5 V) |

The external-supply warning above is about eight coils pulling in together.
One relay draws roughly 70–80 mA including its opto LED, which the Pi's 5 V
header pin supplies comfortably, so `VCC` can come off the header. Keep the
supply external anyway if that relay switches something inductive and you want
the isolation.

The other seven `IN` pins can be left unconnected, and the GPIO pins behind
them stay free for other uses: the tool only configures pins it is actually
asked about, so `on 1` touches BCM 5 and leaves BCM 6, 13, 19, 16, 26, 20 and
21 as untouched inputs.

```bash
python tools/relay_board/relay_board.py on 1
python tools/relay_board/relay_board.py off 1
python tools/relay_board/relay_board.py pulse 1 --duration 1.5
```

`status` still lists all eight; the seven unwired ones read `off`, since an
unconfigured pin leaves its channel de-energized.

### 3.3 V logic into a 5 V board

Pi GPIO pins swing 0–3.3 V, while the board is a 5 V part. On an **active-low**
opto-isolated board this is fine: the opto LED is fed from the board's own 5 V
and the Pi only has to sink its current, which a 3.3 V pin pulled low does
happily.

On an **active-high** board without optos, 3.3 V may not fully saturate the
input transistor. It usually still works; if relays chatter or don't pull in
reliably, that mismatch is the first thing to suspect.

### State at boot and on exit

GPIO pins come up as inputs, so before this tool runs, every `IN` sits at
high-Z — which leaves the relays de-energized on any board that pulls `IN` up
to `VCC` through its opto LED. Good: nothing clatters on at power-on.

Relay state is **latching**, and deliberately so. A pin's direction and output
value live in the SoC, not in this process, so:

*   `relay_board.py on 3` leaves relay 3 energized after the command returns.
*   Addressing one relay never disturbs the other seven — the tool only
    configures the pins it is actually asked about.
*   The first time a pin is configured, it is driven to the de-energized level
    in the same operation, so switching it to an output can't flash the coil
    on. (A pin fresh out of reset reads 0 in its output register, which on an
    active-low board would otherwise mean "energized".)

To hand the pins back and drop every relay, call `RelayBoard.release()`. Note
the scenario runner's `gpio_cleanup_all()` calls `GPIO.cleanup()` process-wide
at the end of a scenario, which releases relay pins too.

## Install

Needs `RPi.GPIO`, which is already in the repo's Pi-only requirements:

```bash
pip install -r requirements-rpi.txt
```

Off-device (or anywhere `RPi.GPIO` won't import) the tool **simulates** instead
of failing, so you can exercise the CLI on a PC. It says so on startup, and no
pins are driven.

## Command line

```bash
python tools/relay_board/relay_board.py on 3
python tools/relay_board/relay_board.py off 3
python tools/relay_board/relay_board.py toggle 3
python tools/relay_board/relay_board.py pulse 3 --duration 1.5
python tools/relay_board/relay_board.py all-on
python tools/relay_board/relay_board.py all-off
python tools/relay_board/relay_board.py status
```

Every command prints the resulting state of all 8 relays:

```
relay 1  BCM 5   off
relay 2  BCM 6   off
relay 3  BCM 13  ON
relay 4  BCM 19  off
relay 5  BCM 16  off
relay 6  BCM 26  off
relay 7  BCM 20  off
relay 8  BCM 21  off
```

Exit codes: `0` success, `2` a bad relay number or `--pins` list.

### Global flags

| Flag | Notes |
| --- | --- |
| `--pins A,B,C,D,E,F,G,H` | BCM pins for relays 1–8, in order (default `5,6,13,19,16,26,20,21`) |
| `--active-high` | Board energizes on a HIGH input instead of LOW |
| `-v`, `--verbose` | Log every pin change |

### Interactive shell

Run with no command (or `repl`) for bring-up work:

```
$ python tools/relay_board/relay_board.py
8-channel relay board. Type 'help' for commands, 'quit' to exit.
Relays stay in whatever state you leave them on exit.
relay> on 2
relay 2 ON
relay> pulse 4 0.25
relay 4 pulsed for 0.25s
relay> all off
all relays off
relay> quit
```

| Command | Notes |
| --- | --- |
| `on <1-8>` | Energize one relay |
| `off <1-8>` | De-energize one relay |
| `toggle <1-8>` | Invert one relay |
| `pulse <1-8> [seconds]` | Energize briefly, then release (default 0.5 s) |
| `all <on\|off>` | Drive all eight at once |
| `status` | Every relay's pin and state |
| `quit` | Exit, leaving relays as they are (Ctrl-D also works) |

## In Docker

Works in the test-node image with no extra flags: `/dev/gpiomem` is already
passed to every node by `deploy_docker_to_rpis.sh`, which is all `RPi.GPIO`
needs.

```bash
docker exec gora-node python tools/relay_board/relay_board.py on 3
```

## Python API

This is what `wrappers/relay_control_wrapper.py` (the `!RelayControl` tag) uses.

```python
from tools.relay_board import RelayBoard

board = RelayBoard()                      # defaults: active-low, DEFAULT_PINS
board = RelayBoard(active_low=False)      # active-high board
board = RelayBoard(pins=(5, 6, 13, 19, 16, 26, 20, 21))

board.on(3)                 # energize relay 3
board.off(3)                # de-energize it
board.toggle(3)             # invert, returns the new state
board.pulse(3, 1.5)         # energize for 1.5 s, then release
board.set(3, True)          # same as on(3)
board.set_all(False)        # de-energize all eight

board.state(3)              # True if energized, read back from the pin
board.states()              # list of 8 bools, relays 1..8
board.pin_for(3)            # 13
board.simulated             # True when RPi.GPIO is unavailable
print(board.describe())     # the table shown above

board.release()             # hand back the pins, dropping every relay
```

`state()` reads the pin back rather than trusting a cached value, so it stays
correct across separate processes — which is what makes `status` meaningful
after an earlier one-shot `on`.

Raises `ValueError` for a relay outside 1–8, a `pins` list that isn't 8 long or
contains duplicates, or a negative `pulse` duration.
