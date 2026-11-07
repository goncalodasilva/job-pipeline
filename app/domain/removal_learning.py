"""Explainable removal patterns. Callers supply one snapshot of the current jobs."""

import hashlib
import json
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from app.domain.job_normalization import currency_key
from app.domain.removal_reasons import PREFERENCE_FIELD_LABELS, REMOVAL_REASONS

DEFAULT_THRESHOLD: Final = 3
_REASON_LABELS = {reason.key: reason.label for reason in REMOVAL_REASONS}
_REASON_FIELDS = {reason.key: reason.maps_to for reason in REMOVAL_REASONS}
_MODE_LABELS = {"remote": "Remote", "hybrid": "Hybrid", "on_site": "Onsite"}


@dataclass(frozen=True, order=True)
class Condition:
    field: str
    value: str

    @property
    def signature(self) -> str:
        payload = json.dumps([self.field, self.value], ensure_ascii=True)
        return hashlib.sha256(payload.encode()).hexdigest()

    @property
    def label(self) -> str:
        return self.describe()

    def describe(self, display_value: str | None = None) -> str:
        value = display_value or self.value
        if self.field == "work_mode":
            value = _MODE_LABELS.get(value, value)
        if self.field == "minimum_salary":
            return f"Advertised pay ceiling: {value}"
        return f"{PREFERENCE_FIELD_LABELS[self.field]}: {value}"


@dataclass(frozen=True)
class JobSignals:
    id: str
    conditions: tuple[Condition, ...]
    removal_reason: str | None = None  # Current removals only, with their latest reason.
    protected: bool = False  # An application exists, or the job is marked To apply.
    in_queue: bool = False
    removal_version: int | None = None


@dataclass(frozen=True)
class Suggestion:
    condition: Condition
    evidence_job_ids: tuple[str, ...]
    explanation: str


def preference_fields(reason: str) -> tuple[str, ...]:
    return _REASON_FIELDS.get(reason, ())


def detect_suggestions(
    jobs: Iterable[JobSignals],
    *,
    threshold: int = DEFAULT_THRESHOLD,
    dismissals: Mapping[str, int] | None = None,
) -> tuple[Suggestion, ...]:
    """Group removals by reason and shared condition, then check contradictory jobs."""
    groups: dict[tuple[Condition, str], set[str]] = defaultdict(set)
    contradicted: set[Condition] = set()
    for job in jobs:
        if job.protected:
            contradicted.update(job.conditions)
        elif job.removal_reason:
            fields = preference_fields(job.removal_reason)
            for condition in job.conditions:
                if condition.field in fields:
                    groups[condition, job.removal_reason].add(job.id)
    dismissed = dismissals or {}
    # One suggestion per condition. If two reasons point to it, show the stronger evidence.
    by_condition: dict[Condition, Suggestion] = {}
    for (condition, reason), evidence in sorted(groups.items()):
        count = len(evidence)
        if condition in contradicted or count < max(
            threshold, 2 * dismissed.get(condition.signature, 0)
        ):
            continue
        previous = by_condition.get(condition)
        if previous is not None and count <= len(previous.evidence_job_ids):
            continue
        explanation = (
            f"You removed {count} jobs for {_REASON_LABELS[reason]} that match {condition.label}."
        )
        by_condition[condition] = Suggestion(condition, tuple(sorted(evidence)), explanation)
    return tuple(
        sorted(
            by_condition.values(), key=lambda item: (-len(item.evidence_job_ids), item.condition)
        )
    )


def salary_condition(
    maximum: int | float | None, currency: str | None, period: str | None
) -> Condition | None:
    unit = currency_key(currency)
    if maximum is None or not unit or not period:
        return None
    if period.casefold() not in {"year", "yearly", "annual", "annually", "annum"}:
        return None
    amount = Decimal(str(maximum))
    if not amount.is_finite():
        return None
    return Condition("minimum_salary", f"{amount.normalize():f} {unit} per year")


def resolve_removal_themes(
    themes: Iterable[Mapping[str, object]],
    sent_ids: frozenset[str],
    jobs: Iterable[JobSignals],
) -> tuple[tuple[Condition, tuple[str, ...]], ...]:
    """Bind model groups to real shared job values and reject fabricated evidence."""
    by_id = {job.id: job for job in jobs}
    resolved: set[tuple[Condition, tuple[str, ...]]] = set()
    for theme in themes:
        field, ids = theme.get("field"), theme.get("job_ids")
        if not isinstance(field, str) or field not in PREFERENCE_FIELD_LABELS:
            continue
        if (
            not isinstance(ids, list)
            or not ids
            or not all(isinstance(id, str) and id in sent_ids and id in by_id for id in ids)
        ):
            continue
        evidence = tuple(sorted(set(ids)))
        if len(evidence) < 2:
            continue
        first, *remaining = evidence
        shared = {item for item in by_id[first].conditions if item.field == field}
        for id in remaining:
            shared.intersection_update(by_id[id].conditions)
        if len(shared) == 1:
            resolved.add((next(iter(shared)), evidence))
    return tuple(sorted(resolved))
