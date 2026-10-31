"""M7.7 Coverage tab: rows, order, Start session, Strengthen (AC-M7.5 through the routes).

The assumed markup and routes are listed in ``tests/coverage_pages.py``. A real migrated SQLite
database in a temp folder, the fake provider, the frozen clock, no network; every value is
fictional.
"""

import json
import re

import pytest

from app.domain.facts import FactType
from app.domain.fit import Requirement
from app.services.budget_gate import BlockedAction, ShowWarning
from app.services.coverage_views import NO_RELATED_TEXT
from app.services.facts import FactInput
from tests.coverage_pages import (
    ASKED,
    COVERAGE_URL,
    EMPTY_TEXT,
    KOTLIN,
    ORDER,
    PAGE_SIZE,
    PYTHON,
    ROUTES,
    STRENGTH,
    TAB_URL,
    TERRAFORM,
    TERRAFORM_FACT,
    UNRELATED_FACT,
    WORD,
    cell,
    cluster_id,
    inside_row,
    labels_in_order,
    row_for,
    rows_of,
    seed_coverage,
    start_form,
    strengthen_controls,
)
from tests.facts_page import TABS, TABS_LABEL, form_values, href_parts, outline, posts_to
from tests.integration.test_board import seeder, snapshot
from tests.integration.test_sessions import current, seed_note, service
from tests.refine_harness import refining
from tests.tailoring_pages import seed_summary

pytestmark = pytest.mark.integration

HOSTILE = '<img src=x onerror="alert(1)">'
HOSTILE_SCRIPT = '"><script id="injected-coverage">window.fictional=1</script>'
NOT_FOUND_IDS = ("0123456789ab", "ffffffffffff", "bad%3Cid", "A" * 40)


@pytest.fixture
def desk(tmp_path):
    with refining(tmp_path) as r:
        yield r


@pytest.fixture
def seeded(desk):
    return seed_coverage(seeder(desk), desk.facts)


def fragment(desk, path=COVERAGE_URL, **params):
    response = desk.client.get(path, params=params, headers=desk.run.run.htmx())
    assert response.status_code == 200, (path, response.status_code, response.text[:300])
    assert "<html" not in response.text and desk.run.run.token not in response.text
    return response


def page(desk, **params):
    response = desk.client.get("/facts", params={"tab": "coverage", **params})
    assert response.status_code == 200, response.text[:300]
    assert "<html" in response.text
    return response


def start_request(desk, row_label, parsed=None):
    parsed = parsed or outline(fragment(desk).text)
    row = row_for(parsed, row_label)
    form = start_form(parsed, row)
    values = form_values(parsed, form)
    return (
        row,
        values,
        desk.run.client.post(
            posts_to(form), data=values, headers=desk.run.run.action(**{"HX-Request": "true"})
        ),
    )


def test_coverage_tab_is_the_fourth_tab(desk, seeded):
    parsed = outline(page(desk).text)
    navs = [e for e in parsed if e.tag == "nav" and e.attrs.get("aria-label") == TABS_LABEL]
    assert len(navs) == 1
    tabs = [e for e in parsed if e.tag == "a" and e.within(navs[0])]
    assert len(tabs) == len(TABS) == 4
    assert tabs[-1].text == TABS["coverage"] == "Coverage"
    assert href_parts(tabs[-1])[1] == {"tab": "coverage"}
    assert tabs[-1].attrs.get("aria-current") == "page"
    assert [e for e in parsed if e.attrs.get("id") == "coverage"], "the table is on the page"


