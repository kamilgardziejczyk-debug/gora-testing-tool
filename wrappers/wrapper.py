from abc import ABC, abstractmethod
from pathlib import Path


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
    """

    wait_after_s: float | None = None
    scenario_dir: Path | None = None
    tag: str | None = None
    raw_yaml: str | None = None
    validation_expected: str | None = None
    validation_actual: str | None = None

    # Capability markers `apply_cli_overrides()` checks instead of an isinstance
    # chain, so a new flashing wrapper opts in here without main.py needing an
    # edit for it. A wrapper declaring one must define the matching attribute
    # (`port` / `firmware_dir`) for the override to actually have somewhere to go.
    supports_port_override: bool = False
    supports_firmware_dir_override: bool = False

    @abstractmethod
    def parse(self) -> None:
        pass

    @abstractmethod
    def execute(self) -> None:
        pass
