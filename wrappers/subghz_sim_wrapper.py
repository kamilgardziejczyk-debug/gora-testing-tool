import logging
import subprocess
import sys
import time
from pathlib import Path

import yaml

from .wrapper import Wrapper


LOGGER = logging.getLogger(__name__)

DEFAULT_BAUD = 115200
PROCESS_EXIT_TIMEOUT_S = 5.0

SCRIPT_PATH = Path(__file__).resolve().parent.parent / "tools" / "subghz_sim" / "subghz_sim.py"

ACTION_VERBS = {"add", "set", "del", "list"}


class SubghzSimWrapper(Wrapper):
    """
    Wrapper that drives tools/subghz_sim as a scripted REPL session: launches
    the simulator, feeds it a sequence of commands with waits in between, keeps
    it active for `duration_s`, then quits it.
    """

    def __init__(self, command_node: yaml.MappingNode):
        self.command_node = command_node
        self.name: str | None = None
        self.port: str | None = None
        self.baud: int = DEFAULT_BAUD
        self.duration_s: float | None = None
        self.actions: list[tuple[str, str, float | None]] = []

    def parse(self) -> None:
        tag_name = self.command_node.tag.lstrip("!").rstrip(":")
        if tag_name != "SubghzSim":
            raise ValueError("Expected !SubghzSim command")

        for key_node, value_node in self.command_node.value:
            if not isinstance(key_node, yaml.ScalarNode):
                continue

            key = key_node.value
            if key == "actions":
                self.actions = self._parse_actions(value_node)
            elif isinstance(value_node, yaml.ScalarNode):
                if key == "name":
                    self.name = value_node.value
                elif key == "port":
                    self.port = value_node.value
                elif key == "baud":
                    self.baud = int(value_node.value)
                elif key == "duration_s":
                    self.duration_s = float(value_node.value)

        if self.port is None:
            raise ValueError("SubghzSim: 'port' field is required")

        LOGGER.info(
            "Parsed SubghzSim values: name=%s, port=%s, baud=%s, duration_s=%s, actions=%d",
            self.name,
            self.port,
            self.baud,
            self.duration_s,
            len(self.actions),
        )

    def _parse_actions(self, actions_node: yaml.Node) -> list[tuple[str, str, float | None]]:
        """Extract (verb, arg, wait_after_ms) tuples from a sequence of action mappings."""
        if not isinstance(actions_node, yaml.SequenceNode):
            return []

        actions: list[tuple[str, str, float | None]] = []
        for action_node in actions_node.value:
            if not isinstance(action_node, yaml.MappingNode):
                continue

            verb: str | None = None
            arg = ""
            wait_after_ms: int | None = None
            for key_node, value_node in action_node.value:
                if not isinstance(key_node, yaml.ScalarNode) or not isinstance(value_node, yaml.ScalarNode):
                    continue
                key = key_node.value
                if key == "wait_after_ms":
                    wait_after_ms = int(value_node.value)
                elif key in ACTION_VERBS:
                    verb = key
                    arg = value_node.value

            if verb is None:
                LOGGER.warning("Skipping SubghzSim action with no recognized verb (%s)", ACTION_VERBS)
                continue

            actions.append((verb, arg, wait_after_ms))

        return actions

    def execute(self) -> None:
        if not SCRIPT_PATH.is_file():
            raise FileNotFoundError(f"SubghzSim: simulator script not found: {SCRIPT_PATH}")

        cmd = [
            sys.executable,
            str(SCRIPT_PATH),
            "--port", self.port,
            "--baud", str(self.baud),
        ]

        LOGGER.info("Starting subghz_sim on %s at %d baud", self.port, self.baud)
        start_time = time.monotonic()
        process = subprocess.Popen(cmd, stdin=subprocess.PIPE, text=True, bufsize=1)

        try:
            for verb, arg, wait_after_ms in self.actions:
                self._send_line(process, f"{verb} {arg}".strip())
                if wait_after_ms is not None:
                    time.sleep(wait_after_ms / 1000)

            if self.duration_s is not None:
                remaining_s = self.duration_s - (time.monotonic() - start_time)
                if remaining_s > 0:
                    LOGGER.info("Keeping subghz_sim active for %.1f more second(s)", remaining_s)
                    time.sleep(remaining_s)

            self._send_line(process, "quit")
            process.wait(timeout=PROCESS_EXIT_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            LOGGER.warning("subghz_sim did not exit after quit, terminating")
            process.terminate()
            try:
                process.wait(timeout=PROCESS_EXIT_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        finally:
            if process.stdin is not None and not process.stdin.closed:
                process.stdin.close()

        LOGGER.info("subghz_sim session finished with exit code %s", process.returncode)

    def _send_line(self, process: subprocess.Popen, line: str) -> None:
        """Write a single REPL command line to the simulator's stdin."""
        if process.stdin is None or process.stdin.closed:
            raise RuntimeError("SubghzSim: process stdin is not available")
        LOGGER.info("subghz_sim <- %s", line)
        process.stdin.write(line + "\n")
        process.stdin.flush()
