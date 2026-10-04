"""Job page fetch interface (spec 2.4, 2.9.6, ADR-020, ADR-036).

One user-supplied URL at a time, http(s) only, SSRF-guarded, never crawling. The fetched
text is untrusted content (spec 3.3). The guarded implementation arrives in M4.
"""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class FetchLimits:
    max_bytes: int = 5 * 1024 * 1024
    timeout_seconds: float = 15.0
    max_redirects: int = 5


@dataclass(frozen=True)
class FetchedPage:
    """A fetched page, already decoded to text (declared charset first, then detection).

    ``text`` is the raw HTML or plain text; extraction happens afterwards. Pages that can't
    be fetched raise ``FetchRefused`` or ``NetworkUnavailable`` (spec 2.11) instead of
    returning an error status.
    """

    requested_url: str
    final_url: str
    status: int
    content_type: str
    text: str  # untrusted content


class JobPageFetcher(Protocol):
    # Implementations clamp ``limits`` to the spec 2.9.6 maximums (5 MB, 15 s); callers can
    # only lower them.
    async def fetch(self, url: str, limits: FetchLimits) -> FetchedPage: ...
