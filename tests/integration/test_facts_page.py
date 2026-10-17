"""The Fact bank page: tabs and the Library (M3.5; AC-M3.6 through the routes).

``/facts`` replaces the placeholder: a page with three tabs as ``?tab=`` links (Library, Drafts
to review with its count, Capture inbox with an honest empty state until M3.7). The Library is
a plain GET form (``q``, ``role``, ``skill``, ``status``) enhanced with htmx
(``GET /facts/library`` answers ``#library-results``), in pages whose links keep the query.
Privacy and the fake provider's label are text badges. Only the page GET is token-free and it
changes nothing; every fragment route needs the token and every POST the origin as well. The
contract's names live in ``tests/facts_page.py``. A real migrated SQLite database in a temp
folder, the frozen clock, the fake provider, no network; every value is fictional.
"""

import logging
from pathlib import Path

import pytest

from app.domain.errors import FactInputInvalid
from app.domain.facts import Privacy
from tests.fact_bank import typed
from tests.facts_page import (
    AREA,
    BADGE_GENERALIZE,
    BADGE_NEVER_USE,
    CAPTURE_EMPTY,
    CAPTURE_FORM_ID,
    CAPTURE_ID,
    CAPTURE_LATER,
    CONFIRM,
    COUNT_ID,
    DRAFTS_URL,
    EMPTY_LIBRARY,
    EMPTY_LINK,
    FILTERS_ID,
    FRAGMENT_URLS,
    HEADING_ID,
    IMPORT_ID,
    IMPORTS_URL,
    LIBRARY_URL,
    NO_MATCH,
    PAGE,
    PAGE_SIZE,
    PAGES_LABEL,
    PRACTICE_BADGE,
    RESULTS_ID,
    SEARCH,
    TABS,
    TABS_LABEL,
    FactsApp,
    announcement,
    button,
    buttons,
    by_id,
    confirm_all_path,
    confirm_path,
    discard_path,
    drafts_tab_text,
    edit_confirm_path,
    empty_with_drafts,
    fact_id_attr,
    fact_ids_shown,
    facts_app,
    focus_targets,
    found_said,
    has_label,
    href_parts,
    inside,
    links,
    named_control,
    option_values,
    required,
    says,
    says_ignoring_case,
    tab_url,
    undo_path,
)
from tests.integration.test_setup_redirect import (
    ORIGIN,
    TOKEN_HEADER,
    Element,
    assert_not_redirected,
    outline,
)
from tests.integration.test_task_routes import database_state
from tests.page_token import page_token

pytestmark = pytest.mark.integration

ROUTES = "Planned 60 delivery routes a week with a team of 3."
INVOICES = "Cut invoice processing time by 35% across 4 warehouses."
DEGREE = "BSc in Industrial Management, Example University, 2018"
NEWSLETTER = "Edited the monthly staff newsletter"
STOCKTAKE = "Ran the quarterly stocktake"
HOSTILE = '"><script id="injected">window.fictional=1</script>'
LOG_MARKERS = ("delivery routes", "invoice processing", "Industrial Management", "Sample Logistics")


# --- helpers -----------------------------------------------------------------------------------


def tab_links(elements: list[Element]) -> dict[str, Element]:
    """The tab links by tab name, from the one labelled tab navigation."""
    navs = [e for e in elements if e.tag == "nav" and e.attrs.get("aria-label") == TABS_LABEL]
    assert len(navs) == 1, f'expected one <nav aria-label="{TABS_LABEL}">, found {len(navs)}'
    found: dict[str, Element] = {}
    for link in (e for e in inside(elements, navs[0]) if e.tag == "a"):
        path, query, _ = href_parts(link)
        assert path == PAGE, link.attrs
        found[query.get("tab", "")] = link
    return found


def current_tab(elements: list[Element]) -> str:
    current = [
        tab for tab, link in tab_links(elements).items() if link.attrs.get("aria-current") == "page"
    ]
    assert len(current) == 1, f"one tab is marked current, found {current}"
    return current[0]


