"""What of a model's company brief the app keeps (M8.3, spec 3.6, ADR-124).

Pure. A brief is a few short sections, each with one source url the search really returned.
Nothing without such a source is kept, so the app never shows an unsourced claim as research.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Final

from app.domain.web_sources import _hidden, _safe_url, _source_date

HEADINGS: Final = (
    "what_they_do",
    "products",
    "size_and_location",
    "recent_news",
    "culture",
    "other",
)
MAX_SECTIONS: Final = 6
MAX_TEXT: Final = 600
_MAX_ITEMS: Final = 50
# The prompt asks for plain facts without links; text holding a web address is not kept.
_WEB_ADDRESS: Final = ("http://", "https://", "www.")
LABELS: Final = {
    "what_they_do": "What they do",
    "products": "Products",
    "size_and_location": "Size and location",
    "recent_news": "Recent news",
    "culture": "Culture",
    "other": "Other",
}


@dataclass(frozen=True)
class BriefSection:
    heading: str
    text: str
    source_url: str
    source_date: str | None


@dataclass(frozen=True)
class ResolvedBrief:
    sections: tuple[BriefSection, ...]
    sources: tuple[str, ...]


def _brief_text(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_TEXT:
        return ""
    if any(_hidden(c) for c in value):
        return ""
    lowered = value.casefold()
    if any(marker in lowered for marker in _WEB_ADDRESS):
        return ""
    return value.strip()


def resolve_brief(data: Mapping[str, Any], searched_urls: Iterable[str]) -> ResolvedBrief:
    allowed = frozenset(searched_urls)
    items = data.get("sections", [])
    if not isinstance(items, list):
        return ResolvedBrief((), ())
    kept: list[BriefSection] = []
    for item in items[:_MAX_ITEMS]:
        if len(kept) >= MAX_SECTIONS:
            break
        if not isinstance(item, dict):
            continue
        heading, url = item.get("heading"), item.get("source_url")
        text = _brief_text(item.get("text"))
        if (
            not text
            or not isinstance(heading, str)
            or heading not in HEADINGS
            or not isinstance(url, str)
            or url not in allowed
            or not _safe_url(url)
        ):
            continue
        kept.append(BriefSection(heading, text, url, _source_date(item.get("source_date"))))
    return ResolvedBrief(tuple(kept), tuple(dict.fromkeys(s.source_url for s in kept)))


def brief_prompt_text(sections: Iterable[BriefSection], limit: int) -> str:
    """The saved sections as plain lines for another task's untrusted ``brief`` input: heading
    label and text only (no urls), cut from the end at ``limit`` characters."""
    lines = (f"{LABELS.get(s.heading, 'Other')}: {s.text}" for s in sections)
    return "\n\n".join(lines)[: max(0, limit)]