def test_ac_m7_5_coverage_rows_show_strength_in_words(desk, seeded):
    symbols = {}
    for body in (fragment(desk).text, page(desk).text):
        parsed = outline(body)
        assert [e for e in parsed if e.attrs.get("id") == "coverage"]
        rows = rows_of(parsed)
        assert len(rows) == len(ORDER)
        for label in ORDER:
            row = row_for(parsed, label)
            strength = STRENGTH[label]
            assert row.attrs["data-strength"] == strength, label
            shown = cell(parsed, row, "strength").text
            assert WORD[strength] in shown.split(), (label, shown)
            symbol = shown.replace(WORD[strength], "").strip()
            assert symbol and not symbol.isalnum(), (
                f"a symbol next to the word, not colour: {shown!r}"
            )
            symbols.setdefault(strength, set()).add(symbol)
            # The row's id is the first 12 hex digits of the SHA-256 of its cluster key.
            assert re.fullmatch(r"[0-9a-f]{12}", row.attrs["data-cluster-id"])
            asked = cell(parsed, row, "asked").text
            jobs, must = ASKED[label]
            if jobs == 1:  # the singular is not specified: "job" or "jobs"
                assert re.search(r"Asked by 1 saved jobs?", asked), (label, asked)
            else:
                assert f"Asked by {jobs} saved jobs" in asked, (label, asked)
            if must:
                assert re.search(
                    rf"\b{must}\b[^.\d]{{0,12}}must-have|must-have[^.\d]{{0,12}}\b{must}\b",
                    asked,
                ), asked
            # Every row can start a session; only Thin rows can be strengthened.
            names = [e.text for e in inside_row(parsed, row) if e.tag == "button"]
            assert "Start session" in names, (label, names)
            assert bool(strengthen_controls(parsed, row)) == (strength == "thin"), label
    assert all(len(found) == 1 for found in symbols.values()), "one symbol per strength"
    assert len({next(iter(found)) for found in symbols.values()}) == 3, "a symbol for each"


def test_coverage_sorted_by_biggest_gap(desk, seeded):
    assert labels_in_order(fragment(desk).text) == list(ORDER)
    assert labels_in_order(page(desk).text) == list(ORDER)
    # Evidence moves a cluster to a later group: two stories make Terraform (demand 6) Strong,
    # so it now follows the Thin Kotlin cluster (demand 2) and precedes Strong Python (1).

    for what in ("a plan", "a checklist"):
        desk.facts.create(
            FactInput(
                type=FactType.STORY,
                situation=f"The depots needed {what}.",
                action=f"Built {what} in Terraform for 2 depots.",
                result="Saved 3 hours a week.",
                skills=("Terraform",),
            )
        )
    expected = [label for label in ORDER if label != TERRAFORM]
    expected.insert(expected.index(PYTHON), TERRAFORM)
    assert expected[-3:] == [KOTLIN, TERRAFORM, PYTHON]
    assert labels_in_order(fragment(desk).text) == expected
    parsed = outline(fragment(desk).text)
    assert row_for(parsed, TERRAFORM).attrs["data-strength"] == "strong"


def test_ac_m7_5_selecting_row_starts_focused_session(desk, seeded):
    parsed = outline(fragment(desk).text)
    row = row_for(parsed, ROUTES)
    expected_id = row.attrs["data-cluster-id"]
    form = start_form(parsed, row)
    values = form_values(parsed, form)
    assert values == {"focus": f"cluster:{expected_id}", "length": "3"}, values
    row, values, response = start_request(desk, ROUTES, parsed)
    assert response.status_code == 200, response.text[:300]
    sid = response.headers["HX-Redirect"].rsplit("/", 1)[-1]
    assert response.headers["HX-Redirect"] == f"/sessions/{sid}"

    stored = desk.run.rows("SELECT focus_kind, focus_ref, length, state FROM sessions")
    assert stored == [
        {"focus_kind": "cluster", "focus_ref": expected_id, "length": 3, "state": "open"}
    ]
    view = current(desk, sid)
    assert desk.run.wait_terminal(view.task.id).status.value == "succeeded"
    # The fake request log shows the cluster's requirements as the item, and no other cluster's.
    requests = [r for r in desk.run.spy.requests if r.task == "enrichment_question"]
    assert len(requests) == 1
    assert ROUTES in requests[0].user
    for other in ORDER:
        if other != ROUTES:
            assert other not in requests[0].user, other
    questions = desk.run.rows("SELECT kind, cluster_key FROM enrichment_questions")
    assert [q["kind"] for q in questions] == ["cluster"]
    assert cluster_id(questions[0]["cluster_key"]) == expected_id
    assert service(desk).view(sid).session.length == 3