def seed_library(facts: FactsApp) -> dict[str, str]:
    """Two roles, two skills and five facts: three confirmed, one archived, one draft."""
    bank = facts.bank
    widgets = bank.roles.create(employer="Example Widgets Ltd", title="Operations Analyst")
    logistics = bank.roles.create(employer="Sample Logistics SA", title="Junior Planner")
    invoices = typed(bank, INVOICES, role_id=widgets.id, skills=("Forecasting",))
    routes = typed(bank, ROUTES, role_id=logistics.id, skills=("Forecasting", "Scheduling"))
    newsletter = typed(bank, NEWSLETTER, role_id=widgets.id)
    old = typed(bank, STOCKTAKE, role_id=logistics.id, skills=("Scheduling",))
    bank.facts.archive(old.id, expected_version=old.version)
    facts.add_import("imp-seed")
    (draft,) = facts.drafts("imp-seed", DEGREE)
    skills = {skill.name: skill.id for skill in bank.skills.list()}
    return {
        "widgets": widgets.id,
        "logistics": logistics.id,
        "forecasting": skills["Forecasting"],
        "scheduling": skills["Scheduling"],
        "invoices": invoices.id,
        "routes": routes.id,
        "newsletter": newsletter.id,
        "archived": old.id,
        "draft": draft.id,
    }


def library_shows(facts: FactsApp, seeded: dict[str, str], **params: str) -> set[str]:
    """The seeded facts the Library fragment lists for these filters, by seed name."""
    elements = facts.fragment(LIBRARY_URL, **params)
    required(elements, RESULTS_ID)
    names = {fact_id: name for name, fact_id in seeded.items()}
    return {names[i] for i in fact_ids_shown(elements, list(names))}


# --- the page and its tabs ---------------------------------------------------------------------


def test_facts_page_full_and_fragment(tmp_path: Path) -> None:
    """A plain GET is the full page (no token needed); with ``HX-Request`` the same route
    answers only the main content, and then needs the token."""
    with facts_app(tmp_path) as facts:
        full = facts.run.client.get(PAGE)
        assert full.status_code == 200
        assert_not_redirected(full)
        assert "<html" in full.text and page_token(full.text) == facts.token
        elements = outline(full.text)
        headings = [e for e in elements if e.tag == "h1"]
        assert [e.text for e in headings] == [AREA]
        assert headings[0].attrs.get("id") == HEADING_ID
        assert set(tab_links(elements)) == set(TABS), "three tabs as ?tab= links"
        assert current_tab(elements) == "library", "the Library is the default tab"
        assert required(elements, FILTERS_ID).tag == "form", "the placeholder is replaced"
        assert "data-events-url" in full.text, "the page follows the live updates"

        fragment = facts.get(PAGE)
        assert fragment.status_code == 200
        assert "<html" not in fragment.text and "<body" not in fragment.text
        assert facts.token not in fragment.text
        parts = outline(fragment.text)
        heading = required(parts, HEADING_ID)
        assert heading.tag == "h1" and heading.text == AREA
        assert "data-autofocus" in heading.attrs, "a swapped page puts focus on its heading"
        assert set(tab_links(parts)) == set(TABS)
        required(parts, FILTERS_ID)

        refused = facts.run.client.get(PAGE, headers={"HX-Request": "true"})
        assert refused.status_code == 403, "the fragment needs the token"
        for tab in TABS:
            assert facts.run.client.get(tab_url(tab)).status_code == 200, tab
            assert facts.run.client.head(tab_url(tab)).status_code == 200, tab


