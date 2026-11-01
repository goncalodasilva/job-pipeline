"""The budget check in front of an action the user starts (M2.24; AC-M2.7, AC-M2.10; spec 3.7;
ADR-076 items 2, 7; ADR-085 item 1).

A route that starts something which will call an external service asks ``BudgetGate.check``
with the service, the tier and whatever confirmation came with the request, and gets one
answer: ``Start`` (go on, perhaps with a note), ``ShowWarning`` (a warning to confirm first),
``ShowTierCap`` (a tier at its cap), ``BlockedAction`` (the blocking point) or ``Unreadable``
(the stored budgets can't be read). Only ``Start`` lets the action begin.

Every rule is the usage service's (``check_start``, over the domain's ``evaluate``): which
warning is due, whether a confirmation names it (service, threshold and cycle start; then
stored in the same transaction as the check), what blocks. This module turns the decision
into what the answer shows; the copy says what the next request will reach, never "you have
used" (ADR-085), and where the app stops when that isn't the allowance. The check looks at the
next request only, also for an action that makes several. The runner's ``reserve`` still
refuses a request at the blocking point, whatever happened here.

Everything here blocks (database).
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from types import MappingProxyType
from typing import ClassVar, Final

from app.domain.budgets import Blocked, CapBlocked, Decision, Note, Warn
from app.domain.errors import (
    BudgetBlocked,
    UsageLimitsUnreadable,
    format_day,
    tier_cap_message,
)
from app.domain.usage import SERVICE_NAMES
from app.services.usage import Confirmation, StartCheck, UsageService
from app.services.usage_panel import block_point_note

# The fields a confirmed warning posts back with the action's own.
SERVICE_FIELD: Final = "budget_service"
THRESHOLD_FIELD: Final = "budget_threshold"
CYCLE_FIELD: Final = "budget_cycle_start"
LAST_REQUEST: Final = "This is the last request before the blocking point."
# Where budgets are changed (Settings > Usage budgets).
LIMITS_URL: Final = "/settings#usage-budgets"
LIMITS_LINK: Final = "Open Usage budgets"

_WARNING_NAMES: Final[Mapping[str, str]] = MappingProxyType(
    {"first": "First warning", "second": "Second warning"}
)
_FAST: Final = "fast"


@dataclass(frozen=True)
class Start:
    """The action may start. ``note``: the line shown once a reached warning was confirmed
    earlier in this cycle."""

    note: str | None = None


@dataclass(frozen=True)
class ShowWarning:
    """A warning the user hasn't confirmed this cycle: nothing starts until they do."""

    heading: str
    description: str
    fields: Mapping[str, str]  # what "Continue" posts with the action's own values

    @property
    def asks(self) -> bool:
        return True


@dataclass(frozen=True)
class ShowTierCap:
    """The tier is at its cap. ``fast_free``: the same action could start on Fast now, so it
    is offered once; otherwise ``message`` makes no such offer."""

    heading: str
    message: str  # spec 2.11, ``TierCapReached``; offers Fast only when ``fast_free``
    fast_free: bool

    @property
    def asks(self) -> bool:
        """Whether there is something to offer (one run on Fast)."""
        return self.fast_free


@dataclass(frozen=True)
class BlockedAction:
    """The service is at its blocking point: the action is disabled until the reset date."""

    message: str  # spec 2.11, ``BudgetBlocked``
    asks: ClassVar[bool] = False


@dataclass(frozen=True)
class Unreadable:
    """The stored budgets can't be read: nothing starts until they are saved again."""

    message: str  # spec 2.11, ``UsageLimitsUnreadable``
    asks: ClassVar[bool] = False


Refusal = ShowWarning | ShowTierCap | BlockedAction | Unreadable
GateAnswer = Start | Refusal


def confirmation_from_form(form: Mapping[str, Sequence[str]]) -> Confirmation | None:
    """The confirmation a submitted form carries, or None unless each of the three fields
    is there once and the cycle start is a date. Whether it names the warning that is due,
    for the service being checked, is the usage service's check."""
    service = form.get(SERVICE_FIELD, ())
    threshold = form.get(THRESHOLD_FIELD, ())
    cycle = form.get(CYCLE_FIELD, ())
    if len(service) != 1 or len(threshold) != 1 or len(cycle) != 1:
        return None
    try:
        cycle_start = date.fromisoformat(cycle[0])
    except ValueError:
        return None
    return Confirmation(service[0], threshold[0], cycle_start)


