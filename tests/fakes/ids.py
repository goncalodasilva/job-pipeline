"""Predictable identifiers for tests (spec 4.2)."""


class SequentialIdFactory:
    def __init__(self, prefix: str = "id") -> None:
        self._prefix = prefix
        self._count = 0

    def new(self) -> str:
        self._count += 1
        return f"{self._prefix}-{self._count:04d}"
