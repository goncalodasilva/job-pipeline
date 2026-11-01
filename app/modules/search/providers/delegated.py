"""Claude built-in search metadata (ADR-009, ADR-124); execution stays in metered AI calls."""

from typing import Final, Literal

from app.modules.search import SearchResult


class DelegatedAISearch:
    name: Final = "claude_builtin"
    mode: Final[Literal["delegated"]] = "delegated"
    storage_allowed: Final = True
    reports_usage: Final = False

    async def search(self, query: str, limit: int) -> list[SearchResult]:
        raise NotImplementedError("Delegated search runs through a metered AI task.")
