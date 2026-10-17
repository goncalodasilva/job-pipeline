"""Possible duplicates after review (M3.7): Undo of "Not a duplicate", a pair that changed
before it was dismissed, and the region going stale when drafts change on the page.

Builds on ``tests/integration/test_fact_duplicates.py`` (same seeding and helpers). A finished
setup with the fake provider saved; a real SQLite database under ``tmp_path``; no provider
call; every value is fictional.
"""

import json
from pathlib import Path

import pytest

from tests.facts_page import (
    DISMISS_URL,
    DUPLICATES_ID,
    DUPLICATES_URL,
    FIND_DUPLICATES_ID,
    SAID_DISMISSED,
    UNDO,
    announcement,
    button,
    confirm_path,
    facts_app,
    posts_to,
    required,
)
from tests.integration.test_fact_duplicates import (
    PAIR_CHANGED,
    dismissals,
    pairs_shown,
    region,
    seed_bank,
)
from tests.integration.test_setup_redirect import BASE_URL, outline

pytestmark = pytest.mark.integration

UNDO_DISMISS_URL = "/facts/duplicates/undo-dismiss"
SAID_RESTORED = "Suggestion restored."
STALE = "Your drafts changed. Find possible duplicates again to see the list as it is now."
STALE_EVENT = "facts:duplicates-stale"


def test_dismissal_offers_undo_that_lists_the_pair_again(tmp_path: Path) -> None:
    with facts_app(tmp_path) as facts:
        ids = seed_bank(facts)
        pair = (ids["invoices_draft"], ids["invoices"])
        data = {"draft_id": pair[0], "fact_id": pair[1]}

        dismissed = facts.post(DISMISS_URL, data)
        assert announcement(dismissed) == SAID_DISMISSED
        answered = outline(dismissed.text)
        undo = button(answered, required(answered, DUPLICATES_ID), UNDO)
        form = undo.parent
        while form is not None and form.tag != "form":
            form = form.parent
        assert form is not None and posts_to(form) == UNDO_DISMISS_URL
        assert dismissals(facts) == [tuple(sorted(pair))]

        restored = facts.post(UNDO_DISMISS_URL, data)
        assert restored.status_code == 200, (restored.status_code, restored.text[:300])
        assert announcement(restored) == SAID_RESTORED
        shown = outline(restored.text)
        assert required(shown, "fact-duplicates-status").text == SAID_RESTORED
        assert pair in pairs_shown(shown)
        assert dismissals(facts) == []
        assert pair in pairs_shown(region(facts)), "listed again"

        # Undoing again changes nothing; a plain request goes back to the Drafts tab.
        plain = facts.post(UNDO_DISMISS_URL, data, htmx=False)
        assert plain.status_code == 303
        assert dismissals(facts) == []


def test_undo_dismiss_refuses_unknown_ids_and_needs_token(tmp_path: Path) -> None:
    with facts_app(tmp_path) as facts:
        ids = seed_bank(facts)
        for data in (
            {"draft_id": "no-such-fact", "fact_id": ids["routes"]},
            {"draft_id": "../../etc", "fact_id": ids["routes"]},
        ):
            response = facts.post(UNDO_DISMISS_URL, data)
            assert response.status_code == 404
            assert "no-such-fact" not in response.text and "etc" not in response.text
        data = {"draft_id": ids["routes_draft"], "fact_id": ids["routes"]}
        client = facts.run.client
        assert (
            client.post(UNDO_DISMISS_URL, data=data, headers={"origin": BASE_URL}).status_code
            == 403
        )


def test_dismissing_a_pair_that_changed_answers_the_region_without_it(tmp_path: Path) -> None:
    """The draft was confirmed elsewhere after the list was made: 409 with the region as it
    is now (that pair gone), saying so; nothing is stored."""
    with facts_app(tmp_path) as facts:
        ids = seed_bank(facts)
        draft = ids["routes_draft"]
        facts.bank.facts.confirm(draft, expected_version=facts.bank.facts.get(draft).version)

        response = facts.post(DISMISS_URL, {"draft_id": draft, "fact_id": ids["routes"]})

        assert response.status_code == 409
        assert announcement(response) == PAIR_CHANGED
        shown = outline(response.text)
        assert required(shown, "fact-duplicates-status").text == PAIR_CHANGED
        assert (draft, ids["routes"]) not in pairs_shown(shown)
        assert dismissals(facts) == []
        plain = facts.post(DISMISS_URL, {"draft_id": draft, "fact_id": ids["routes"]}, htmx=False)
        assert plain.status_code == 409 and "<" not in plain.text


def test_region_goes_stale_when_drafts_change(tmp_path: Path) -> None:
    """A filled region swaps itself for a line when a draft action answers (or new drafts
    arrive); the line reads nothing, takes no focus and is not announced. The empty region
    and the Find button don't listen."""
    with facts_app(tmp_path) as facts:
        ids = seed_bank(facts)
        tab = facts.tab("drafts")
        find = required(tab, FIND_DUPLICATES_ID)
        assert find.attrs.get("hx-disabled-elt") == "this"
        assert "hx-trigger" not in required(tab, DUPLICATES_ID).attrs, "empty: nothing to refresh"

        filled = required(region(facts), DUPLICATES_ID)
        trigger = filled.attrs.get("hx-trigger") or ""
        assert STALE_EVENT in trigger and "facts:drafts-changed" in trigger
        assert filled.attrs.get("hx-get") == f"{DUPLICATES_URL}?changed=1"

        draft = ids["invoices_draft"]
        acted = facts.act(confirm_path(draft), draft)
        assert acted.status_code == 200
        fired = json.loads(acted.headers["hx-trigger"])
        assert STALE_EVENT in fired and "announce" in fired

        before = facts.snapshot()
        stale = facts.get(DUPLICATES_URL, changed="1")
        assert stale.status_code == 200
        assert "hx-trigger" not in stale.headers, "not announced"
        shown = outline(stale.text)
        line = required(shown, "fact-duplicates-status")
        assert line.text == STALE and "data-autofocus" not in line.attrs
        assert pairs_shown(shown) == set()
        assert "hx-trigger" not in required(shown, DUPLICATES_ID).attrs
        assert facts.snapshot() == before, "changes nothing"
