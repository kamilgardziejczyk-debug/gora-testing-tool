from abc import ABC, abstractmethod
from pathlib import Path


class Wrapper(ABC):
    """Base class for scenario command wrappers.

    Both attributes below are generic across all tags and are filled in by the
    Parser, so individual wrappers don't need to parse them themselves.

    `wait_after_s` comes from the command's YAML node (if present) and the
    runner sleeps for it after `execute()` returns.

    `scenario_dir` is the directory holding the scenario file, set before
    `parse()` is called. Wrappers taking file paths should resolve relative
    ones against it so scenarios stay portable.
    """

    wait_after_s: float | None = None
    scenario_dir: Path | None = None

    @abstractmethod
    def parse(self) -> None:
        pass

    @abstractmethod
    def execute(self) -> None:
        pass