def test_coverage_start_form_without_script_redirects_to_the_session(desk, seeded):
    parsed = outline(fragment(desk).text)
    form = start_form(parsed, row_for(parsed, PYTHON))  # Strong rows can start a session too
    values = form_values(parsed, form)
    response = desk.run.client.post(
        posts_to(form), data=values, headers=desk.run.run.action(), follow_redirects=False
    )
    assert response.status_code == 303 and response.headers["location"].startswith("/sessions/")
    assert desk.run.rows("SELECT focus_kind FROM sessions") == [{"focus_kind": "cluster"}]


def test_strengthen_lists_related_facts_with_refine_links(desk, seeded):
    parsed = outline(fragment(desk).text)
    thin = row_for(parsed, TERRAFORM)
    (control,) = strengthen_controls(parsed, thin)
    path = f"{COVERAGE_URL}/{thin.attrs['data-cluster-id']}"
    assert control.attrs.get("hx-get") == path
    detail = outline(fragment(desk, path).text)
    (section,) = [e for e in detail if e.attrs.get("id") == "coverage-detail"]
    (heading,) = [e for e in detail if e.attrs.get("id") == "coverage-detail-heading"]
    assert TERRAFORM in heading.text
    assert "data-autofocus" in heading.attrs and heading.attrs.get("tabindex") == "-1"
    items = [e for e in detail if e.tag == "li" and "data-fact-id" in e.attrs]
    assert [i.attrs["data-fact-id"] for i in items] == [seeded["terraform"]]
    assert TERRAFORM_FACT in items[0].text and UNRELATED_FACT not in section.text
    refine = [
        e for e in detail if e.tag == "a" and e.within(items[0]) and e.text == "Refine with AI"
    ]
    assert len(refine) == 1
    assert href_parts(refine[0]) == (f"/facts/{seeded['terraform']}/edit", {}, "fact-refine")
    # The one-story cluster is Thin as well and names its single story.
    kotlin = row_for(parsed, KOTLIN)
    again = outline(fragment(desk, f"{COVERAGE_URL}/{kotlin.attrs['data-cluster-id']}").text)
    assert [
        e.attrs["data-fact-id"] for e in again if e.tag == "li" and "data-fact-id" in e.attrs
    ] == [seeded["kotlin"]]
    # The editor page the link leads to has the Refine with AI panel.
    editor = desk.client.get(f"/facts/{seeded['terraform']}/edit")
    assert editor.status_code == 200 and 'id="fact-refine"' in editor.text


def test_coverage_unknown_cluster_id_fixed_404(desk, seeded):
    answers = set()
    for bad in NOT_FOUND_IDS:
        response = desk.client.get(f"{COVERAGE_URL}/{bad}", headers=desk.run.run.htmx())
        assert response.status_code == 404, (bad, response.status_code)
        assert response.headers["content-type"].startswith("text/plain")
        assert bad not in response.text and response.text, bad
        answers.add(response.text)
    assert len(answers) == 1, "the same fixed answer for every id"
    # An id that existed once but names no cluster any more answers the same.
    for row in rows_of(outline(fragment(desk).text)):
        if row.attrs["data-strength"] == "thin":
            stale = row.attrs["data-cluster-id"]
    desk.run.execute("UPDATE jobs SET triage_state = 'removed'")
    gone = desk.client.get(f"{COVERAGE_URL}/{stale}", headers=desk.run.run.htmx())
    assert gone.status_code == 404 and gone.text in answers
    # A bad id with no token is refused before it is looked up.
    assert desk.client.get(f"{COVERAGE_URL}/0123456789ab").status_code == 403


