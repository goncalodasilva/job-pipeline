"""Jobs domain: triage transitions, snooze due on the local date, "Processing" derived from an
active ``summarize_job`` task, and the field merge that never replaces a user-corrected field
(M4.2; groundwork for AC-M4.2 to AC-M4.9; ADR-098 items 3 and 8).

``app.domain.jobs`` is pure: ``TriageState`` and ``TriageAction``, the frozen ``Triage`` value
(state, the previous state kept for undo, the snooze date), ``effective_state`` (a snoozed job
whose date has come reads as New; reading never writes), ``local_today`` (the user's local date
from the Clock's aware UTC instant), ``apply`` (one allowed move, or ``TriageNotAllowed``),
``is_processing`` and the field model ``JOB_FIELDS`` / ``merge_fields``.

Imports are inside the tests so each one fails on its own while the module is missing. Job ids
and values are fictional.
"""

from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given
from hypothesis import strategies as st

if TYPE_CHECKING:
    from app.domain.jobs import Triage

TODAY = date(2026, 10, 19)
LATER = TODAY + timedelta(days=7)

# The states and actions, by value (ADR-098 item 3: "to_apply"; the schema CHECK in 0004).
STATE_VALUES = ("new", "seen", "snoozed", "removed", "to_apply")
ACTION_VALUES = ("show", "snooze", "remove", "to_apply", "undo", "restore")

# Starting points: (state, previous_state, snooze_until).
STARTS: dict[str, tuple[str, str | None, date | None]] = {
    "new": ("new", None, None),
    "seen": ("seen", None, None),
    "snoozed_due": ("snoozed", "seen", TODAY),
    "snoozed_overdue": ("snoozed", "seen", TODAY - timedelta(days=2)),
    "snoozed_later": ("snoozed", "seen", TODAY + timedelta(days=1)),
    "removed_from_new": ("removed", "new", None),
    "removed_from_seen": ("removed", "seen", None),
    "removed_no_previous": ("removed", None, None),
    "to_apply_from_new": ("to_apply", "new", None),
    "to_apply_from_seen": ("to_apply", "seen", None),
    "to_apply_no_previous": ("to_apply", None, None),
}

SEEN = ("seen", None, None)