def test_tab_links_and_draft_count(tmp_path: Path) -> None:
    with facts_app(tmp_path) as facts:
        links_now = tab_links(facts.tab("library"))
        assert links_now["library"].text == TABS["library"]
        assert links_now["drafts"].text == drafts_tab_text(0)
        assert links_now["capture"].text == TABS["capture"]

        seeded = seed_library(facts)
        facts.drafts("imp-seed", ROUTES + " (again)", "English")
        for tab in TABS:
            elements = facts.tab(tab)
            assert current_tab(elements) == tab
            shown = tab_links(elements)
            assert shown["drafts"].text == drafts_tab_text(3), "drafts are counted on every tab"
            assert [e.text for e in elements if e.tag == "h1"] == [AREA]

        # Only drafts count: confirming one and discarding another leaves one.
        draft = facts.bank.facts.get(seeded["draft"])
        facts.bank.facts.confirm(draft.id, expected_version=draft.version)
        assert tab_links(facts.tab("drafts"))["drafts"].text == drafts_tab_text(2)


def test_unknown_tab_shows_library_and_echoes_nothing(tmp_path: Path) -> None:
    with facts_app(tmp_path) as facts:
        for htmx in (False, True):
            response = facts.get(PAGE, htmx=htmx, tab=HOSTILE)
            assert response.status_code == 200
            assert 'id="injected"' not in response.text and "window.fictional" not in response.text
            elements = outline(response.text)
            assert current_tab(elements) == "library"
            required(elements, FILTERS_ID)


def test_capture_tab_has_honest_empty_state(tmp_path: Path) -> None:
    """The inbox (M3.7; it replaced the M3.5 placeholder): the tab shows the note form and,
    with no note yet, says so; nothing of the Library or the import is on it."""
    with facts_app(tmp_path) as facts:
        seed_library(facts)
        response = facts.get(PAGE, tab="capture")
        assert response.status_code == 200
        elements = outline(response.text)
        assert current_tab(elements) == "capture"
        assert any(e.tag == "h2" and e.text == TABS["capture"] for e in elements)
        assert not any(e.tag == "p" and CAPTURE_LATER in e.text for e in elements), (
            "the placeholder is gone"
        )
        inbox = required(elements, CAPTURE_ID)
        form = required(elements, CAPTURE_FORM_ID)
        assert form.tag == "form" and form.within(inbox)
        assert [e.attrs.get("name") for e in elements if e.tag == "textarea"] == ["text"]
        assert CAPTURE_EMPTY in inbox.text
        assert by_id(elements, FILTERS_ID) is None and by_id(elements, IMPORT_ID) is None


# --- the Library -------------------------------------------------------------------------------


def test_empty_state_offers_import(tmp_path: Path) -> None:
    with facts_app(tmp_path) as facts:
        for elements in (facts.tab("library"), facts.fragment(LIBRARY_URL)):
            results = required(elements, RESULTS_ID)
            assert says(elements, EMPTY_LIBRARY, results), sorted(
                e.text for e in inside(elements, results) if e.tag == "p"
            )
            (offer,) = links(elements, EMPTY_LINK, results)
            path, query, anchor = href_parts(offer)
            assert (path, query.get("tab"), anchor) == (PAGE, "drafts", IMPORT_ID)
            assert not says(elements, NO_MATCH, results)

        # The import form is where that link lands.
        assert required(facts.tab("drafts"), IMPORT_ID) is not None

        # Drafts alone are not facts yet: the Library says so and leads to them.
        facts.add_import("imp-seed")
        facts.drafts("imp-seed", DEGREE)
        elements = facts.fragment(LIBRARY_URL)
        assert says(elements, empty_with_drafts(1), required(elements, RESULTS_ID))

        # With a fact, a search that finds nothing says so instead.
        typed(facts.bank, ROUTES)
        elements = facts.fragment(LIBRARY_URL)
        assert not says(elements, EMPTY_LIBRARY) and not says(elements, NO_MATCH)
        elements = facts.fragment(LIBRARY_URL, q="xylophone")
        results = required(elements, RESULTS_ID)
        assert says(elements, NO_MATCH, results) and not says(elements, EMPTY_LIBRARY, results)
        assert not links(elements, EMPTY_LINK, results)