def test_coverage_empty_state_without_jobs(desk):
    desk.typed("Planned 60 delivery routes a week with a team of 3.")  # facts alone: no demand
    for body in (fragment(desk).text, page(desk).text):
        parsed = outline(body)
        coverage = [e for e in parsed if e.attrs.get("id") == "coverage"]
        assert len(coverage) == 1
        assert EMPTY_TEXT in {e.text for e in parsed if e.within(coverage[0])} | {coverage[0].text}
        assert not rows_of(parsed) and not [e for e in parsed if e.tag == "table"]
        assert not [e for e in parsed if e.tag == "button" and e.text == "Start session"]


def test_coverage_pages_of_fifty(desk):
    seed = seeder(desk)
    seed.job("cov-many", "Depot Clerk", "Harbor Kite Fictional")
    seed_summary(
        seed.db,
        "cov-many",
        tuple(Requirement(f"many-{n:02d}", "must_have", f"Zorblax{n:02d}") for n in range(51)),
        summary_id="summary-many",
    )
    first = labels_in_order(fragment(desk).text)
    assert first == [f"Zorblax{n:02d}" for n in range(PAGE_SIZE)]
    assert labels_in_order(fragment(desk, page="2").text) == ["Zorblax50"]
    assert labels_in_order(fragment(desk, page="99").text) == ["Zorblax50"], "clamped"
    parsed = outline(page(desk).text)
    navs = [e for e in parsed if e.tag == "nav" and e.attrs.get("aria-label") == "Coverage pages"]
    assert len(navs) == 1
    queries = [href_parts(a)[1] for a in parsed if a.tag == "a" and a.within(navs[0])]
    assert {"tab": "coverage", "page": "2"} in queries


def test_coverage_escaped(desk):
    seed = seeder(desk)
    seed.job("cov-hostile", "Depot Clerk", "Harbor Kite Fictional")
    seed_summary(
        seed.db,
        "cov-hostile",
        (
            Requirement("hostile-1", "must_have", HOSTILE),
            Requirement("hostile-2", "key", HOSTILE_SCRIPT),
            Requirement("hostile-3", "key", "Experience with Terraform"),
        ),
        summary_id="summary-hostile",
    )
    desk.typed(HOSTILE_SCRIPT + " Terraform", skills=("Terraform",))
    desk.typed(HOSTILE + " Terraform", skills=("Terraform",))
    bodies = [fragment(desk).text, page(desk).text]
    terraform = next(r for r in rows_of(outline(bodies[0])) if r.attrs["data-strength"] == "thin")
    bodies.append(fragment(desk, f"{COVERAGE_URL}/{terraform.attrs['data-cluster-id']}").text)
    for body in bodies:
        assert HOSTILE not in body and HOSTILE_SCRIPT not in body
        assert "<script id=" not in body.replace("&lt;script", "")
        assert not [e for e in outline(body) if e.attrs.get("id") == "injected-coverage"]
        assert not [e for e in outline(body) if e.tag == "img" and "onerror" in e.attrs]
    assert "&lt;img" in bodies[0] and "&lt;img" in bodies[2]


