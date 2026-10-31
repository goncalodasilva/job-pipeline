"""Fictional data and the assumed markup of the session entry points (M7.9; AC-M7.1 (capture notes
raise, expiry), AC-M7.5; scope "entry points from applications, interviews and rejections",
"capture inbox expansion"; design "Starting points", "Quality hints"; ADR-117 items 4, 8, 9 and 11,
ADR-121, ADR-123, ADR-125 item 4). The integration and end-to-end tests share these names. The
implementer follows this docstring wherever the spec is silent. No application module for the
entry points is imported here, so a missing button or route fails the test that needs it.

Routes
- No new route. Every entry point is a small form that posts to the existing ``POST /sessions``
  (launch token and origin, as the Coverage and Today starts), so the checks, the budget gate, the
  one-open-session rule and the redirect to ``/sessions/<id>`` are the ones already built.
  An htmx success is ``200`` with ``HX-Redirect: /sessions/<id>``; a plain post is ``303``.
- Page reads stay token-free and read-only (they write nothing: no session, task, usage row or
  provider request). Showing an entry point costs nothing.

Markup of every entry point (``read_offer`` checks all of it)
- ``<form method="post" action="/sessions" hx-post="/sessions" hx-target="#<slot>"
  hx-swap="outerHTML" data-session-offer="<place>">`` with exactly two fields, the hidden
  ``focus`` and the hidden ``length`` (the Sessions setting's default length, "3" or "5"), and
  one submit button whose whole text is the label below. The feedback text, notes, answers and
  fact wording are never fields of this form.
- ``<slot>`` is the id of an empty ``div`` that is NOT inside the form (a sibling, next to it) and
  starts with ``session-refusal-``. It is what htmx sends as ``HX-Target`` for the post. Its id is
  unique on the page, so a refusal finds its own slot.
- Where several forms of one kind share a page (events, notes, facts) the button also has
  ``aria-describedby`` naming elements that carry the item's own words, so the accessible names
  differ (as "Turn into a fact" does).

The places, their ``focus`` value, label and slot
- ``gap``: Tailoring step 1 (the gap check), ``focus`` = ``job:<job id>``, label "Answer a few
  questions about these gaps", slot ``session-refusal-gap``. Shown in ``#tailoring`` (the page
  and the ``/applications/{id}/tailoring/1`` fragment) only when the stored, shown result has at
  least one requirement whose verdict is ``partial`` or ``missing``: not before a check, not when
  every requirement is met, not while a check is running. One form for the step (not one per
  requirement).
- ``interview``: the application drawer (``/applications/{id}/detail``) and page
  (``/applications/{id}``), in the timeline item ``li[data-timeline-kind="event"]`` of each
  interview event, upcoming or past. ``focus`` = ``job:<job id>``, label "Talk through this
  interview in a session", slot ``session-refusal-interview-<event id>``. Other event types
  (follow-up, deadline, offer deadline) have none. (Whether a cancelled interview has one is left
  to the implementer; no test looks.)
- ``rejection``: the "Any feedback worth noting?" follow-up dialog ``dialog#board-followup``
  (after a move to Rejected on the board, and inside the drawer and page when the move was made
  there), next to the feedback form and NOT inside ``form#feedback-form`` (forms cannot nest).
  ``focus`` = ``job:<job id>``, label "Start a session about this role's requirements", slot
  ``session-refusal-rejection``. The Interviewing date dialog has none.
- ``note``: the Capture inbox, in the item ``li#note-<note id>`` of each open note, beside "Turn
  into a fact" and "Delete". ``focus`` = ``capture_note:<note id>``, label "Expand in a session",
  slot ``session-refusal-note-<note id>``, ``aria-describedby="capture-text-<note id>"``. It is in
  the full ``#capture-inbox`` section every time it is rendered (the tab and every htmx answer).
- ``hint``: a confirmed, usable fact that has a quality hint (``app/domain/quality_hints.py``), in
  the Library's card ``div#fact-<fact id>`` and on the fact's editor page ``#fact-editor-page``
  (outside ``form#fact-editor``). Each hint is a ``p[data-quality-hint]`` whose whole text is the
  hint ("Add a result?", "Add a number?", "Add what you did?"); no other text. ``focus`` =
  ``fact:<fact id>``, label "Answer in a session", slot ``session-refusal-hint-<fact id>``. One
  form per fact, shown with its hints, and only when there is at least one hint. No hint, no
  form, for a fact with nothing to add, never-use, draft or archived. A hint never blocks: no
  ``role="alert"``, no ``aria-invalid``, no disabled or required control, and saving still works.
  The editor re-derives its hints from the saved wording (a save that adds the result drops "Add
  a result?" from the answer).

A refused start (reason only, in the slot; the Coverage and Today approach, ADR-125 item 4)
- When the post is htmx and its ``HX-Target`` is a slot of the shape above, every refusal
  (no AI provider, a budget block or warning dialog, an open session, nothing left to ask, a
  stale or invalid form) answers just that slot, never the whole new-session screen: the status
  code the whole-screen answer would have had (409 for a block, 200 for a warning dialog, 422 or
  409 for an invalid form), no ``<html``, no ``h1``/``h2``, no ``select`` or ``form`` outside a
  budget dialog, ``<div id="<the same slot>">`` kept so a next refusal has a target, exactly one
  ``data-autofocus`` element (the reason, or the dialog's focus), a "Continue your session" link
  to ``/sessions/<id>`` for an open session, and a budget dialog that posts the form's own fields
  again to the same slot. The same refusal without that target is the whole screen as before.

Sessions the entry points start
- ``focus_kind`` ``job`` with ``focus_ref`` = the job id (and ``job_id``), ``capture_note`` with the
  note id, ``fact`` with the fact id. The first question is the top of the backlog for that focus:
  for a job, clusters with no evidence come before thin ones (already built in ``session_backlog``).

Open questions the tests do NOT settle (see the task report)
- A job whose application is closed (rejected, withdrawn) is out of the coverage clusters, so
  ``job:<id>`` for it may have nothing to ask. The rejection test accepts either a started session
  or a reason in the slot; the rule that no feedback text reaches the AI holds either way.

Everything here is fictional.
"""

