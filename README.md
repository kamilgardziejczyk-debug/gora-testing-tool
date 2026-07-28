# Gora Testing Tool

An automated, YAML-driven test execution and hardware control tool designed to parse test scenarios, toggle GPIOs (e.g. on a Raspberry Pi), manipulate USB switches, run terminal commands, simulate sub-GHz sensors, interact with Bluetooth LE devices over GATT, listen to messages published to AWS IoT Core, and flash device microcontrollers using both `esptool` and SEGGER `J-Link`.

---

## 1. Setup and Installation

### Prerequisites
*   Python 3.8 or higher.
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
*   `firmware`: (Required) Filename of the binary to flash.
*   `address`: (Required for `.bin` / raw files) The load address (e.g., `0x18000000`). Automatically omitted for `.hex` and `.elf` files since J-Link automatically parses internal addresses.

### `!ProgramEsptool` (or `!ProgrammEsptool`)
Flashes an ESP32 microcontroller using the `esptool` library.
*   `name`: (Optional) Descriptive log name.
*   `port`: (Required if not overridden via `-p` / `--port`) Destination serial port.
*   `baudrate`: (Optional) Upload baudrate. Defaults to `460800`.
*   `bootloader`: (Required) Bootloader filename.
*   `partition_table`: (Required) Partition table filename.
*   `firmware`: (Required) App firmware filename.

### `!GpioControl` (or `GpioControl`)
Toggles Raspberry Pi GPIO pins (requires `RPi.GPIO`).
*   `name`: (Optional) Descriptive log name.
*   `pin`: (Required) BCM pin number.
*   `state`: (Required) `true` (HIGH) or `false` (LOW).
*   `wait_after_s`: (Optional) Time in seconds to sleep after executing the pin change.

### `!UsbSwitch` (or `UsbSwitch`)
Placeholder wrapper for manipulating a physical USB switch hardware component.
*   `name`: (Optional) Descriptive log name.
*   `state`: (Required) `true` (enabled) or `false` (disabled).
*   `wait_after_s`: (Optional) Time in seconds to wait.

### `!ExecuteCommand` (or `!ExecuteCommand:`)
Runs a host terminal command using shell execution.
*   `name`: (Optional) Descriptive log name.
*   `command`: (Required) The bash command string.
*   `wait_after_s`: (Optional) Wait time in seconds after command execution.

### `!SubghzSim`
Runs a scripted sub-GHz simulator session over a serial link: opens the port, applies a sequence of sensor actions with waits in between, keeps the simulator reporting for `duration_s`, then closes the link. Wraps `tools/subghz_sim` — see [its README](tools/subghz_sim/README.md) for the standalone REPL, the wire format, and the Python API.
*   `name`: (Optional) Descriptive log name.
*   `port`: (Required) Serial port the simulator connects to. Set directly in the YAML — not overridable via `-p` / `--port`, since a scenario may also flash a device (e.g. `!ProgramEsptool`) on a different port at the same time.
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
*   `actions`: (Required) A non-empty list run in order, on the one connection. Each entry has exactly one verb key. Every verb also accepts `attempts` and `retry_wait_ms` (below), so any action can retry itself without a separate wrapping construct.
    *   `write`: Write a characteristic.
        *   `uuid`: (Required) Characteristic UUID — 16-bit shorthand (`2a00`) or full 128-bit.
        *   `value`: (Required) The value to write, interpreted per `encoding`.
        *   `encoding`: (Optional) `hex` (default), `utf8`, `uint8`, `uint16`, or `uint32`. Integers are little-endian, matching the Bluetooth spec's own numeric fields.
        *   `service`: (Optional) Overrides the command-level `service`.
        *   `response`: (Optional) `true` (default) waits for the device to acknowledge the write, so a rejection fails the test; `false` is fire-and-forget.
        *   `wait_after_ms`: (Optional) Pause after this action succeeds, before the next one in `actions` runs.
    *   `read`: Read a characteristic, optionally asserting on its value.
        *   `uuid`: (Required) Characteristic UUID to read.
        *   `validation`: (Optional) A `"value <op> <literal>"` expression — see below. Omit to just read and log the value without asserting anything about it.
        *   `encoding`, `service`, `wait_after_ms`: (Optional) Same as `write`.
    *   `notify`: Wait for a `validation` expression to be satisfied by a **pushed** notification. Ignores values that don't satisfy it and keeps waiting, rather than failing on the first mismatch — a device reporting an intermediate state (e.g. "booting") before the expected one is normal. A real push notification can be missed in a narrow window right after reconnecting (e.g. following a device reset); `read` with `attempts` (below) is the reliable alternative for that case.
        *   `uuid`: (Required) Characteristic UUID to subscribe to.
        *   `validation`: (Required) See below.
        *   `encoding`, `service`, `wait_after_ms`: (Optional) Same as `write`.
        *   `timeout_s`: (Optional) Seconds to wait before failing the command. Defaults to `30`.
    *   `attempts`: (Optional, any verb) Retries this one action, on the same connection, up to this many times before giving up. Defaults to `1` (no retry). The general way to poll: a `read` with `validation` fails whenever the value doesn't satisfy it yet, so giving it `attempts` repeats the read until it does — replacing what would otherwise need a hand-written retry loop.
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
