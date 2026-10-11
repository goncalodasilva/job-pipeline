"""Budget domain math (M2.14; spec 3.7; AC-M2.7, AC-M2.8, AC-M2.10; ADR-076 items 5-8, ADR-078).

The API under test (pure ``app.domain.budgets``):

- ``Budget(allowance, first_warning, second_warning, block_at, reset_day, unit="requests",
  fast_cap=None, thorough_cap=None)``: frozen; building it with invalid values raises
  ``InvalidBudget`` (a ``ValueError``) whose ``errors`` equal ``validate_budget(...)``.
- ``validate_budget(**same fields) -> tuple[BudgetFieldError(field, code), ...]``, in field
  order, for M2.22's inline errors. Empty when valid. A rule comparing against a field that is
  itself invalid is skipped, so each mistake is reported once, on its own field.
- ``cycle_bounds(now, reset_day, tz) -> Cycle(start, end, start_date, reset_date)``: the
  cycle holding ``now`` is [start, end), both local midnight in ``tz``; reset days 29-31 move
  to the month's last day; ``start_date`` is the local start date (``budget_notices
  .cycle_start``), ``reset_date`` the local date the next cycle starts.
- ``ProviderCount(unit, window, used)``: the domain-side comparable count M2.15 maps from
  ``ProviderUsageSnapshot``. ``effective_count(local, provider, *, budget_window="month") ->
  EffectiveCount(value, source)``: the higher of the local count and the provider counts
  whose unit is "requests" and whose window matches; a tie keeps "local".
- ``evaluate(budget, count, *, shown, cycle, tier=None, tier_count=None) -> Decision``:
  ``count`` is the effective count used so far in the cycle, NOT including the request about
  to run; the check is made on ``count + 1`` (the count once this request runs), and the
  ``count`` reported in ``Warn`` and ``Note`` is that ``count + 1``. ``tier_count`` is the
  requests already made on ``tier`` this cycle. Order: ``Blocked`` (``count >= block_at``),
  then ``CapBlocked`` (``tier_count >= cap`` for the requested tier), then ``Warn`` for the
  highest threshold reached by ``count + 1`` if not yet shown, else ``Note`` if any threshold
  is reached, else ``Allow``. ``CapBlocked.offer_fast`` is True only for Thorough. No budget
  always allows.

All values are fictional. Times use zoneinfo from the OS tz database.
"""

from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.domain.budgets import (
    Allow,
    Blocked,
    Budget,
    BudgetFieldError,
    CapBlocked,
    Cycle,
    EffectiveCount,
    InvalidBudget,
    Note,
    ProviderCount,
    TierUsage,
    Warn,
    budget_level,
    cap_reached,
    cycle_bounds,
    effective_count,
    evaluate,
    validate_budget,
)

LISBON = ZoneInfo("Europe/Lisbon")
NEW_YORK = ZoneInfo("America/New_York")
LORD_HOWE = ZoneInfo("Australia/Lord_Howe")  # 30-minute DST shift
SAO_PAULO = ZoneInfo("America/Sao_Paulo")  # DST began at midnight until 2018
HAVANA = ZoneInfo("America/Havana")  # DST ends at 01:00, so midnight happens twice
ZONES: tuple[tzinfo, ...] = (UTC, LISBON, NEW_YORK, LORD_HOWE, SAO_PAULO)

VALID: dict[str, Any] = {
    "allowance": 100,
    "first_warning": 50,
    "second_warning": 80,
    "block_at": 100,
    "reset_day": 1,
}


def budget(**overrides: Any) -> Budget:
    return Budget(**{**VALID, **overrides})


def utc(moment: datetime) -> datetime:
    return moment.astimezone(UTC)


