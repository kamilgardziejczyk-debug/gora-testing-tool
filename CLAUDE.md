# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this tool does

`gora-testing-tool` is a CLI tool that runs hardware test scenarios defined in YAML files. It orchestrates actions against embedded hardware (ESP32 flashing via esptool, SD card operations, USB switch control) by parsing a scenario file and executing commands sequentially.

## Running the tool

```bash
python main.py -t scenarios/test.yml
python main.py -t scenarios/test.yml -p /dev/ttyUSB0   # override serial port
```

There are no automated tests or a linter configured yet.

## Architecture

The flow is: `main.py` → `Parser` → list of `Wrapper` instances → sequential `execute()` calls.

**`parser/parser.py` — `Parser` class**
Reads and validates YAML using `yaml.compose()` (low-level node API, not `yaml.safe_load`). Resolves `!Loop` blocks by expanding them inline before constructing wrappers. Unknown tags are silently skipped. The `WRAPPER_BY_TAG` dict maps YAML tag strings to wrapper classes; add new commands here.

**`wrappers/` — one file per command type**
Each wrapper implements the `Wrapper` ABC (`parse()` + `execute()`). `parse()` extracts fields from the raw `yaml.MappingNode` passed by the parser. `execute()` performs the actual hardware action. Currently only `ProgramEsptoolWarpper` has a real `execute()` implementation; the SD card and USB switch wrappers log a stub message.

**YAML scenario format** (`scenarios/test.yml` for reference)
```yaml
commands:
  - !ProgramEsptool:
    port: "COM5"
    baudrate: 115200
    firmware: "path/to/firmware.bin"
    flash_address: "0x0"      # optional, default 0x0

  - !Loop:
    iterations: 5
    commands:
      - !SDCardMount:
        sd_card_path: "D:/SDCard"
        wait_after_s: 1
      - !UsbSwitch:
        state: true
        wait_after_s: 10
```

Supported tags: `ProgramEsptool` (also `ProgrammEsptool` as legacy alias), `SDCardMount`, `SDCardUnmount`, `SdCardDeleteFiles`, `SdCardFindFiles`, `UsbSwitch`.

## Adding a new command

1. Create `wrappers/my_command_warpper.py` implementing `Wrapper`.
2. Export it from `wrappers/__init__.py`.
3. Add its tag → class mapping in `parser/parser.py::WRAPPER_BY_TAG`.

## Code standards (from GEMINI.md)

- Python 3.8+ with type hints on all function signatures.
- PEP 8; functions ≤ 50 lines.
- Docstrings on all public functions and classes.
- No global state; use class attributes or parameters.
