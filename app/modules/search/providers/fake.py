"""Offline delegated search metadata with an explicit storage policy."""

from typing import Final, Literal

from app.modules.search import SearchResult


class FakeSearchProvider:
    name: Final = "fake"
    mode: Final[Literal["delegated"]] = "delegated"
    reports_usage: Final = False

    def __init__(self, *, storage_allowed: bool | None = True) -> None:
        self.storage_allowed = storage_allowed

    async def search(self, query: str, limit: int) -> list[SearchResult]:
        raise NotImplementedError("Delegated search runs through a metered AI task.")
