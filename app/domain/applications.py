"""Pure application status and transition rules (M5.2, ADR-102 and ADR-103).

Every transition returns its history data, including a move to the current status.
Services persist the status, history and first applied time in the same transaction.
"""

import unicodedata
from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from app.domain.ids import is_safe_id

HistorySource = Literal["created", "board", "menu", "drawer", "backfill"]
FollowUp = Literal["interview_date", "feedback"]


class ApplicationPhase(StrEnum):
    PREPARING = "preparing"
    ACTIVE = "active"
    CLOSED = "closed"


@dataclass(frozen=True)
class ApplicationStatus:
    key: str
    label: str
    phase: ApplicationPhase
    position: int
    built_in: bool = False


def create_status(
    *,
    key: str,
    label: str,
    phase: str,
    position: int,
    built_in: bool = False,
    existing_labels: Collection[str] = (),
) -> ApplicationStatus:
    """Validate a new or renamed status; callers exclude its own label when renaming."""
    cleaned = label.strip()
    if (
        not cleaned
        or len(cleaned) > 100
        or any(unicodedata.category(c)[0] == "C" or c in "\n\r\u2028\u2029" for c in cleaned)
    ):
        raise ValueError("Give the status a visible name on one line, up to 100 characters.")
    if cleaned.casefold() in {value.strip().casefold() for value in existing_labels}:
        raise ValueError("Another status already has that name.")
    if not is_safe_id(key):
        raise ValueError("The status key is invalid.")
    if type(position) is not int or position < 0:
        raise ValueError("The status position must be a non-negative integer.")
    return ApplicationStatus(key, cleaned, ApplicationPhase(phase), position, built_in)


def ensure_status_deletable(status: ApplicationStatus, *, in_use: bool) -> None:
    if status.built_in:
        raise ValueError("Built-in statuses cannot be deleted.")
    if in_use:
        raise ValueError("This status is still in use.")


@dataclass(frozen=True)
class StatusTransition:
    from_status: str
    to_status: str
    changed_at: datetime
    source: HistorySource
    applied_at: datetime | None
    follow_up: FollowUp | None


def move_application(
    current_status: str,
    applied_at: datetime | None,
    target: ApplicationStatus,
    *,
    changed_at: datetime,
    source: HistorySource,
) -> StatusTransition:
    """All status pairs are allowed. Follow-ups depend on built-in keys, never labels."""
    if changed_at.tzinfo is None or changed_at.utcoffset() is None:
        raise ValueError("A status change needs an aware instant.")
    if applied_at is not None and (applied_at.tzinfo is None or applied_at.utcoffset() is None):
        raise ValueError("The applied time must be an aware instant.")
    if source not in {"created", "board", "menu", "drawer", "backfill"}:
        raise ValueError("The status change source is invalid.")
    instant = changed_at.astimezone(UTC)
    first_applied = applied_at
    if first_applied is None and target.phase == ApplicationPhase.ACTIVE:
        first_applied = instant
    follow_up: FollowUp | None = None
    if target.built_in and target.key == "interviewing":
        follow_up = "interview_date"
    elif target.built_in and target.key == "rejected":
        follow_up = "feedback"
    return StatusTransition(current_status, target.key, instant, source, first_applied, follow_up)
