"""M9.5 part A: the command palette's two protected fragments (AC-M9.5; ADR-098 item 7;
ADR-131 item 10; detailed design "Cross-cutting UX"; spec 2.9 rules 2, 3 and 12, 2.10).

The routes, the markup hooks and the rules are described in ``tests/palette_pages.py``. Real
SQLite in a temp folder, the app through ``TestClient`` (and the real CLI for the access log), no
network. Every value is fictional.
"""

from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from app.modules.storage import Database
from tests.fakes.clock import FrozenClock
from tests.fakes.ids import SequentialIdFactory
from tests.integration.test_settings_areas import area_app
from tests.job_fixtures import generate_jobs, seed_jobs
from tests.page_token import TOKEN_HEADER_NAME, page_token
from tests.palette_pages import (
    ACTION_HREFS,
    ACTIONS,
    GROUP_HEADINGS,
    HOSTILE_COMPANY,
    HOSTILE_FACT,
    HOSTILE_TITLE,
    KINDS,
    MAX_RECENT,
    MAX_RESULTS,
    OWN_COMPANY,
    RECENT,
    SEARCH,
    WORD,
    actions,
    group_heading,
    groups,
    item_path,
    kinds_in_order,
    make_seeder,
    recents,
    result_pairs,
    results,
    row_counts,
    seed_hostile,
    seed_palette,
)
from tests.running_app import start_app

pytestmark = pytest.mark.integration

HTMX = {"HX-Request": "true"}


@contextmanager
def palette_app(tmp_path: Path, *, hostile: bool = False) -> Any:
    """The app on a finished setup, with the fictional palette rows (or the hostile ones)."""
    with area_app(tmp_path) as run:
        state = run.app.state
        seed = seed_hostile if hostile else seed_palette
        seeded = seed(
            state.workspace.db, state.applications, state.application_statuses, state.facts
        )
        yield run, seeded


def search(run: Any, query: str) -> Any:
    return run.client.get(SEARCH, params={"q": query}, headers=run.htmx())


def recent(run: Any, *paths: str) -> Any:
    return run.client.get(RECENT, params=[("path", p) for p in paths], headers=run.htmx())


# --- search ---------------------------------------------------------------------------------------


def test_palette_search_finds_jobs_companies_applications_facts(tmp_path: Path) -> None:
    with palette_app(tmp_path) as (run, seeded):
        response = search(run, WORD)
        assert response.status_code == 200
        assert "<html" not in response.text and run.token not in response.text
        # Jobs (jobs_fts: the title), the application of one of them, and the fact (facts_fts).
        pairs = result_pairs(response.text)
        assert sorted(pairs) == sorted(
            [
                ("job", item_path("job", seeded.wrangler_job)),
                ("job", item_path("job", seeded.rigger_job)),
                ("application", item_path("application", seeded.application)),
                ("fact", item_path("fact", seeded.fact)),
            ]
        )
        by_href = {href: e for e in results(response.text) for href in [e.attrs["href"]]}
        assert "Quokka Wrangler" in by_href[item_path("job", seeded.wrangler_job)].text
        assert "Quokka Rigger" in by_href[item_path("job", seeded.rigger_job)].text
        assert "quokka sanctuary" in by_href[item_path("fact", seeded.fact)].text

        # Companies are found by name, case-insensitively, as whole words.
        for typed, company, other in (
            ("lantern", seeded.own_company, seeded.other_company),
            ("HARBOR", seeded.other_company, seeded.own_company),
        ):
            found = search(run, typed)
            assert found.status_code == 200
            company_pairs = [(k, h) for k, h in result_pairs(found.text) if k == "company"]
            assert company_pairs == [("company", item_path("company", company))]
            assert item_path("company", other) not in found.text

        # A query that matches nothing: no result, and an empty state with words.
        nothing = search(run, "zzzxqwv")
        assert nothing.status_code == 200
        assert results(nothing.text) == []
        assert any(e.text for e in _with_attr(nothing.text, "data-palette-empty"))