def test_library_fragment_needs_token(tmp_path: Path) -> None:
    with facts_app(tmp_path) as facts:
        seed_library(facts)
        client = facts.run.client
        wrong = "x" * len(facts.token)
        for path in FRAGMENT_URLS:
            for label, headers in {
                "no token": {},
                "htmx, no token": {"HX-Request": "true"},
                "wrong token": {TOKEN_HEADER: wrong, "HX-Request": "true"},
            }.items():
                response = client.get(path, headers=headers)
                assert response.status_code == 403, (path, label, response.status_code)
                assert ROUTES not in response.text and facts.token not in response.text
            # The same with the token: a fragment, with or without HX-Request.
            for headers in (facts.run.run.with_token(), facts.run.run.htmx()):
                response = client.get(path, headers=headers)
                assert response.status_code == 200, (path, response.status_code)
                assert "<html" not in response.text and facts.token not in response.text
        elements = facts.fragment(LIBRARY_URL)
        assert required(elements, RESULTS_ID) is elements[0], "the fragment is #library-results"
        assert ROUTES in elements[0].text


def test_library_form_is_a_plain_get_form_enhanced_with_htmx(tmp_path: Path) -> None:
    with facts_app(tmp_path) as facts:
        seeded = seed_library(facts)
        elements = facts.tab("library", q="routes", role=seeded["logistics"], status="confirmed")
        form = required(elements, FILTERS_ID)
        assert form.tag == "form"
        assert (form.attrs.get("method") or "get").lower() == "get"
        assert form.attrs.get("action") == PAGE, "it works without scripts"
        assert form.attrs.get("hx-get") == LIBRARY_URL
        assert form.attrs.get("hx-target") == f"#{RESULTS_ID}"
        assert named_control(elements, form, "tab").attrs.get("value") == "library"
        button(elements, form, SEARCH)

        search = named_control(elements, form, "q")
        assert search.tag == "input" and search.attrs.get("value") == "routes"
        selects = {
            name: named_control(elements, form, name) for name in ("role", "skill", "status")
        }
        for name, control in {"q": search, **selects}.items():
            assert has_label(elements, control), f"{name} has a visible label"
        assert all(control.tag == "select" for control in selects.values())

        roles = option_values(elements, selects["role"])
        assert set(roles) == {"", seeded["widgets"], seeded["logistics"]}
        assert "Example Widgets Ltd" in roles[seeded["widgets"]]
        assert "Operations Analyst" in roles[seeded["widgets"]]
        skills = option_values(elements, selects["skill"])
        assert skills[seeded["forecasting"]] == "Forecasting"
        assert set(skills) == {"", seeded["forecasting"], seeded["scheduling"]}
        statuses = option_values(elements, selects["status"])
        assert {"confirmed", "draft", "archived"} <= set(statuses)
        assert all(text for text in statuses.values()), "every status option has words"

        chosen = {
            name: [
                e.attrs.get("value")
                for e in inside(elements, control)
                if e.tag == "option" and "selected" in e.attrs
            ]
            for name, control in selects.items()
        }
        assert chosen["role"] == [seeded["logistics"]] and chosen["status"] == ["confirmed"]
        assert chosen["skill"] in ([], [""]), "no skill was chosen"
        # The page itself shows the filtered results (no scripts needed).
        results = required(elements, RESULTS_ID)
        assert fact_ids_shown(elements, list(seeded.values()), results) == [seeded["routes"]]


