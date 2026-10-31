"""Fictional data and the assumed markup of Today's "Suggested session" block (M7.8; AC-M7.6;
design "Today" block 5 and "Frequency controls"; ADR-117 item 8, ADR-119). The integration and
end-to-end tests share these names. The implementer follows this docstring where the spec is
silent. No application module for the block is imported, so a missing route or template fails
the test that needs it.

Rules the tests rely on (``suggestion_due`` in ``app/domain/enrichment.py`` already holds them)
- The setting ``session_suggestion`` stores ``{"last_reset": "<ISO instant>"}`` (written by
  ``SessionService.start`` since M7.4). Dismiss writes the same shape with the clock's now.
- Weekly = 7 elapsed days after the last reset, twice a week = 84 elapsed hours, after each
  application = some application's ``applied_at`` is after the last reset (and not in the
  future). With no reset at all, weekly and twice a week are due at once; after-application
  needs an application; never and paused are never due.
- The block needs ALL of: due, not paused, a non-empty ``SessionService.backlog()`` and an AI
  provider saved (the ``ai_provider`` setting names a kind). Otherwise there is no block and
  nothing about it on the page.

Markup (a ``section`` in ``#today-content``; Today's other blocks are untouched)
- ``<section id="today-session" class="today-block" aria-labelledby="today-session-heading">``
  placed after ``#today-drafts`` (design: block 5). It is only present when it has something to
  offer. ``today.empty`` ("Nothing needs your attention today.") is unchanged by the block.
- Suggestion: ``<h2 id="today-session-heading">Suggested session</h2>`` and text "3 questions,
  about 5 minutes" (length 5: "5 questions, about 8 minutes"), the length being the Sessions
  setting's default length. Two forms:
  * ``<form id="today-session-start" method="post" action="/sessions" hx-post="/sessions">`` with
    hidden ``focus`` = ``most_useful`` and hidden ``length`` = the default length ("3" or "5"),
    and a submit button whose text is "Start session" (the existing ``POST /sessions``: it
    already resets ``session_suggestion``; an open session or a refusal is answered as there).
  * ``<form id="today-session-dismiss" method="post" action="/today/suggestion/dismiss"
    hx-post="/today/suggestion/dismiss" hx-target="#today-content" hx-swap="outerHTML">`` with no
    fields at all and a submit button whose visible text is "Dismiss" and whose accessible name
    starts with "Dismiss" and says it is about the session suggestion.
- Open session: the same section (``#today-session``) shows a link "Continue your session" to
  ``/sessions/<id>`` and none of: the words "Suggested session", the time text, the Start and
  Dismiss forms. It shows whatever the frequency, pause, window or backlog (a session is
  already running; the link is not a suggestion). An ended session shows nothing special.

``POST /today/suggestion/dismiss`` (launch token and origin; POST only; a form with no fields,
anything else is a plain-text 400 "This request isn't valid."; GET with the token is a 405)
- stores ``{"last_reset": <clock now>}`` in ``session_suggestion`` (inserted when missing,
  updated when present; nothing else is written: no task, no session). It does not matter
  whether a suggestion was showing.
- htmx: 200 with ``#today-content`` again (Today's ``content_response(focus=True)``: no
  ``<html``; the page heading ``#page-heading`` is the one ``data-autofocus`` element), the
  block gone, and ``HX-Trigger`` carrying the announcement "Session suggestion dismissed.".
  Plain: 303 to ``/``.

Everything here is fictional.
"""

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from tests.board_pages import Seeder, by_id, elements, stamp_after, ticker
from tests.facts_page import form_values, posts_to
from tests.integration.test_app_shell import Element

DISMISS_PATH = "/today/suggestion/dismiss"
START_PATH = "/sessions"
BLOCK_ID = "today-session"
START_ID = "today-session-start"
DISMISS_ID = "today-session-dismiss"
SUGGESTION_KEY = "session_suggestion"
DISMISSED = "Session suggestion dismissed."
CONTINUE = "Continue your session"
NOTHING_DUE = "Nothing needs your attention today."
HEADING = "Suggested session"
TEXT = {3: "3 questions, about 5 minutes", 5: "5 questions, about 8 minutes"}

T0 = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)  # the frozen clock of the window tests

ROUTES = "Five years of route planning"
TERRAFORM = "Experience with Terraform"


def seed_backlog(seed: Seeder) -> None:
    """One saved job (seen, so Today has no "New jobs" block) asking for two clusters nobody has
    a fact for: a non-empty backlog, and no drafts or reminders, so Today is otherwise empty."""
    from app.domain.fit import Requirement
    from tests.tailoring_pages import seed_summary

    seed.job("sugg-job", "Route Planner", "Harbor Kite Fictional")
    seed_summary(
        seed.db,
        "sugg-job",
        (
            Requirement("sugg-r1", "must_have", ROUTES),
            Requirement("sugg-r2", "key", TERRAFORM),
        ),
        summary_id="summary-sugg-job",
    )
    with seed.db.transaction() as conn:
        conn.execute("UPDATE jobs SET triage_state='seen' WHERE id='sugg-job'")


def make_seeder(app: Any, clock: Any) -> Seeder:
    return Seeder.make(
        app.state.workspace.db,
        app.state.applications,
        app.state.application_statuses,
        ticker(clock),
        stamp_after(clock),
    )


# --- reading Today -------------------------------------------------------------------------


def block(html: str) -> Element | None:
    return by_id(html, BLOCK_ID)


def inside(html: str, container: Element) -> list[Element]:
    return [e for e in elements(html) if e.within(container)]


def form_in(html: str, form_id: str) -> Element | None:
    found = by_id(html, form_id)
    if found is None:
        return None
    assert found.tag == "form", found
    section = block(html)
    assert section is not None and found.within(section), f"#{form_id} is inside #{BLOCK_ID}"
    return found


def suggested(html: str) -> bool:
    """The block is offering a session: it has the Start form."""
    return form_in(html, START_ID) is not None


def start_values(html: str) -> dict[str, Any]:
    form = form_in(html, START_ID)
    assert form is not None, "no Start form"
    assert posts_to(form) == START_PATH
    return dict(form_values(elements(html), form))


def continue_link(html: str) -> Element | None:
    section = block(html)
    if section is None:
        return None
    found = [e for e in inside(html, section) if e.tag == "a" and e.text == CONTINUE]
    assert len(found) <= 1, "one Continue link"
    return found[0] if found else None


# --- stored suggestion state ---------------------------------------------------------------


def stored_reset(rows: Callable[[str], list[Any]]) -> datetime | None:
    """The ``last_reset`` in the ``session_suggestion`` setting, or None when there is no row."""
    found = rows(f"SELECT value FROM settings WHERE key='{SUGGESTION_KEY}'")  # noqa: S608
    if not found:
        return None
    row = found[0]
    value = row["value"] if isinstance(row, dict) else row[0]
    return datetime.fromisoformat(json.loads(value)["last_reset"])
