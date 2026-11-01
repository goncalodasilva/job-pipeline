"""Request budgets and the usage records they count (spec 3.7; ADR-076 items 3-8, ADR-078).

Every count comes from the records and snapshots inside one ``BEGIN IMMEDIATE``
transaction, so the block check and the insert in ``reserve`` can't race another reserve,
on this connection or another one. Cycles are local (``tz``) and compared as UTC instants.
"""

import logging
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, tzinfo

from pydantic import ValidationError

from app.domain.budgets import (
    Blocked,
    Budget,
    BudgetLevel,
    BudgetTier,
    CapBlocked,
    Cycle,
    Decision,
    EffectiveCount,
    ProviderCount,
    TierUsage,
    Warn,
    budget_level,
    cycle_bounds,
    effective_count,
    evaluate,
)
from app.domain.clock import Clock
from app.domain.errors import (
    BudgetBlocked,
    BudgetNoticeRefused,
    TierCapReached,
    UsageLimitsUnreadable,
    VersionConflict,
)
from app.domain.ids import IdFactory
from app.domain.settings import USAGE_BUDGETS, UsageBudgets
from app.domain.usage import (
    CONNECTION_TEST_TASK_TYPE,
    SERVICE_NAMES,
    ServiceId,
    is_service,
    is_stored_count,
    is_task_type,
)
from app.modules.ai import ProviderUsageSnapshot, TokenUsage
from app.modules.storage import (
    Database,
    get_setting,
    insert_setting,
    update_setting,
    usage_repo,
)
from app.services.settings import Versioned
from app.services.usage_values import (
    check_service,
    check_tier,
    snapshot_from_row,
    token_counts,
    utc_text,
)

_log = logging.getLogger(__name__)
_THRESHOLDS: frozenset[str] = frozenset({"first", "second"})
_BUDGET_WINDOW = "month"
# The cycle a service without a budget is shown in: the calendar month.
_DEFAULT_RESET_DAY = 1
_TIERS: tuple[BudgetTier, ...] = ("fast", "thorough")

BreakdownRow = usage_repo.BreakdownRow


@dataclass(frozen=True)
class UsageStatus:
    budget: Budget | None
    cycle: Cycle
    local_count: int
    provider_count: int | None  # the highest comparable provider count this cycle
    effective: EffectiveCount
    unfinished_count: int
    provider_snapshots: tuple[ProviderUsageSnapshot, ...]
    shown_thresholds: frozenset[str]  # warnings already shown this cycle
    # The stored budgets can't be read: requests are refused until they are saved again.
    budgets_unreadable: bool
    # The snapshots that aren't compared with the local count and whose window hasn't
    # reset: information only (ADR-079).
    information_only: tuple[ProviderUsageSnapshot, ...]
    # Each cap that is set, with its tier's count this cycle (in the order Fast, Thorough).
    tier_caps: tuple[TierUsage, ...]

    @property
    def level(self) -> BudgetLevel | None:
        """How far the count that is used has come in the budget; None without a budget."""
        return None if self.budget is None else budget_level(self.budget, self.effective.value)


@dataclass(frozen=True)
class UsageReport:
    """One service's status and its breakdown for the same cycle, from one read."""

    status: UsageStatus
    breakdown: tuple[BreakdownRow, ...]


@dataclass(frozen=True)
class StoredBudgets:
    """The stored budgets and their one version (ADR-079). ``unreadable``: the stored value
    can't be read, so ``budgets`` is empty and requests are refused until one is saved."""

    budgets: Mapping[ServiceId, Budget]
    version: int
    unreadable: bool = False


@dataclass(frozen=True)
class Confirmation:
    """The warning a start says the user confirmed (M2.24): checked, never trusted."""

    service: str  # as it was sent: it counts only when it is the service being checked
    threshold: str
    cycle_start: date


