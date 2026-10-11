"""Settings > the usage panel: what each service's block shows (M2.23; AC-M2.6, AC-M2.8;
spec 3.7; ADR-076 items 4, 5, 6; ADR-079; ADR-084).

Per offered service: the count that is used against the allowance, the status in words, the
blocking point and tier caps and what follows once they are reached, the reset date, calls
that are unfinished, the local and provider counts when both exist, what the provider reports
for information only, and the requests and tokens by task type and tier.
Every rule (which count is used, what was reached, which snapshot is compared or has reset)
is the usage service's; this module only turns its answers into the copy shown.
One read snapshot for all services; reads write nothing. Everything here blocks (database).
"""

from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import tzinfo
from types import MappingProxyType

from app.domain.budgets import Budget, BudgetLevel, BudgetTier, TierUsage
from app.domain.errors import format_day, format_day_time
from app.domain.usage import CONNECTION_TEST_TASK_TYPE, SERVICE_NAMES, ServiceId
from app.modules.ai import ProviderUsageSnapshot
from app.services.usage import BreakdownRow, UsageReport, UsageService, UsageStatus

TOTAL_LABEL = "Total"
CONNECTION_TEST_LABEL = "Connection test"
NO_LIMIT = "No limit"
PAUSED = "Paused: the saved budgets couldn't be read. Save a budget again below."
TABLE_NOTE = "The table shows only requests made from this computer."
PROVIDER_USED = "The provider's count is used because it is higher."
LOCAL_USED = "The count on this computer is used."

_STATUS: Mapping[BudgetLevel, str] = MappingProxyType(
    {
        "within": "Within budget",
        "first": "First warning reached",
        "second": "Second warning reached",
        "limit": "Blocking point reached",  # the budget forms' word for it
    }
)
_TIER_NAMES: Mapping[BudgetTier, str] = MappingProxyType({"fast": "Fast", "thorough": "Thorough"})
# The windows providers report on, in words; any other is shown by its id.
_WINDOWS: Mapping[str, str] = MappingProxyType(
    {
        "five_hour": "5-hour window",
        "seven_day": "7-day window",
        "seven_day_opus": "7-day window (Opus)",
        "seven_day_sonnet": "7-day window (Sonnet)",
        "day": "Day",
        "week": "Week",
        "month": "Month",
    }
)


@dataclass(frozen=True)
class BreakdownLine:
    """One row of the table: a task type (or the total) and its counts."""

    label: str
    fast: int
    thorough: int
    total: int
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class CapLine:
    """A set tier cap: the tier's count against it and, once reached, what follows."""

    tier: BudgetTier
    text: str


@dataclass(frozen=True)
class ServiceUsage:
    service: str
    name: str
    count: int  # the count that is used: the higher of the local and the provider's
    count_text: str  # "7 / 20 requests" with a budget, "7 requests" without
    allowance: int | None  # None: no budget (or the budgets can't be read)
    status: str | None  # budget only: what the count has reached, in words
    block_note: str | None  # only when the blocking point isn't the allowance
    stopped: str | None  # only once the blocking point is reached: what follows
    caps: tuple[CapLine, ...]  # one per tier cap that is set
    reset: str | None  # budget only: the reset date as the interface writes dates
    limit_note: str | None  # no budget: "No limit", or paused while budgets can't be read
    unfinished: str | None  # only when calls are still running or didn't finish
    local_count: int
    provider_count: int | None  # None: no provider count that can be compared
    used: str | None  # with a provider count: which of the two is used
    information: tuple[str, ...]  # what the provider reports that is never compared
    table_note: str | None  # only when the provider's count is the one used
    since: str  # the day the current period started
    rows: tuple[BreakdownLine, ...]  # one per task type; empty: nothing this period
    total: BreakdownLine


@dataclass(frozen=True)
class UsagePanelView:
    services: tuple[ServiceUsage, ...]


def _requests(count: int) -> str:
    return "1 request" if count == 1 else f"{count} requests"


def _count_text(count: int, allowance: int | None) -> str:
    return _requests(count) if allowance is None else f"{count} / {allowance} requests"


def _unfinished(count: int) -> str | None:
    if count == 0:
        return None
    if count == 1:
        return "1 call is still running or didn't finish. It is counted."
    return f"{count} calls are still running or didn't finish. They are counted."


def _block_note(budget: Budget | None) -> str | None:
    if budget is None or budget.block_at == budget.allowance:
        return None
    return f"The app stops at {_requests(budget.block_at)} (your blocking point)."


