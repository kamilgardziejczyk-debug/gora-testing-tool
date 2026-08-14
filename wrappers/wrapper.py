from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # imported for typing only, keeping the base class dependency-free
    from tools.dut_cli import DutShell
    from tools.dut_logger import DutLogger, LogSession


class Wrapper(ABC):
    """Base class for scenario command wrappers.

    All attributes below are generic across all tags and are filled in by the
    Parser, so individual wrappers don't need to parse them themselves.

    `wait_after_s` comes from the command's YAML node (if present) and the
    runner sleeps for it after `execute()` returns.

    `scenario_dir` is the directory holding the scenario file, set before
    `parse()` is called. Wrappers taking file paths should resolve relative
    ones against it so scenarios stay portable.

    `tag` and `raw_yaml` are set before `parse()` is called, for the HTML test
    report: `tag` is the command's tag name (e.g. `"SubghzSim"`) and `raw_yaml`
    is that command's exact source text, so the report can show what was
    configured without each wrapper re-serializing its own fields.

    `validation_expected` / `validation_actual` stay `None` for wrappers with
    no pass/fail assertion of their own. A wrapper that does have one (like
    `!MqttExpect`) sets both, so the report can render them uniformly without
    knowing which wrapper type produced them.

    `log_session` is the run's `LogSession`, set by main.py before the scenario
    runs on wrappers declaring either `requires_dut_log` or `captures_mqtt_log`.
    For `requires_dut_log` it is only set when a DUT console is actually being
    captured, and stays `None` otherwise - which a wrapper needing it must
    report as the misconfiguration it is rather than waiting for output that
    cannot arrive. For `captures_mqtt_log` it is always set, since the MQTT log
    does not depend on any hardware being attached.

    `dut_shell` is the run's shared `DutShell`, set by main.py on wrappers
    declaring `requires_dut_cli`, and only when a shell UART is configured -
    one shell serves the whole scenario, so commands do not each pay for
    opening the port and re-syncing on the prompt.

    `dut_logger` is the run's `DutLogger` itself, set by main.py on wrappers
    declaring `controls_dut_log`. Distinct from `log_session`, which is where
    captured lines end up: a wrapper that only reads the capture wants the
    session, and only one that suspends or resumes the capture needs the reader
    behind it.
    """

    wait_after_s: float | None = None
    scenario_dir: Path | None = None
    tag: str | None = None
    raw_yaml: str | None = None
    validation_expected: str | None = None
    validation_actual: str | None = None
    log_session: LogSession | None = None
    dut_shell: DutShell | None = None
    dut_logger: DutLogger | None = None

    # Capability markers main.py checks instead of an isinstance chain, so a new
    # wrapper opts in here without main.py needing an edit for it. A wrapper
    # declaring one of the override markers must define the matching attribute
    # (`port` / `firmware_dir`) for the override to have somewhere to go.
    supports_port_override: bool = False
    supports_firmware_dir_override: bool = False
    requires_dut_log: bool = False
    requires_dut_cli: bool = False
    captures_mqtt_log: bool = False
    controls_dut_log: bool = False

    @abstractmethod
    def parse(self) -> None:
        pass

    @abstractmethod
    def execute(self) -> None:
        pass