from dataclasses import dataclass
from typing import Any

from tests.board_pages import elements
from tests.facts_page import described_by, form_values, posts_to
from tests.integration.test_app_shell import Element

OFFER_ATTR = "data-session-offer"
SESSIONS_PATH = "/sessions"
SLOT_PREFIX = "session-refusal-"

LABELS = {
    "gap": "Answer a few questions about these gaps",
    "interview": "Talk through this interview in a session",
    "rejection": "Start a session about this role's requirements",
    "note": "Expand in a session",
    "hint": "Answer in a session",
}
# Places whose forms repeat on one page, so their buttons need their own description.
REPEATED = frozenset({"interview", "note", "hint"})
HINT_ATTR = "data-quality-hint"
HINT_TEXTS = ("Add a result?", "Add a number?", "Add what you did?")
CONTINUE = "Continue your session"

JOB_ID = "tailoring-job"  # Tailoring.ready()'s default job
UPCOMING = "2026-11-03T14:00:00+00:00"  # after the frozen clock (2026-10-20 09:00 UTC)
PAST = "2026-10-10T14:00:00+00:00"
TERRAFORM = "Experience with Terraform"
ROUTES = "Five years of route planning"
ENGLISH = "Fluent English"
SAP = "Knowledge of SAP"
NOTE_TEXT = "Ran the fictional spring stocktake at the Example Freight depot in two days."
OTHER_NOTE = "Trained two new fictional drivers on the pallet scanner."
FEEDBACK_MARKER = "FEEDBACK-MARKER-ZEPHYR-4471 they wanted deeper irrigation planning"

STORY_NO_RESULT = ("The depot had no roster.", "Built a roster for 4 teams.", "")
STORY_NO_ACTION = ("The depot lost 30 parcels.", "", "Found every parcel.")
STORY_COMPLETE = (
    "The depot had no roster.",
    "Built a roster for 4 teams.",
    "Cut manual steps by half.",
)
FACT_NO_NUMBER = "Planned delivery routes for the fictional harbor depot."
NEVER_USE_NO_NUMBER = "Confidential fictional audit of the harbor books."


