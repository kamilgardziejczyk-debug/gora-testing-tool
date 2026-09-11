import logging
import threading
from pathlib import Path

import esptool
import yaml

from .wrapper import Wrapper


LOGGER = logging.getLogger(__name__)

DEFAULT_BAUDRATE = 460800
DEFAULT_BOOTLOADER_FLASH_ADDRESS = 0x0000
DEFAULT_PARTITION_TABLE_FLASH_ADDRESS = 0x8000
DEFAULT_FIRMWARE_FLASH_ADDRESS = 0x10000
FLASH_SECTOR_SIZE = 0x1000


class ProgramEsptoolWrapper(Wrapper):
    """
    Wrapper for flashing ESP32 microcontrollers using the `esptool` library.

    Mirrors `!ProgramJlink`'s shape: `firmware` plus an optional `address` is
    enough to flash a single image. `bootloader`, `partition_table` and
    `ota_data` stay available for a full flash, each at its own address.
    """

    supports_port_override = True
    supports_firmware_dir_override = True

    def __init__(self, command_node: yaml.MappingNode):
        self.command_node = command_node
        self.name: str | None = None
        self.port: str | None = None
        self.baudrate: int = DEFAULT_BAUDRATE
        self.firmware_dir: str | None = None
        self.bootloader: str | None = None
        self.bootloader_address: int = DEFAULT_BOOTLOADER_FLASH_ADDRESS
        self.partition_table: str | None = None
        self.partition_table_address: int = DEFAULT_PARTITION_TABLE_FLASH_ADDRESS
        self.ota_data: str | None = None
        self.ota_data_address: int | None = None
        self.firmware: str | None = None
        self.address: int = DEFAULT_FIRMWARE_FLASH_ADDRESS
        self.timeout_s: float | None = None

    def parse(self) -> None:
        tag_name = self.command_node.tag.lstrip("!").rstrip(":")
        if tag_name != "ProgramEsptool":
            raise ValueError("Expected !ProgramEsptool command")

        for key_node, value_node in self.command_node.value:
            if not isinstance(key_node, yaml.ScalarNode) or not isinstance(value_node, yaml.ScalarNode):
                continue
            self._parse_field(key_node.value, value_node.value)

        self._validate_parsed_fields()
        self._resolve_relative_firmware_dir()

        images = ", ".join(f"{name}@0x{address:X}" for address, name in self._images() if name is not None)
        LOGGER.info(
            "Parsed ProgramEsptool values: name=%s, port=%s, baudrate=%s, firmware_dir=%s, images=[%s], timeout_s=%s",
            self.name,
            self.port,
            self.baudrate,
            self.firmware_dir,
            images,
            self.timeout_s,
        )

    def _parse_field(self, key: str, value: str) -> None:
        """Store one scalar YAML field. Unknown keys are ignored."""
        if key == "name":
            self.name = value
        elif key == "port":
            self.port = value
        elif key == "baudrate":
            self.baudrate = int(value)
        elif key == "firmware_dir":
            self.firmware_dir = value
        elif key == "bootloader":
            self.bootloader = value
        elif key == "bootloader_address":
            self.bootloader_address = self._parse_address(key, value)
        elif key == "partition_table":
            self.partition_table = value
        elif key == "partition_table_address":
            self.partition_table_address = self._parse_address(key, value)
        elif key == "ota_data":
            self.ota_data = value
        elif key == "ota_data_address":
            self.ota_data_address = self._parse_address(key, value)
        elif key == "firmware":
            self.firmware = value
        elif key == "address":
            self.address = self._parse_address(key, value)
        elif key == "timeout_s":
            self.timeout_s = float(value)

    @staticmethod
    def _parse_address(key: str, raw_address: str) -> int:
        """Convert a YAML flash address (`0x10000`, `65536`) to an int."""
        try:
            return int(raw_address, 0)
        except ValueError:
            raise ValueError(f"ProgramEsptool: '{key}' is not a valid number: {raw_address}") from None

    def _validate_parsed_fields(self) -> None:
        """Reject a scenario missing YAML-only required fields, before any hardware is touched."""
        if self.firmware is None:
            raise ValueError("ProgramEsptool: no firmware filename specified in YAML")
        if self.ota_data is not None and self.ota_data_address is None:
            raise ValueError(
                "ProgramEsptool: 'ota_data' needs 'ota_data_address' (the otadata offset from the partition table)"
            )

    def _images(self) -> list[tuple[int | None, str | None]]:
        """Every (address, filename) pair this command can flash, set or not."""
        return [
            (self.bootloader_address, self.bootloader),
            (self.partition_table_address, self.partition_table),
            (self.ota_data_address, self.ota_data),
            (self.address, self.firmware),
        ]

    def _resolve_relative_firmware_dir(self) -> None:
        """Resolve a relative firmware_dir set in YAML against the scenario file.

        Not applied to a CLI --firmware value, which is already relative to the
        shell's own working directory.
        """
        if self.firmware_dir is None:
            return
        firmware_dir_path = Path(self.firmware_dir)
        if not firmware_dir_path.is_absolute() and self.scenario_dir is not None:
            self.firmware_dir = str(self.scenario_dir / firmware_dir_path)

    def execute(self) -> None:
        flash_data = self._resolve_flash_data()

        LOGGER.info("Connecting to ESP32 on %s at %d baud", self.port, self.baudrate)
        for address, path in flash_data:
            LOGGER.info("Will flash %s at 0x%X", path, address)

        self._run_esptool(flash_data)

    def _resolve_flash_data(self) -> list[tuple[int, str]]:
        """Build the (address, file) list to flash, validating every path first.

        port and firmware_dir can't be validated at parse time like the fields
        above: apply_cli_overrides() may still fill them in from -p/--firmware
        after parse() runs, so their absence is only certain once execute() is
        reached.
        """
        if self.port is None:
            raise ValueError("ProgramEsptool: no serial port specified (set in YAML or pass --port on the command line)")
        if self.firmware_dir is None:
            raise ValueError("ProgramEsptool: no firmware directory specified (pass --firmware on the command line)")

        base = Path(self.firmware_dir)
        if not base.is_dir():
            raise ValueError(f"ProgramEsptool: firmware path is not a directory: {self.firmware_dir}")

        flash_data: list[tuple[int, str]] = []
        for address, filename in self._images():
            if filename is None:  # everything but firmware is optional
                continue
            path = base / filename
            if not path.is_file():
                raise FileNotFoundError(f"ProgramEsptool: binary not found: {path}")
            flash_data.append((address, str(path)))

        # Low address first, the order a manual esptool invocation would write them.
        flash_data.sort()
        self._reject_overlaps(flash_data)
        return flash_data

    @staticmethod
    def _reject_overlaps(flash_data: list[tuple[int, str]]) -> None:
        """Fail before connecting if writing one image would erase part of the next.

        esptool erases whole flash sectors, so each image occupies its size
        rounded up to a sector.
        """
        for (address, path), (next_address, next_path) in zip(flash_data, flash_data[1:]):
            sectors = -(-Path(path).stat().st_size // FLASH_SECTOR_SIZE)
            end = address + sectors * FLASH_SECTOR_SIZE
            if end > next_address:
                raise ValueError(
                    f"ProgramEsptool: {path} at 0x{address:X} runs to 0x{end:X} and overlaps {next_path} "
                    f"at 0x{next_address:X}; check the addresses against the partition table"
                )

    def _run_esptool(self, flash_data: list[tuple[int, str]]) -> None:
        """Flash the device, failing the step if `timeout_s` elapses first.

        Unlike `!ProgramJlink`, which kills its own subprocess, esptool runs
        in-process and cannot be interrupted mid-write. The timeout therefore
        fails the command (which stops the scenario) while the flash thread is
        left to finish on its own; it is a daemon, so it never holds up exit.
        """
        if self.timeout_s is None:
            self._flash(flash_data)
            return

        error: list[BaseException] = []

        def worker() -> None:
            try:
                self._flash(flash_data)
            except BaseException as exc:  # noqa: BLE001 - re-raised on the calling thread
                error.append(exc)

        thread = threading.Thread(target=worker, name="esptool-flash", daemon=True)
        thread.start()
        thread.join(self.timeout_s)

        if thread.is_alive():
            raise TimeoutError(f"ProgramEsptool: flashing did not finish within {self.timeout_s} second(s)")
        if error:
            raise error[0]

    def _flash(self, flash_data: list[tuple[int, str]]) -> None:
        """Connect, write every (address, file) pair, and reset the chip."""
        # `with` relies on ESPLoader's own __enter__/__exit__ to close the port,
        # rather than reaching into its private `_port` attribute ourselves.
        # run_stub() returns a different (stub loader) instance sharing the same
        # underlying port, but the `with` statement holds onto the original
        # detect_chip() instance regardless of what `esp` gets reassigned to, so
        # __exit__ still closes the right port on the way out.
        with esptool.detect_chip(port=self.port, baud=self.baudrate) as esp:
            esp = esptool.run_stub(esp)
            esptool.write_flash(esp, flash_data)
            LOGGER.info("Flash complete, resetting device")
            esp.hard_reset()