def test_ac_m3_6_library_search_and_filters_combine(tmp_path: Path) -> None:
    with facts_app(tmp_path) as facts:
        seeded = seed_library(facts)
        confirmed = {"invoices", "routes", "newsletter"}
        cases: list[tuple[dict[str, str], set[str]]] = [
            ({}, confirmed),
            ({"q": "routes"}, {"routes"}),
            ({"q": "Example Widgets"}, {"invoices", "newsletter"}),
            ({"role": seeded["widgets"]}, {"invoices", "newsletter"}),
            ({"skill": seeded["forecasting"]}, {"invoices", "routes"}),
            ({"status": "archived"}, {"archived"}),
            ({"status": "draft"}, {"draft"}),
            ({"status": "confirmed"}, confirmed),
            ({"role": seeded["logistics"], "skill": seeded["forecasting"]}, {"routes"}),
            (
                {"q": "invoice", "role": seeded["widgets"], "skill": seeded["forecasting"]},
                {"invoices"},
            ),
            ({"q": "invoice", "role": seeded["logistics"]}, set()),
            ({"skill": seeded["scheduling"], "status": "archived"}, {"archived"}),
            ({"q": "", "role": "", "skill": "", "status": ""}, confirmed),
            ({"role": "nope"}, set()),
        ]
        for params, expected in cases:
            assert library_shows(facts, seeded, **params) == expected, params
            # The page without scripts gives the same answer.
            elements = facts.tab("library", **params)
            results = required(elements, RESULTS_ID)
            names = {fact_id: name for name, fact_id in seeded.items()}
            shown = {names[i] for i in fact_ids_shown(elements, list(names), results)}
            assert shown == expected, ("page", params)

        # What a fact shows: its wording, role and skills, and its status in words when it
        # isn't a confirmed fact.
        elements = facts.fragment(LIBRARY_URL, q="routes")
        item = required(elements, fact_id_attr(seeded["routes"]))
        for part in (ROUTES, "Sample Logistics SA", "Junior Planner", "Forecasting", "Scheduling"):
            assert part in item.text, part
        elements = facts.fragment(LIBRARY_URL, status="archived")
        assert says(elements, "Archived", required(elements, fact_id_attr(seeded["archived"])))
        # Operators and markup in the search are literal text.
        for text in ('routes" OR "invoice', "routes*", HOSTILE):
            response = facts.get(LIBRARY_URL, q=text)
            assert response.status_code == 200, text
            assert 'id="injected"' not in response.text


def test_library_announces_result_count(tmp_path: Path) -> None:
    """A search by htmx tells the live region what it found and leaves focus where the user
    is typing."""
    with facts_app(tmp_path) as facts:
        seeded = seed_library(facts)
        for params, total in (
            ({}, 3),
            ({"q": "routes"}, 1),
            ({"role": seeded["widgets"]}, 2),
            ({"q": "xylophone"}, 0),
        ):
            response = facts.get(LIBRARY_URL, **params)
            assert response.status_code == 200
            assert announcement(response) == found_said(total), params
            elements = outline(response.text)
            assert focus_targets(elements) == [], "focus stays in the filter being used"
            if total:
                count = required(elements, COUNT_ID)
                assert str(total) in count.text.split(), count.text


