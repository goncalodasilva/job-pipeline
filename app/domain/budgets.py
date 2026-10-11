"""Usage budget math (spec 3.7; ADR-076 items 5-8, ADR-078).

Pure: no I/O and no clock. Callers pass the current time and the local tzinfo (wiring
supplies it, ADR-076 item 8). A budget counts requests only in M2 (ADR-076 item 5).
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo
from typing import Literal, TypeGuard

Unit = Literal["requests"]
Threshold = Literal["first", "second"]
CountSource = Literal["local", "provider"]
BudgetTier = Literal["fast", "thorough"]

_UNITS: frozenset[str] = frozenset({"requests"})


@dataclass(frozen=True)
class BudgetFieldError:
    """One invalid budget field and a stable code for its inline message."""

    field: str
    code: str


class InvalidBudget(ValueError):
    """A budget built with invalid values; ``errors`` lists each one."""

    def __init__(self, errors: tuple[BudgetFieldError, ...]) -> None:
        super().__init__("invalid budget: " + ", ".join(f"{e.field} {e.code}" for e in errors))
        self.errors = errors


def _is_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _count_error(value: object) -> str | None:
    """The code for a value that must be a positive integer, or None when it is one."""
    if not _is_int(value):
        return "must_be_integer"
    return "must_be_positive" if value <= 0 else None


def _usable(value: object) -> int | None:
    """The value when it is a positive integer, so other rules may compare against it."""
    return value if _is_int(value) and value > 0 else None


def validate_budget(
    allowance: object,
    first_warning: object,
    second_warning: object,
    block_at: object,
    reset_day: object,
    unit: object = "requests",
    fast_cap: object = None,
    thorough_cap: object = None,
) -> tuple[BudgetFieldError, ...]:
    """Every rule broken, in field order; empty when the budget is valid.

    A field is reported once, with its first broken rule. A rule comparing against a field
    that is not a positive integer is skipped, since that field reports its own error.
    """
    limit = _usable(allowance)
    first = _usable(first_warning)
    second = _usable(second_warning)
    errors: list[BudgetFieldError] = []

    def add(field: str, code: str | None) -> None:
        if code is not None:
            errors.append(BudgetFieldError(field, code))

    add("allowance", _count_error(allowance))
    add("first_warning", _count_error(first_warning))
    add("second_warning", _ordered(second_warning, first, "must_exceed_first_warning"))
    block_code = _ordered(block_at, second, "must_exceed_second_warning")
    add("block_at", block_code or _within(block_at, limit))
    if not _is_int(reset_day):
        add("reset_day", "must_be_integer")
    elif not 1 <= reset_day <= 31:
        add("reset_day", "out_of_range")
    if not isinstance(unit, str) or unit not in _UNITS:
        add("unit", "unsupported")
    for field, cap in (("fast_cap", fast_cap), ("thorough_cap", thorough_cap)):
        if cap is not None:
            add(field, _count_error(cap) or _within(cap, limit))
    return tuple(errors)


def _ordered(value: object, lower: int | None, code: str) -> str | None:
    """A positive integer above ``lower`` (when ``lower`` is usable)."""
    if not _is_int(value) or value <= 0:
        return _count_error(value)
    return code if lower is not None and value <= lower else None


def _within(value: object, limit: int | None) -> str | None:
    """At most the allowance (when the allowance is usable); ``value`` is already valid."""
    over = limit is not None and _is_int(value) and value > limit
    return "must_not_exceed_allowance" if over else None


@dataclass(frozen=True)
class Budget:
    """A request budget for one service, with optional per-tier caps.

    A cap above ``block_at`` never triggers: the overall block is checked first and wins.
    """

    allowance: int
    first_warning: int
    second_warning: int
    block_at: int
    reset_day: int
    unit: Unit = "requests"
    fast_cap: int | None = None
    thorough_cap: int | None = None

    def __post_init__(self) -> None:
        errors = validate_budget(
            self.allowance,
            self.first_warning,
            self.second_warning,
            self.block_at,
            self.reset_day,
            self.unit,
            self.fast_cap,
            self.thorough_cap,
        )
        if errors:
            raise InvalidBudget(errors)

    def cap_for(self, tier: BudgetTier) -> int | None:
        return self.fast_cap if tier == "fast" else self.thorough_cap


# --- Cycles -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Cycle:
    """The budget cycle [start, end), both local midnight in the given tzinfo.

    ``start_date`` is the local start date (``budget_notices.cycle_start``); ``reset_date``
    is the local date the next cycle starts.

    ``start`` and ``end`` carry the tzinfo and may fall on a skipped or repeated midnight
    (fold 0). Compare them as instants in UTC, or compare dates, never by wall time.
    """

    start: datetime
    end: datetime
    start_date: date
    reset_date: date


def _month_last_day(year: int, month: int) -> int:
    following = date(year + month // 12, month % 12 + 1, 1)
    return (following - timedelta(days=1)).day


def _reset_in(year: int, month: int, reset_day: int) -> date:
    """The month's reset date, moved to its last day for reset days past the month's end."""
    return date(year, month, min(reset_day, _month_last_day(year, month)))


def _shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    index = year * 12 + (month - 1) + delta
    return index // 12, index % 12 + 1


def cycle_bounds(now: datetime, reset_day: int, tz: tzinfo) -> Cycle:
    """The cycle holding ``now`` (timezone-aware), with reset day 1-31 in ``tz``."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if not _is_int(reset_day) or not 1 <= reset_day <= 31:
        raise ValueError("reset_day must be an integer from 1 to 31")
    today = now.astimezone(tz).date()
    start = _reset_in(today.year, today.month, reset_day)
    if today < start:
        start = _reset_in(*_shift_month(today.year, today.month, -1), reset_day)
    reset = _reset_in(*_shift_month(start.year, start.month, 1), reset_day)
    return Cycle(
        start=datetime.combine(start, time(), tzinfo=tz),
        end=datetime.combine(reset, time(), tzinfo=tz),
        start_date=start,
        reset_date=reset,
    )