def test_palette_results_are_grouped_by_kind_with_headings_and_a_count(tmp_path: Path) -> None:
    with palette_app(tmp_path) as (run, _seeded):
        markup = search(run, WORD).text
        order = kinds_in_order(markup)
        assert set(order) == {"job", "application", "fact"}
        # Every kind is one contiguous run.
        collapsed = [kind for i, kind in enumerate(order) if i == 0 or order[i - 1] != kind]
        assert len(collapsed) == len(set(collapsed)), order
        # Each result sits in the group of its kind, and a group has its heading.
        by_kind = {str(g.attrs["data-palette-group"]): g for g in groups(markup)}
        for result in results(markup):
            kind = str(result.attrs["data-kind"])
            assert kind in by_kind and result.within(by_kind[kind])
        for kind in ("job", "application", "fact"):
            assert group_heading(by_kind[kind], markup) == GROUP_HEADINGS[kind]
        # The count is in a status element, for the screen reader (htmx-ui: announce swaps).
        status = [e for e in _with_attr(markup, "data-palette-status")]
        assert len(status) == 1 and status[0].attrs.get("role") == "status"
        assert str(len(results(markup))) in status[0].text


def test_palette_search_returns_at_most_20_results(tmp_path: Path) -> None:
    with area_app(tmp_path) as run:
        seed_jobs(run.app.state.workspace.db, generate_jobs(30))
        response = search(run, "greenhouse")
        assert response.status_code == 200
        assert len(results(response.text)) == MAX_RESULTS
        # Still grouped: one contiguous run per kind.
        order = kinds_in_order(response.text)
        collapsed = [kind for i, kind in enumerate(order) if i == 0 or order[i - 1] != kind]
        assert len(collapsed) == len(set(collapsed)), order
        assert set(order) <= set(KINDS)
        assert {str(g.attrs["data-palette-group"]) for g in groups(response.text)} >= set(order)


def test_palette_blank_query_lists_only_the_actions(tmp_path: Path) -> None:
    with palette_app(tmp_path) as (run, _seeded):
        for blank in ("", "   "):
            response = search(run, blank)
            assert response.status_code == 200
            assert results(response.text) == []
            assert recents(response.text) == []
            assert_actions(response.text)


def test_palette_actions_are_filtered_by_the_typed_words(tmp_path: Path) -> None:
    with palette_app(tmp_path) as (run, _seeded):
        markup = search(run, "session").text
        found = {str(e.attrs["data-action"]) for e in actions(markup)}
        assert "start-session" in found
        assert "add-job" not in found and "new-fact" not in found and "new-event" not in found


@pytest.mark.parametrize(
    "query",
    [
        '"',
        '""',
        '"quokka',
        "*",
        "quokka*",
        "NEAR(",
        "NEAR(quokka rigger, 2)",
        "quokka AND",
        "OR",
        "NOT",
        "-quokka",
        "title:quokka",
        "(",
        ")",
        "'; DROP TABLE jobs; --",
        "quokka\\",
        "héron ünïcode",
        "{}[]^~",
    ],
)
def test_palette_hostile_query_text_never_fails(tmp_path: Path, query: str) -> None:
    with palette_app(tmp_path) as (run, _seeded):
        before = row_counts(run.app.state.workspace.db)
        response = search(run, query)
        assert response.status_code == 200, query
        assert "<html" not in response.text
        assert row_counts(run.app.state.workspace.db) == before


def test_palette_overlong_query_is_bounded_not_a_500(tmp_path: Path) -> None:
    with palette_app(tmp_path) as (run, _seeded):
        response = search(run, "quokka " * 700)
        assert response.status_code == 200


def test_palette_includes_drafts_and_removed_jobs_but_not_discarded_facts(tmp_path: Path) -> None:
    from app.services.facts import DraftInput, DraftOrigin

    with palette_app(tmp_path) as (run, seeded):
        facts = run.app.state.facts
        draft = facts.create_drafts(DraftOrigin("session"), [DraftInput(text="Quokka draft")])[0]
        with run.app.state.workspace.db.transaction() as conn:
            conn.execute(
                "UPDATE jobs SET triage_state='removed' WHERE id=?", (seeded.wrangler_job,)
            )
        response = search(run, WORD)
        assert response.status_code == 200
        assert item_path("fact", draft.id) in response.text
        assert item_path("job", seeded.wrangler_job) in response.text
        facts.discard(draft.id, expected_version=draft.version)
        assert item_path("fact", draft.id) not in search(run, WORD).text