# What each (start, action) gives; None means refused with TriageNotAllowed. SNOOZE is asked
# with snooze_until=LATER. Every pair is listed, so a move nobody decided on fails here.
EXPECTED: dict[tuple[str, str], tuple[str, str | None, date | None] | None] = {
    # SHOW: a card is marked seen once displayed; a due snooze reads as New and can be shown.
    ("new", "show"): SEEN,
    ("seen", "show"): SEEN,
    ("snoozed_due", "show"): SEEN,
    ("snoozed_overdue", "show"): SEEN,
    ("snoozed_later", "show"): None,
    ("removed_from_new", "show"): None,
    ("removed_from_seen", "show"): None,
    ("removed_no_previous", "show"): None,
    ("to_apply_from_new", "show"): None,
    ("to_apply_from_seen", "show"): None,
    ("to_apply_no_previous", "show"): None,
    # SNOOZE: from Seen (a card is marked seen when shown, before it can be snoozed), from a
    # due snooze (it reads as New, which Undo then goes back to), or again from a snooze not
    # yet due, which changes its date.
    ("new", "snooze"): None,
    ("seen", "snooze"): ("snoozed", "seen", LATER),
    ("snoozed_due", "snooze"): ("snoozed", "new", LATER),
    ("snoozed_overdue", "snooze"): ("snoozed", "new", LATER),
    ("snoozed_later", "snooze"): ("snoozed", "seen", LATER),
    ("removed_from_new", "snooze"): None,
    ("removed_from_seen", "snooze"): None,
    ("removed_no_previous", "snooze"): None,
    ("to_apply_from_new", "snooze"): None,
    ("to_apply_from_seen", "snooze"): None,
    ("to_apply_no_previous", "snooze"): None,
    # REMOVE: New or Seen as read today (a due snooze reads as New), keeping that state for
    # undo; a snooze not yet due is removed from the Snoozed view and keeps Seen.
    ("new", "remove"): ("removed", "new", None),
    ("seen", "remove"): ("removed", "seen", None),
    ("snoozed_due", "remove"): ("removed", "new", None),
    ("snoozed_overdue", "remove"): ("removed", "new", None),
    ("snoozed_later", "remove"): ("removed", "seen", None),
    ("removed_from_new", "remove"): None,
    ("removed_from_seen", "remove"): None,
    ("removed_no_previous", "remove"): None,
    ("to_apply_from_new", "remove"): None,
    ("to_apply_from_seen", "remove"): None,
    ("to_apply_no_previous", "remove"): None,
    # TO_APPLY: as Remove (ADR-098 item 3).
    ("new", "to_apply"): ("to_apply", "new", None),
    ("seen", "to_apply"): ("to_apply", "seen", None),
    ("snoozed_due", "to_apply"): ("to_apply", "new", None),
    ("snoozed_overdue", "to_apply"): ("to_apply", "new", None),
    ("snoozed_later", "to_apply"): ("to_apply", "seen", None),
    ("removed_from_new", "to_apply"): None,
    ("removed_from_seen", "to_apply"): None,
    ("removed_no_previous", "to_apply"): None,
    ("to_apply_from_new", "to_apply"): None,
    ("to_apply_from_seen", "to_apply"): None,
    ("to_apply_no_previous", "to_apply"): None,
    # UNDO: back to the kept state, which is then cleared; a snooze not yet due is unsnoozed.
    # A due snooze already reads as New, so there is nothing to undo.
    ("new", "undo"): None,
    ("seen", "undo"): None,
    ("snoozed_due", "undo"): None,
    ("snoozed_overdue", "undo"): None,
    ("snoozed_later", "undo"): SEEN,
    ("removed_from_new", "undo"): ("new", None, None),
    ("removed_from_seen", "undo"): ("seen", None, None),
    ("removed_no_previous", "undo"): None,
    ("to_apply_from_new", "undo"): ("new", None, None),
    ("to_apply_from_seen", "undo"): ("seen", None, None),
    ("to_apply_no_previous", "undo"): None,
    # RESTORE: the lasting way back from the Removed view, always to Seen.
    ("new", "restore"): None,
    ("seen", "restore"): None,
    ("snoozed_due", "restore"): None,
    ("snoozed_overdue", "restore"): None,
    ("snoozed_later", "restore"): None,
    ("removed_from_new", "restore"): SEEN,
    ("removed_from_seen", "restore"): SEEN,
    ("removed_no_previous", "restore"): SEEN,
    ("to_apply_from_new", "restore"): None,
    ("to_apply_from_seen", "restore"): None,
    ("to_apply_no_previous", "restore"): None,
}

TABLE = sorted(EXPECTED.items())


def _triage(state: str, previous: str | None, snooze_until: date | None) -> "Triage":
    from app.domain.jobs import Triage, TriageState

    return Triage(
        state=TriageState(state),
        previous_state=None if previous is None else TriageState(previous),
        snooze_until=snooze_until,
    )


# --- values -----------------------------------------------------------------


def test_triage_state_and_action_values() -> None:
    from app.domain.jobs import TriageAction, TriageState

    assert tuple(state.value for state in TriageState) == STATE_VALUES
    assert tuple(action.value for action in TriageAction) == ACTION_VALUES


def test_triage_is_frozen() -> None:
    import dataclasses

    triage = _triage("new", None, None)
    with pytest.raises(dataclasses.FrozenInstanceError):
        triage.state = triage.state  # type: ignore[misc]


