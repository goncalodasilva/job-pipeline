"""Fictional data, small HTML readers and the markup contract for the M8.3 part A debrief tests.

Shared by ``tests/integration/test_debrief.py``, ``tests/integration/test_today_debrief.py`` and
``tests/e2e/test_debrief.py``. The implementer matches these names.

Routes (``app/api/debrief.py``)
-------------------------------
- ``GET /applications/{application_id}/debriefs/{event_id}``: a token-free PAGE (``base.html``,
  an ``h1``, then a SHELL: the heading, the interview's time, the privacy line and a ``#debrief``
  placeholder with ``hx-get`` (the same address), ``hx-trigger="load"`` and
  ``hx-swap="outerHTML"``; no form and none of the saved texts, which are personal data). With
  ``HX-Request`` it answers the full ``#debrief`` fragment and needs the launch token (with
  ``?active=1`` it answers 204 while the draft facts task runs). Both ids are safe ids; an
  unknown or unsafe id, an event of another
  application and an event that is not an interview answer the same fixed 404 (the text never
  holds the id). Reading writes nothing.
- ``POST .../save``, ``.../dismiss``, ``.../drafts``, ``.../status``: launch token and origin
  (403 otherwise), form fields given once each (an unknown or repeated field is a plain-text
  400), ``version`` a whole number (missing or not a number: 400; stale: 409 with the reload
  prompt, nothing changes). ``version`` is the debrief's for save, dismiss and drafts (0 while
  the debrief has no row) and the APPLICATION's for status. A success answers 200 with the
  ``#debrief`` fragment (htmx) and an ``HX-Trigger`` header ``{"announce": "<text>"}``, or 303
  to the page for a plain post. Announcements: ``SAVED``, ``DISMISSED``, ``DRAFTS_STARTED``
  below, and ``Moved to <status label>.`` for the status.
- ``save`` fields: ``version``, ``questions``, ``how_it_went``, ``experiences``, ``company_only``
  (``"1"`` or absent), ``next_step`` (``next_round``, ``offer``, ``rejected``, ``other`` or
  blank), ``next_date`` (``YYYY-MM-DD``), ``next_time`` (``HH:MM``), ``next_zone`` (an IANA
  name). Line ends are normalised to LF and NUL is dropped before anything is checked or kept.
  A refused input answers 422 with the form again (typed text kept, escaped) and a
  ``role="alert"`` message; nothing is stored. A next-round event is made only when both
  ``next_date`` and ``next_time`` are given (a date without a time, an unknown zone or an
  impossible time is a 422). The event is a 60-minute interview at that wall time in that zone.
- ``dismiss`` fields: ``version`` and optionally ``return=today``; with ``return=today`` the
  answer is the refreshed Today content (``#today-content``) with the Today heading focused.
- ``drafts`` fields: ``version``, optionally ``tier`` and the budget-dialog fields (as the
  practice answer route takes them). Refusals: 409 when the debrief is not saved, has no
  experiences, or already has drafts; 409 with ``#debrief-reason`` when the budget blocks; 200
  with ``dialog#debrief-budget-dialog`` for a budget warning. Nothing is stored or queued on any
  refusal.
- ``status`` field: ``version`` (the application's). 409 when the debrief is not saved, when
  nothing is suggested now, or the version is stale; nothing changes.

Page markup (inside ``#debrief``)
---------------------------------
- ``h2#debrief-heading`` (``tabindex="-1"``, ``data-autofocus`` after an action).
- Copy: the questions "join your question bank" and "may appear in later prep requests"; how it
  went "stays on this computer".
- Before Save: ``form#debrief-form`` (``hx-post`` and ``action`` ``.../save``) with hidden
  ``version`` and the labelled controls of ``LABELS`` (textareas ``questions``, ``how_it_went``,
  ``experiences``; checkbox ``company_only``; select ``next_step`` with the values above and a
  blank first option; inputs ``next_date`` (``type=date``), ``next_time`` (``type=time``),
  ``next_zone``) and the button ``#debrief-save`` "Save debrief" (it sits beside a small separate
  form ``.../dismiss`` with the button "Not needed"; the next-round fields are one fieldset with
  the legend "Next round (only if there is one)"; "Optional: add only what you want to
  remember." says the debrief is optional).
- After Save the three texts are read-only: no textarea, each text shown escaped in an element
  with ``data-debrief-text="questions|how_it_went|experiences"``; the next round, when made, in
  ``#debrief-next``.
- "Turn into draft facts": ``form#debrief-drafts`` (``.../drafts``) with the hidden ``version``
  and the button ``#debrief-drafts-start``; present only while the debrief is saved, has
  experiences and has no drafts. While its task runs ``#debrief-working`` (plain text, no live
  region) says so; a failed task shows ``role="alert"`` and the button reads "Try again". When
  the budget blocks, the button has ``aria-disabled="true"`` and
  ``aria-describedby="debrief-reason"``.
- Drafts made: one ``li[data-debrief-draft="<fact id>"]`` each (the wording; the words "Check
  this wording" when the draft needs a check) and a link to ``/facts?tab=drafts``.
- Status: ``form#debrief-status`` (``.../status``; hidden ``version`` = the application's) with
  the words "Move to <label>?" and the button ``#debrief-status-confirm`` "Move to <label>";
  absent when nothing is suggested or it was confirmed (the page then says "Moved to <label>"
  in ``#debrief-moved``, plain text). After an action ``#debrief-message`` shows its words as
  plain text (the header announces them).
- ``Dismissed``: the page says the debrief was marked not needed.

Application timeline: each INTERVIEW event that is scheduled and already over has
``a[data-debrief-link]`` "Debrief" to the page (``aria-describedby`` the event's text
``event-text-<id>``); none for future, cancelled or other events.

Today (``#today-debriefs``, directly after ``#today-interviews``)
------------------------------------------------------------------
``section#today-debriefs.today-block`` with ``h2`` "Interview debriefs" and one
``li[data-debrief-event-id="<event id>"]`` per offer: a ``p#debrief-offer-<event id>`` with the
words "How did the interview go?" (and that a debrief is optional), a link "Debrief" to the page
and ``form`` ``.../dismiss`` (hidden ``version`` 0 and ``return`` ``today``; ``hx-post``;
``hx-target="#today-content"``) with the button "Not needed". The link and the button have the
``p``'s id as ``aria-describedby``, so each offer's controls are told apart. The block
is absent without an offer. ``TodayView.debriefs`` lists them and ``TodayView.empty`` is false
while one is offered.

Everything here is fictional.
"""

