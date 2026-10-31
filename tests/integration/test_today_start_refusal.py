"""M7.8 review note: a refused Start session on Today answers with the reason only.

The Start form on Today's "Suggested session" block aims its answer at ``#today-session-refusal``
(htmx sends that id as ``HX-Target``). When the start is refused (no provider, a budget block or
dialog, an open session, nothing left to ask) the answer is that element alone: the reason, a
Continue link or the budget dialog. It has no heading and no focus form, so the chosen focus
cannot be lost; a retry or confirm control posts ``focus=most_useful`` and the default length
again. The same refusal not aimed at the slot is still the whole new-session screen, as for the
Coverage tab (``tests/integration/test_coverage_tab.py``). Fictional data, no network.
"""

import pytest

from app.services.budget_gate import BlockedAction, ShowWarning
from tests.board_pages import elements
from tests.facts_page import form_values, posts_to
from tests.integration.test_session_suggestion import (  # noqa: F401  (the desk fixture)
    desk,
    open_session,
    save_sessions,
    today,
)
from tests.today_suggestion_pages import (
    START_ID,
    START_PATH,
    block,
    form_in,
    inside,
    start_values,
)

pytestmark = pytest.mark.integration

SLOT_ID = "today-session-refusal"
SLOT_TARGET = {"HX-Request": "true", "HX-Target": SLOT_ID}  # what htmx sends for the Start form


def start_from_today(run, *, target=True):
    return start_from(run, today(run), target=target)


def start_from(run, page, *, target=True):
    form = form_in(page, START_ID)
    assert form is not None
    headers = run.run.action(**(SLOT_TARGET if target else {"HX-Request": "true"}))
    return run.client.post(posts_to(form), data=form_values(elements(page), form), headers=headers)


def test_today_start_form_aims_at_a_slot_outside_the_form(desk):  # noqa: F811
    page = today(desk)
    form = form_in(page, START_ID)
    assert form is not None and posts_to(form) == START_PATH
    assert form.attrs.get("hx-target") == f"#{SLOT_ID}"
    assert form.attrs.get("hx-swap") == "outerHTML"
    section = block(page)
    assert section is not None
    slots = [e for e in inside(page, section) if e.attrs.get("id") == SLOT_ID]
    assert len(slots) == 1 and slots[0].tag == "div"
    assert not slots[0].within(form), "the slot is not inside the form, which keeps two fields"
    assert start_values(page) == {"focus": "most_useful", "length": "3"}
    assert not slots[0].text, "nothing is said until a start is refused"


@pytest.mark.parametrize("why", ["no_provider", "blocked", "open_session", "vanished"])
def test_today_refused_start_shows_only_the_reason(desk, monkeypatch, why):  # noqa: F811
    page = today(desk)  # read while there is something to ask
    if why == "no_provider":
        monkeypatch.setattr(desk.app.state.sessions, "_settings", lambda: None)
    elif why == "blocked":
        monkeypatch.setattr(
            desk.app.state.budget_gate,
            "check",
            lambda *args, **kwargs: BlockedAction("Fictional limit reached. Resets on 1 May."),
        )
    elif why == "open_session":
        open_session(desk)
    else:
        desk.execute("UPDATE jobs SET triage_state='removed'")
    _check(desk, start_from(desk, page), why)


def _check(desk, response, why):  # noqa: F811
    assert response.status_code == 409, response.text[:300]
    assert "<html" not in response.text
    answer = elements(response.text)
    assert not [e for e in answer if e.tag in ("h1", "h2")], "no heading of its own"
    assert not [e for e in answer if e.tag in ("select", "form")], "no focus or length form"
    (slot,) = [e for e in answer if e.attrs.get("id") == SLOT_ID]
    assert slot.tag == "div", "the answer keeps the slot, so a next refusal has a target"
    assert [e for e in answer if "data-autofocus" in e.attrs], "focus moves to the reason"
    assert len([e for e in answer if "data-autofocus" in e.attrs]) == 1
    if why == "blocked":
        assert "Fictional limit reached" in response.text
    if why == "open_session":
        links = [e for e in answer if e.tag == "a" and e.text == "Continue your session"]
        assert len(links) == 1 and links[0].attrs["href"] == "/sessions/open-session"
    assert desk.rows("SELECT id FROM sessions") == (
        [{"id": "open-session"}] if why == "open_session" else []
    )


def test_today_refused_start_not_aimed_at_the_slot_is_the_whole_screen(desk, monkeypatch):  # noqa: F811
    monkeypatch.setattr(desk.app.state.sessions, "_settings", lambda: None)
    whole = start_from_today(desk, target=False)
    assert whole.status_code == 409 and "<h1" in whole.text


def test_today_refused_start_dialog_keeps_the_focus_and_length(desk, monkeypatch):  # noqa: F811
    save_sessions(desk, length=5)
    monkeypatch.setattr(
        desk.app.state.budget_gate,
        "check",
        lambda *args, **kwargs: ShowWarning(
            "First warning",
            "Fictional request limit.",
            {
                "budget_service": "fake",
                "budget_threshold": "first",
                "budget_cycle_start": "2026-04-01",
            },
        ),
    )
    response = start_from_today(desk)
    assert response.status_code == 200
    answer = elements(response.text)
    assert not [e for e in answer if e.tag in ("h1", "h2") and "session" in e.text.lower()]
    assert not [e for e in answer if e.tag == "h1"]
    (dialog,) = [e for e in answer if e.tag == "dialog"]
    assert dialog.attrs["data-return-focus"] == f"#{START_ID} button"
    (form,) = [e for e in answer if e.tag == "form" and e.within(dialog)]
    assert form.attrs["hx-target"] == f"#{SLOT_ID}"
    fields = form_values(answer, form)
    assert fields["focus"] == "most_useful" and fields["length"] == "5"
    assert fields["budget_service"] == "fake"
    assert desk.rows("SELECT id FROM sessions") == []
    assert [e.attrs["id"] for e in answer if e.attrs.get("id") == SLOT_ID] == [SLOT_ID]


def test_today_start_aimed_at_the_slot_still_starts_the_session(desk):  # noqa: F811
    response = start_from_today(desk)
    assert response.status_code == 200 and response.headers["HX-Redirect"].startswith("/sessions/")
    for row in desk.rows("SELECT id FROM tasks"):
        desk.wait_terminal(row["id"])
    assert [r["focus_kind"] for r in desk.rows("SELECT focus_kind FROM sessions")] == [
        "most_useful"
    ]
