"""Settings page layout and re-running the setup guide (M1.20 and M1.21 follow-ups).

Layout: the page's one ``<h1>`` comes first, outside any card; then the Providers section.
Since M9.2 the small "Coming later" card is gone (every part is an area page). A plain GET
(htmx or not) focuses only the ``<h1>``.

Re-running the guide (detailed design: the guide "can be re-run from Settings at any time"):
Settings links to the guide's first step. A finished guide reached that way offers "Back to
the app" (ADR-066); visiting the guide and leaving it (``POST /setup/leave``) changes no
stored data and no step status.

Every folder lives under ``tmp_path``; every value is fictional.
"""

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
def test_settings_heading_then_providers_and_no_coming_later_card(
    tmp_path: Path, htmx: bool
) -> None:
    """M9.2 (ADR-131 item 7): Settings is split into area pages and ``/settings`` is Providers
    and AI. It keeps the layout rule of M1.21 (one ``<h1>`` leads the page, outside any card,
    and is the only focus of a plain load) with Providers as the first section after it, and the
    "Coming later" card for parts that had not arrived is gone: every part exists as an area."""
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
    assert heading.text == "Settings", heading.text
    providers = next(e for e in elements if e.attrs.get("id") == "providers")
    sections = [e for e in elements if e.tag == "section" and "settings-section" in class_tokens(e)]
    assert sections and sections[0] is providers, "Providers and AI is the first section"
    cards = [e for e in elements if "empty-state" in class_tokens(e)]
    assert cards == [], "no card for parts that arrive later"
    assert not [e for e in elements if "settings-later" in class_tokens(e)]
    page_words = " ".join(e.text for e in elements if e.text)
    assert "coming later" not in page_words.lower(), page_words

    order = [elements.index(e) for e in (heading, providers)]
    assert order == sorted(order), "the h1, then Providers"
    assert not heading.within(providers), "the h1 leads the page, not the Providers section"

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
