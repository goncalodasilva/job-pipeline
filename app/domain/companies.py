"""Companies (M4.10): the list's sort and paging, and the checks for a company's details.

Pure: no I/O. Company names, notes and websites come from the web or the user and are untrusted
data (spec 3.3); this module only bounds and shapes them, and the templates escape them.
"""

import unicodedata
from collections.abc import Collection, Mapping, Sequence
from typing import Any, Final

from app.domain.errors import JobLinkInvalid
from app.domain.job_intake import checked_link
from app.domain.job_normalization import company_key

PAGE_SIZE: Final = 50
MAX_PAGE: Final = 1_000_000
SORTS: Final = {
    "name": "Name",
    "open_roles": "Open roles",
    "to_apply": "To apply",
    "last_activity": "Last activity",
}
# The direction a column starts in: names A to Z, the others biggest or newest first.
NATURAL: Final = {"name": "asc", "open_roles": "desc", "to_apply": "desc", "last_activity": "desc"}
_WORDS: Final = {
    "name": ("A to Z", "Z to A"),
    "last_activity": ("oldest first", "newest first"),
}
_COUNT_WORDS: Final = ("fewest first", "most first")

COMPANY_FIELDS: Final = ("name", "website", "industry", "size", "locations", "notes")
LIMITS: Final = {
    "name": 200,
    "website": 2000,
    "industry": 100,
    "size": 100,
    "locations": 10000,
    "notes": 10000,
}
MAX_LOCATION: Final = 200
MAX_LOCATIONS: Final = 100
NAME_TAKEN: Final = "Another company is already called that."


class CompanyEditInvalid(Exception):
    def __init__(self, errors: dict[str, str]) -> None:
        self.errors = errors
        super().__init__("Check the highlighted fields.")


def sort_choice(sort: str, direction: str) -> tuple[str, str]:
    """The sort column and direction to use: an unknown column is the name, an unknown
    direction the column's natural one."""
    column = sort if sort in SORTS else "name"
    return column, direction if direction in ("asc", "desc") else NATURAL[column]


def other_direction(direction: str) -> str:
    return "desc" if direction == "asc" else "asc"


def sort_message(sort: str, direction: str) -> str:
    ascending, descending = _WORDS.get(sort, _COUNT_WORDS)
    return f"Sorted by {SORTS[sort]}, {descending if direction == 'desc' else ascending}."


def ordered(
    rows: Sequence[Mapping[str, Any]], sort: str, direction: str
) -> list[Mapping[str, Any]]:
    """The rows by ``sort``. Ties sort by name without regard to case, then id, whichever way
    the column goes (the name column itself runs both ways)."""
    first = sorted(rows, key=lambda row: (row["name"].casefold(), row["id"]))
    if sort == "name":
        return first[::-1] if direction == "desc" else first
    return sorted(first, key=lambda row: row[sort], reverse=direction == "desc")


def page_number(raw: str) -> int:
    """A page from the query: digits from 1, else the first page."""
    if raw.isascii() and raw.isdigit() and 1 <= int(raw) <= MAX_PAGE:
        return int(raw)
    return 1


_JOINERS: Final = "\u200c\u200d"  # zero-width (non-)joiner: needed to spell some scripts


def _clean_line(value: str) -> bool:
    """No control, format or invisible characters (bidi controls, zero-width space, BOM,
    line and paragraph separators), except the joiners some scripts need."""
    return not any(
        c not in _JOINERS
        and (unicodedata.category(c)[0] == "C" or unicodedata.category(c) in ("Zl", "Zp"))
        for c in value
    )


def _clean_text(value: str) -> bool:
    return _clean_line(value.replace("\n", "").replace("\t", ""))


def safe_website(value: str | None) -> str | None:
    """The website as a link target: http or https only, no markup characters, else None."""
    if not value or len(value) > LIMITS["website"] or any(c in value for c in '<>"\\'):
        return None
    try:
        return checked_link(value)
    except JobLinkInvalid:
        return None


def checked_edit(
    values: Mapping[str, str],
    current: Mapping[str, Any],
    *,
    industries: Collection[str],
) -> dict[str, Any]:
    """The posted fields, checked and in stored form (``None`` or ``[]`` for an empty one).

    Only fields present in ``values`` are checked. An industry must be one of ``industries``
    (the controlled list) or the one already stored. Raises ``CompanyEditInvalid`` with an
    error per field. The name's uniqueness is the caller's check (it needs the other names).
    """
    clean: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for field in COMPANY_FIELDS:
        if field not in values:
            continue
        value = values[field].replace("\r\n", "\n").replace("\r", "\n").strip()
        multiline = field in ("notes", "locations")
        if len(value) > LIMITS[field]:
            errors[field] = f"Use {LIMITS[field]:,} characters or fewer."
        elif not (_clean_text(value) if multiline else _clean_line(value)):
            errors[field] = "Remove invisible or control characters."
        elif field == "name":
            if not value:
                errors[field] = "Enter a company name."
            elif not company_key(value):
                errors[field] = "Enter a company name with letters or numbers."
            else:
                clean[field] = value
        elif field == "website":
            safe = safe_website(value) if value else None
            if value and safe is None:
                errors[field] = "Enter a web address starting with http:// or https://."
            else:
                clean[field] = safe
        elif field == "industry":
            if value and value not in industries and value != current.get("industry"):
                errors[field] = "Choose an industry from the list."
            else:
                clean[field] = value or None
        elif field == "locations":
            places = list(dict.fromkeys(p.strip() for p in value.split("\n") if p.strip()))
            if any(len(p) > MAX_LOCATION for p in places):
                errors[field] = f"Use {MAX_LOCATION} characters or fewer for each place."
            elif len(places) > MAX_LOCATIONS:
                errors[field] = f"Use {MAX_LOCATIONS} places or fewer."
            else:
                clean[field] = places
        else:
            clean[field] = value or None
    if errors:
        raise CompanyEditInvalid(errors)
    return clean