# --- Effective count --------------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderCount:
    """A provider-reported count, mapped from ``ProviderUsageSnapshot`` by the usage service."""

    unit: str
    window: str
    used: int

    def __post_init__(self) -> None:
        if not _is_int(self.used) or self.used < 0:
            raise ValueError("used must be a non-negative integer")


@dataclass(frozen=True)
class EffectiveCount:
    value: int
    source: CountSource


def effective_count(
    local: int, provider: Iterable[ProviderCount], *, budget_window: str = "month"
) -> EffectiveCount:
    """The higher of the local count and comparable provider counts; a tie keeps local.

    Comparable means unit "requests" and the budget's window (ADR-076 item 6).
    """
    if not _is_int(local) or local < 0:
        raise ValueError("local must be a non-negative integer")
    best = EffectiveCount(local, "local")
    for count in provider:
        if count.unit == "requests" and count.window == budget_window and count.used > best.value:
            best = EffectiveCount(count.used, "provider")
    return best


# --- Decisions --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Allow:
    pass


@dataclass(frozen=True)
class Warn:
    """A threshold dialog, shown once per threshold per cycle."""

    threshold: Threshold
    count: int
    allowance: int
    reset_date: date


@dataclass(frozen=True)
class Note:
    """The inline note once a reached threshold's dialog has been shown."""

    count: int
    allowance: int
    reset_date: date


@dataclass(frozen=True)
class Blocked:
    reset_date: date


@dataclass(frozen=True)
class CapBlocked:
    """A tier cap is reached.

    ``offer_fast=True`` (Thorough only) means "offer Fast", not that Fast is available: the
    caller must evaluate again with ``tier="fast"`` before offering it, and never switches
    tier by itself.
    """

    tier: BudgetTier
    offer_fast: bool
    reset_date: date


Decision = Allow | Warn | Note | Blocked | CapBlocked


def cap_reached(cap: int, tier_count: int) -> bool:
    """Whether a tier with ``tier_count`` requests this cycle is at its cap: the next request
    on it is refused. The one rule for it: ``evaluate`` blocks by it, the usage panel shows it."""
    return tier_count >= cap


@dataclass(frozen=True)
class TierUsage:
    """A tier's cap and the requests counted on that tier this cycle."""

    tier: BudgetTier
    cap: int
    count: int

    @property
    def reached(self) -> bool:
        return cap_reached(self.cap, self.count)


# How far the count used so far has come: shown next to the count (the usage panel).
BudgetLevel = Literal["within", "first", "second", "limit"]


def budget_level(budget: Budget, count: int) -> BudgetLevel:
    """The highest point ``count`` requests used this cycle have reached (count >= point).

    "limit" is where ``evaluate`` blocks. A warning counts as reached once the count is at
    it, one request after ``evaluate`` first warns about it (that check is on ``count + 1``).
    """
    if not _is_int(count) or count < 0:
        raise ValueError("count must be a non-negative integer")
    if count >= budget.block_at:
        return "limit"
    if count >= budget.second_warning:
        return "second"
    return "first" if count >= budget.first_warning else "within"


def evaluate(
    budget: Budget | None,
    count: int,
    *,
    shown: frozenset[str],
    cycle: Cycle,
    tier: BudgetTier | None = None,
    tier_count: int | None = None,
) -> Decision:
    """Decide on the next request, given ``count`` requests already used this cycle.

    The check is made on ``count + 1``. Order: blocked, tier cap, warning not yet shown,
    note, allow. No budget always allows. A ``CapBlocked`` offering Fast needs a second
    evaluation on Fast before the offer is shown.
    """
    if not _is_int(count) or count < 0:
        raise ValueError("count must be a non-negative integer")
    if budget is None:
        return Allow()
    reset = cycle.reset_date
    if count >= budget.block_at:
        return Blocked(reset)
    cap = budget.cap_for(tier) if tier is not None else None
    if tier is not None and cap is not None:
        if tier_count is None or not _is_int(tier_count) or tier_count < 0:
            raise ValueError("a capped tier needs a non-negative tier_count")
        if cap_reached(cap, tier_count):
            return CapBlocked(tier=tier, offer_fast=tier == "thorough", reset_date=reset)
    after = count + 1
    reached: Threshold | None = None
    if after >= budget.second_warning:
        reached = "second"
    elif after >= budget.first_warning:
        reached = "first"
    if reached is None:
        return Allow()
    if reached not in shown:
        return Warn(reached, after, budget.allowance, reset)
    return Note(after, budget.allowance, reset)
