"""Board screen rules (M5.4): the view, sort and page values from the address, the words for
days and sorts, and the address a view lives at.

Pure: no I/O. Query values come from the address bar, so each is validated here and a bad one
is dropped for its default, never echoed or turned into an error.
"""

from typing import Final
from urllib.parse import urlencode

VIEWS: Final = ("board", "table")
SORT_LABELS: Final = {
    "company": "Company",
    "role": "Role",
    "status": "Status",
    "applied": "Applied",
    "activity": "Last activity",
}
DEFAULT_SORT: Final = "activity"
# The direction a column starts in: names and the pipeline A to Z, dates newest first.
NATURAL: Final = {
    "company": "asc",
    "role": "asc",
    "status": "asc",
    "applied": "desc",
    "activity": "desc",
}
_SORT_WORDS: Final = {
    "company": ("A to Z", "Z to A"),
    "role": ("A to Z", "Z to A"),
    "status": ("pipeline order", "reverse pipeline order"),
    "applied": ("oldest first", "newest first"),
    "activity": ("oldest first", "newest first"),
}
MAX_PAGE: Final = 1_000_000
MAX_FEEDBACK: Final = 5000
MAX_FEEDBACK_MESSAGE: Final = "Keep the feedback to 5000 characters or fewer."
EVENT_LABELS: Final = {
    "interview": "Interview",
    "follow_up": "Follow-up",
    "deadline": "Deadline",
    "offer_deadline": "Offer deadline",
}


def view_choice(raw: str) -> str:
    return raw if raw in VIEWS else "board"


def sort_choice(sort: str, direction: str) -> tuple[str, str]:
    """The sort column and direction to use. An unknown column is the default order (latest
    activity first) whatever the direction; an unknown direction is the column's natural one."""
    if sort not in SORT_LABELS:
        return DEFAULT_SORT, NATURAL[DEFAULT_SORT]
    return sort, direction if direction in ("asc", "desc") else NATURAL[sort]


def other_direction(direction: str) -> str:
    return "desc" if direction == "asc" else "asc"


def page_number(raw: str) -> int:
    """A page from the query: digits from 1. A number too big to be a page is the biggest
    allowed (the service reads it as the last page); anything else is the first page."""
    if not (raw.isascii() and raw.isdigit()):
        return 1
    if len(raw) > 9:
        return MAX_PAGE
    return min(max(int(raw), 1), MAX_PAGE)


def sort_message(sort: str, direction: str) -> str:
    ascending, descending = _SORT_WORDS[sort]
    return f"Sorted by {SORT_LABELS[sort]}, {descending if direction == 'desc' else ascending}."


def days_in_words(days: int) -> str:
    """Whole days since the last change, in words: today, yesterday, 3 days ago."""
    if days <= 0:
        return "today"
    if days == 1:
        return "yesterday"
    return f"{days} days ago"


def address(
    view: str, sort: str = DEFAULT_SORT, direction: str = "", page: int = 1, closed: bool = False
) -> str:
    """The page address for validated values: the board view carries only what it uses."""
    if view != "table":
        return "/board?" + urlencode({"closed": "1"}) if closed else "/board"
    values = {"view": "table", "sort": sort, "dir": direction or NATURAL[sort]}
    if page > 1:
        values["page"] = str(page)
    return "/board?" + urlencode(values)


def clean_feedback(text: str) -> str:
    """Feedback as stored: line breaks as ``\\n`` (a browser sends ``\\r\\n``) and no NUL."""
    return text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
