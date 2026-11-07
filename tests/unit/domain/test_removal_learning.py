"""AC-M9.1: qualifying removals produce explainable, reversible suggestions."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.domain.removal_learning import (
    Condition,
    JobSignals,
    detect_suggestions,
    preference_fields,
)

ONSITE = Condition("work_mode", "on_site")


def jobs(count: int) -> list[JobSignals]:
    return [JobSignals(f"job-{i:04d}", (ONSITE,), removal_reason="location") for i in range(count)]


def test_ac_m9_1_suggestion_only_at_or_above_threshold() -> None:
    assert detect_suggestions(jobs(2), threshold=3) == ()
    assert len(detect_suggestions(jobs(3), threshold=3)) == 1
    assert len(detect_suggestions(jobs(4), threshold=3)) == 1


def test_ac_m9_1_no_suggestion_when_an_application_contradicts() -> None:
    contradicting = JobSignals("other-0001", (ONSITE,), protected=True)
    assert detect_suggestions([*jobs(3), contradicting]) == ()


@pytest.mark.parametrize(
    ("reason", "fields"),
    [
        ("too_junior", ("seniority",)),
        ("too_senior", ("seniority",)),
        ("location", ("location", "work_mode")),
        ("salary", ("minimum_salary",)),
        ("industry", ("industry",)),
        ("company", ("company",)),
        ("not_interested", ()),
        ("other", ()),
    ],
)
def test_removal_reason_maps_to_preference_field(reason: str, fields: tuple[str, ...]) -> None:
    assert preference_fields(reason) == fields


def test_restored_jobs_not_counted() -> None:
    assert detect_suggestions([*jobs(2), JobSignals("restored-0001", (ONSITE,))]) == ()


def test_dismissed_suggestion_returns_only_when_evidence_doubles() -> None:
    (suggestion,) = detect_suggestions(jobs(3))
    dismissals = {suggestion.condition.signature: 3}
    assert detect_suggestions(jobs(5), dismissals=dismissals) == ()
    (again,) = detect_suggestions(jobs(6), dismissals=dismissals)
    assert again.condition == suggestion.condition
    assert len(again.evidence_job_ids) == 6


def test_suggestion_explains_its_evidence() -> None:
    (suggestion,) = detect_suggestions(jobs(3))
    assert suggestion.evidence_job_ids == ("job-0000", "job-0001", "job-0002")
    assert suggestion.explanation == "You removed 3 jobs for Location that match Work mode: Onsite."


@given(st.permutations(jobs(5)))
def test_detection_deterministic_and_order_independent(shuffled: list[JobSignals]) -> None:
    assert detect_suggestions(shuffled) == detect_suggestions(jobs(5))


def test_group_removal_reasons_unknown_ids_and_fields_dropped() -> None:
    from app.domain.removal_learning import resolve_removal_themes

    current = jobs(3)
    valid_ids = [job.id for job in current]
    themes = [
        {"field": "notes", "job_ids": valid_ids},
        {"field": "work_mode", "job_ids": [*valid_ids, "unknown-0001"]},
        {"field": "work_mode", "job_ids": valid_ids},
        {"field": "work_mode", "job_ids": list(reversed(valid_ids))},
    ]
    assert resolve_removal_themes(themes, frozenset(valid_ids), current) == (
        (ONSITE, tuple(valid_ids)),
    )


def test_group_removal_reasons_drops_ambiguous_locations_and_ids_not_sent() -> None:
    from app.domain.removal_learning import resolve_removal_themes

    paris = Condition("location", "Paris")
    london = Condition("location", "London")
    all_jobs = tuple(JobSignals(f"job-{i}", (paris, london)) for i in range(4))
    sent = frozenset(job.id for job in all_jobs[:3])
    theme = {"field": "location", "job_ids": sorted(sent)}
    assert resolve_removal_themes([theme], sent, all_jobs) == ()
    single_location = tuple(JobSignals(job.id, (paris,)) for job in all_jobs)
    invalid = [
        {**theme, "field": "notes"},
        {**theme, "job_ids": [*sorted(sent), all_jobs[3].id]},
    ]
    assert resolve_removal_themes(invalid, sent, single_location) == ()
    assert resolve_removal_themes([theme], sent, single_location) == ((paris, tuple(sorted(sent))),)
