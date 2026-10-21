"""A company's application history (M5.6): status wording, order and the re-apply count.

Pure: the caller passes the instants. The note is information only; it never blocks anything.
The window is a fixed 30 days (ADR-102).
"""

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any, Final

REAPPLY_DAYS: Final = 30
REAPPLY_MINIMUM: Final = 2


def status_words(label: str, phase: str) -> str:
    """The status in words: ``Closed: rejected`` for a closed status, else its label.

    The label's first letter is lowercased after ``Closed: `` unless the second letter is
    uppercase too, which marks an acronym or a name (``Closed: NDA signed``)."""
    if phase != "closed":
        return label
    keep = len(label) > 1 and label[1].isupper()
    return f"Closed: {label if keep else label[:1].lower() + label[1:]}"


def ordered_history(rows: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Applications that were applied for come first, the latest applied date first; ones not
    yet applied for follow, the latest created first. Ties go by id, newest id first."""
    applied = [row for row in rows if row["applied_at"]]
    waiting = [row for row in rows if not row["applied_at"]]
    return [
        *sorted(applied, key=lambda row: (row["applied_at"], row["id"]), reverse=True),
        *sorted(waiting, key=lambda row: (row["created_at"], row["id"]), reverse=True),
    ]


def recent_applications(applied_at: Iterable[datetime | None], now: datetime) -> int:
    """How many applications were made in the last 30 days (ones not yet applied don't count)."""
    start = now - timedelta(days=REAPPLY_DAYS)
    return sum(1 for when in applied_at if when is not None and when >= start)


def reapply_note(count: int) -> str:
    """The gentle note for two or more recent applications, else an empty string."""
    if count < REAPPLY_MINIMUM:
        return ""
    return f"{count} applications here in the last {REAPPLY_DAYS} days."