def test_palette_query_text_is_never_echoed_unescaped(tmp_path: Path) -> None:
    with palette_app(tmp_path) as (run, _seeded):
        response = search(run, "<img src=x onerror=alert(1)>")
        assert response.status_code == 200
        assert "<img" not in response.text.lower()


# --- protection and side effects -----------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [f"{SEARCH}?q=quokka", f"{SEARCH}?q=", f"{RECENT}", f"{RECENT}?path=/jobs/job-pal-0001"],
)
def test_palette_fragment_needs_token_and_writes_nothing(tmp_path: Path, path: str) -> None:
    with palette_app(tmp_path) as (run, _seeded):
        db = run.app.state.workspace.db
        before = row_counts(db)
        data = tmp_path / "data"
        files_before = sorted(p.relative_to(data).as_posix() for p in data.rglob("*"))
        for headers in (
            {},
            HTMX,
            {TOKEN_HEADER_NAME: "wrong-token", **HTMX},
            {TOKEN_HEADER_NAME: ""},
        ):
            refused = run.client.get(path, headers=headers)
            assert refused.status_code == 403, (path, headers)
            assert "quokka" not in refused.text.lower()
        for _ in range(2):
            allowed = run.client.get(path, headers=run.htmx())
            assert allowed.status_code == 200, path
            assert "<html" not in allowed.text and run.token not in allowed.text
        # Rows of every table (tasks, events, usage, the search index) and files are unchanged.
        assert row_counts(db) == before
        assert sorted(p.relative_to(data).as_posix() for p in data.rglob("*")) == files_before


def test_palette_routes_are_not_page_routes(tmp_path: Path) -> None:
    from fastapi.routing import APIRoute

    from app.api.security import PAGE_TAG

    with palette_app(tmp_path) as (run, _seeded):
        palette_routes = [
            r for r in run.app.routes if isinstance(r, APIRoute) and r.path.startswith("/palette/")
        ]
        assert {r.path for r in palette_routes} == {SEARCH, RECENT}
        for route in palette_routes:
            assert PAGE_TAG not in (route.tags or []), route.path
            assert route.methods is not None and route.methods <= {"GET", "HEAD"}
        # A POST is not offered (the fragments only read).
        assert run.client.post(SEARCH, headers=run.action()).status_code in (403, 405)


def test_palette_a_page_visit_carries_the_dialog_without_the_token_in_it(tmp_path: Path) -> None:
    with palette_app(tmp_path) as (run, _seeded):
        page = run.client.get("/board").text
        assert 'id="palette"' in page
        token = page_token(page)
        # The token lives only in the body's hx-headers, never inside the dialog.
        start = page.index('id="palette"')
        end = page.index("</dialog>", start)
        assert token not in page[start:end]


# --- escaping ---------------------------------------------------------------------------------


def test_palette_results_escaped(tmp_path: Path) -> None:
    with palette_app(tmp_path, hostile=True) as (run, seeded):
        response = search(run, "heron")
        assert response.status_code == 200
        text = response.text
        for raw in ("<script>alert", "<img src=x", "<svg onload"):
            assert raw not in text, raw
        assert "&lt;script&gt;" in text
        # The markup is shown as text: the parsed result holds the typed characters.
        titles = " | ".join(e.text for e in results(text))
        assert HOSTILE_TITLE in titles
        assert HOSTILE_FACT.split(" with ")[0] in titles
        assert HOSTILE_COMPANY in titles
        found = {k for k, _h in result_pairs(text)}
        assert found == set(KINDS)
        assert item_path("company", seeded.own_company) in text
        # No element came from the hostile text: the only scripts in the answer are none.
        assert "<script" not in text.lower()