def test_library_paging_links_keep_the_query(tmp_path: Path) -> None:
    with facts_app(tmp_path) as facts:
        role = facts.bank.roles.create(employer="Example Widgets Ltd", title="Operations Analyst")
        total = PAGE_SIZE + 5
        made = [
            typed(facts.bank, f"Reviewed supplier report number {n}", role_id=role.id).id
            for n in range(total)
        ]
        typed(facts.bank, NEWSLETTER)  # outside the filter
        query = {"q": "supplier report", "role": role.id, "status": "confirmed"}

        def pages(elements: list[Element]) -> dict[str, Element]:
            navs = [
                e for e in elements if e.tag == "nav" and e.attrs.get("aria-label") == PAGES_LABEL
            ]
            assert len(navs) == 1, f'one <nav aria-label="{PAGES_LABEL}">, found {len(navs)}'
            return {e.text: e for e in inside(elements, navs[0]) if e.tag == "a"}

        for first in (facts.tab("library", **query), facts.fragment(LIBRARY_URL, **query)):
            results = required(first, RESULTS_ID)
            assert len(fact_ids_shown(first, made, results)) == PAGE_SIZE
            assert str(total) in required(first, COUNT_ID).text.split()
            paging = pages(first)
            assert "Next" in paging and "Previous" not in paging
            path, sent, _ = href_parts(paging["Next"])
            assert path == PAGE, "the link works without scripts"
            assert sent.get("tab") == "library" and sent.get("page") == "2"
            assert {name: sent.get(name) for name in query} == query, "the query is kept"

        on_first = set(fact_ids_shown(first, made))
        second = facts.fragment(LIBRARY_URL, **query, page="2")
        on_second = set(fact_ids_shown(second, made))
        assert len(on_second) == 5 and not on_first & on_second
        assert on_first | on_second == set(made)
        paging = pages(second)
        assert "Previous" in paging and "Next" not in paging
        path, sent, _ = href_parts(paging["Previous"])
        assert path == PAGE and sent.get("page", "1") == "1"
        assert {name: sent.get(name) for name in query} == query

        # A page that isn't one is the first page, never an error.
        for page in ("0", "-3", "many", HOSTILE, "999"):
            response = facts.get(LIBRARY_URL, **query, page=page)
            assert response.status_code == 200, page
            assert 'id="injected"' not in response.text
        # One page needs no paging links.
        elements = facts.fragment(LIBRARY_URL, q="newsletter")
        assert not [e for e in elements if e.tag == "a" and e.text in ("Next", "Previous")]


def test_library_unknown_status_is_422_tied_to_the_field(tmp_path: Path) -> None:
    """``FactInputInvalid`` answers 422 with its message tied to its field (spec 2.11)."""
    with facts_app(tmp_path) as facts:
        seed_library(facts)
        from app.services.fact_library import LibraryQuery

        with pytest.raises(FactInputInvalid) as refused:
            facts.bank.library.search(LibraryQuery(status="fictional"))  # type: ignore[arg-type]
        assert refused.value.field == "status"
        message = refused.value.user_message

        for htmx in (True, False):
            response = facts.get(PAGE, htmx=htmx, tab="library", status="fictional")
            assert response.status_code == 422, (htmx, response.status_code)
            elements = outline(response.text)
            form = required(elements, FILTERS_ID)
            status = named_control(elements, form, "status")
            assert status.attrs.get("aria-invalid") == "true"
            described = [
                required(elements, i) for i in (status.attrs.get("aria-describedby") or "").split()
            ]
            assert message in [e.text for e in described], "the message is tied to the field"
            assert "fictional" not in response.text.replace("window.fictional", "")
            assert ROUTES not in response.text, "no results for a refused filter"
        response = facts.get(PAGE, tab="library", status=HOSTILE)
        assert response.status_code == 422 and 'id="injected"' not in response.text
        assert facts.get(LIBRARY_URL, status="fictional").status_code == 422


def test_privacy_shown_as_text_badges(tmp_path: Path) -> None:
    with facts_app(tmp_path) as facts:
        bank = facts.bank
        normal = typed(bank, ROUTES)
        never = typed(bank, INVOICES)
        general = typed(bank, NEWSLETTER)
        bank.facts.set_privacy(never.id, Privacy.NEVER_USE, expected_version=never.version)
        bank.facts.set_privacy(general.id, Privacy.GENERALIZE, expected_version=general.version)

        for elements in (facts.tab("library"), facts.fragment(LIBRARY_URL)):
            expected = {
                normal.id: (False, False),
                never.id: (True, False),
                general.id: (False, True),
            }
            for fact_id, badges in expected.items():
                item = required(elements, fact_id_attr(fact_id))
                found = (
                    says_ignoring_case(elements, BADGE_NEVER_USE, item),
                    says_ignoring_case(elements, BADGE_GENERALIZE, item),
                )
                assert found == badges, (fact_id, found)