class BudgetGate:
    def __init__(self, usage: Callable[[], UsageService | None]) -> None:
        """``usage`` answers None while no workspace is open (nothing can start then, and
        nothing is counted)."""
        self._usage = usage

    def check(
        self,
        service: str,
        *,
        tier: str,
        confirmation: Confirmation | None = None,
        lookahead: int = 1,
    ) -> GateAnswer:
        """What starting an action on ``service`` and ``tier`` meets right now. A
        ``confirmation`` naming the warning that is due for ``service`` is stored, and the
        answer is ``Start``; any other is ignored."""
        usage = self._usage()
        if usage is None:
            return Start()
        try:
            checked = (
                usage.check_start(service, tier=tier, confirming=confirmation)
                if lookahead == 1
                else usage.check_start(
                    service, tier=tier, confirming=confirmation, lookahead=lookahead
                )
            )
        except UsageLimitsUnreadable as error:
            return Unreadable(error.user_message)
        return _answer(service, checked, lookahead=lookahead)

    def blocked(self, service: str, *, lookahead: int = 1) -> BlockedAction | None:
        """The block to show on an action's button before anyone presses it, if the service
        is at its blocking point. Writes nothing."""
        usage = self._usage()
        if usage is None:
            return None
        try:
            decision = (
                usage.preflight(service, tier=_FAST)
                if lookahead == 1
                else usage.preflight(service, tier=_FAST, lookahead=lookahead)
            )  # the block is the same on any tier
        except UsageLimitsUnreadable:
            return None  # said when the action is started, and in Settings > Usage budgets
        return _blocked(SERVICE_NAMES[service], decision)


def _blocked(name: str, decision: Decision) -> BlockedAction | None:
    if not isinstance(decision, Blocked):
        return None
    return BlockedAction(BudgetBlocked(name, decision.reset_date).user_message)


def _warning_text(decision: Warn, block_at: int | None, *, lookahead: int = 1) -> str:
    """The warning's figures: what this request reaches, where the app stops when that
    isn't the allowance, whether this is the last request it lets through, and the reset."""
    parts = [f"This request will be number {decision.count} of {decision.allowance} this period."]
    if lookahead > 1:
        parts = [
            f"This action uses up to {lookahead} searches and can reach "
            f"{decision.count} of {decision.allowance} this period."
        ]
    stops = None if block_at is None else block_point_note(block_at, decision.allowance)
    if stops is not None:
        parts.append(stops)
    if decision.count == block_at:
        parts.append(LAST_REQUEST)
    parts.append(f"The count resets on {format_day(decision.reset_date)}.")
    return " ".join(parts)


def _answer(service: str, checked: StartCheck, *, lookahead: int = 1) -> GateAnswer:
    name = SERVICE_NAMES[service]
    decision = checked.decision
    blocked = _blocked(name, decision)
    if blocked is not None:
        return blocked
    if isinstance(decision, CapBlocked):
        fast_free = decision.offer_fast and not isinstance(checked.fast, Blocked | CapBlocked)
        message = tier_cap_message(name, decision.tier, decision.reset_date, offer_fast=fast_free)
        heading = f"{decision.tier.capitalize()} limit reached for {name}"
        return ShowTierCap(heading, message, fast_free)
    if isinstance(decision, Warn):
        return ShowWarning(
            heading=f"{_WARNING_NAMES[decision.threshold]} for {name}",
            description=_warning_text(decision, checked.block_at, lookahead=lookahead),
            fields=MappingProxyType(
                {
                    SERVICE_FIELD: service,
                    THRESHOLD_FIELD: decision.threshold,
                    CYCLE_FIELD: checked.cycle_start.isoformat(),
                }
            ),
        )
    if isinstance(decision, Note) and not checked.acknowledged:
        if lookahead > 1:
            return Start(
                f"Past a warning point: up to {lookahead} searches can reach "
                f"{decision.count} of {decision.allowance} this period."
            )
        return Start(
            f"Past a warning point: this request is number {decision.count} of "
            f"{decision.allowance} this period."
        )
    return Start()