def test_palette_recent_titles_escaped(tmp_path: Path) -> None:
    with palette_app(tmp_path, hostile=True) as (run, seeded):
        response = recent(
            run,
            item_path("job", seeded.wrangler_job),
            item_path("company", seeded.own_company),
            item_path("application", seeded.application),
            item_path("fact", seeded.fact),
        )
        assert response.status_code == 200
        text = response.text
        for raw in ("<script>alert", "<img src=x", "<svg onload"):
            assert raw not in text, raw
        assert len(recents(text)) == 4
        assert HOSTILE_TITLE in " | ".join(e.text for e in recents(text))
        assert "<script" not in text.lower()


# --- recent items -------------------------------------------------------------------------------


def test_palette_recent_resolves_titles_and_keeps_the_order_sent(tmp_path: Path) -> None:
    with palette_app(tmp_path) as (run, seeded):
        paths = [
            item_path("company", seeded.own_company),
            item_path("fact", seeded.fact),
            item_path("job", seeded.wrangler_job),
            item_path("application", seeded.application),
        ]
        response = recent(run, *paths)
        assert response.status_code == 200
        shown = recents(response.text)
        assert [str(e.attrs["href"]) for e in shown] == paths
        assert [str(e.attrs["data-kind"]) for e in shown] == [
            "company",
            "fact",
            "job",
            "application",
        ]
        assert OWN_COMPANY in shown[0].text
        assert "quokka sanctuary" in shown[1].text
        assert "Quokka Wrangler" in shown[2].text
        assert "Quokka Rigger" in shown[3].text
        # The recent group has its heading, and the actions come with it.
        group = {str(g.attrs["data-palette-group"]): g for g in groups(response.text)}
        assert group_heading(group["recent"], response.text) == GROUP_HEADINGS["recent"]
        assert_actions(response.text)
        assert results(response.text) == []


def test_palette_recent_drops_unsafe_foreign_and_unknown_paths(tmp_path: Path) -> None:
    with palette_app(tmp_path) as (run, seeded):
        good = item_path("job", seeded.wrangler_job)
        dropped = [
            "https://evil.example.test/jobs/job-pal-0001",
            "//evil.example.test/jobs/job-pal-0001",
            "/\\evil.example.test/jobs/job-pal-0001",
            "javascript:alert(1)",
            "/jobs/job-pal-9999",
            "/jobs/../settings/data",
            "/jobs/job pal 0001",
            "/jobs/job-pal-0001/extra",
            "/jobs/%2e%2e%2fsettings",
            "/jobs/<script>",
            "/companies/company-nope",
            "/applications/app-nope",
            "/facts/fact-nope/edit",
            "/facts/" + seeded.fact,  # not an item page shape
            "/settings/data",
            "/inbox",
            "/board",
            "/",
            "",
            "jobs/job-pal-0001",
            "/jobs/" + "a" * 500,
            "/jobs/job-pal-0001?x=1",
            "/jobs/job-pal-0001#frag",
        ]
        response = recent(run, *dropped, good)
        assert response.status_code == 200
        assert [str(e.attrs["href"]) for e in recents(response.text)] == [good]
        for text in ("evil.example", "job-pal-9999", "javascript", "settings/data", "nope"):
            assert text not in response.text, text
        # Nothing valid: no recent group, no error.
        empty = recent(run, *dropped)
        assert empty.status_code == 200
        assert recents(empty.text) == []
        assert_actions(empty.text)
        assert recent(run).status_code == 200


def test_palette_recent_shows_at_most_8(tmp_path: Path) -> None:
    with area_app(tmp_path) as run:
        seed_jobs(run.app.state.workspace.db, generate_jobs(12))
        paths = [f"/jobs/job-{i:04d}" for i in range(1, 13)]
        response = recent(run, *paths)
        assert response.status_code == 200
        assert [str(e.attrs["href"]) for e in recents(response.text)] == paths[:MAX_RECENT]
        for e in recents(response.text):
            assert "Fictional Greenhouse Engineer" in e.text
        # A very long list is bounded too (not a 500, still at most 8).
        many = recent(run, *(paths * 20))
        assert many.status_code in (200, 400, 414)
        if many.status_code == 200:
            assert len(recents(many.text)) <= MAX_RECENT


