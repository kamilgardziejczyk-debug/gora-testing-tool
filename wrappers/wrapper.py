from abc import ABC, abstractmethod


class Wrapper(ABC):
    """Base class for scenario command wrappers.

    `wait_after_s` is generic across all tags: the Parser fills it in from the
    command's YAML node (if present) and the runner sleeps for it after
    `execute()` returns, so individual wrappers don't need to parse or apply
    it themselves.
    """

    wait_after_s: float | None = None

    @abstractmethod
    def parse(self) -> None:
        pass

    @abstractmethod
    def execute(self) -> None:
        pass