import html
import re
from typing import Any

from tests.board_pages import elements
from tests.integration.test_app_shell import Element

SAVED = "Debrief saved."
DISMISSED = "Debrief marked not needed."
DRAFTS_STARTED = "Draft facts started."
TODAY_DISMISSED = DISMISSED

NOW = "2026-10-20T12:00:00+00:00"
EVENT_START = "2026-10-20T09:00:00+00:00"
EVENT_END = "2026-10-20T10:00:00+00:00"
FUTURE_START = "2026-10-25T09:00:00+00:00"
FUTURE_END = "2026-10-25T10:00:00+00:00"

LABELS = {
    "questions": "Questions you were asked (one per line)",
    "company_only": "Only for this company",
    "how_it_went": "How it went",
    "experiences": "Experiences you mentioned or wish you had said",
    "next_step": "What happens next",
    "next_date": "Next round date",
    "next_time": "Next round time",
    "next_zone": "Next round time zone",
}

# Paragraphs the fake provider turns into fixtures that the app's own resolver accepts
# (``tests/fixtures/ai/structure_answer/ok.json`` and ``ok_story.json``).
PARAGRAPH_ONE = "I planned 12 routes."
PARAGRAPH_TWO = "The team had no routes. I planned 12 routes. Everyone used the routes."
TWO_PARAGRAPHS = f"{PARAGRAPH_ONE}\n\n{PARAGRAPH_TWO}"


def base(application_id: str, event_id: str) -> str:
    return f"/applications/{application_id}/debriefs/{event_id}"


def says(page: str) -> str:
    """Visible-ish text with entities decoded and whitespace collapsed."""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", page)).split())


def one(page: str, element_id: str) -> Element | None:
    found = [e for e in elements(page) if e.attrs.get("id") == element_id]
    assert len(found) <= 1, f"#{element_id} appears {len(found)} times"
    return found[0] if found else None


def has(page: str, element_id: str) -> bool:
    return one(page, element_id) is not None


def control(page: str, label: str) -> Element:
    """The form control whose ``<label for>`` text is exactly ``label``."""
    labels = [e for e in elements(page) if e.tag == "label" and e.text == label]
    assert len(labels) == 1, f"expected one label {label!r}, found {len(labels)}"
    target = labels[0].attrs.get("for")
    assert target, f"label {label!r} has no for="
    found = [e for e in elements(page) if e.attrs.get("id") == target]
    assert len(found) == 1, f"label {label!r} points at {target!r}: {len(found)} controls"
    return found[0]


def texts_shown(page: str) -> dict[str, str]:
    """The read-only texts of a saved debrief, by field."""
    return {
        str(e.attrs["data-debrief-text"]): e.text
        for e in elements(page)
        if "data-debrief-text" in e.attrs
    }


def draft_items(page: str) -> dict[str, Element]:
    return {
        str(e.attrs["data-debrief-draft"]): e
        for e in elements(page)
        if e.tag == "li" and "data-debrief-draft" in e.attrs
    }


def offers(page: str) -> dict[str, Element]:
    """The Today offers, by event id."""
    return {
        str(e.attrs["data-debrief-event-id"]): e
        for e in elements(page)
        if e.tag == "li" and "data-debrief-event-id" in e.attrs
    }


def inside(page: str, parent: Element, predicate: Any = lambda e: True) -> list[Element]:
    return [e for e in elements(page) if e.within(parent) and predicate(e)]


def hidden_value(page: str, form: Element, name: str) -> str:
    found = inside(page, form, lambda e: e.tag == "input" and e.attrs.get("name") == name)
    assert len(found) == 1, f"expected one {name} input in the form, found {len(found)}"
    return str(found[0].attrs.get("value", ""))