# --- the actions ---------------------------------------------------------------------------------


def assert_actions(markup: str) -> None:
    found = {str(e.attrs["data-action"]): e for e in actions(markup)}
    assert set(found) == set(ACTIONS), set(found)
    for key, label in ACTIONS.items():
        assert label in found[key].text
        href = str(found[key].attrs.get("href"))
        assert href.startswith("/") and not href.startswith("//"), (key, href)
        if key in ACTION_HREFS:
            assert href == ACTION_HREFS[key]
    group = {str(g.attrs["data-palette-group"]): g for g in groups(markup)}
    assert group_heading(group["actions"], markup) == GROUP_HEADINGS["actions"]


def test_palette_action_pages_exist(tmp_path: Path) -> None:
    with palette_app(tmp_path) as (run, _seeded):
        markup = search(run, "").text
        assert len(actions(markup)) == len(ACTIONS)
        for element in actions(markup):
            href = str(element.attrs["href"]).split("?")[0].split("#")[0]
            assert run.client.get(href).status_code == 200, href


# --- the access log -------------------------------------------------------------------------------


def test_palette_query_not_in_access_log(tmp_path: Path) -> None:
    """The real CLI: a typed query (and the paths of recent items) is a GET query string, which
    uvicorn's access record drops whole (ADR-093); the palette must add no log line of its own
    that holds the text, at any level, in the console or the log files."""
    marker = "CanaryPaletteHeron"  # letters only
    app = start_app(tmp_path)
    try:
        clock = FrozenClock(datetime(2026, 1, 5, 9, 0, tzinfo=UTC))
        from app.services.applications import ApplicationService, ApplicationStatuses

        with Database.open(app.data_dir / "app.db") as db:
            seeder = make_seeder(
                db,
                ApplicationService(lambda: db, clock, SequentialIdFactory("palseed")),
                ApplicationStatuses(lambda: db, clock, SequentialIdFactory("palstatus")),
            )
            seeder.job("job-canary", f"{marker} Rigger", f"{marker} Quay Fictional")
        with app.client() as client:
            token = page_token(client.get("/").text)
            headers = {TOKEN_HEADER_NAME: token, "HX-Request": "true"}
            found = client.get(f"{SEARCH}?q={marker}", headers=headers)
            assert found.status_code == 200
            assert marker in found.text  # the app did search for it
            for path in (
                f"{SEARCH}?q={marker}&{marker}={marker}",
                f"{SEARCH}?q=%22{marker}%22+AND+{marker}*",
                f"{RECENT}?path=/jobs/job-canary&path=/jobs/{marker}&{marker}={marker}",
                f"{RECENT}?path=/companies/{marker}",
            ):
                response = client.get(path, headers=headers)
                assert response.status_code == 200, path.split("?")[0]
            # Refused requests carry the query too: the log must not.
            assert client.get(f"{SEARCH}?q={marker}").status_code == 403
            assert client.get(f"{RECENT}?path=/jobs/{marker}").status_code == 403
    finally:
        app.stop()
    output = app.stdout_path.read_text() + app.stderr_path.read_text()
    output += "".join(p.read_text() for p in app.log_dir.glob("*.log*"))
    assert f"GET {SEARCH} HTTP/1.1" in output
    assert f"GET {RECENT} HTTP/1.1" in output
    assert marker not in output
    for fragment in ("?q=", "&q=", "?path=", "&path="):
        assert fragment not in output, fragment
    # Nor is the typed text kept in the database.
    with Database.open(app.data_dir / "app.db") as db:
        tables = list(row_counts(db))  # opens its own read: not inside the one below
        hits = 0
        with db.read() as conn:
            for table in tables:
                if table.startswith(("jobs", "companies", "sqlite")):
                    continue
                for row in conn.execute(f'SELECT * FROM "{table}"').fetchall():  # noqa: S608
                    if marker in repr(tuple(row)):
                        hits += 1
        assert hits == 0


def _with_attr(markup: str, name: str) -> list[Any]:
    from tests.board_pages import elements

    return [e for e in elements(markup) if name in e.attrs]
