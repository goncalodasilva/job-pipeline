"""The prep state shown on a board card, in the drawer and in Today (M8.2 part D; ADR-124).

Three states, from counts the storage layer reads: no prep items, items with no practice
attempt, and at least one practiced item. Pure rules: no I/O, clock or ids.
"""

from typing import Final

NOT_STARTED: Final = "not_started"
QUESTIONS_READY: Final = "questions_ready"
PRACTICED: Final = "practiced"

PREP_STATE_LABELS: Final = {
    NOT_STARTED: "Prep not started",
    QUESTIONS_READY: "Questions ready",
    PRACTICED: "Practiced for the interview",
}


def prep_state(item_count: int, practiced_count: int) -> str:
    """The state key. ``practiced_count`` counts items with at least one attempt."""
    if practiced_count > 0:
        return PRACTICED
    return QUESTIONS_READY if item_count > 0 else NOT_STARTED


def should_show(item_count: int, has_future_interview: bool, closed: bool = False) -> bool:
    """The state is shown only for an application with prep items or a future interview.

    Never for a closed application (rejected, withdrawn...): interview prep is moot there, a
    still-scheduled event must not read "Prep not started", and one that already has prep items
    is hidden too, so a closed application shows no prep state at all.
    """
    return not closed and (item_count > 0 or has_future_interview)
