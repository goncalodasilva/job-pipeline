"""Search module interface (spec 2.4). The first provider delegates to the AI provider (ADR-009)."""

from dataclasses import dataclass
from typing import Literal, Protocol


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str


class SearchProvider(Protocol):
    # Read-only members, so a provider may declare narrower types (e.g. always "delegated").
    @property
    def name(self) -> str: ...

    @property
    def mode(self) -> Literal["delegated", "standalone"]:
        """delegated: the AI provider searches itself."""
        ...

    @property
    def storage_allowed(self) -> bool | None:
        """None means unclear: do not cache derived output."""
        ...

    @property
    def reports_usage(self) -> bool: ...

    async def search(self, query: str, limit: int) -> list[SearchResult]: ...  # standalone only