def test_triage_not_allowed_is_a_user_error_with_plain_words() -> None:
    from app.domain.errors import AppError, TriageNotAllowed, message_for_kind

    error = TriageNotAllowed()
    assert isinstance(error, AppError)
    assert TriageNotAllowed.kind == "TriageNotAllowed"
    assert error.user_message == (
        "This job can't be changed that way now. Reload to see its current state."
    )
    assert message_for_kind("TriageNotAllowed") == error.user_message


# --- transitions --------------------------------------------------------------


@pytest.mark.parametrize(
    ("pair", "expected"),
    TABLE,
    ids=[
        f"{start}-{action}-{'allowed' if result else 'refused'}"
        for (start, action), result in TABLE
    ],
)
def test_triage_transitions(
    pair: tuple[str, str], expected: tuple[str, str | None, date | None] | None
) -> None:
    from app.domain.errors import TriageNotAllowed
    from app.domain.jobs import TriageAction, apply

    start, action = pair
    triage = _triage(*STARTS[start])
    snooze_until = LATER if action == "snooze" else None
    if expected is None:
        with pytest.raises(TriageNotAllowed):
            apply(triage, TriageAction(action), today=TODAY, snooze_until=snooze_until)
        return
    result = apply(triage, TriageAction(action), today=TODAY, snooze_until=snooze_until)
    assert result == _triage(*expected)
    assert triage == _triage(*STARTS[start]), "apply must return a new value"


def test_triage_table_covers_every_start_and_action() -> None:
    assert {start for start, _ in EXPECTED} == set(STARTS)
    assert {action for _, action in EXPECTED} == set(ACTION_VALUES)
    assert len(EXPECTED) == len(STARTS) * len(ACTION_VALUES)


@pytest.mark.parametrize(
    "until",
    [None, TODAY, TODAY - timedelta(days=1)],
    ids=["no-date", "today", "yesterday"],
)
def test_snooze_needs_a_date_after_today(until: date | None) -> None:
    from app.domain.errors import TriageNotAllowed
    from app.domain.jobs import TriageAction, apply

    with pytest.raises(TriageNotAllowed):
        apply(_triage("seen", None, None), TriageAction.SNOOZE, today=TODAY, snooze_until=until)


def test_snooze_until_tomorrow_is_allowed() -> None:
    from app.domain.jobs import TriageAction, apply

    tomorrow = TODAY + timedelta(days=1)
    result = apply(
        _triage("seen", None, None), TriageAction.SNOOZE, today=TODAY, snooze_until=tomorrow
    )
    assert result == _triage("snoozed", "seen", tomorrow)


@pytest.mark.parametrize(
    ("first", "start", "kept"),
    [
        ("remove", "new", "new"),
        ("remove", "seen", "seen"),
        ("to_apply", "new", "new"),
        ("to_apply", "seen", "seen"),
        ("snooze", "seen", "seen"),
    ],
    ids=[
        "remove-from-new",
        "remove-from-seen",
        "to-apply-from-new",
        "to-apply-from-seen",
        "snooze-from-seen",
    ],
)
def test_undo_restores_previous_state(first: str, start: str, kept: str) -> None:
    from app.domain.errors import TriageNotAllowed
    from app.domain.jobs import TriageAction, TriageState, apply

    until = LATER if first == "snooze" else None
    moved = apply(_triage(start, None, None), TriageAction(first), today=TODAY, snooze_until=until)
    assert moved.previous_state == TriageState(kept)

    undone = apply(moved, TriageAction.UNDO, today=TODAY)
    assert undone == _triage(kept, None, None)

    # Undo is spent: a second one has nothing to go back to.
    with pytest.raises(TriageNotAllowed):
        apply(undone, TriageAction.UNDO, today=TODAY)


def test_restore_from_removed_is_seen_whatever_it_was_before() -> None:
    from app.domain.jobs import TriageAction, apply

    removed = apply(_triage("new", None, None), TriageAction.REMOVE, today=TODAY)
    assert apply(removed, TriageAction.RESTORE, today=TODAY) == _triage("seen", None, None)


