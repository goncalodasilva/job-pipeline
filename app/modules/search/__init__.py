"""Search module interface (spec 2.4). The first provider delegates to the AI provider (ADR-009)."""

import threading
from collections import OrderedDict
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


class TransientSearchResults:
    """Bounded process-local results for providers that do not permit saving output."""

    def __init__(self) -> None:
        self._values: OrderedDict[str, object] = OrderedDict()
        self._lock = threading.Lock()

    def put(self, key: str, value: object) -> None:
        with self._lock:
            self._values[key] = value
            self._values.move_to_end(key)
            while len(self._values) > 100:
                self._values.popitem(last=False)

    def get(self, key: str) -> object | None:
        with self._lock:
            return self._values.get(key)

    def clear(self) -> None:
        """Forget everything held (delete all data, ADR-131 item 2)."""
        with self._lock:
            self._values.clear()
