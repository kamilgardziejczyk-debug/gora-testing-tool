import logging
import os
import subprocess
import tempfile
from pathlib import Path

import yaml

from .wrapper import Wrapper


LOGGER = logging.getLogger(__name__)

DEFAULT_INTERFACE = "SWD"
DEFAULT_SPEED = 4000


class ProgramJlinkWrapper(Wrapper):
    """
    Wrapper for programming devices using the SEGGER J-Link command-line tool.
    """

    def __init__(self, command_node: yaml.MappingNode):
        self.command_node = command_node
        self.name: str | None = None
        self.device: str | None = None
        self.interface: str = DEFAULT_INTERFACE
        self.speed: int = DEFAULT_SPEED
        self.firmware_dir: str | None = None
        self.firmware: str | None = None
        self.address: str | None = None
        self.timeout_s: float | None = None

    def parse(self) -> None:
        tag_name = self.command_node.tag.lstrip("!").rstrip(":")
        if tag_name != "ProgramJlink":
            raise ValueError("Expected !ProgramJlink command")

        for key_node, value_node in self.command_node.value:
            if not isinstance(key_node, yaml.ScalarNode) or not isinstance(value_node, yaml.ScalarNode):
                continue

            key = key_node.value
            if key == "name":
                self.name = value_node.value
            elif key == "device":
                self.device = value_node.value
            elif key == "interface":
                self.interface = value_node.value
            elif key == "speed":
                self.speed = int(value_node.value)
            elif key == "firmware_dir":
                self.firmware_dir = value_node.value
            elif key == "firmware":
                self.firmware = value_node.value
            elif key == "address":
                self.address = value_node.value
            elif key == "timeout_s":
                self.timeout_s = float(value_node.value)

        self._validate_parsed_fields()
        self._resolve_relative_firmware_dir()

        LOGGER.info(
            "Parsed ProgramJlink values: name=%s, device=%s, interface=%s, speed=%d, firmware_dir=%s, "
            "firmware=%s, address=%s, timeout_s=%s",
            self.name,
            self.device,
            self.interface,
            self.speed,
            self.firmware_dir,
            self.firmware,
            self.address,
            self.timeout_s,
        )

    def _validate_parsed_fields(self) -> None:
        """Reject a scenario missing YAML-only required fields, before any hardware is touched."""
        if self.device is None:
            raise ValueError("ProgramJlink: 'device' (MCU name) must be specified in YAML")
        if self.firmware is None:
            raise ValueError("ProgramJlink: no firmware filename specified in YAML")

        # Only depends on the filename's own suffix, not the resolved firmware_dir,
        # so it can be checked here rather than waiting for execute().
        suffix = Path(self.firmware).suffix.lower()
        if suffix in {".bin", ".raw"} and not self.address:
            raise ValueError("ProgramJlink: 'address' is required for raw binary files (.bin / .raw)")

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
        firmware_path = self._resolve_firmware_path()
        script_file_path = self._write_script_file(firmware_path)
        cmd = self._build_command(script_file_path)

        LOGGER.info("Flashing device %s using J-Link on interface %s at %d kHz", self.device, self.interface, self.speed)
        LOGGER.info("Executing: %s", " ".join(cmd))

        try:
            self._run_jlink(cmd)
        finally:
            try:
                os.remove(script_file_path)
            except Exception:
                pass

    def _resolve_firmware_path(self) -> Path:
        """Validate firmware_dir/firmware and resolve them to the binary's path.

        firmware_dir can't be validated at parse time: apply_cli_overrides() may
        still fill it in from --firmware after parse() runs, so its absence is
        only certain once execute() is reached.
        """
        if self.firmware_dir is None:
            raise ValueError("ProgramJlink: no firmware directory specified (pass --firmware on command line)")

        base = Path(self.firmware_dir)
        if not base.is_dir():
            raise ValueError(f"ProgramJlink: firmware path is not a directory: {self.firmware_dir}")

        firmware_path = base / self.firmware
        if not firmware_path.is_file():
            raise FileNotFoundError(f"ProgramJlink: binary not found: {firmware_path}")

        return firmware_path

    def _write_script_file(self, firmware_path: Path) -> str:
        """Write the J-Link Commander script (reset, load, reset, run) to a tempfile."""
        script_cmds = ["r", "h"]  # Reset, Halt

        if self.address:
            script_cmds.append(f"loadfile {firmware_path} {self.address}")
        else:
            script_cmds.append(f"loadfile {firmware_path}")

        script_cmds.extend(["r", "g", "qc"])  # Reset, Start CPU, Quit Commander

        with tempfile.NamedTemporaryFile(mode="w", suffix=".jlink", delete=False) as f:
            f.write("\n".join(script_cmds) + "\n")
            return f.name

    def _build_command(self, script_file_path: str) -> list[str]:
        """Construct the JLinkExe/JLink.exe invocation for this device/interface."""
        jlink_bin = "JLink.exe" if os.name == "nt" else "JLinkExe"
        return [
            jlink_bin,
            "-device", self.device,
            "-if", self.interface,
            "-speed", str(self.speed),
            "-autoconnect", "1",
            "-ExitOnError", "1",
            "-CommanderScript", script_file_path,
        ]

    def _run_jlink(self, cmd: list[str]) -> None:
        """Run JLinkExe/JLink.exe, logging output on success, failure, or timeout."""
        try:
            result = subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=self.timeout_s)
            LOGGER.info("J-Link Execution successful:\n%s", result.stdout)
        except subprocess.CalledProcessError as e:
            LOGGER.error("J-Link command failed with exit code %d", e.returncode)
            LOGGER.error("J-Link stdout:\n%s", e.stdout)
            LOGGER.error("J-Link stderr:\n%s", e.stderr)
            raise
        except subprocess.TimeoutExpired as e:
            LOGGER.error("J-Link command timed out after %s second(s)", self.timeout_s)
            LOGGER.error("J-Link stdout:\n%s", e.stdout)
            LOGGER.error("J-Link stderr:\n%s", e.stderr)
            raise
