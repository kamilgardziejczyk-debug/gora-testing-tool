# Gora Testing Tool

An automated, YAML-driven test execution and hardware control tool designed to parse test scenarios, toggle GPIOs (e.g. on a Raspberry Pi), manipulate USB switches, run terminal commands, and flash device microcontrollers using both `esptool` and SEGGER `J-Link`.

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
Drives the sub-GHz sensor simulator (`tools/subghz_sim`) as a scripted REPL session over a serial link: launches the simulator, feeds it a sequence of commands with waits in between, then quits it.
*   `name`: (Optional) Descriptive log name.
*   `port`: (Required) Serial port the simulator connects to. Set directly in the YAML — not overridable via `-p` / `--port`, since a scenario may also flash a device (e.g. `!ProgramEsptool`) on a different port at the same time.
*   `baud`: (Optional) Baud rate. Defaults to `115200`.
*   `interval`: (Optional) Heartbeat interval in seconds the simulator uses to re-send sensor state. Defaults to `5`.
*   `actions`: (Optional) A list of REPL commands to run in order. Each entry has exactly one verb key (`add`, `set`, `del`, or `list`) plus an optional `wait_after_ms`:
    ```yaml
    actions:
      - add: temp_hum          # add <heat|smoke|co|temp_hum>
        wait_after_ms: 1000
      - set: "1 temp 30 humidity 70"   # set <sensor_id> <field> <value> ...
        wait_after_ms: 5000
      - del: 1                 # del <sensor_id>
        wait_after_ms: 1000
    ```

### `!Loop`
Runs nested scenario commands sequentially multiple times.
*   `name`: (Optional) Descriptive log name.
*   `iterations`: (Required) Number of loop iterations.
*   `commands`: (Required) A list of nested scenario commands.