def test_coverage_fragment_needs_token(desk, seeded):
    wrong = "x" * len(desk.run.run.token)
    thin = next(
        r for r in rows_of(outline(fragment(desk).text)) if r.attrs["data-strength"] == "thin"
    )
    paths = (COVERAGE_URL, f"{COVERAGE_URL}/{thin.attrs['data-cluster-id']}")
    for path in paths:
        for label, headers in {
            "no token": {},
            "htmx, no token": {"HX-Request": "true"},
            "wrong token": {"X-JP-Token": wrong, "HX-Request": "true"},
        }.items():
            response = desk.client.get(path, headers=headers)
            assert response.status_code == 403, (path, label, response.status_code)
            assert TERRAFORM not in response.text and desk.run.run.token not in response.text
        for headers in (desk.run.run.with_token(), desk.run.run.htmx()):
            response = desk.client.get(path, headers=headers)
            assert response.status_code == 200 and "<html" not in response.text
    # Only the tab is a page: token-free GET and HEAD; the same address with htmx needs the token.
    assert desk.client.get(TAB_URL).status_code == 200
    assert desk.client.head(TAB_URL).status_code == 200
    assert desk.client.get(TAB_URL, headers={"HX-Request": "true"}).status_code == 403
    for method in ("post", "put", "delete"):
        response = getattr(desk.client, method)(COVERAGE_URL, headers=desk.run.run.action())
        assert response.status_code in (404, 405), (method, response.status_code)


def test_coverage_gets_write_nothing(desk, seeded):
    parsed = outline(fragment(desk).text)
    thin = [
        r.attrs["data-cluster-id"] for r in rows_of(parsed) if r.attrs["data-strength"] == "thin"
    ]
    before = snapshot(desk)
    for _ in range(2):
        for method in ("get", "head"):
            assert getattr(desk.client, method)(TAB_URL).status_code == 200
        assert desk.client.get(TAB_URL, headers=desk.run.run.htmx()).status_code == 200
        assert desk.client.get(COVERAGE_URL, headers=desk.run.run.htmx()).status_code == 200
        assert (
            desk.client.get(COVERAGE_URL, params={"page": "2"}, headers=desk.run.run.htmx())
        ).status_code == 200
        for cid in thin:
            path = f"{COVERAGE_URL}/{cid}"
            assert desk.client.get(path, headers=desk.run.run.htmx()).status_code == 200
        assert (
            desk.client.get(f"{COVERAGE_URL}/0123456789ab", headers=desk.run.run.htmx())
        ).status_code == 404
    assert snapshot(desk) == before, "nothing was written"
    assert desk.run.tasks() == [] and desk.run.spy.requests == []
    assert desk.run.rows("SELECT * FROM sessions") == []


# --- review fixes: refusals, detail announce and close, paging announce -----------------------

SLOT_TARGET = {"HX-Target": "coverage-start"}  # what htmx sends for the row form's target


def seed_many(desk):
    seed = seeder(desk)
    seed.job("cov-many", "Depot Clerk", "Harbor Kite Fictional")
    seed_summary(
        seed.db,
        "cov-many",
        tuple(Requirement(f"many-{n:02d}", "must_have", f"Zorblax{n:02d}") for n in range(51)),
        summary_id="summary-many",
    )


def start_from_row(desk, parsed, label, *, target=True):
    row = row_for(parsed, label)
    form = start_form(parsed, row)
    headers = desk.run.run.action(**{"HX-Request": "true", **(SLOT_TARGET if target else {})})
    return desk.run.client.post(posts_to(form), data=form_values(parsed, form), headers=headers)


