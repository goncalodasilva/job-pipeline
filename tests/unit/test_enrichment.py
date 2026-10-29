"""M7.2 pure backlog, scheduling and suggestion rules with fictional signals."""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from app.domain.enrichment import (
    BacklogItem,
    ItemSignals,
    ItemState,
    JobDemand,
    expired,
    order_backlog,
    record_outcome,
    score,
    suggestion_due,
)
from app.domain.facts import FactContent
from app.domain.quality_hints import quality_hints
from app.domain.settings import SESSIONS, SessionSettings
from tests.fakes.clock import FrozenClock

NOW = datetime(2026, 3, 27, 11, tzinfo=UTC)
ZONE = ZoneInfo("Europe/Paris")
CLOCK = FrozenClock(NOW)


def item(key="a", kind="cluster", strength="none", focused=False):
    return BacklogItem(kind, key, strength, focused)


def signals(weight="saved", kind="key", *, job="j1", interview=None, **extra):
    return ItemSignals(jobs=(JobDemand(job, weight, kind, interview),), **extra)


def ordered(items, signal_map=None, states=None, now=NOW):
    if signal_map is None:
        signal_map = {i.key: signals() for i in items}
    return order_backlog(items, signal_map, states or {}, now)


@pytest.mark.parametrize(
    "case",
    [
        "demand_counts_applications_and_interviews_most",
        "requirement_kind_weights",
        "none_before_thin_before_hints",
        "capture_note_raises",
        "cooldown_lowers",
        "not_sure_returns_after_cooldown",
        "later_next_session_first",
        "expired_never_offered",
    ],
)
def test_ac_m7_1_backlog_order(case):
    a, b, c, d = (item(key) for key in "abcd")
    if case == "demand_counts_applications_and_interviews_most":
        rows = [a, b, c, d]
        sm = {
            a.key: signals(),
            b.key: signals("preparing"),
            c.key: signals("active"),
            d.key: signals("active", interview=NOW + timedelta(days=14)),
        }
        assert ordered(rows, sm) == (d, c, b, a)
    elif case == "requirement_kind_weights":
        assert ordered(
            [a, b, c],
            {
                a.key: signals(kind="nice"),
                b.key: signals(kind="key"),
                c.key: signals(kind="must_have"),
            },
        ) == (c, b, a)
    elif case == "none_before_thin_before_hints":
        b, c = item("b", strength="thin"), item("c", "quality_hint")
        assert ordered([c, b, a]) == (a, b, c)
    elif case == "capture_note_raises":
        b = item("b", "capture_note")
        assert ordered([a, b], {a.key: signals(), b.key: ItemSignals()}) == (b, a)
    elif case == "cooldown_lowers":
        state = ItemState(cooldown_until=NOW + timedelta(days=1))
        assert ordered([a, b], states={a.key: state}) == (b,)
        assert ordered([a], states={a.key: state}) == (a,)
    elif case == "not_sure_returns_after_cooldown":
        state = record_outcome(
            ItemState(), "not_sure", NOW, ZONE, wording="Tell me about an orchard project."
        )
        assert ordered([a, b], states={a.key: state}) == (b,)
        assert ordered([a, b], states={a.key: state}, now=NOW + timedelta(days=21)) == (a, b)
    elif case == "later_next_session_first":
        state = record_outcome(ItemState(), "later", NOW, ZONE, "next_session")
        assert ordered([a, b], states={b.key: state}) == (b, a)
    elif case == "expired_never_offered":
        assert ordered([a, b], {a.key: ItemSignals(), b.key: signals()}) == (b,)
        assert ordered([a], states={a.key: ItemState(status="expired")}) == ()


def test_backlog_excludes_later_dates_even_when_nothing_else_remains():
    a = item()
    state = record_outcome(ItemState(), "later", NOW, ZONE, "week")
    assert ordered([a], states={a.key: state}) == ()
    assert ordered([a], states={a.key: state}, now=state.later_until) == (a,)


def test_strong_clusters_only_offered_when_focused():
    strong = item(strength="strong")
    assert ordered([strong]) == ()
    assert ordered([replace(strong, focused=True)]) == (replace(strong, focused=True),)


@given(
    st.permutations([item("a"), item("b"), item("c", strength="thin"), item("d", "capture_note")])
)
def test_backlog_order_total_and_deterministic(rows):
    expected = ordered(sorted(rows, key=lambda i: i.key))
    assert ordered(rows) == expected
    assert len({i.key for i in expected}) == len(expected)


def test_demand_counts_each_job_once_and_interview_window_is_bounded():
    a = item()
    one = signals("active", "must_have")
    duplicate = ItemSignals(jobs=one.jobs * 2)
    assert score(a, one, ItemState(), NOW) == score(a, duplicate, ItemState(), NOW)
    for interview in [NOW - timedelta(seconds=1), NOW + timedelta(days=14, seconds=1)]:
        assert score(
            a, signals("active", "must_have", interview=interview), ItemState(), NOW
        ) == score(a, one, ItemState(), NOW)
    many = ItemSignals(jobs=(*one.jobs, JobDemand("j2", "saved", "must_have")))
    assert score(a, many, ItemState(), NOW) > score(a, one, ItemState(), NOW)