def test_practice_badge_kept_in_library_after_confirmation(tmp_path: Path) -> None:
    """ADR-089 item 8: what the fake provider made keeps its label once confirmed, also
    after the user rewords it; what the user typed never has it."""
    with facts_app(tmp_path) as facts:
        from app.services.facts import FactInput

        bank = facts.bank
        facts.add_import("imp-seed")
        confirmed, edited = facts.drafts("imp-seed", ROUTES, INVOICES)
        bank.facts.confirm(confirmed.id, expected_version=confirmed.version)
        bank.facts.edit_and_confirm(
            edited.id,
            FactInput(text="Cut invoice processing time across 4 warehouses."),
            expected_version=edited.version,
        )
        own = typed(bank, NEWSLETTER)
        assert bank.facts.get(edited.id).origin_provider == "fake"

        for elements in (facts.tab("library"), facts.fragment(LIBRARY_URL)):
            for fact_id, labelled in ((confirmed.id, True), (edited.id, True), (own.id, False)):
                item = required(elements, fact_id_attr(fact_id))
                assert says(elements, PRACTICE_BADGE, item) is labelled, fact_id
            # The Library has no draft actions.
            results = required(elements, RESULTS_ID)
            assert not buttons(elements, results, CONFIRM)


# --- security ----------------------------------------------------------------------------------


def test_fact_routes_need_token_and_origin(tmp_path: Path) -> None:
    """Every route but the page refuses without the token; the POSTs also without the app's
    own origin. A refusal changes nothing."""
    with facts_app(tmp_path) as facts:
        seeded = seed_library(facts)
        extra, flagged = facts.drafts("imp-seed", ROUTES + " (again)", "English")
        draft = seeded["draft"]
        discarded = facts.bank.facts.discard(flagged.id, expected_version=flagged.version)
        token = facts.token
        refused = {
            "no token, no origin": {},
            "origin only": {"origin": ORIGIN},
            "token only": {TOKEN_HEADER: token},
            "wrong token": {TOKEN_HEADER: "x" * len(token), "origin": ORIGIN},
            "another site's origin": {TOKEN_HEADER: token, "origin": "http://evil.example"},
            "origin null": {TOKEN_HEADER: token, "origin": "null"},
        }
        version = {"version": str(facts.bank.facts.get(draft).version)}
        posts: dict[str, dict[str, str]] = {
            confirm_path(draft): version,
            edit_confirm_path(draft): {**version, "text": "Reworded by another site"},
            discard_path(draft): version,
            undo_path(discarded.id): {"version": str(discarded.version)},
            confirm_all_path("imp-seed"): {f"version:{draft}": version["version"]},
        }
        before = facts.snapshot()
        for label, headers in refused.items():
            for path, data in posts.items():
                for htmx in ({}, {"HX-Request": "true"}):
                    response = facts.run.client.post(path, data=data, headers={**headers, **htmx})
                    assert response.status_code == 403, (label, path, response.status_code)
                    assert token not in response.text and DEGREE not in response.text
        assert facts.snapshot() == before, "a refused request changes nothing"

        # None is a page: a GET never reaches a POST route, with or without the token.
        for path in posts:
            assert facts.run.client.get(path).status_code in (403, 404, 405), path
            got = facts.run.client.get(path, headers=facts.run.run.with_token())
            assert got.status_code in (404, 405), (path, got.status_code)
        assert facts.snapshot() == before

        # The same requests with the token and the origin are served: the refusals above
        # were the checks', not a missing route.
        assert (
            facts.post(undo_path(discarded.id), posts[undo_path(discarded.id)]).status_code == 200
        )
        assert (
            facts.post(discard_path(extra.id), {"version": str(extra.version)}).status_code == 200
        )
        assert (
            facts.post(edit_confirm_path(draft), posts[edit_confirm_path(draft)]).status_code == 200
        )
        assert facts.status(draft) == "confirmed"
        assert facts.confirm_all("imp-seed", [flagged.id]).status_code == 200
        assert facts.status(flagged.id) == "confirmed"