@pytest.mark.parametrize("why", ["no_provider", "blocked", "open_session", "vanished"])
def test_coverage_refused_start_shows_only_the_reason(desk, seeded, monkeypatch, why):
    parsed = outline(fragment(desk).text)  # read before the refusal is set up
    if why == "no_provider":
        monkeypatch.setattr(service(desk), "_settings", lambda: None)
    elif why == "blocked":
        monkeypatch.setattr(
            desk.run.app.state.budget_gate,
            "check",
            lambda *args, **kwargs: BlockedAction("Fictional limit reached. Resets on 1 May."),
        )
    elif why == "open_session":
        opened = service(desk).start(seed_note(desk), 3)
        desk.run.wait_terminal(opened.task.id)
    else:
        desk.run.execute("UPDATE jobs SET triage_state = 'removed'")
    response = start_from_row(desk, parsed, ROUTES)
    assert response.status_code == 409, response.text[:300]
    answer = outline(response.text)
    assert not [e for e in answer if e.tag in ("h1", "h2")], "no heading of its own"
    assert not [e for e in answer if e.tag in ("select", "form")], "no focus or length form"
    (slot,) = [e for e in answer if e.attrs.get("id") == "coverage-start"]
    assert slot.tag == "div", "the answer keeps the slot, so a next refusal has a target"
    assert [e for e in answer if "data-autofocus" in e.attrs], "focus moves to the reason"
    if why == "blocked":
        assert "Fictional limit reached" in response.text
    if why == "open_session":
        links = [e for e in answer if e.tag == "a" and e.text == "Continue your session"]
        assert len(links) == 1 and links[0].attrs["href"].startswith("/sessions/")
    # The same refusal not aimed at the slot is still the whole new-session screen.
    whole = start_from_row(desk, parsed, ROUTES, target=False)
    assert whole.status_code == 409 and "<h1" in whole.text


