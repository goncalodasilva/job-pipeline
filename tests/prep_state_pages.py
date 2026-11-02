"""Fictional prep rows, events and small HTML readers for the M8.2 part D prep-state tests.

Markup contract (the implementer matches it; the integration and e2e tests share these names):

- Wherever a prep state is shown it is ONE element carrying ``data-prep-state="<state>"``
  (``not_started``, ``questions_ready`` or ``practiced``) whose text is exactly the state's label
  (``LABELS`` below). It has no child elements, no ``tabindex`` and no link or button inside, so
  it adds no tab stop.
- Board card: the element sits inside ``li.board-card[data-application-id]`` (suggested
  ``<p class="board-card-prep">``).
- Application drawer: inside ``article#application-detail`` (next to the "Next event" line).
- Today: inside ``li[data-interview-id]`` of ``#today-interviews``.
- The element is absent where the state is not shown (no prep items and no future interview).

Services: ``BoardCard.prep_state`` and ``Interview.prep_state`` (Today) are ``None`` where the
state is not shown, else the state key.
"""

from typing import Any

from tests.board_pages import elements
from tests.integration.test_app_shell import Element

NOW = "2026-10-20T09:00:00+00:00"
FUTURE = "2026-10-22T09:00:00+00:00"
FUTURE_END = "2026-10-22T10:00:00+00:00"
PAST = "2026-10-18T09:00:00+00:00"
PAST_END = "2026-10-18T10:00:00+00:00"
STAMP = "2026-10-01T09:00:00+00:00"
LABELS = {
    "not_started": "Prep not started",
    "questions_ready": "Questions ready",
    "practiced": "Practiced for the interview",
}
ADD_QUESTION = (
    "INSERT INTO bank_questions (id, text, type, origin, created_at, updated_at) "
    "VALUES (?, ?, 'behavioral', 'user', ?, ?)"
)
ADD_ITEM = (
    "INSERT INTO prep_items (id, application_id, question_id, ready, created_at, updated_at) "
    "VALUES (?, ?, ?, 0, ?, ?)"
)
ADD_ATTEMPT = (
    "INSERT INTO practice_attempts (id, prep_item_id, answer_text, created_at, updated_at) "
    "VALUES (?, ?, ?, ?, ?)"
)


def seed_prep(db: Any, application_id: str, *, items: int, practiced: int = 0) -> list[str]:
    """``items`` prep items for the application (each with its own fictional bank question) and
    one practice attempt on each of the first ``practiced`` items; returns the item ids."""
    assert 0 <= practiced <= items
    made = []
    with db.transaction() as conn:
        for number in range(items):
            question = f"ps-q-{application_id}-{number}"
            item = f"ps-i-{application_id}-{number}"
            conn.execute(
                ADD_QUESTION, (question, f"Describe fictional task {number}.", STAMP, STAMP)
            )
            conn.execute(ADD_ITEM, (item, application_id, question, STAMP, STAMP))
            if number < practiced:
                conn.execute(
                    ADD_ATTEMPT,
                    (f"ps-a-{application_id}-{number}", item, "A fictional answer.", STAMP, STAMP),
                )
                # A second try on the same item must not count as a second practiced item.
                conn.execute(
                    ADD_ATTEMPT,
                    (f"ps-b-{application_id}-{number}", item, "Another try.", STAMP, STAMP),
                )
            made.append(item)
    return made


def prep_marks(html: str, within: Element | None = None) -> list[Element]:
    return [
        e
        for e in elements(html)
        if "data-prep-state" in e.attrs and (within is None or e.within(within))
    ]


def card_of(html: str, application_id: str) -> Element:
    [card] = [
        e
        for e in elements(html)
        if e.tag == "li" and e.attrs.get("data-application-id") == application_id
    ]
    return card


def interview_of(html: str, event_id: str) -> Element:
    [row] = [
        e for e in elements(html) if e.tag == "li" and e.attrs.get("data-interview-id") == event_id
    ]
    return row


def drawer_of(html: str) -> Element:
    [article] = [
        e
        for e in elements(html)
        if e.tag == "article" and e.attrs.get("id") == "application-detail"
    ]
    return article


def mark_state(html: str, region: Element) -> tuple[str, str] | None:
    """The state key and the words of the one prep-state element inside ``region``, or None."""
    marks = prep_marks(html, region)
    assert len(marks) <= 1, f"expected at most one prep state, found {len(marks)}"
    if not marks:
        return None
    mark = marks[0]
    assert not [e for e in elements(html) if e.within(mark)], "the prep state has no children"
    assert not mark.is_focusable(), "the prep state is plain text, not a tab stop"
    return str(mark.attrs["data-prep-state"]), mark.text


def focusable_count(html: str, region: Element) -> int:
    return len([e for e in elements(html) if e.within(region) and e.is_focusable()])