@pytest.mark.parametrize(
    "state,kind,expected",
    [
        ("saved", "nice", 3),
        ("saved", "key", 6),
        ("saved", "must_have", 9),
        ("preparing", "nice", 6),
        ("preparing", "key", 12),
        ("preparing", "must_have", 18),
        ("active", "nice", 9),
        ("active", "key", 18),
        ("active", "must_have", 27),
    ],
)
def test_exact_demand_and_weakness_weights_ignore_closed_jobs(state, kind, expected):
    asking = signals(state, kind)
    mixed = replace(
        asking, jobs=(*asking.jobs, JobDemand("closed-job", "closed", "must_have", NOW))
    )
    assert score(item(), mixed, ItemState(), NOW) == expected
    assert score(item(strength="thin"), mixed, ItemState(), NOW) == expected * 2 // 3
    assert score(item(kind="quality_hint"), mixed, ItemState(), NOW) == expected // 3


def test_exact_interview_and_capture_note_weights():
    assert score(item(), signals("saved", "must_have", interview=NOW), ItemState(), NOW) == 36
    assert score(item(kind="capture_note"), ItemSignals(), ItemState(), NOW) == 21


@pytest.mark.parametrize(
    "kind,updates",
    [
        ("cluster", {}),
        ("capture_note", {"note_exists": False}),
        ("capture_note", {"note_resolved": True}),
        ("quality_hint", {"fact_status": "draft"}),
        ("quality_hint", {"fact_status": "archived"}),
        ("quality_hint", {"fact_privacy": "never_use"}),
        ("quality_hint", {"hint_applies": False}),
    ],
)
def test_item_expiry(kind, updates):
    assert expired(item(kind=kind), ItemSignals(**updates))


@pytest.mark.parametrize(
    "choice,expected",
    [
        ("next_session", None),
        ("week", datetime(2026, 4, 3, 10, tzinfo=UTC)),
        ("month", datetime(2026, 4, 27, 10, tzinfo=UTC)),
        (date(2026, 4, 5), datetime(2026, 4, 4, 22, tzinfo=UTC)),
    ],
)
def test_ac_m7_4_ask_me_later_schedules(choice, expected):
    state = record_outcome(
        ItemState(cooldown_until=NOW + timedelta(days=30)), "later", CLOCK.now(), ZONE, choice
    )
    assert state.later_until == expected
    assert state.later_next_session == (choice == "next_session")
    assert state.cooldown_until is None


def test_ac_m7_4_ask_me_later_month_end_clamped():
    now = datetime(2028, 1, 31, 11, tzinfo=UTC)
    state = record_outcome(ItemState(), "later", now, ZONE, "month")
    assert state.later_until == datetime(2028, 2, 29, 11, tzinfo=UTC)


def test_ac_m7_4_ask_me_later_default_from_settings():
    settings = SessionSettings(ask_later_default="next_session")
    state = record_outcome(ItemState(), "later", NOW, ZONE, settings.ask_later_default)
    assert state.later_next_session


@pytest.mark.parametrize("outcome,days", [("answered", 30), ("skipped", 14), ("not_sure", 21)])
def test_ac_m7_4_cooldowns(outcome, days):
    before = ItemState(later_next_session=True, earlier_wordings=("Earlier wording",))
    state = record_outcome(before, outcome, NOW, ZONE, wording="Current wording")
    assert state.cooldown_until == NOW + timedelta(days=days)
    assert state.later_until is None and not state.later_next_session
    assert state.last_outcome == outcome and state.last_asked_at == NOW
    assert state.asked_count == 1
    assert state.not_sure_count == int(outcome == "not_sure")
    assert before.later_next_session  # no mutation


@pytest.mark.parametrize("day", [date(2026, 3, 27), date(2026, 3, 26)])
def test_ask_later_date_must_be_after_today(day):
    with pytest.raises(ValueError, match="after today"):
        record_outcome(ItemState(), "later", NOW, ZONE, day)


def test_not_sure_keeps_earlier_wordings_capped():
    state = ItemState()
    for n in range(8):
        state = record_outcome(state, "not_sure", NOW, ZONE, wording=f"Fictional angle {n}")
    assert state.not_sure_count == state.asked_count == 8
    assert state.earlier_wordings == tuple(f"Fictional angle {n}" for n in range(3, 8))