@dataclass(frozen=True)
class StartCheck:
    """What starting an action on a tier meets, from one transaction (M2.24)."""

    decision: Decision
    cycle_start: date  # the current cycle's local start date: what a warning is confirmed for
    # The confirmation that came with the start named the warning that was due: it is stored
    # now, and ``decision`` is what the start meets after it.
    acknowledged: bool = False
    # For a tier cap that offers Fast: what the same start meets on Fast.
    fast: Decision | None = None
    # The budget's blocking point, for the warning's wording; None without a budget.
    block_at: int | None = None


def _reached(budget: Budget, count: int) -> frozenset[str]:
    """The thresholds the next request reaches (``evaluate`` checks ``count + 1`` too)."""
    after = count + 1
    levels = (("first", budget.first_warning), ("second", budget.second_warning))
    return frozenset(name for name, level in levels if after >= level)


@dataclass(frozen=True)
class _Counts:
    budget: Budget | None
    unreadable: bool
    cycle: Cycle
    local: int
    provider: int | None
    effective: EffectiveCount
    snapshots: tuple[ProviderUsageSnapshot, ...]
    shown: frozenset[str]
    information_only: tuple[ProviderUsageSnapshot, ...]


class UsageService:
    def __init__(self, db: Database, clock: Clock, ids: IdFactory, tz: tzinfo) -> None:
        self._db = db
        self._clock = clock
        self._ids = ids
        self._tz = tz

    # --- budgets -------------------------------------------------------------------------

    def load_budgets(self) -> Versioned[Mapping[ServiceId, Budget]]:
        with self._db.transaction() as conn:
            loaded = self._budgets(conn)
        return Versioned(loaded.budgets, loaded.version)

    def read_budgets(self) -> StoredBudgets:
        """Like ``load_budgets``, and whether the stored budgets can't be read."""
        with self._db.transaction() as conn:
            return self._budgets(conn)

    def save_budget(self, service: str, budget: Budget | None, *, expected_version: int) -> None:
        """Set (or with ``None`` remove) one service's budget; ``VersionConflict`` when the
        budgets changed since ``expected_version`` was loaded.

        Unreadable stored budgets are replaced by this one alone: the user re-enters the
        others (``UsageLimitsUnreadable`` tells them to).
        """
        checked = check_service(service)
        now = self._clock.now().isoformat()
        with self._db.transaction() as conn:
            current = self._budgets(conn)
            if current.version != expected_version:
                raise VersionConflict(detail="usage budgets changed elsewhere")
            budgets = dict(current.budgets)
            if budget is None:
                budgets.pop(checked, None)
            else:
                budgets[checked] = budget
            text = UsageBudgets(budgets=budgets).model_dump_json()
            if expected_version == 0:
                insert_setting(conn, USAGE_BUDGETS.key, text, now)
            else:
                update_setting(
                    conn, USAGE_BUDGETS.key, text, expected_version=expected_version, now=now
                )

    def _budgets(self, conn: sqlite3.Connection) -> StoredBudgets:
        stored = get_setting(conn, USAGE_BUDGETS.key)
        if stored is None:
            return StoredBudgets(USAGE_BUDGETS.default.budgets, 0)
        try:
            loaded = UsageBudgets.model_validate_json(stored.value)
        except ValidationError as error:
            # Only the type: pydantic's message repeats the stored value. Fails closed.
            _log.warning("usage budgets are not valid (%s)", type(error).__name__)
            return StoredBudgets(USAGE_BUDGETS.default.budgets, stored.version, unreadable=True)
        return StoredBudgets(loaded.budgets, stored.version)

    # --- records -------------------------------------------------------------------------

    def reserve(
        self,
        service: str,
        *,
        tier: str,
        task_id: str | None = None,
        task_type: str | None = None,
        skip_budget: bool = False,
    ) -> str:
        """Count one request before it is made. Nothing is written when it raises
        ``BudgetBlocked`` (budget used up), ``TierCapReached`` (only the tier's cap) or
        ``UsageLimitsUnreadable``. ``skip_budget`` is only for the connection test (ADR-076
        item 3): counted, never warned or blocked."""
        service = check_service(service)
        checked_tier = check_tier(tier)
        if skip_budget:
            if task_type not in (None, CONNECTION_TEST_TASK_TYPE):
                raise ValueError("only the connection test skips the budget")
            task_type = CONNECTION_TEST_TASK_TYPE
        if not is_task_type(task_type):
            raise ValueError("a task type must be an id")
        record_id = self._ids.new()
        with self._db.transaction() as conn:
            now = self._clock.now()
            if not skip_budget:
                decision = self._decide(conn, service, now, checked_tier)
                if isinstance(decision, Blocked):
                    raise BudgetBlocked(SERVICE_NAMES[service], decision.reset_date)
                if isinstance(decision, CapBlocked):
                    raise TierCapReached(SERVICE_NAMES[service], decision.tier, decision.reset_date)
            usage_repo.insert_record(
                conn,
                record_id=record_id,
                service=service,
                tier=checked_tier,
                task_type=task_type,
                task_id=task_id,
                now=utc_text(now),
            )
        return record_id

    def record_reported_searches(
        self, count: int, *, tier: str, task_type: str, task_id: str | None, success: bool
    ) -> None:
        """M8: search usage reported after it happened is counted even above a budget."""
        checked_tier = check_tier(tier)
        if type(count) is not int or count < 0 or not is_task_type(task_type):
            raise ValueError("Invalid reported search usage.")
        with self._db.transaction() as conn:
            now = utc_text(self._clock.now())
            for _ in range(count):
                record_id = self._ids.new()
                usage_repo.insert_record(
                    conn,
                    record_id=record_id,
                    service="web_search",
                    tier=checked_tier,
                    task_type=task_type,
                    task_id=task_id,
                    now=now,
                )
                usage_repo.complete_record(
                    conn, record_id, success=success, input_tokens=None, output_tokens=None, now=now
                )

    def complete(self, record_id: str, *, success: bool, tokens: TokenUsage | None) -> None:
        """Record a request's outcome; only the first completion counts."""
        input_tokens, output_tokens = token_counts(tokens)
        with self._db.transaction() as conn:
            usage_repo.complete_record(
                conn,
                record_id,
                success=success,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                now=utc_text(self._clock.now()),
            )

    # --- reading -------------------------------------------------------------------------

    def status(self, service: str) -> UsageStatus:
        service = check_service(service)
        with self._db.transaction() as conn:
            return self._status(conn, service, self._clock.now())

    def reports(self, services: Iterable[str]) -> tuple[UsageReport, ...]:
        """Each service's status and breakdown at one moment, from one read snapshot (the
        usage panel). It takes no write lock and writes nothing."""
        checked = tuple(check_service(service) for service in services)
        now = self._clock.now()
        with self._db.read() as conn:
            return tuple(self._report(conn, service, now) for service in checked)

    def _report(self, conn: sqlite3.Connection, service: ServiceId, now: datetime) -> UsageReport:
        status = self._status(conn, service, now)
        return UsageReport(status, self._breakdown(conn, service, status.cycle))

    def _status(self, conn: sqlite3.Connection, service: ServiceId, now: datetime) -> UsageStatus:
        counts = self._counts(conn, service, now)
        unfinished = usage_repo.count_unfinished(
            conn, service, start=utc_text(counts.cycle.start), end=utc_text(counts.cycle.end)
        )
        return UsageStatus(
            budget=counts.budget,
            cycle=counts.cycle,
            local_count=counts.local,
            provider_count=counts.provider,
            effective=counts.effective,
            unfinished_count=unfinished,
            provider_snapshots=counts.snapshots,
            shown_thresholds=counts.shown,
            budgets_unreadable=counts.unreadable,
            information_only=counts.information_only,
            tier_caps=self._tier_caps(conn, service, counts),
        )

    def _tier_caps(
        self, conn: sqlite3.Connection, service: ServiceId, counts: _Counts
    ) -> tuple[TierUsage, ...]:
        """Each set cap with its tier's count: what ``_decide`` hands to ``evaluate``."""
        if counts.budget is None:
            return ()
        return tuple(
            TierUsage(tier, cap, self._tier_count(conn, service, counts.cycle, tier))
            for tier in _TIERS
            if (cap := counts.budget.cap_for(tier)) is not None
        )

    def _tier_count(
        self, conn: sqlite3.Connection, service: ServiceId, cycle: Cycle, tier: BudgetTier
    ) -> int:
        return usage_repo.count_requests(
            conn, service, start=utc_text(cycle.start), end=utc_text(cycle.end), tier=tier
        )

    def preflight(self, service: str, *, tier: str) -> Decision:
        """What the next request on ``tier`` would meet; writes nothing. Raises
        ``UsageLimitsUnreadable`` when the stored budgets can't be read."""
        service = check_service(service)
        checked_tier = check_tier(tier)
        with self._db.transaction() as conn:
            return self._decide(conn, service, self._clock.now(), checked_tier)

    def check_start(
        self, service: str, *, tier: str, confirming: Confirmation | None = None
    ) -> StartCheck:
        """``preflight`` for an action the user starts, with what its answer needs.

        ``confirming`` is the warning the user confirmed with this start. It is stored only
        when its service, threshold and cycle start are exactly those of the warning due
        right now for ``service``, in the same transaction as the check; any other value is
        ignored, so a confirmation never gets past a warning it doesn't name, and one made
        for another service counts for none. Raises ``UsageLimitsUnreadable``.
        """
        service = check_service(service)
        checked_tier = check_tier(tier)
        with self._db.transaction() as conn:
            now = self._clock.now()
            counts = self._counts(conn, service, now)
            cycle_start = counts.cycle.start_date
            decision = self._decide_on(conn, service, counts, checked_tier)
            block_at = None if counts.budget is None else counts.budget.block_at
            if isinstance(decision, Warn) and confirming == Confirmation(
                service, decision.threshold, cycle_start
            ):
                usage_repo.insert_notice(
                    conn,
                    notice_id=self._ids.new(),
                    service=service,
                    cycle_start=cycle_start,
                    threshold=decision.threshold,
                    now=utc_text(now),
                )
                after = self._decide(conn, service, now, checked_tier)
                return StartCheck(after, cycle_start, acknowledged=True, block_at=block_at)
            fast = None
            if isinstance(decision, CapBlocked) and decision.offer_fast:
                fast = self._decide_on(conn, service, counts, "fast")
            return StartCheck(decision, cycle_start, fast=fast, block_at=block_at)

    def breakdown(
        self, service: str, *, cycle: Cycle | None = None
    ) -> tuple[usage_repo.BreakdownRow, ...]:
        """Requests and tokens by tier and task type, in ``cycle`` (default: the current one)."""
        checked = check_service(service)
        with self._db.transaction() as conn:
            if cycle is None:
                cycle = self._cycle(self._budgets(conn).budgets.get(checked))
            return self._breakdown(conn, checked, cycle)

    def _breakdown(
        self, conn: sqlite3.Connection, service: ServiceId, cycle: Cycle
    ) -> tuple[usage_repo.BreakdownRow, ...]:
        rows = usage_repo.breakdown(
            conn, service, start=utc_text(cycle.start), end=utc_text(cycle.end)
        )
        return tuple(rows)

    # --- warnings ------------------------------------------------------------------------

    def acknowledge(self, service: str, threshold: str, cycle_start: date) -> None:
        """Remember that the warning for ``threshold`` was shown in the current cycle.

        Any threshold already reached this cycle is accepted (again: idempotent), so a
        dialog confirmed after a later request passed the next threshold still counts.
        Anything else raises ``BudgetNoticeRefused``.
        """
        if not is_service(service) or threshold not in _THRESHOLDS:
            raise BudgetNoticeRefused(detail="unknown service or threshold")
        with self._db.transaction() as conn:
            now = self._clock.now()
            counts = self._counts(conn, service, now)
            if counts.budget is None or cycle_start != counts.cycle.start_date:
                raise BudgetNoticeRefused(detail="no budget, or another cycle")
            if threshold in counts.shown:
                return
            if threshold not in _reached(counts.budget, counts.effective.value):
                raise BudgetNoticeRefused(detail="threshold not reached")
            usage_repo.insert_notice(
                conn,
                notice_id=self._ids.new(),
                service=service,
                cycle_start=counts.cycle.start_date,
                threshold=threshold,
                now=utc_text(now),
            )

    # --- provider-reported usage ---------------------------------------------------------

    def save_snapshot(self, snapshot: ProviderUsageSnapshot) -> bool:
        """Keep the latest snapshot per (service, window). Refused (False) for an unknown
        service, a count SQLite can't hold, or an observation older than the stored one."""
        if not is_service(snapshot.service):
            return False
        for count in (snapshot.used, snapshot.limit):
            if count is not None and not is_stored_count(count):
                return False
        with self._db.transaction() as conn:
            return usage_repo.upsert_snapshot(
                conn,
                snapshot_id=self._ids.new(),
                service=snapshot.service,
                window=snapshot.window,
                unit=snapshot.unit,
                used=snapshot.used,
                limit_count=snapshot.limit,
                utilization=snapshot.utilization,
                resets_at=None if snapshot.resets_at is None else utc_text(snapshot.resets_at),
                observed_at=utc_text(snapshot.observed_at),
                now=utc_text(self._clock.now()),
            )

    # --- helpers (inside the caller's transaction) ---------------------------------------

    def _cycle(self, budget: Budget | None, now: datetime | None = None) -> Cycle:
        reset_day = _DEFAULT_RESET_DAY if budget is None else budget.reset_day
        return cycle_bounds(self._clock.now() if now is None else now, reset_day, self._tz)

    def _counts(self, conn: sqlite3.Connection, service: ServiceId, now: datetime) -> _Counts:
        stored = self._budgets(conn)
        budget = stored.budgets.get(service)
        cycle = self._cycle(budget, now)
        local = usage_repo.count_requests(
            conn, service, start=utc_text(cycle.start), end=utc_text(cycle.end)
        )
        snapshots = tuple(
            snapshot
            for row in usage_repo.list_snapshots(conn, service)
            if (snapshot := snapshot_from_row(row)) is not None
        )
        compared = tuple(s for s in snapshots if self._comparable(s, cycle))
        comparable = [
            ProviderCount(unit=s.unit, window=s.window, used=s.used)
            for s in compared
            if s.used is not None
        ]
        provider = max((c.used for c in comparable), default=None)
        effective = effective_count(local, comparable, budget_window=_BUDGET_WINDOW)
        shown = usage_repo.shown_thresholds(conn, service, cycle.start_date)
        # A window that has reset says nothing about now: it is stored, no longer shown.
        others = tuple(
            s for s in snapshots if s not in compared and (s.resets_at is None or s.resets_at > now)
        )
        return _Counts(
            budget, stored.unreadable, cycle, local, provider, effective, snapshots, shown, others
        )

    @staticmethod
    def _comparable(snapshot: ProviderUsageSnapshot, cycle: Cycle) -> bool:
        """A request count for the budget's window, observed in this cycle (ADR-079)."""
        return (
            snapshot.unit == "requests"
            and snapshot.window == _BUDGET_WINDOW
            and snapshot.used is not None
            and cycle.start <= snapshot.observed_at < cycle.end
            # Instants, not wall time.
            and (snapshot.resets_at is None or snapshot.resets_at == cycle.end)
        )

    def _decide(
        self, conn: sqlite3.Connection, service: ServiceId, now: datetime, tier: BudgetTier
    ) -> Decision:
        return self._decide_on(conn, service, self._counts(conn, service, now), tier)

    def _decide_on(
        self, conn: sqlite3.Connection, service: ServiceId, counts: _Counts, tier: BudgetTier
    ) -> Decision:
        if counts.unreadable:
            raise UsageLimitsUnreadable(detail="stored usage budgets are not valid")
        if counts.budget is None:
            return evaluate(None, counts.effective.value, shown=frozenset(), cycle=counts.cycle)
        tier_count = None
        if counts.budget.cap_for(tier) is not None:
            tier_count = self._tier_count(conn, service, counts.cycle, tier)
        return evaluate(
            counts.budget,
            counts.effective.value,
            shown=counts.shown,
            cycle=counts.cycle,
            tier=tier,
            tier_count=tier_count,
        )