# --- snooze due on the local date -----------------------------------------------


@pytest.mark.parametrize(
    ("start", "today", "expected"),
    [
        ("new", TODAY, "new"),
        ("seen", TODAY, "seen"),
        ("removed_from_seen", TODAY, "removed"),
        ("to_apply_from_new", TODAY, "to_apply"),
        ("snoozed_later", TODAY, "snoozed"),
        ("snoozed_later", TODAY + timedelta(days=1), "new"),
        ("snoozed_due", TODAY, "new"),
        ("snoozed_due", TODAY - timedelta(days=1), "snoozed"),
        ("snoozed_overdue", TODAY, "new"),
    ],
)
def test_effective_state_reads_a_due_snooze_as_new(start: str, today: date, expected: str) -> None:
    from app.domain.jobs import TriageState, effective_state

    triage = _triage(*STARTS[start])
    assert effective_state(triage, today) == TriageState(expected)
    # Reading never changes the stored value.
    assert triage == _triage(*STARTS[start])


@pytest.mark.parametrize(
    ("zone_name", "until", "local_midnight_utc"),
    [
        # NZDT (UTC+13): the local date turns while the UTC date is still the day before.
        ("Pacific/Auckland", date(2026, 10, 26), datetime(2026, 10, 25, 11, 0, tzinfo=UTC)),
        # PDT (UTC-7): the UTC date turns seven hours before the local date does.
        ("America/Los_Angeles", date(2026, 10, 26), datetime(2026, 10, 26, 7, 0, tzinfo=UTC)),
    ],
    ids=["auckland", "los-angeles"],
)
def test_snooze_due_on_local_date(
    zone_name: str, until: date, local_midnight_utc: datetime
) -> None:
    from app.domain.jobs import TriageState, effective_state, local_today
    from tests.fakes.clock import FrozenClock

    zone = ZoneInfo(zone_name)
    assert datetime(until.year, until.month, until.day, tzinfo=zone) == local_midnight_utc
    triage = _triage("snoozed", "seen", until)
    clock = FrozenClock(local_midnight_utc - timedelta(seconds=1))

    # One second before local midnight: still the day before, still snoozed.
    today = local_today(clock.now(), zone)
    assert today == until - timedelta(days=1)
    assert effective_state(triage, today) == TriageState.SNOOZED

    # Local midnight: due, exactly on its local date.
    clock.advance(timedelta(seconds=1))
    today = local_today(clock.now(), zone)
    assert today == until
    assert effective_state(triage, today) == TriageState.NEW


def test_snooze_not_due_on_the_utc_date_alone() -> None:
    from app.domain.jobs import TriageState, effective_state, local_today

    zone = ZoneInfo("America/Los_Angeles")
    until = date(2026, 10, 26)
    # 03:00 UTC on the 26th is 20:00 on the 25th in Los Angeles.
    now = datetime(2026, 10, 26, 3, 0, tzinfo=UTC)
    assert now.date() == until
    today = local_today(now, zone)
    assert today == date(2026, 10, 25)
    assert effective_state(_triage("snoozed", "seen", until), today) == TriageState.SNOOZED


# --- Processing ---------------------------------------------------------------


def _active_keys(tasks: list[tuple[str, str, str]]) -> set[str]:
    """The dedupe keys of the queued and running tasks among (type, target, status)."""
    from app.domain.tasks import ACTIVE, TaskStatus, dedupe_key

    return {
        dedupe_key(task_type, target)
        for task_type, target, status in tasks
        if TaskStatus(status) in ACTIVE
    }


@pytest.mark.parametrize(
    ("status", "processing"),
    [
        ("queued", True),
        ("running", True),
        ("succeeded", False),
        ("failed", False),
        ("cancelled", False),
        ("interrupted", False),
    ],
)
def test_processing_only_while_summary_task_active(status: str, processing: bool) -> None:
    from app.domain.jobs import is_processing

    keys = _active_keys([("summarize_job", "job-0001", status)])
    assert is_processing("job-0001", keys) is processing
    assert is_processing("job-0002", keys) is False