@pytest.mark.parametrize(
    "case,settings,last_reset,last_applied,expected",
    [
        ("never", {"frequency": "never"}, None, NOW, False),
        ("weekly", {}, NOW - timedelta(days=7), None, True),
        ("weekly_not_yet", {}, NOW - timedelta(days=7) + timedelta(seconds=1), None, False),
        ("twice_weekly", {"frequency": "twice_weekly"}, NOW - timedelta(hours=84), None, True),
        ("twice_not_yet", {"frequency": "twice_weekly"}, NOW - timedelta(hours=83), None, False),
        (
            "after_application",
            {"frequency": "after_application"},
            NOW - timedelta(days=1),
            NOW,
            True,
        ),
        ("application_already_seen", {"frequency": "after_application"}, NOW, NOW, False),
        ("no_application", {"frequency": "after_application"}, None, None, False),
        ("paused", {"paused": True}, None, NOW, False),
        ("reset_by_dismiss", {}, NOW, None, False),
        ("reset_by_session", {}, NOW, NOW, False),
        ("first_weekly", {}, None, None, True),
    ],
)
def test_ac_m7_6_suggestion_due(case, settings, last_reset, last_applied, expected):
    assert (
        suggestion_due(SessionSettings(**settings), last_reset, last_applied, CLOCK.now())
        == expected
    )


@pytest.mark.parametrize(
    "case,kind,content,status,privacy,expected",
    [
        (
            "story_without_result",
            "story",
            FactContent(action="Counted 12 seedlings"),
            "confirmed",
            "normal",
            ("Add a result?",),
        ),
        (
            "no_number",
            "fact",
            FactContent(text="Managed orchard inventory"),
            "confirmed",
            "normal",
            ("Add a number?",),
        ),
        (
            "story_without_action",
            "story",
            FactContent(result="Saved 12 hours"),
            "confirmed",
            "normal",
            ("Add what you did?",),
        ),
        (
            "complete_none",
            "story",
            FactContent(action="Counted 12 seedlings", result="Saved 2 hours"),
            "confirmed",
            "normal",
            (),
        ),
        ("never_use_none", "story", FactContent(), "confirmed", "never_use", ()),
        ("draft_none", "story", FactContent(), "draft", "normal", ()),
    ],
)
def test_quality_hints(case, kind, content, status, privacy, expected):
    assert quality_hints(kind, content, status, privacy) == expected


@pytest.mark.parametrize(
    "field,value",
    [
        ("length", 4),
        ("length", True),
        ("frequency", "daily"),
        ("ask_later_default", "tomorrow"),
        ("paused", "false"),
    ],
)
def test_session_settings_validation(field, value):
    with pytest.raises(ValidationError):
        SessionSettings.model_validate({field: value})


def test_session_settings_defaults_and_roundtrip():
    settings = SessionSettings()
    assert (settings.frequency, settings.length, settings.ask_later_default, settings.paused) == (
        "weekly",
        3,
        "week",
        False,
    )
    assert SESSIONS.key == "sessions" and SESSIONS.default == settings
    assert SessionSettings.model_validate_json(settings.model_dump_json()) == settings


@pytest.mark.parametrize(
    "now,expected",
    [
        # Automatic local week delays retain 02:30; a spring gap advances to 03:30.
        (datetime(2026, 3, 22, 1, 30, tzinfo=UTC), datetime(2026, 3, 29, 1, 30, tzinfo=UTC)),
        # An autumn repeated 02:30 uses its first occurrence.
        (datetime(2026, 10, 18, 0, 30, tzinfo=UTC), datetime(2026, 10, 25, 0, 30, tzinfo=UTC)),
    ],
)
def test_later_week_gap_and_repeated_hour(now, expected):
    assert record_outcome(ItemState(), "later", now, ZONE, "week").later_until == expected


def test_later_month_rolls_into_next_year():
    now = datetime(2026, 12, 31, 11, tzinfo=UTC)
    assert record_outcome(ItemState(), "later", now, ZONE, "month").later_until == datetime(
        2027, 1, 31, 11, tzinfo=UTC
    )


def test_ask_later_validates_against_local_today():
    now = datetime(2026, 3, 27, 23, 30, tzinfo=UTC)  # March 28 locally
    with pytest.raises(ValueError, match="after today"):
        record_outcome(ItemState(), "later", now, ZONE, date(2026, 3, 28))


def test_missing_signals_and_duplicate_keys_are_not_silently_ranked():
    assert order_backlog([item()], {}, {}, NOW) == ()
    with pytest.raises(ValueError, match="unique"):
        ordered([item(), item()])


def test_unrecognized_outcome_and_later_choice_refused():
    with pytest.raises(ValueError, match="outcome"):
        record_outcome(ItemState(), "unknown", NOW, ZONE)
    with pytest.raises(ValueError, match="Choose"):
        record_outcome(ItemState(), "later", NOW, ZONE, "tomorrow")
    with pytest.raises(ValueError, match="aware"):
        suggestion_due(SessionSettings(), None, None, NOW.replace(tzinfo=None))