def _cap_line(cap: TierUsage, reset: str) -> CapLine:
    name = _TIER_NAMES[cap.tier]
    text = f"{name} tasks: {cap.count} / {cap.cap}."
    if cap.reached:
        text += f" Cap reached; {name} tasks won't run until {reset}."
    return CapLine(cap.tier, text)


def _information(snapshot: ProviderUsageSnapshot, tz: tzinfo) -> str:
    """One line for a snapshot that is never compared: a whole percentage or a count, and
    the local time it was observed."""
    window = _WINDOWS.get(snapshot.window, f"Other window ({snapshot.window})")
    as_of = f"(as of {format_day_time(snapshot.observed_at.astimezone(tz))})"
    if snapshot.unit == "fraction":
        return f"{window}: {round((snapshot.utilization or 0) * 100)}% used {as_of}"
    return f"{window}: {_requests(snapshot.used or 0)} {as_of}"


def _line(label: str, rows: Iterable[BreakdownRow]) -> BreakdownLine:
    found = tuple(rows)
    return BreakdownLine(
        label=label,
        fast=sum(row.requests for row in found if row.tier == "fast"),
        thorough=sum(row.requests for row in found if row.tier == "thorough"),
        total=sum(row.requests for row in found),
        input_tokens=sum(row.input_tokens for row in found),
        output_tokens=sum(row.output_tokens for row in found),
    )


class UsagePanel:
    """``usage`` gives the open workspace's usage service (None before one is open),
    ``offered`` the services shown in order, ``label_for`` a registered task type's label,
    ``tz`` the zone times are written in."""

    def __init__(
        self,
        usage: Callable[[], UsageService | None],
        *,
        offered: Callable[[], tuple[ServiceId, ...]],
        label_for: Callable[[str], str | None],
        tz: tzinfo,
    ) -> None:
        self._usage = usage
        self._offered = offered
        self._label_for = label_for
        self._tz = tz

    def view(self) -> UsagePanelView:
        usage = self._usage()
        if usage is None:
            return UsagePanelView(services=())
        offered = self._offered()
        reports = usage.reports(offered)  # one read snapshot for every service
        return UsagePanelView(
            tuple(self._service(s, r) for s, r in zip(offered, reports, strict=True))
        )

    def _service(self, service: ServiceId, report: UsageReport) -> ServiceUsage:
        status, rows = report.status, report.breakdown
        budget, level = status.budget, status.level
        allowance = None if budget is None else budget.allowance
        compared = status.provider_count is not None
        from_provider = status.effective.source == "provider"
        reset = format_day(status.cycle.reset_date)
        return ServiceUsage(
            service=service,
            name=SERVICE_NAMES[service],
            count=status.effective.value,
            count_text=_count_text(status.effective.value, allowance),
            allowance=allowance,
            status=None if level is None else _STATUS[level],
            block_note=_block_note(budget),
            stopped=(
                f"The app won't use this service until {reset}." if level == "limit" else None
            ),
            caps=tuple(_cap_line(cap, reset) for cap in status.tier_caps),
            reset=None if budget is None else reset,
            limit_note=_limit_note(status),
            unfinished=_unfinished(status.unfinished_count),
            local_count=status.local_count,
            provider_count=status.provider_count,
            used=(PROVIDER_USED if from_provider else LOCAL_USED) if compared else None,
            information=tuple(_information(s, self._tz) for s in status.information_only),
            table_note=TABLE_NOTE if from_provider else None,
            since=format_day(status.cycle.start_date),
            rows=self._lines(rows),
            total=_line(TOTAL_LABEL, rows),
        )

    def _lines(self, rows: tuple[BreakdownRow, ...]) -> tuple[BreakdownLine, ...]:
        """A line per task type, by label. A label two types share, or the total's, gets the
        type's id added, so every row header is its own."""
        labels = {row.task_type: self._label(row.task_type) for row in rows}
        shared = Counter(labels.values())
        lines = []
        for task_type, label in labels.items():
            if shared[label] > 1 or label == TOTAL_LABEL:
                label = f"{label} ({task_type})"
            lines.append(_line(label, (row for row in rows if row.task_type == task_type)))
        return tuple(sorted(lines, key=lambda line: line.label.lower()))

    def _label(self, task_type: str) -> str:
        if task_type == CONNECTION_TEST_TASK_TYPE:
            return CONNECTION_TEST_LABEL
        return self._label_for(task_type) or task_type


def _limit_note(status: UsageStatus) -> str | None:
    """What a service without a budget says: never "No limit" while the stored budgets
    can't be read (ADR-084)."""
    if status.budgets_unreadable:
        return PAUSED
    return NO_LIMIT if status.budget is None else None