def last_day(year: int, month: int) -> int:
    following = date(year + month // 12, month % 12 + 1, 1)
    return (following - timedelta(days=1)).day


CYCLE = cycle_bounds(datetime(2026, 10, 10, 12, tzinfo=UTC), 12, UTC)
RESET = date(2026, 10, 12)


# --- AC-M2.7: threshold validation ------------------------------------------------------------


def test_valid_budget_has_no_errors_and_builds() -> None:
    assert validate_budget(**VALID) == ()
    built = budget(fast_cap=100, thorough_cap=1)
    assert built.unit == "requests"
    assert (built.fast_cap, built.thorough_cap) == (100, 1)
    assert budget().fast_cap is None and budget().thorough_cap is None


@pytest.mark.parametrize(
    ("overrides", "field", "code"),
    [
        ({"allowance": 0}, "allowance", "must_be_positive"),
        ({"allowance": -5}, "allowance", "must_be_positive"),
        ({"allowance": True}, "allowance", "must_be_integer"),
        ({"first_warning": 0}, "first_warning", "must_be_positive"),
        ({"first_warning": 2.5}, "first_warning", "must_be_integer"),
        ({"second_warning": 50}, "second_warning", "must_exceed_first_warning"),
        ({"second_warning": 40}, "second_warning", "must_exceed_first_warning"),
        ({"block_at": 80}, "block_at", "must_exceed_second_warning"),
        ({"block_at": 101}, "block_at", "must_not_exceed_allowance"),
        ({"reset_day": 0}, "reset_day", "out_of_range"),
        ({"reset_day": 32}, "reset_day", "out_of_range"),
        ({"reset_day": "1"}, "reset_day", "must_be_integer"),
        ({"reset_day": 1.0}, "reset_day", "must_be_integer"),
        ({"second_warning": 60.0}, "second_warning", "must_be_integer"),
        ({"block_at": True}, "block_at", "must_be_integer"),
        ({"fast_cap": True}, "fast_cap", "must_be_integer"),
        ({"thorough_cap": True}, "thorough_cap", "must_be_integer"),
        ({"unit": ["requests"]}, "unit", "unsupported"),
        ({"unit": {"requests": 1}}, "unit", "unsupported"),
        ({"unit": "tokens"}, "unit", "unsupported"),
        ({"fast_cap": 0}, "fast_cap", "must_be_positive"),
        ({"fast_cap": 101}, "fast_cap", "must_not_exceed_allowance"),
        ({"thorough_cap": -1}, "thorough_cap", "must_be_positive"),
        ({"thorough_cap": 101}, "thorough_cap", "must_not_exceed_allowance"),
    ],
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_ac_m2_7_threshold_validation(overrides: dict[str, Any], field: str, code: str) -> None:
    values = {**VALID, **overrides}
    errors = validate_budget(**values)
    assert BudgetFieldError(field, code) in errors
    assert {error.field for error in errors} == {field}
    with pytest.raises(InvalidBudget) as raised:
        Budget(**values)
    assert isinstance(raised.value, ValueError)
    assert raised.value.errors == errors


def test_threshold_errors_are_tied_to_each_field_in_order() -> None:
    errors = validate_budget(
        allowance=10, first_warning=9, second_warning=5, block_at=4, reset_day=40, thorough_cap=11
    )
    assert errors == (
        BudgetFieldError("second_warning", "must_exceed_first_warning"),
        BudgetFieldError("block_at", "must_exceed_second_warning"),
        BudgetFieldError("reset_day", "out_of_range"),
        BudgetFieldError("thorough_cap", "must_not_exceed_allowance"),
    )


def test_block_at_may_equal_allowance_and_reset_day_spans_1_to_31() -> None:
    assert validate_budget(**{**VALID, "block_at": 100}) == ()
    for day in (1, 28, 29, 30, 31):
        assert validate_budget(**{**VALID, "reset_day": day}) == ()


@given(
    allowance=st.integers(-3, 30),
    first=st.integers(-3, 30),
    second=st.integers(-3, 30),
    block=st.integers(-3, 30),
    reset_day=st.integers(-1, 33),
    cap=st.none() | st.integers(-3, 33),
)
def test_validation_matches_rules_and_construction(
    allowance: int, first: int, second: int, block: int, reset_day: int, cap: int | None
) -> None:
    values = {
        "allowance": allowance,
        "first_warning": first,
        "second_warning": second,
        "block_at": block,
        "reset_day": reset_day,
        "fast_cap": cap,
    }
    rules_hold = (
        allowance > 0
        and 0 < first < second < block <= allowance
        and 1 <= reset_day <= 31
        and (cap is None or 0 < cap <= allowance)
    )
    errors = validate_budget(**values)
    assert (errors == ()) == rules_hold
    if rules_hold:
        Budget(**values)
    else:
        with pytest.raises(InvalidBudget):
            Budget(**values)


def test_budget_is_frozen() -> None:
    with pytest.raises((AttributeError, TypeError)):
        budget().allowance = 5  # type: ignore[misc]


# --- AC-M2.7: cycles ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reset_day", "now", "start", "end"),
    [
        (31, date(2026, 2, 15), date(2026, 1, 31), date(2026, 2, 28)),
        (31, date(2026, 2, 28), date(2026, 2, 28), date(2026, 3, 31)),
        (31, date(2026, 4, 30), date(2026, 4, 30), date(2026, 5, 31)),
        (31, date(2026, 4, 29), date(2026, 3, 31), date(2026, 4, 30)),
        (30, date(2026, 3, 5), date(2026, 2, 28), date(2026, 3, 30)),
        (30, date(2026, 3, 30), date(2026, 3, 30), date(2026, 4, 30)),
        (29, date(2026, 2, 28), date(2026, 2, 28), date(2026, 3, 29)),
        (29, date(2026, 2, 27), date(2026, 1, 29), date(2026, 2, 28)),
        (31, date(2026, 12, 31), date(2026, 12, 31), date(2027, 1, 31)),
        (31, date(2027, 1, 30), date(2026, 12, 31), date(2027, 1, 31)),
        (1, date(2026, 1, 1), date(2026, 1, 1), date(2026, 2, 1)),
        (15, date(2026, 1, 14), date(2025, 12, 15), date(2026, 1, 15)),
    ],
    ids=lambda value: value.isoformat() if isinstance(value, date) else f"day{value}",
)
def test_ac_m2_7_reset_day_29_30_31_clamped_on_short_months(
    reset_day: int, now: date, start: date, end: date
) -> None:
    cycle = cycle_bounds(datetime.combine(now, time(12), tzinfo=LISBON), reset_day, LISBON)
    assert cycle.start_date == start
    assert cycle.reset_date == end
    assert cycle.start == datetime.combine(start, time(), tzinfo=LISBON)
    assert cycle.end == datetime.combine(end, time(), tzinfo=LISBON)


def test_ac_m2_7_february_leap_and_non_leap() -> None:
    leap = cycle_bounds(datetime(2028, 2, 29, 9, tzinfo=UTC), 29, UTC)
    assert (leap.start_date, leap.reset_date) == (date(2028, 2, 29), date(2028, 3, 29))
    leap_31 = cycle_bounds(datetime(2028, 2, 10, 9, tzinfo=UTC), 31, UTC)
    assert (leap_31.start_date, leap_31.reset_date) == (date(2028, 1, 31), date(2028, 2, 29))
    plain = cycle_bounds(datetime(2027, 2, 28, 9, tzinfo=UTC), 29, UTC)
    assert (plain.start_date, plain.reset_date) == (date(2027, 2, 28), date(2027, 3, 29))
    before = cycle_bounds(datetime(2027, 2, 27, 23, 59, tzinfo=UTC), 30, UTC)
    assert (before.start_date, before.reset_date) == (date(2027, 1, 30), date(2027, 2, 28))


def test_start_is_inclusive_and_end_exclusive() -> None:
    midnight = datetime(2026, 10, 12, tzinfo=LISBON)
    at_start = cycle_bounds(midnight, 12, LISBON)
    assert at_start.start_date == date(2026, 10, 12)
    just_before = cycle_bounds(midnight - timedelta(microseconds=1), 12, LISBON)
    assert just_before.reset_date == date(2026, 10, 12)
    assert just_before.start_date == date(2026, 9, 12)


def test_cycle_uses_the_local_date_not_utc() -> None:
    # 23:30 UTC on 31 March is 00:30 on 1 April in Lisbon (summer time).
    cycle = cycle_bounds(datetime(2026, 3, 31, 23, 30, tzinfo=UTC), 1, LISBON)
    assert cycle.start_date == date(2026, 4, 1)
    # 03:00 UTC on 1 March is still 28 February in New York.
    cycle = cycle_bounds(datetime(2026, 3, 1, 3, tzinfo=UTC), 1, NEW_YORK)
    assert cycle.start_date == date(2026, 2, 1)
    assert cycle.reset_date == date(2026, 3, 1)


def test_cycle_bounds_rejects_naive_time_and_bad_reset_day() -> None:
    with pytest.raises(ValueError):
        cycle_bounds(datetime(2026, 10, 10, 12), 1, UTC)  # noqa: DTZ001 (naive on purpose)
    for day in (0, 32):
        with pytest.raises(ValueError):
            cycle_bounds(datetime(2026, 10, 10, 12, tzinfo=UTC), day, UTC)


def test_cycle_starting_at_a_skipped_midnight() -> None:
    # Sao Paulo skipped 00:00-00:59 on 4 November 2018 (summer time began at midnight).
    cycle = cycle_bounds(datetime(2018, 11, 4, 12, tzinfo=SAO_PAULO), 4, SAO_PAULO)
    assert cycle.start_date == date(2018, 11, 4)
    assert utc(cycle.start) == datetime(2018, 11, 4, 3, tzinfo=UTC)
    before = cycle_bounds(datetime(2018, 11, 3, 23, 59, 59, tzinfo=SAO_PAULO), 4, SAO_PAULO)
    assert (before.start_date, before.reset_date) == (date(2018, 10, 4), date(2018, 11, 4))
    assert utc(before.end) == utc(cycle.start)


def test_cycle_starting_at_a_repeated_midnight_uses_the_first() -> None:
    # Havana went back from 01:00 to 00:00 on 2 November 2025, so midnight happened twice.
    cycle = cycle_bounds(datetime(2025, 11, 2, 12, tzinfo=HAVANA), 2, HAVANA)
    assert cycle.start_date == date(2025, 11, 2)
    assert cycle.start.fold == 0
    assert utc(cycle.start) == datetime(2025, 11, 2, 4, tzinfo=UTC)


def test_cycle_across_a_dst_change_has_both_offsets() -> None:
    # New York moves to summer time at 02:00 on 8 March 2026.
    cycle = cycle_bounds(datetime(2026, 3, 8, 12, tzinfo=NEW_YORK), 8, NEW_YORK)
    assert cycle.start.utcoffset() == timedelta(hours=-5)
    assert cycle.end.utcoffset() == timedelta(hours=-4)
    assert utc(cycle.start) == datetime(2026, 3, 8, 5, tzinfo=UTC)
    # Lord Howe moves forward 30 minutes at 02:00 on 4 October 2026.
    cycle = cycle_bounds(datetime(2026, 10, 4, 3, tzinfo=LORD_HOWE), 4, LORD_HOWE)
    assert cycle.start.utcoffset() == timedelta(hours=10, minutes=30)
    assert cycle.end.utcoffset() == timedelta(hours=11)


# Hypothesis takes naive bounds and attaches the timezone itself.
instants = st.datetimes(
    min_value=datetime(2000, 1, 1),  # noqa: DTZ001
    max_value=datetime(2099, 12, 31),  # noqa: DTZ001
    timezones=st.just(UTC),
)


@settings(max_examples=300)
@given(now=instants, reset_day=st.integers(1, 31), tz=st.sampled_from(ZONES))
def test_ac_m2_7_cycles_contiguous_and_cover_every_instant(
    now: datetime, reset_day: int, tz: tzinfo
) -> None:
    cycle = cycle_bounds(now, reset_day, tz)
    assert utc(cycle.start) <= now < utc(cycle.end)
    following = cycle_bounds(cycle.end, reset_day, tz)
    assert utc(following.start) == utc(cycle.end)
    assert following.start_date == cycle.reset_date
    previous = cycle_bounds(cycle.start - timedelta(microseconds=1), reset_day, tz)
    if utc(cycle.start - timedelta(microseconds=1)) < utc(cycle.start):
        assert utc(previous.end) == utc(cycle.start)
        assert previous.reset_date == cycle.start_date
    # Exactly one cycle: any instant inside gives the same cycle.
    inside = utc(cycle.start) + (utc(cycle.end) - utc(cycle.start)) / 2
    assert cycle_bounds(inside, reset_day, tz) == cycle


@settings(max_examples=300)
@given(
    now=instants,
    reset_day=st.integers(1, 31),
    tz=st.sampled_from((LISBON, NEW_YORK, LORD_HOWE, SAO_PAULO)),
)
def test_cycle_boundaries_at_local_midnight_across_dst(
    now: datetime, reset_day: int, tz: tzinfo
) -> None:
    cycle = cycle_bounds(now, reset_day, tz)
    assert isinstance(cycle, Cycle)
    for moment, day in ((cycle.start, cycle.start_date), (cycle.end, cycle.reset_date)):
        assert moment.tzinfo is tz
        assert moment.replace(tzinfo=None) == datetime.combine(day, time())
        assert day.day == min(reset_day, last_day(day.year, day.month))
    months_apart = (cycle.reset_date.year - cycle.start_date.year) * 12 + (
        cycle.reset_date.month - cycle.start_date.month
    )
    assert months_apart == 1
    assert cycle.start_date <= now.astimezone(tz).date() < cycle.reset_date


# --- AC-M2.8: effective count ---------------------------------------------------------------


def test_ac_m2_8_provider_count_higher_wins() -> None:
    result = effective_count(
        12, [ProviderCount("requests", "month", 30), ProviderCount("requests", "month", 41)]
    )
    assert result == EffectiveCount(41, "provider")


def test_ac_m2_8_local_higher_wins() -> None:
    assert effective_count(50, [ProviderCount("requests", "month", 41)]) == EffectiveCount(
        50, "local"
    )
    assert effective_count(41, [ProviderCount("requests", "month", 41)]) == EffectiveCount(
        41, "local"
    )
    assert effective_count(7, []) == EffectiveCount(7, "local")


def test_snapshot_from_other_window_or_unit_ignored() -> None:
    others = [
        ProviderCount("requests", "five_hour", 900),
        ProviderCount("requests", "seven_day", 900),
        ProviderCount("fraction", "month", 900),
        ProviderCount("tokens", "month", 900),
    ]
    assert effective_count(3, others) == EffectiveCount(3, "local")
    assert effective_count(3, others, budget_window="five_hour") == EffectiveCount(900, "provider")


def test_budget_window_is_keyword_only() -> None:
    with pytest.raises(TypeError):
        effective_count(3, [], "month")  # type: ignore[misc]


def test_effective_count_rejects_negative_counts() -> None:
    with pytest.raises(ValueError):
        effective_count(-1, [])
    with pytest.raises(ValueError):
        ProviderCount("requests", "month", -1)


# --- AC-M2.7: warnings, notes and the block -------------------------------------------------


def test_ac_m2_7_each_warning_once_per_cycle_then_note() -> None:
    small = budget(allowance=10, first_warning=5, second_warning=8, block_at=10, reset_day=12)

    def run_cycle(shown: set[str]) -> list[Any]:
        decisions = []
        for count in range(11):
            decision = evaluate(small, count, shown=frozenset(shown), cycle=CYCLE)
            if isinstance(decision, Warn):
                shown.add(decision.threshold)
            decisions.append(decision)
        return decisions

    decisions = run_cycle(set())
    kinds = [type(decision).__name__ for decision in decisions]
    assert kinds == ["Allow"] * 4 + ["Warn", "Note", "Note", "Warn", "Note", "Note", "Blocked"]
    first, second = decisions[4], decisions[7]
    assert first == Warn("first", 5, 10, RESET)
    assert second == Warn("second", 8, 10, RESET)
    assert decisions[5] == Note(6, 10, RESET)
    # A new cycle has shown nothing yet, so both warnings come back.
    again = [type(d).__name__ for d in run_cycle(set())]
    assert again.count("Warn") == 2


def test_jumping_past_both_thresholds_shows_only_the_second() -> None:
    small = budget(allowance=10, first_warning=5, second_warning=8, block_at=10, reset_day=12)
    assert evaluate(small, 8, shown=frozenset(), cycle=CYCLE) == Warn("second", 9, 10, RESET)
    assert evaluate(small, 8, shown=frozenset({"second"}), cycle=CYCLE) == Note(9, 10, RESET)


def test_ac_m2_7_block_reports_reset_date() -> None:
    small = budget(allowance=10, first_warning=5, second_warning=8, block_at=9, reset_day=12)
    assert not isinstance(evaluate(small, 8, shown=frozenset({"second"}), cycle=CYCLE), Blocked)
    for count in (9, 10, 500):
        decision = evaluate(small, count, shown=frozenset({"first", "second"}), cycle=CYCLE)
        assert isinstance(decision, Blocked)
        assert decision.reset_date == RESET == CYCLE.reset_date


def test_evaluate_rejects_a_negative_count() -> None:
    with pytest.raises(ValueError):
        evaluate(budget(), -1, shown=frozenset(), cycle=CYCLE)


# --- AC-M2.10: no budget --------------------------------------------------------------------


@given(
    count=st.integers(0, 10**12),
    shown=st.frozensets(st.sampled_from(["first", "second"])),
    tier=st.sampled_from([None, "fast", "thorough"]),
    tier_count=st.none() | st.integers(0, 10**12),
)
def test_ac_m2_10_no_budget_never_warns_or_blocks(
    count: int, shown: frozenset[str], tier: Any, tier_count: int | None
) -> None:
    decision = evaluate(None, count, shown=shown, cycle=CYCLE, tier=tier, tier_count=tier_count)
    assert isinstance(decision, Allow)


# --- Tier caps (spec 3.7) -------------------------------------------------------------------


def test_thorough_cap_blocks_and_offers_fast() -> None:
    capped = budget(thorough_cap=3, fast_cap=5, reset_day=12)
    under = evaluate(capped, 10, shown=frozenset(), cycle=CYCLE, tier="thorough", tier_count=2)
    assert isinstance(under, Allow)
    decision = evaluate(capped, 10, shown=frozenset(), cycle=CYCLE, tier="thorough", tier_count=3)
    assert decision == CapBlocked(tier="thorough", offer_fast=True, reset_date=RESET)
    # A reached cap wins over a pending warning.
    warned = evaluate(capped, 60, shown=frozenset(), cycle=CYCLE, tier="thorough", tier_count=3)
    assert isinstance(warned, CapBlocked)
    # The overall block wins over the cap.
    blocked = evaluate(capped, 100, shown=frozenset(), cycle=CYCLE, tier="thorough", tier_count=3)
    assert isinstance(blocked, Blocked)
    # A Fast cap blocks without offering anything.
    fast = evaluate(capped, 10, shown=frozenset(), cycle=CYCLE, tier="fast", tier_count=5)
    assert fast == CapBlocked(tier="fast", offer_fast=False, reset_date=RESET)
    # Calls with no tier (web search, the connection count) ignore the caps.
    assert isinstance(
        evaluate(capped, 10, shown=frozenset(), cycle=CYCLE, tier=None, tier_count=99), Allow
    )


@given(
    cap=st.integers(1, 50),
    over=st.integers(0, 50),
    count=st.integers(0, 49),
    shown=st.frozensets(st.sampled_from(["first", "second"])),
)
def test_cap_never_changes_tier_by_itself(
    cap: int, over: int, count: int, shown: frozenset[str]
) -> None:
    capped = budget(thorough_cap=cap, reset_day=12)
    decision = evaluate(
        capped, count, shown=shown, cycle=CYCLE, tier="thorough", tier_count=cap + over
    )
    # Never a go-ahead: running on Fast needs the user's choice and a new evaluation.
    assert isinstance(decision, CapBlocked)
    assert decision.tier == "thorough"
    run_on_fast = evaluate(
        capped, count, shown=shown, cycle=CYCLE, tier="fast", tier_count=cap + over
    )
    assert not isinstance(run_on_fast, CapBlocked | Blocked)


def test_cap_given_without_tier_count_is_an_error() -> None:
    with pytest.raises(ValueError):
        evaluate(budget(thorough_cap=3), 1, shown=frozenset(), cycle=CYCLE, tier="thorough")


# --- budget_level (M2.23: the usage panel's status) ---------------------------------------------


@pytest.mark.parametrize(
    ("count", "level"),
    [
        (0, "within"),
        (2, "within"),
        (3, "first"),
        (4, "first"),
        (5, "second"),
        (6, "second"),
        (7, "limit"),
        (8, "limit"),
        (900, "limit"),
    ],
)
def test_budget_level_is_the_highest_point_the_count_is_at(count: int, level: str) -> None:
    """A point is reached once the count is at it (count >= point); the limit is the
    blocking point, where ``evaluate`` blocks."""
    limits = Budget(allowance=8, first_warning=3, second_warning=5, block_at=7, reset_day=15)
    assert budget_level(limits, count) == level


@pytest.mark.parametrize("count", [-1, True, 1.0, "1", None])
def test_budget_level_rejects_a_count_that_is_not_a_non_negative_integer(count: Any) -> None:
    with pytest.raises(ValueError):
        budget_level(budget(), count)


# --- tier caps (M2.23 review: the usage panel shows what ``evaluate`` refuses) ---------------


@pytest.mark.parametrize(("tier_count", "reached"), [(0, False), (2, False), (3, True), (9, True)])
def test_cap_reached_is_the_rule_evaluate_blocks_a_tier_with(
    tier_count: int, reached: bool
) -> None:
    """One rule: a cap counts as reached exactly when the next request on the tier is
    refused for it."""
    capped = budget(thorough_cap=3)
    decision = evaluate(
        capped, 0, shown=frozenset(), cycle=CYCLE, tier="thorough", tier_count=tier_count
    )

    assert cap_reached(3, tier_count) is reached
    assert isinstance(decision, CapBlocked) is reached
    assert TierUsage("thorough", 3, tier_count).reached is reached