def test_facts_page_get_changes_nothing(tmp_path: Path) -> None:
    """Spec 2.9 rule 12: the page and the fragment GETs write nothing, start nothing and call
    no provider, whatever the query."""
    with facts_app(tmp_path) as facts:
        seeded = seed_library(facts)
        facts.add_import("imp-kept", state="uploaded")
        data_dir = tmp_path / "data"
        before = database_state(data_dir)
        client = facts.run.client
        queries: list[dict[str, str]] = [
            {},
            {"tab": "library", "q": "routes", "role": seeded["widgets"], "page": "2"},
            {"tab": "drafts"},
            {"tab": "capture"},
            {"tab": "drafts", "version": "1", "confirm": seeded["draft"], "action": "confirm"},
        ]
        for query in queries:
            assert client.get(PAGE, params=query).status_code == 200, query
            assert client.head(PAGE, params=query).status_code == 200, query
            assert facts.get(PAGE, **query).status_code == 200, query
        for path in FRAGMENT_URLS:
            assert facts.get(path).status_code == 200, path
            assert facts.get(path, q="invoice", status="draft", page="3").status_code == 200, path

        assert database_state(data_dir) == before, "nothing was written"
        assert facts.run.tasks() == [] and facts.run.spy.requests == []
        assert facts.status(seeded["draft"]) == "draft"
        # The page is not a way in for writes either.
        for method in ("post", "put", "delete"):
            response = getattr(client, method)(PAGE, headers=facts.run.run.action())
            assert response.status_code == 405, (method, response.status_code)
        assert database_state(data_dir) == before


def test_no_cv_text_in_logs_after_rendering(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Rendering and acting on facts logs ids and counts only (spec 2.10)."""
    caplog.set_level(logging.DEBUG)
    with facts_app(tmp_path) as facts:
        seeded = seed_library(facts)
        (extra,) = facts.drafts("imp-seed", ROUTES + " at Sample Logistics SA")
        shown = []
        for tab in TABS:
            shown.append(facts.get(PAGE, htmx=False, tab=tab).text)
        for path in FRAGMENT_URLS:
            shown.append(facts.get(path, q="delivery routes").text)
        facts.get(PAGE, tab="library", status="fictional delivery routes")
        facts.act(confirm_path(seeded["draft"]), seeded["draft"])
        facts.act(edit_confirm_path(extra.id), extra.id, text="Planned delivery routes weekly.")
        facts.post(confirm_path(seeded["draft"]), {"version": "999"})  # a stale answer
        facts.post(confirm_path("nobody-delivery routes"), {"version": "1"})
        # Control: the markers are on the pages, where the user's own text belongs.
        assert any("delivery routes" in text for text in shown)
        assert any("Industrial Management" in text for text in shown)
        assert facts.status(seeded["draft"]) == "confirmed", "the actions were served"
        assert facts.status(extra.id) == "confirmed"

    records = [
        f"{record.getMessage()} {record.args!r} {record.exc_text or ''}"
        for record in caplog.records
    ]
    files = [
        path.read_text(encoding="utf-8", errors="replace")
        for path in (tmp_path / "data" / "logs").rglob("*")
        if path.is_file()
    ]
    for where, text in {"log records": "\n".join(records), "log files": "\n".join(files)}.items():
        for marker in LOG_MARKERS:
            assert marker not in text, f"fact text ({marker!r}) found in {where}"


def test_fragment_routes_are_not_pages(tmp_path: Path) -> None:
    """Only ``/facts`` is registered as a page; the fragment routes answer no full page."""
    with facts_app(tmp_path) as facts:
        seed_library(facts)
        for path in (DRAFTS_URL, IMPORTS_URL, LIBRARY_URL):
            response = facts.get(path, htmx=False)
            assert response.status_code == 403, "no token, no fragment"
            served = facts.run.client.get(path, headers=facts.run.run.with_token())
            assert served.status_code == 200 and "<html" not in served.text, path
