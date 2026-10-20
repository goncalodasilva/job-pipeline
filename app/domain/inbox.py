"""Read-only inbox options and results; no framework or storage access."""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class InboxFilters:
    view: str = "queue"
    band: str | None = None
    work_mode: str | None = None
    industry: str | None = None
    seniority: str | None = None
    company: str | None = None
    flagged: bool | None = None
    search: str = ""
    sort: str = "newest"

    def __post_init__(self) -> None:
        if self.view not in ("queue", "snoozed", "removed", "to_apply"):
            raise ValueError("unknown inbox view")
        if self.sort not in ("newest", "posting_age", "band"):
            raise ValueError("unknown inbox sort")
        if self.band not in (None, "strong", "possible", "stretch"):
            raise ValueError("unknown fit band")
        if len(self.search) > 500:
            raise ValueError("search is longer than 500 characters")


@dataclass(frozen=True)
class InboxPage:
    items: tuple[dict[str, Any], ...]
    total: int
    page: int
    next_card: dict[str, Any] | None


@dataclass(frozen=True)
class InboxGroup:
    value: str | None
    label: str
    count: int
