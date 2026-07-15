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


class ProgramJlinkWarpper(Wrapper):
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
            elif key == "firmware":
                self.firmware = value_node.value
            elif key == "address":
                self.address = value_node.value

        LOGGER.info(
            "Parsed ProgramJlink values: name=%s, device=%s, interface=%s, speed=%d, firmware=%s, address=%s",
            self.name,
            self.device,
            self.interface,
            self.speed,
            self.firmware,
            self.address,
        )

    def execute(self) -> None:
        if self.device is None:
            raise ValueError("ProgramJlink: 'device' (MCU name) must be specified in YAML")
        if self.firmware is None:
            raise ValueError("ProgramJlink: no firmware filename specified in YAML")
        if self.firmware_dir is None:
            raise ValueError("ProgramJlink: no firmware directory specified (pass --firmware on command line)")

        base = Path(self.firmware_dir)
        if not base.is_dir():
            raise ValueError(f"ProgramJlink: firmware path is not a directory: {self.firmware_dir}")

        firmware_path = base / self.firmware
        if not firmware_path.is_file():
            raise FileNotFoundError(f"ProgramJlink: binary not found: {firmware_path}")

        # If it's a raw binary, an address is required for JLinkExe
        suffix = firmware_path.suffix.lower()
        if suffix in {".bin", ".raw"} and not self.address:
            raise ValueError("ProgramJlink: 'address' is required for raw binary files (.bin / .raw)")

        # Create temporary script file for J-Link Commander
        script_cmds = [
            "r",  # Reset
            "h",  # Halt
        ]

        if self.address:
            script_cmds.append(f"loadfile {firmware_path} {self.address}")
        else:
            script_cmds.append(f"loadfile {firmware_path}")

        script_cmds.extend([
            "r",  # Reset
            "g",  # Start CPU
            "qc"  # Quit Commander
        ])

        with tempfile.NamedTemporaryFile(mode="w", suffix=".jlink", delete=False) as f:
            f.write("\n".join(script_cmds) + "\n")
            script_file_path = f.name

        # Construct JLinkExe command (Windows: JLink.exe, Linux/macOS: JLinkExe)
        jlink_bin = "JLink.exe" if os.name == "nt" else "JLinkExe"

        cmd = [
            jlink_bin,
            "-device", self.device,
            "-if", self.interface,
            "-speed", str(self.speed),
            "-autoconnect", "1",
            "-ExitOnError", "1",
            "-CommanderScript", script_file_path,
        ]

        LOGGER.info("Flashing device %s using J-Link on interface %s at %d kHz", self.device, self.interface, self.speed)
        LOGGER.info("Executing: %s", " ".join(cmd))

        try:
            result = subprocess.run(cmd, check=True, capture_output=True, text=True)
            LOGGER.info("J-Link Execution successful:\n%s", result.stdout)
        except subprocess.CalledProcessError as e:
            LOGGER.error("J-Link command failed with exit code %d", e.returncode)
            LOGGER.error("J-Link stdout:\n%s", e.stdout)
            LOGGER.error("J-Link stderr:\n%s", e.stderr)
            raise
        finally:
            try:
                os.remove(script_file_path)
            except Exception:
                pass