@pytest.mark.parametrize(
    "keys",
    [
        set(),
        {"summarize_job:job-0002"},
        {"assess_fit:job-0001"},
        {"extract_cv:job-0001"},
        {"summarize_job:job-00011"},
        {"summarize_job"},
        {"job-0001"},
    ],
    ids=[
        "none",
        "another-job",
        "fit-task",
        "another-type",
        "longer-id",
        "type-only",
        "id-only",
    ],
)
def test_processing_is_false_without_this_jobs_summary_key(keys: set[str]) -> None:
    from app.domain.jobs import is_processing

    assert is_processing("job-0001", keys) is False


def test_processing_key_is_the_summarize_job_dedupe_key() -> None:
    from app.domain.jobs import is_processing
    from app.domain.tasks import dedupe_key

    key = dedupe_key("summarize_job", "job-0001")
    assert is_processing("job-0001", frozenset({key, "assess_fit:job-0001"})) is True
    assert is_processing("job-0001", [key]) is True


# --- field model ----------------------------------------------------------------

JOB_FIELDS = (
    "title",
    "company",
    "industry",
    "department",
    "work_mode",
    "locations",
    "employment_type",
    "seniority",
    "experience_min",
    "experience_max",
    "experience_text",
    "salary_min",
    "salary_max",
    "salary_currency",
    "salary_period",
    "posted_date",
)

_values = st.none() | st.integers(min_value=0, max_value=500_000) | st.text(max_size=12)
_fields = st.dictionaries(st.sampled_from(JOB_FIELDS), _values)
_corrected = st.lists(
    st.sampled_from(JOB_FIELDS) | st.sampled_from(("not_a_field", "", "Title", "notes")),
    unique=True,
)


def test_job_fields_are_the_editable_fields() -> None:
    from app.domain.jobs import JOB_FIELDS as fields

    assert tuple(fields) == JOB_FIELDS


@given(current=_fields, new=_fields, corrected=_corrected)
def test_ac_m4_9_user_corrected_fields_never_replaced_by_merge(
    current: dict[str, object], new: dict[str, object], corrected: list[str]
) -> None:
    from app.domain.jobs import merge_fields

    current_before, new_before = dict(current), dict(new)
    merged = merge_fields(current, new, corrected)
    assert set(merged) == set(JOB_FIELDS)
    for name in set(corrected) & set(JOB_FIELDS):
        assert merged[name] == current.get(name), name
    assert current == current_before and new == new_before, "inputs must not change"


@given(current=_fields, new=_fields, corrected=_corrected)
def test_field_merge_takes_new_values_for_uncorrected_fields(
    current: dict[str, object], new: dict[str, object], corrected: list[str]
) -> None:
    from app.domain.jobs import merge_fields

    merged = merge_fields(current, new, corrected)
    assert set(merged) == set(JOB_FIELDS)
    for name in set(JOB_FIELDS) - set(corrected):
        # A field the new summary lacks becomes None, even if the old one had a value.
        assert merged[name] == new.get(name), name


def test_field_merge_examples() -> None:
    from app.domain.jobs import merge_fields

    current = {"title": "Orchard Supervisor", "work_mode": "hybrid", "salary_min": 41000}
    new = {"title": "Orchard Lead", "work_mode": "remote", "locations": ["Larkspur Vale"]}
    merged = merge_fields(current, new, ["work_mode", "unknown_field"])
    assert merged["title"] == "Orchard Lead"
    assert merged["work_mode"] == "hybrid"  # corrected by the user: kept
    assert merged["locations"] == ["Larkspur Vale"]
    assert merged["salary_min"] is None  # not in the new summary, not corrected
    assert "unknown_field" not in merged
