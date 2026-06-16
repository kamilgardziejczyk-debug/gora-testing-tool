from abc import ABC, abstractmethod


class Wrapper(ABC):
    @abstractmethod
    def parse(self) -> None:
        pass

    @abstractmethod
    def execute(self) -> None:
        pass