def test_coverage_refused_start_dialog_keeps_the_cluster_focus(desk, seeded, monkeypatch):
    monkeypatch.setattr(
        desk.run.app.state.budget_gate,
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
    parsed = outline(fragment(desk).text)
    cid = row_for(parsed, ROUTES).attrs["data-cluster-id"]
    response = start_from_row(desk, parsed, ROUTES)
    assert response.status_code == 200
    answer = outline(response.text)
    assert not [e for e in answer if e.tag in ("h1", "h2") and "session" in e.text.lower()]
    (dialog,) = [e for e in answer if e.tag == "dialog"]
    assert dialog.attrs["data-return-focus"] == f"#coverage-start-{cid}"
    (form,) = [e for e in answer if e.tag == "form" and e.within(dialog)]
    assert form.attrs["hx-target"] == "#coverage-start"
    fields = form_values(answer, form)
    assert fields["focus"] == f"cluster:{cid}" and fields["length"] == "3"
    assert fields["budget_service"] == "fake"
    assert desk.run.rows("SELECT id FROM sessions") == []


def test_coverage_start_aimed_at_the_slot_still_starts_the_session(desk, seeded):
    response = start_from_row(desk, outline(fragment(desk).text), ROUTES)
    assert response.status_code == 200 and response.headers["HX-Redirect"].startswith("/sessions/")
    assert [r["focus_kind"] for r in desk.run.rows("SELECT focus_kind FROM sessions")] == [
        "cluster"
    ]


def test_coverage_row_controls_have_ids_and_the_slot_is_replaced(desk, seeded):
    parsed = outline(fragment(desk).text)
    for label in ORDER:
        row = row_for(parsed, label)
        cid = row.attrs["data-cluster-id"]
        form = start_form(parsed, row)
        assert form.attrs.get("hx-target") == "#coverage-start"
        assert form.attrs.get("hx-swap") == "outerHTML"
        (button,) = [e for e in inside_row(parsed, row) if e.tag == "button" and e.within(form)]
        assert button.attrs.get("id") == f"coverage-start-{cid}"
        for control in strengthen_controls(parsed, row):
            assert control.attrs.get("id") == f"coverage-strengthen-{cid}"


def test_coverage_detail_announces_and_offers_close(desk, seeded):
    parsed = outline(fragment(desk).text)
    cid = row_for(parsed, TERRAFORM).attrs["data-cluster-id"]
    response = fragment(desk, f"{COVERAGE_URL}/{cid}")
    assert json.loads(response.headers["HX-Trigger"]) == {
        "announce": f"Showing the facts behind {TERRAFORM}."
    }
    detail = outline(response.text)
    (section,) = [e for e in detail if e.attrs.get("id") == "coverage-detail"]
    (close,) = [e for e in detail if e.tag == "button" and e.text == "Close"]
    assert close.within(section) and close.attrs["type"] == "button"
    assert close.attrs["data-dismiss-into"] == "#coverage-detail-slot"
    assert close.attrs["data-return-focus"] == f"#coverage-strengthen-{cid}"


def test_coverage_detail_announce_is_json_escaped(desk):
    seed = seeder(desk)
    seed.job("cov-hostile", "Depot Clerk", "Harbor Kite Fictional")
    seed_summary(
        seed.db,
        "cov-hostile",
        (Requirement("hostile-1", "key", HOSTILE_SCRIPT + " Terraform é"),),
        summary_id="summary-hostile",
    )
    desk.typed("A Terraform story.", skills=("Terraform",))
    (row,) = rows_of(outline(fragment(desk).text))
    response = fragment(desk, f"{COVERAGE_URL}/{row.attrs['data-cluster-id']}")
    raw = response.headers["HX-Trigger"]
    assert raw.isascii()
    announce = json.loads(raw)["announce"]
    assert announce.startswith("Showing the facts behind ") and "injected-coverage" in announce
    assert "<script" not in response.text.replace("&lt;script", "")


def test_coverage_history_path_is_a_fixed_404_whichever_route_takes_it(desk, seeded):
    # The fact editor's "/facts/{id:path}/history" is registered first and takes this address.
    response = desk.client.get(f"{COVERAGE_URL}/history", headers=desk.run.run.htmx())
    assert response.status_code == 404 and "history" not in response.text


def test_coverage_detail_without_related_facts_says_so(desk, seeded):
    parsed = outline(fragment(desk).text)
    cid = row_for(parsed, ROUTES).attrs["data-cluster-id"]  # None: no fact is tied to it
    answer = fragment(desk, f"{COVERAGE_URL}/{cid}")
    assert NO_RELATED_TEXT in {e.text for e in outline(answer.text)}


@pytest.mark.parametrize(
    ("trigger", "page", "text", "paging"),
    [
        ("coverage-next", "2", "51 requirements. Page 2 of 2.", True),
        ("coverage-previous", "1", "51 requirements. Page 1 of 2.", True),
        (None, "2", "51 requirements. Page 2 of 2.", False),
    ],
)
def test_coverage_paging_links_announce_and_focus_the_count(desk, trigger, page, text, paging):
    seed_many(desk)
    headers = {**desk.run.run.htmx(), **({"HX-Trigger": trigger} if trigger else {})}
    response = desk.client.get(COVERAGE_URL, params={"page": page}, headers=headers)
    assert response.status_code == 200
    (count,) = [e for e in outline(response.text) if e.attrs.get("id") == "coverage-count"]
    assert count.text == text
    assert ("data-autofocus" in count.attrs) is paging
    assert (count.attrs.get("tabindex") == "-1") is paging
    if paging:
        assert json.loads(response.headers["HX-Trigger"]) == {"announce": text}
    else:
        assert "HX-Trigger" not in response.headers


def test_coverage_paging_links_fetch_the_fragment_and_push_the_page(desk):
    seed_many(desk)
    first = {e.attrs["id"]: e for e in outline(fragment(desk).text) if e.tag == "a"}
    assert "coverage-previous" not in first
    nxt = first["coverage-next"]
    assert nxt.attrs["hx-get"] == "/facts/coverage?page=2"
    assert nxt.attrs["hx-push-url"] == nxt.attrs["href"] == "/facts?tab=coverage&page=2"
    assert nxt.attrs["hx-target"] == "#coverage" and nxt.attrs["hx-swap"] == "outerHTML"
    second = {e.attrs["id"]: e for e in outline(fragment(desk, page="2").text) if e.tag == "a"}
    assert "coverage-next" not in second
    prev = second["coverage-previous"]
    assert prev.attrs["hx-get"] == "/facts/coverage?page=1"
    assert prev.attrs["hx-push-url"] == prev.attrs["href"] == "/facts?tab=coverage"
