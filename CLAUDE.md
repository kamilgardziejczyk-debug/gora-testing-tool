# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

User-facing documentation lives elsewhere: `README.md` for the scenario tag reference, `tools/*/README.md` for individual tools. Keep it there, not here.

## What this tool does

`gora-testing-tool` is a CLI tool that runs hardware test scenarios defined in YAML files. It orchestrates actions against embedded hardware (ESP32/J-Link flashing, Raspberry Pi GPIO, sub-GHz sensor simulation, listening to AWS IoT Core) by parsing a scenario file and executing commands sequentially.

## Running the tool

```bash
python main.py -t scenarios/test.yml
python main.py -t scenarios/test.yml -p /dev/ttyUSB0   # override serial port
```

There are no automated tests or a linter configured yet.

## Architecture

The flow is: `main.py` → `Parser` → list of `Wrapper` instances → sequential `execute()` calls.

**`parser/parser.py` — `Parser` class**
Reads and validates YAML using `yaml.compose()` (low-level node API, not `yaml.safe_load`). Resolves `!Loop` blocks by expanding them inline before constructing wrappers. The `WRAPPER_BY_TAG` dict maps YAML tag strings to wrapper classes; add new commands here.

Unknown tags are silently skipped. A tag written without its leading `!` therefore parses as a plain mapping and is dropped without warning — this is why the `!Loop` in `scenarios/test.yml` currently runs zero commands.

**`wrappers/` — one file per command type**
Each wrapper implements the `Wrapper` ABC (`parse()` + `execute()`). `parse()` extracts fields from the raw `yaml.MappingNode`; prefer failing there over in `execute()`, so a bad scenario is rejected before any hardware is touched. Every wrapper has a real `execute()` except `UsbSwitchWarpper`, which logs a stub message.

Two `Wrapper` base-class fields are generic across all tags and filled in by the Parser, so wrappers must not parse them themselves:
- `wait_after_s` — the runner sleeps for it after `execute()` returns.
- `scenario_dir` — directory of the scenario file, set before `parse()`. Resolve relative paths against it.

`ProgramEsptoolWarpper.parse()` accepts a `ProgrammEsptool` spelling, but that tag is absent from `WRAPPER_BY_TAG`, so it is unreachable. Don't document it as a working alias.

**`tools/` — standalone hardware tools, each with its own `requirements.txt`**
Two different integration styles, deliberately:
- `tools/subghz_sim/` — flat script directory, no `__init__.py`, driven as a subprocess via stdin by its wrapper.
- `tools/mqtt_listener/` — importable package whose API the MQTT wrappers call directly. This makes `paho-mqtt` a hard dependency of `main.py`, unlike `pyserial`.

**MQTT sessions**
`!MqttSubscribe` opens a listener and buffers messages; a later command reads it. The two rendezvous through `wrappers/mqtt_registry.py` (see Code standards). `run_scenario()` closes any open session in a `finally`, so a failing command cannot leave an orphan holding the client id.

Nothing reads the buffered messages yet — the comparison step is unimplemented and intended to live in the wrapper layer, not in `tools/mqtt_listener/`.

## Adding a new command

1. Create `wrappers/my_command_wrapper.py` implementing `Wrapper`.
2. Export it from `wrappers/__init__.py`.
3. Add its tag → class mapping in `parser/parser.py::WRAPPER_BY_TAG`.
4. Document the tag and its fields in `README.md`.

## Code standards (from GEMINI.md)

- Python 3.8+ with type hints on all function signatures.
- PEP 8; functions ≤ 50 lines.
- Docstrings on all public functions and classes.
- No global state; use class attributes or parameters.

One deliberate exception: `wrappers/mqtt_registry.py` keeps a module-level dict of live MQTT listeners. `!MqttSubscribe` must start buffering before the command that triggers a publish, while a later command reads that same listener, and the Parser builds every wrapper independently with no way to pass a reference between them. Don't "fix" it without solving that rendezvous another way.
