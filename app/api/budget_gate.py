"""What the budget warning dialog and a blocked action are given to render (M2.24; AC-M2.7).

The decision is ``BudgetGate``'s (``app/services/budget_gate.py``); a route that starts an
action passes its answer here with where the action posts and what it posts. The fragments
(``fragments/budget/_dialog.html``, ``fragments/budget/_blocked.html``) know nothing about
the action, so any route can reuse them.
"""

from collections.abc import Mapping, Sequence
from typing import Any, Final

from app.services.budget_gate import (
    LIMITS_LINK,
    LIMITS_URL,
    Refusal,
    ShowTierCap,
    ShowWarning,
)

CONTINUE: Final = "Continue"
RUN_ON_FAST: Final = "Run once on Fast"
TIER_FIELD: Final = "tier"
FAST: Final = "fast"
# Posted by "Run once on Fast": the tier the action's form showed, which it keeps showing.
CHOSEN_TIER_FIELD: Final = "budget_chosen_tier"
# Added to a warning's figures for a request that can't show the dialog (no htmx).
CONFIRM_IN_APP: Final = "Start this from the app to confirm it."


def dialog_context(
    answer: Refusal,
    *,
    dialog_id: str,
    action: str,
    target: str,
    fields: Mapping[str, str],
    return_focus: str,
    select: str = "",
) -> dict[str, Any] | None:
    """The dialog for ``answer``, or None when it asks nothing (a block, unreadable budgets,
    a tier cap without a free Fast tier).

    ``action`` is where the confirmed start posts and ``target`` the element its answer
    replaces; ``fields`` are the action's own accepted values, posted again as hidden fields
    (a tier field is ``tier``); ``return_focus`` selects the control that gets focus back
    when the dialog is closed without a start. ``select`` (optional) is the part of the answer
    to swap in (``hx-select``) when ``target`` is only a part of the page that answers.
    """
    if isinstance(answer, ShowWarning):
        heading, description, label = answer.heading, answer.description, CONTINUE
        posted = {**fields, **answer.fields}
    elif isinstance(answer, ShowTierCap) and answer.fast_free:
        heading, description, label = answer.heading, answer.message, RUN_ON_FAST
        posted = {**fields, TIER_FIELD: FAST}  # this run only: nothing stored changes
        if TIER_FIELD in fields:
            posted[CHOSEN_TIER_FIELD] = fields[TIER_FIELD]
    else:
        return None
    return {
        "id": dialog_id,
        "heading": heading,
        "description": description,
        "action": action,
        "target": target,
        "select": select,
        "fields": posted,
        "confirm_label": label,
        "limits_url": LIMITS_URL,
        "return_focus": return_focus,
    }


def reason_context(message: str, *, reason_id: str, focus: bool) -> dict[str, Any]:
    """Why an action can't start (the blocking point, a tier cap, unreadable budgets), shown
    above its button, which names ``reason_id`` in its ``aria-describedby``. ``focus``: this
    answers a start, so the reason takes focus."""
    return {
        "id": reason_id,
        "message": message,
        "limits_url": LIMITS_URL,
        "link": LIMITS_LINK,
        "focus": focus,
    }


def chosen_tier(form: Mapping[str, Sequence[str]], allowed: Sequence[str]) -> str | None:
    """The tier the action's form showed when "Run once on Fast" was pressed, or None unless
    the field is there once with a value from ``allowed``."""
    given = form.get(CHOSEN_TIER_FIELD, ())
    return given[0] if len(given) == 1 and given[0] in allowed else None


def refusal_message(answer: Refusal) -> str:
    """Why nothing started, in one line: for an answer that can't show the dialog."""
    if isinstance(answer, ShowWarning):
        return f"{answer.heading}. {answer.description} {CONFIRM_IN_APP}"
    return answer.message