@dataclass(frozen=True)
class Offer:
    """One entry point as the page shows it."""

    place: str
    form: Element
    button: Element
    slot: Element
    values: dict[str, Any]

    @property
    def focus(self) -> str:
        return str(self.values["focus"])

    @property
    def slot_id(self) -> str:
        return str(self.slot.attrs["id"])


def forms_of(parsed: list[Element], place: str, within: Element | None = None) -> list[Element]:
    return [
        e
        for e in parsed
        if e.tag == "form"
        and e.attrs.get(OFFER_ATTR) == place
        and (within is None or e.within(within))
    ]


def read_offer(parsed: list[Element], form: Element, *, length: str = "3") -> Offer:
    """Check the whole shared contract of one entry point form and return it."""
    place = str(form.attrs.get(OFFER_ATTR))
    assert place in LABELS, f"unknown entry point {place!r}"
    assert posts_to(form) == SESSIONS_PATH
    target = str(form.attrs.get("hx-target") or "")
    assert target.startswith("#" + SLOT_PREFIX), f"hx-target {target!r} is not a refusal slot"
    assert form.attrs.get("hx-swap") == "outerHTML"
    slot_id = target[1:]
    slots = [e for e in parsed if e.attrs.get("id") == slot_id]
    assert len(slots) == 1, f"expected one #{slot_id}, found {len(slots)}"
    (slot,) = slots
    assert slot.tag == "div" and not slot.within(form), "the slot is a div outside the form"
    assert not slot.text, "nothing is said until a start is refused"
    values = dict(form_values(parsed, form))
    assert set(values) == {"focus", "length"}, f"the form's fields are {sorted(values)}"
    assert values["length"] == length
    submits = [
        e
        for e in parsed
        if e.within(form) and e.tag == "button" and (e.attrs.get("type") or "submit") == "submit"
    ]
    assert [b.text for b in submits] == [LABELS[place]], [b.text for b in submits]
    (button,) = submits
    if place in REPEATED:
        assert button.attrs.get("aria-describedby"), "repeated buttons are described by the item"
        assert all(d.text for d in described_by(parsed, button))
    return Offer(place, form, button, slot, values)


def offers(
    html: str, place: str, within: Element | None = None, *, length: str = "3"
) -> list[Offer]:
    parsed = elements(html)
    return [read_offer(parsed, f, length=length) for f in forms_of(parsed, place, within)]


def one_offer(html: str, place: str, within: Element | None = None, *, length: str = "3") -> Offer:
    found = offers(html, place, within, length=length)
    assert len(found) == 1, f"expected one {place!r} entry point, found {len(found)}"
    return found[0]


def no_offer(html: str, place: str | None = None) -> None:
    parsed = elements(html)
    places = [place] if place else list(LABELS)
    for name in places:
        assert forms_of(parsed, name) == [], f"unexpected {name!r} entry point"
    labels = [LABELS[name] for name in places]
    assert not [e for e in parsed if e.tag in ("button", "a") and e.text in labels], (
        "unexpected entry point control"
    )


def hints_in(html: str, scope: Element) -> list[str]:
    parsed = elements(html)
    return [e.text for e in parsed if e.within(scope) and HINT_ATTR in e.attrs]


# --- posting an entry point --------------------------------------------------------------------


def submit(
    run: Any, offer: Offer, *, htmx: bool = True, target: bool = True, **headers: str
) -> Any:
    """Post the form as it stands (``run`` is an ``Importing``): htmx adds ``HX-Request`` and the
    slot as ``HX-Target``; a plain post sends neither."""
    extra = {"HX-Request": "true", "HX-Target": offer.slot_id} if htmx and target else {}
    if htmx and not target:
        extra = {"HX-Request": "true"}
    return run.client.post(
        SESSIONS_PATH, data=offer.values, headers=run.run.action(**extra, **headers)
    )


def settle(run: Any) -> None:
    """Wait for every queued task to end, so nothing runs while the app closes."""
    for row in run.rows("SELECT id FROM tasks"):
        run.wait_terminal(row["id"])
