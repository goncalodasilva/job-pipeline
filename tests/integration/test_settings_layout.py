"""Settings page layout and re-running the setup guide (M1.20 and M1.21 follow-ups).

Layout: the page's one ``<h1>`` ("Settings") comes first, outside any card; then the
Providers section; then a small ``.empty-state`` card for the parts that arrive later, which
never says "below". A plain GET (htmx or not) focuses only the ``<h1>``.

Re-running the guide (detailed design: the guide "can be re-run from Settings at any time"):
Settings links to the guide's first step. A finished guide reached that way offers "Back to
the app" (ADR-066); visiting the guide and leaving it (``POST /setup/leave``) changes no
stored data and no step status.

Every folder lives under ``tmp_path``; every value is fictional.
"""

import re
from pathlib import Path

import pytest

from app.onboarding.registry import m1_registry
from tests.integration.test_setup_preferences_done import live_statuses
from tests.integration.test_setup_redirect import Element, assert_see_other, outline
from tests.integration.test_setup_reopen_banner import (
    ALL_M1_DONE,
    back_to_app,
    back_to_app_forms,
    class_tokens,
    finished_guide,
    get_ok,
    started,
    stored_settings,
)

pytestmark = pytest.mark.integration

SETTINGS = "/settings"
FIRST_STEP = f"/setup/{m1_registry().first.id}"
RERUN_TEXT = "re-run the setup guide"


def main_elements(page: str) -> list[Element]:
    elements = outline(page)
    mains = [e for e in elements if e.tag == "main"]
    if not mains:  # an htmx response is the main content itself
        return elements
    return [e for e in elements if e.within(mains[0])]


def rerun_links(elements: list[Element]) -> list[Element]:
    return [e for e in elements if e.tag == "a" and RERUN_TEXT in e.text.lower()]


# --- layout --------------------------------------------------------------------------------------


@pytest.mark.parametrize("htmx", [False, True], ids=["page", "htmx"])
def test_settings_heading_then_providers_then_coming_later_card(tmp_path: Path, htmx: bool) -> None:
    finished_guide(tmp_path)
    with started(tmp_path) as run:
        if htmx:
            response = run.client.get(SETTINGS, headers=run.htmx())
            assert response.status_code == 200
        else:
            response = get_ok(run, SETTINGS)
        elements = main_elements(response.text)

    headings = [e for e in elements if e.tag == "h1"]
    assert len(headings) == 1, "one <h1>"
    (heading,) = headings
    assert heading.text == "Settings"
    providers = next(e for e in elements if e.attrs.get("id") == "providers")
    cards = [e for e in elements if "empty-state" in class_tokens(e)]
    assert len(cards) == 1, "one card for the parts that arrive later"
    (card,) = cards

    order = [elements.index(e) for e in (heading, providers, card)]
    assert order == sorted(order), "the h1, then Providers, then the coming-later card"
    assert not heading.within(card), "the h1 leads the page, not the card"
    assert not card.within(providers)
    card_text = " ".join(e.text for e in elements if e.within(card) and e.text)
    assert "later" in card_text.lower(), card_text
    assert not re.search(r"\bbelow\b", card_text, re.IGNORECASE), card_text

    focused = [e for e in elements if "data-autofocus" in e.attrs]
    assert focused == [heading], "only the h1 is focused on a plain load"


# --- re-running the setup guide ------------------------------------------------------------------


def test_settings_links_to_the_first_guide_step(tmp_path: Path) -> None:
    finished_guide(tmp_path)
    with started(tmp_path) as run:
        elements = main_elements(get_ok(run, SETTINGS).text)
    links = rerun_links(elements)
    assert len(links) == 1, [e.text for e in elements if e.tag == "a"]
    assert links[0].attrs.get("href") == FIRST_STEP


def test_rerunning_the_guide_from_settings_and_back_keeps_every_data(tmp_path: Path) -> None:
    finished_guide(tmp_path)
    with started(tmp_path) as run:
        before = stored_settings(run)
        decision = run.decision
        assert live_statuses(run) == ALL_M1_DONE

        (link,) = rerun_links(main_elements(get_ok(run, SETTINGS).text))
        guide = get_ok(run, str(link.attrs.get("href"))).text
        assert back_to_app_forms(guide), "a finished guide offers Back to the app"
        for step in m1_registry().steps:  # walking the guide changes nothing
            get_ok(run, f"/setup/{step.id}")

        assert_see_other(back_to_app(run), "/")
        get_ok(run, "/")
        get_ok(run, SETTINGS)

        assert stored_settings(run) == before, "no setting changed"
        assert live_statuses(run) == ALL_M1_DONE, "no step status changed"
        assert run.decision == decision
