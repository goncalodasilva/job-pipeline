"""M4.10 companies: the sortable list, the company page by safe id, versioned edits, links.

Assumed interface (the implementer matches it):

- ``GET /companies`` is a token-free page; with ``HX-Request`` and the token it returns the
  ``#companies`` block. Query: ``sort`` (``name``, ``open_roles``, ``to_apply``,
  ``last_activity``; default ``name``), ``dir`` (``asc`` or ``desc``; default ``asc`` for name,
  ``desc`` for the others) and ``page`` (50 companies a page). Anything else falls back to the
  default (200). Ties always sort by name (case-insensitive), then id.
- ``GET /companies/{id}`` is a token-free page by safe id (``#company-detail``); anything else
  answers the fixed plain-text 404 "This company wasn't found.".
- ``POST /companies/{id}/edit`` (token and origin): ``version`` and any of ``name``, ``website``,
  ``industry``, ``size``, ``locations`` (one per line) and ``notes``. Only posted fields change.
  It answers the ``#company-edit`` block: 200 "Saved.", 422 with errors tied to the fields
  (``company-edit-<field>`` and ``company-edit-<field>-error``) or 409 with the reload prompt.
"""

import json
import re
from urllib.parse import parse_qs, urlsplit

import pytest

from app.domain.job_normalization import company_key
from tests.company_pages import (
    SORT_COUNTS,
    SORT_SET,
    listed_ids,
    role_items,
    seed_many,
    seed_sort_set,
    table_rows,
)
from tests.integration.test_inbox import get, post, seeded
from tests.refine_harness import refining

pytestmark = pytest.mark.integration
NOT_FOUND = "This company wasn't found."
EDIT = "/companies/company-a/edit"
A, B, C, D = "company-a", "company-b", "company-c", "company-d"


def sort_seeded(r):
    seed_sort_set(r.run.app.state.workspace.db)


def company(r, id=A):
    return r.run.rows("SELECT * FROM companies WHERE id=?", (id,))[0]


def query(href):
    return {k: v[0] for k, v in parse_qs(urlsplit(href).query).items()}


# --- the list ---------------------------------------------------------------------------------

ORDERS = {
    # alder sorts before Birch: names sort without regard to case
    "name": {"asc": [B, A, C, D], "desc": [D, C, A, B]},
    "open_roles": {"asc": [D, B, A, C], "desc": [C, A, B, D]},
    "last_activity": {"asc": [A, C, B, D], "desc": [D, B, C, A]},
}
DEFAULT_DIR = {"name": "asc", "open_roles": "desc", "last_activity": "desc"}
COLUMNS = ["Name", "Open roles", "To apply", "Last activity"]


@pytest.mark.parametrize("sort", ["name", "open_roles", "last_activity"])
def test_companies_list_sortable(tmp_path, sort):
    with refining(tmp_path) as r:
        sort_seeded(r)
        for direction in ("asc", "desc"):
            shown = get(r, f"/companies?sort={sort}&dir={direction}")
            assert shown.status_code == 200 and "<html" not in shown.text
            assert listed_ids(shown.text) == ORDERS[sort][direction], (sort, direction)
            header = table_rows(shown.text)[0]
            assert all(n in c for n, c in zip(COLUMNS, header["cells"], strict=True))
            assert len(header["cells"]) == 4, "name, open roles, to apply, last activity"
            wanted = {"asc": "ascending", "desc": "descending"}[direction]
            assert [v for v in header["aria_sort"] if v != "none"] == [wanted]
        # No direction given: the column's natural one.
        default = get(r, f"/companies?sort={sort}")
        assert listed_ids(default.text) == ORDERS[sort][DEFAULT_DIR[sort]]
        # Each sort link names its column and the direction it would switch to.
        links = table_rows(default.text)[0]["hrefs"]
        assert len(links) == 4
        for key, href in zip(
            ["name", "open_roles", "to_apply", "last_activity"], links, strict=True
        ):
            asked = query(href)
            assert asked["sort"] == key
            if key == sort:
                assert asked["dir"] == {"asc": "desc", "desc": "asc"}[DEFAULT_DIR[sort]]
            else:
                assert asked["dir"] == ("asc" if key == "name" else "desc")
        # The full page holds the same table.
        full = r.client.get(f"/companies?sort={sort}&dir=asc")
        assert "<html" in full.text and listed_ids(full.text) == ORDERS[sort]["asc"]


def test_companies_list_shows_open_roles_to_apply_and_last_activity(tmp_path):
    with refining(tmp_path) as r:
        sort_seeded(r)
        shown = get(r, "/companies")
        rows = table_rows(shown.text)[1:]
        by_id = {row["hrefs"][0].rsplit("/", 1)[1]: row["cells"] for row in rows}
        for id, (open_roles, to_apply, last) in SORT_COUNTS.items():
            cells = by_id[id]
            assert len(cells) == 4
            assert cells[1] == str(open_roles), (id, cells)
            assert cells[2] == str(to_apply), (id, cells)
            assert last in cells[3], (id, cells)
        names = {id: name for id, name, _ in SORT_SET}
        assert all(names[id] in cells[0] for id, cells in by_id.items())
        assert re.search(r"<time[^>]*>\s*2026-10-18\s*</time>", shown.text)


def test_companies_list_to_apply_sort(tmp_path):
    with refining(tmp_path) as r:
        sort_seeded(r)
        # A and C tie on 1, B and D on 0; ties sort by name whichever way the column goes.
        assert listed_ids(get(r, "/companies?sort=to_apply&dir=desc").text) == [A, C, B, D]
        assert listed_ids(get(r, "/companies?sort=to_apply&dir=asc").text) == [B, D, A, C]
        assert listed_ids(get(r, "/companies?sort=to_apply").text) == [A, C, B, D]


def test_open_roles_counts_follow_triage_not_a_stored_number(tmp_path):
    with refining(tmp_path) as r:
        sort_seeded(r)
        r.run.execute(
            "UPDATE jobs SET triage_state='removed',version=version+1 WHERE id='company-b-job-1'"
        )
        r.run.execute(
            "UPDATE jobs SET triage_state='new',version=version+1 WHERE id='company-d-job-1'"
        )
        shown = get(r, "/companies?sort=name&dir=asc")
        counts = {
            row["hrefs"][0].rsplit("/", 1)[1]: row["cells"][1] for row in table_rows(shown.text)[1:]
        }
        assert counts == {B: "0", A: "2", C: "4", D: "1"}


def test_companies_list_is_50_per_page_and_keeps_the_sort(tmp_path):
    with refining(tmp_path) as r:
        seed_many(r.run.app.state.workspace.db, 55)
        first = get(r, "/companies?sort=name&dir=asc")
        ids = listed_ids(first.text)
        assert ids == [f"company-{i:04d}" for i in range(1, 51)]
        second = get(r, "/companies?sort=name&dir=asc&page=2")
        assert listed_ids(second.text) == [f"company-{i:04d}" for i in range(51, 56)]
        nxt = re.search(r'<a[^>]*href="([^"]*)"[^>]*>\s*Next page', first.text)
        assert nxt is not None and "Previous page" not in first.text
        assert query(nxt.group(1).replace("&amp;", "&")) == {
            "sort": "name",
            "dir": "asc",
            "page": "2",
        }
        back = re.search(r'<a[^>]*href="([^"]*)"[^>]*>\s*Previous page', second.text)
        assert back is not None and "Next page" not in second.text
        assert query(back.group(1).replace("&amp;", "&")).get("page") in (None, "1")
        desc = get(r, "/companies?sort=name&dir=desc&page=2")
        assert listed_ids(desc.text) == [f"company-{i:04d}" for i in range(5, 0, -1)]
        # A page past the end shows the last one; other page values show the first.
        assert listed_ids(get(r, "/companies?page=99").text) == listed_ids(second.text)
        for bad in ("0", "-3", "abc", "1.5", ""):
            assert listed_ids(get(r, f"/companies?page={bad}").text) == ids, bad


@pytest.mark.parametrize(
    "path",
    [
        "/companies?sort=evil&dir=sideways&page=-5",
        "/companies?sort=name;DROP&dir=asc",
        "/companies?sort=name&sort=open_roles&dir=desc&dir=asc",
        "/companies?sort[]=x",
        "/companies?sort=%00&dir=%00",
    ],
    ids=["unknown-values", "injection", "repeated", "bracket", "nul"],
)
def test_companies_list_garbage_query_falls_back_to_default(tmp_path, path):
    with refining(tmp_path) as r:
        sort_seeded(r)
        shown = get(r, path)
        assert shown.status_code == 200
        assert len(listed_ids(shown.text)) == 4
        assert "DROP" not in shown.text and "evil" not in shown.text


def test_companies_list_empty_state_and_links(tmp_path):
    with refining(tmp_path) as r:
        empty = r.client.get("/companies")
        assert empty.status_code == 200 and "<html" in empty.text
        text = " ".join(empty.text.split())
        assert "No companies yet." in text and "empty-state" in empty.text
        assert "will appear here" not in text, "the placeholder is gone"
        assert listed_ids(empty.text) == []
        sort_seeded(r)
        shown = r.client.get("/companies")
        assert "No companies yet." not in shown.text
        for id in (A, B, C, D):
            assert f'href="/companies/{id}"' in shown.text


def test_companies_list_escapes_names(tmp_path):
    with refining(tmp_path) as r:
        sort_seeded(r)
        r.run.execute(
            "UPDATE companies SET name='<script>Fictional</script> & Co' WHERE id=?", (A,)
        )
        for shown in (get(r, "/companies"), r.client.get("/companies")):
            assert "<script>Fictional" not in shown.text
            assert "&lt;script&gt;Fictional&lt;/script&gt; &amp; Co" in shown.text


def test_companies_page_has_no_unbuilt_columns(tmp_path):
    with refining(tmp_path) as r:
        sort_seeded(r)
        shown = get(r, "/companies").text.lower()
        for absent in (
            "active application",
            "contact",
            "watch",
            "brief",
            "coming",
            "later version",
        ):
            assert absent not in shown, absent


# --- the page ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "id",
    ["company-a", "missing", "bad!id", "x" * 65, "a.b", "a%20b", "caf%C3%A9", "id%0A", "%2E%2E"],
    ids=["known", "unknown", "bang", "too-long", "dot", "space", "non-ascii", "newline", "dots"],
)
def test_company_page_by_safe_id_only_fixed_404(tmp_path, id):
    with refining(tmp_path) as r:
        sort_seeded(r)
        shown = r.client.get(f"/companies/{id}")
        if id == "company-a":
            assert shown.status_code == 200 and "<html" in shown.text
            assert 'id="company-detail"' in shown.text
            assert re.search(r"<h1[^>]*>\s*Birch Fictional Farms\s*</h1>", shown.text)
            assert r.client.head(f"/companies/{id}").status_code == 200
            bare = r.client.get(f"/companies/{id}", headers={"HX-Request": "true"})
            assert bare.status_code == 403
            fragment = get(r, f"/companies/{id}")
            assert fragment.status_code == 200 and "<html" not in fragment.text
            assert 'id="company-detail"' in fragment.text
        else:
            assert shown.status_code == 404 and shown.text == NOT_FOUND
            assert id not in shown.text
            assert r.client.head(f"/companies/{id}").status_code == 404
            assert "text/plain" in shown.headers["content-type"]


def test_company_page_shows_its_details_and_edit_form(tmp_path):
    with refining(tmp_path) as r:
        sort_seeded(r)
        r.run.execute(
            "UPDATE companies SET website='https://example.test/about',industry='agriculture',"
            "size='11-50',locations=?,notes='Fictional note.' WHERE id=?",
            (json.dumps(["Porto", "Faro"]), A),
        )
        shown = get(r, f"/companies/{A}").text
        assert 'href="https://example.test/about"' in shown
        for label in ("Name", "Website", "Industry", "Size", "Locations", "Notes"):
            assert re.search(rf'<label for="company-edit-\w+"[^>]*>\s*{label}\b', shown), label
        assert 'name="version" value="1"' in shown
        assert re.search(
            r'<input[^>]*id="company-edit-name"[^>]*value="Birch Fictional Farms"', shown
        )
        assert "Porto\nFaro" in shown and "Fictional note." in shown
        # Industry is chosen from the controlled list.
        select = re.search(r'<select[^>]*id="company-edit-industry".*?</select>', shown, re.S)
        assert select is not None
        options = re.findall(r'<option[^>]*value="([^"]*)"([^>]*)>', select.group(0))
        values = {value for value, _ in options}
        assert {"", "agriculture", "technology", "professional_services"} <= values
        assert any(v == "agriculture" and "selected" in rest for v, rest in options)
        assert "Fictional agriculture" not in values


def test_company_page_has_no_placeholder_for_unbuilt_features(tmp_path):
    with refining(tmp_path) as r:
        sort_seeded(r)
        for response in (get(r, f"/companies/{A}"), r.client.get(f"/companies/{A}")):
            assert response.status_code == 200 and 'id="company-detail"' in response.text
            shown = response.text
            main = shown.split("<main", 1)[-1].lower()
            for absent in (
                "watch",
                "coming soon",
                "later version",
                "will appear",
            ):
                assert absent not in main, absent


def test_company_gets_change_nothing(tmp_path):
    with refining(tmp_path) as r:
        sort_seeded(r)
        before = (r.run.rows("SELECT * FROM companies"), r.run.rows("SELECT * FROM jobs"))
        for path in ("/companies", "/companies/company-a", "/companies?sort=open_roles"):
            assert get(r, path).status_code == 200
            assert r.client.get(path).status_code == 200
        assert (r.run.rows("SELECT * FROM companies"), r.run.rows("SELECT * FROM jobs")) == before
        assert not r.run.spy.requests
        assert r.run.rows("SELECT count(*) AS n FROM tasks")[0]["n"] == 0


# --- jobs at the company ----------------------------------------------------------------------

STATES = {
    "company-a-job-1": "Removed",
    "company-a-job-2": "To apply",
    "company-a-job-3": "New",
}


def test_company_jobs_listed_with_states(tmp_path):
    with refining(tmp_path) as r:
        sort_seeded(r)
        listed = role_items(get(r, f"/companies/{A}").text)
        assert set(listed) == set(STATES), "every job at the company, removed ones too"
        for job_id, label in STATES.items():
            assert label in listed[job_id], (job_id, listed[job_id])
        assert "Fictional Birch Fictional Farms role 1" in listed["company-a-job-1"]
        assert "Seen" not in listed["company-a-job-3"]
        other = role_items(r.client.get(f"/companies/{C}").text)
        assert set(other) == {f"company-c-job-{n}" for n in (1, 2, 3, 4)}
        assert "Snoozed" in other["company-c-job-3"] and "To apply" in other["company-c-job-4"]
        assert "Seen" in other["company-c-job-1"] and "Seen" in other["company-c-job-2"]
        # Each link opens that job's page.
        for job_id in (*listed, *other):
            assert r.client.get(f"/jobs/{job_id}").status_code == 200, job_id
        # The state shown is the job's current one.
        r.run.execute("UPDATE jobs SET triage_state='seen' WHERE id='company-a-job-3'")
        assert "Seen" in role_items(get(r, f"/companies/{A}").text)["company-a-job-3"]


def test_company_without_jobs_says_so(tmp_path):
    with refining(tmp_path) as r:
        seed_many(r.run.app.state.workspace.db, 1)
        shown = get(r, "/companies/company-0001").text
        assert "No jobs at this company yet." in " ".join(shown.split())
        assert role_items(shown) == {}


def test_company_links_from_card_drawer_and_job_page(tmp_path):
    with refining(tmp_path) as r:
        seeded(r, 3)
        href = 'href="/companies/company-0001"'
        for path in (
            "/inbox/jobs/job-0001/card",
            "/inbox/jobs/job-0001/detail",
            "/inbox/jobs/job-0001/header",
        ):
            shown = get(r, path).text
            assert href in shown, path
            assert re.search(rf"{href}[^>]*>\s*Fictional Orchard 1\s*</a>", shown), path
        assert href in r.client.get("/jobs/job-0001").text
        assert get(r, "/companies/company-0001").status_code == 200
        # A job with no company links nowhere.
        r.run.execute("UPDATE jobs SET company_id=NULL WHERE id='job-0002'")
        assert "/companies/" not in get(r, "/inbox/jobs/job-0002/card").text


# --- edits ------------------------------------------------------------------------------------


def test_company_edit_versioned_and_name_collision_refused(tmp_path):
    with refining(tmp_path) as r:
        sort_seeded(r)
        saved = post(
            r,
            EDIT,
            version="1",
            name="Birch Orchards",
            website="https://example.test/careers",
            industry="agriculture",
            size="11-50",
            locations="Porto\r\nFaro\r\n\r\nPorto",
            notes="Fictional note.",
        )
        assert saved.status_code == 200 and "Saved." in saved.text
        row = company(r)
        assert row["version"] == 2 and row["name"] == "Birch Orchards"
        assert row["name_key"] == company_key("Birch Orchards")
        assert row["website"] == "https://example.test/careers" and row["industry"] == "agriculture"
        assert row["size"] == "11-50" and row["notes"] == "Fictional note."
        assert json.loads(row["locations"]) == ["Porto", "Faro"]
        assert row["updated_at"] != row["created_at"]
        announced = json.loads(saved.headers["HX-Trigger-After-Settle"])
        assert announced["announce"] == "Saved."
        assert 'name="version" value="2"' in saved.text
        # Only posted fields change.
        assert post(r, EDIT, version="2", size="51-200").status_code == 200
        later = company(r)
        assert later["size"] == "51-200" and later["name"] == "Birch Orchards"
        assert later["website"] == "https://example.test/careers" and later["version"] == 3
        # Empty values clear a field.
        assert (
            post(r, EDIT, version="3", website="", industry="", size="", notes="").status_code
            == 200
        )
        cleared = company(r)
        assert [cleared[k] for k in ("website", "industry", "size", "notes")] == [None] * 4

        # A stale version is refused with the reload prompt and keeps the typed values.
        stale = post(r, EDIT, version="1", name="Stale Name", notes="Stale note")
        assert stale.status_code == 409 and "Reload the page" in stale.text
        assert 'href="/companies/company-a"' in stale.text and "Stale Name" in stale.text
        assert company(r)["name"] == "Birch Orchards" and company(r)["version"] == 4

        # A name another company already uses (by its name key) is refused, whatever its form.
        for taken in (
            "alder Fictional Mills",
            "ALDER FICTIONAL MILLS",
            "  alder   fictional  mills ",
            "Alder Fictional Mills Ltd.",
            "Alder Fictional Mills, Inc",
        ):
            version = str(company(r)["version"])
            refused = post(r, EDIT, version=version, name=taken, notes="Must not be saved")
            assert refused.status_code == 422, taken
            assert 'aria-describedby="company-edit-name-error"' in refused.text, taken
            assert 'id="company-edit-name-error"' in refused.text and "already" in refused.text
            assert 'aria-invalid="true"' in refused.text and "data-autofocus" in refused.text
            assert company(r)["name"] == "Birch Orchards" and company(r)["notes"] is None
            assert company(r)["version"] == int(version), "nothing was saved"
        assert company(r, B)["name"] == "alder Fictional Mills"
        # Its own name in another form is fine: it is the same company.
        own = post(r, EDIT, version=str(company(r)["version"]), name="BIRCH ORCHARDS ltd")
        assert own.status_code == 200 and company(r)["name"] == "BIRCH ORCHARDS ltd"


def test_company_edit_missing_or_bad_version_is_400(tmp_path):
    with refining(tmp_path) as r:
        sort_seeded(r)
        for values in ({}, {"version": "abc"}, {"version": "0"}, {"version": "-1"}):
            assert post(r, EDIT, name="Other Name", **values).status_code == 400, values
        assert company(r)["version"] == 1 and company(r)["name"] == "Birch Fictional Farms"


def test_company_edit_unknown_or_unsafe_id_is_the_fixed_404(tmp_path):
    with refining(tmp_path) as r:
        sort_seeded(r)
        for id in ("missing", "bad!id", "x" * 65):
            shown = post(r, f"/companies/{id}/edit", version="1", name="New Name")
            assert shown.status_code == 404 and shown.text == NOT_FOUND, id
            assert id not in shown.text
        assert r.run.rows("SELECT count(*) AS n FROM companies")[0]["n"] == 4


@pytest.mark.parametrize(
    "field,value",
    [
        ("name", " "),
        ("name", "x" * 201),
        ("name", "Bad\x07name"),
        ("website", "javascript:alert(1)"),
        ("website", "example.test"),
        ("website", "https://"),
        ("industry", "Fictional agriculture"),
        ("industry", "agriculture; DROP TABLE companies"),
        ("size", "x" * 101),
        ("locations", "Porto\n" + "y" * 201),
        ("notes", "x" * 10001),
    ],
    ids=[
        "name-blank",
        "name-too-long",
        "name-control-character",
        "website-javascript",
        "website-no-scheme",
        "website-no-host",
        "industry-not-in-list",
        "industry-injection",
        "size-too-long",
        "locations-line-too-long",
        "notes-too-long",
    ],
)
def test_company_edit_422_ties_errors_to_fields(tmp_path, field, value):
    with refining(tmp_path) as r:
        sort_seeded(r)
        before = company(r)
        shown = post(r, EDIT, version="1", **{field: value})
        assert shown.status_code == 422
        assert f'aria-describedby="company-edit-{field}-error"' in shown.text
        assert f'id="company-edit-{field}-error"' in shown.text
        assert 'aria-invalid="true"' in shown.text and "data-autofocus" in shown.text
        assert "<script" not in shown.text
        assert company(r) == before


def test_company_edit_with_several_errors_saves_nothing(tmp_path):
    with refining(tmp_path) as r:
        sort_seeded(r)
        before = company(r)
        shown = post(
            r, EDIT, version="1", name="Fine New Name", website="ftp://example.test", notes="n"
        )
        assert shown.status_code == 422 and "company-edit-website-error" in shown.text
        assert "company-edit-name-error" not in shown.text
        assert "Fine New Name" in shown.text, "typed values stay in the form"
        assert company(r) == before


def test_company_rename_shows_on_its_jobs_and_job_edit_still_matches_by_key(tmp_path):
    with refining(tmp_path) as r:
        sort_seeded(r)
        assert post(r, EDIT, version="1", name="Birch Orchards").status_code == 200
        card = get(r, "/inbox/jobs/company-a-job-3/card").text
        # The job's title still holds the old name; the company link shows the new one.
        assert re.search(r'href="/companies/company-a"[^>]*>\s*Birch Orchards\s*</a>', card)
        assert not re.search(r">\s*Birch Fictional Farms\s*</a>", card)
        renamed = post(
            r, "/inbox/jobs/company-a-job-3/edit", version="1", company="birch orchards LTD"
        )
        assert renamed.status_code == 200
        assert r.run.rows("SELECT company_id FROM jobs WHERE id='company-a-job-3'")[0] == {
            "company_id": A
        }
        assert r.run.rows("SELECT count(*) AS n FROM companies")[0]["n"] == 4


def test_company_notes_saved_and_escaped(tmp_path):
    with refining(tmp_path) as r:
        sort_seeded(r)
        saved = post(
            r, EDIT, version="1", notes="<script>Fictional note</script>\r\nSecond & third line"
        )
        assert saved.status_code == 200
        assert company(r)["notes"] == "<script>Fictional note</script>\nSecond & third line"
        for shown in (
            saved.text,
            get(r, f"/companies/{A}").text,
            r.client.get(f"/companies/{A}").text,
        ):
            assert "<script>Fictional" not in shown
            assert "&lt;script&gt;Fictional note&lt;/script&gt;" in shown
        assert company(r)["name"] == "Birch Fictional Farms"


@pytest.mark.parametrize(
    "bad",
    [
        "javascript:alert(1)",
        "JavaScript:alert(1)",
        "data:text/html,<b>x</b>",
        "ftp://example.test/file",
        "//example.test/x",
        "example.test",
        "https://",
        "http://exa mple.test",
        "https://example.test/\njunk",
        "https://example.test/" + "a" * 2100,
    ],
    ids=[
        "javascript",
        "javascript-mixed-case",
        "data",
        "ftp",
        "protocol-relative",
        "no-scheme",
        "no-host",
        "space-in-host",
        "newline",
        "too-long",
    ],
)
def test_website_link_only_http_https(tmp_path, bad):
    with refining(tmp_path) as r:
        sort_seeded(r)
        refused = post(r, EDIT, version="1", website=bad)
        assert refused.status_code == 422 and 'id="company-edit-website-error"' in refused.text
        assert company(r)["website"] is None and company(r)["version"] == 1
        for scheme in ('href="javascript:', 'href="data:', 'href="ftp:', 'href="//'):
            assert scheme.lower() not in refused.text.lower()


def test_website_link_only_http_https_renders_safe_links(tmp_path):
    with refining(tmp_path) as r:
        sort_seeded(r)
        good = post(r, EDIT, version="1", website="https://example.test/careers")
        assert good.status_code == 200
        page = get(r, f"/companies/{A}").text
        assert 'href="https://example.test/careers"' in page and 'rel="noopener noreferrer"' in page
        plain = post(r, EDIT, version="2", website="http://example.test")
        assert plain.status_code == 200 and company(r)["website"] == "http://example.test"
        # An upper-case scheme is either stored as a valid link or refused, never a 500.
        upper = post(r, EDIT, version="3", website="HTTPS://Example.test/x")
        assert upper.status_code in (200, 422)
        if upper.status_code == 200:
            assert re.match(r"https?://", company(r)["website"])
        else:
            assert 'id="company-edit-website-error"' in upper.text
        cleared = post(r, EDIT, version=str(company(r)["version"]), website="")
        assert cleared.status_code == 200 and company(r)["website"] is None
        assert 'rel="noopener noreferrer"' not in get(r, f"/companies/{A}").text


@pytest.mark.parametrize(
    "stored",
    [
        "javascript:alert(1)",
        "data:text/html,x",
        "ftp://example.test/x",
        "https://example.test/\njunk",
        'https://example.test/"><script>alert(1)</script>',
    ],
    ids=["javascript", "data", "ftp", "newline", "markup"],
)
def test_stored_website_that_is_not_safe_is_never_a_link(tmp_path, stored):
    """The table's CHECK is deliberately simple. Legacy or corrupt values must not become
    executable links when shown, and must not break the page or its form."""
    with refining(tmp_path) as r:
        sort_seeded(r)
        with r.run.app.state.workspace.db.transaction() as conn:
            conn.execute("PRAGMA ignore_check_constraints=ON")
            conn.execute("UPDATE companies SET website=? WHERE id=?", (stored, A))
            conn.execute("PRAGMA ignore_check_constraints=OFF")
        assert company(r)["website"] == stored, "the corrupt value is really stored"
        for shown in (get(r, f"/companies/{A}"), r.client.get(f"/companies/{A}")):
            assert shown.status_code == 200
            lowered = shown.text.lower()
            for scheme in ('href="javascript:', 'href="data:', 'href="ftp:'):
                assert scheme not in lowered
            assert "<script>alert(1)" not in shown.text
            assert 'href="https://example.test/\n' not in shown.text
            assert 'href="https://example.test/"' not in shown.text


# --- protection -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["/companies", "/companies?sort=open_roles&dir=desc&page=1", "/companies/company-a"],
)
def test_company_fragments_need_token(tmp_path, path):
    with refining(tmp_path) as r:
        sort_seeded(r)
        assert r.client.get(path, headers={"HX-Request": "true"}).status_code == 403
        shown = get(r, path)
        assert shown.status_code == 200 and "<html" not in shown.text
        assert 'id="companies"' in shown.text or 'id="company-detail"' in shown.text
        assert r.client.get(path).status_code == 200, "the page itself needs no token"


def test_company_edit_needs_token_and_origin(tmp_path):
    with refining(tmp_path) as r:
        sort_seeded(r)
        data = {"version": "1", "name": "Hostile Name"}
        assert r.client.post(EDIT, data=data).status_code == 403
        assert r.client.post(EDIT, data=data, headers={"HX-Request": "true"}).status_code == 403
        hostile = {**r.run.run.htmx(), "Origin": "https://hostile.example.test"}
        assert r.client.post(EDIT, data=data, headers=hostile).status_code == 403
        assert r.client.get(EDIT, headers=r.run.run.htmx()).status_code in (404, 405)
        assert company(r)["name"] == "Birch Fictional Farms" and company(r)["version"] == 1
        # With both, the same request is accepted.
        assert post(r, EDIT, version="1", name="Valid New Name").status_code == 200
        assert company(r)["name"] == "Valid New Name"


# --- review fixes: header after a save, rarely used branches, hardening ---------------------------


def _insert_unsafe_company(r):
    from app.modules.storage.jobs_repo import insert_company

    with r.run.app.state.workspace.db.transaction() as conn:
        insert_company(
            conn,
            id="bad!id",
            name="Unsafe Fictional Co",
            name_key=company_key("Unsafe Fictional Co"),
            now="2026-10-01T09:00:00+00:00",
        )


def test_saved_edit_sends_the_header_again_with_website_and_summary(tmp_path):
    with refining(tmp_path) as r:
        sort_seeded(r)
        page = get(r, f"/companies/{A}").text
        assert 'id="company-header"' in page and "Website:" not in page
        assert 'id="company-summary"' not in page, "nothing to summarize yet"
        saved = post(
            r,
            EDIT,
            version="1",
            website="https://example.test/careers",
            industry="agriculture",
            size="11-50",
            locations="Porto\nFaro",
        )
        assert saved.status_code == 200
        header = re.search(r'<div id="company-header"[^>]*>.*?</div>', saved.text, re.S)
        assert header is not None and 'hx-swap-oob="true"' in header.group(0)
        assert 'href="https://example.test/careers"' in header.group(0)
        assert "Industry: Agriculture · Size: 11-50 · Locations: Porto, Faro" in header.group(0)
        assert re.search(r"<h1[^>]*>\s*Birch Fictional Farms\s*</h1>", header.group(0))
        # The page itself shows the same header, and a cleared website leaves no link.
        assert 'href="https://example.test/careers"' in get(r, f"/companies/{A}").text
        cleared = post(r, EDIT, version="2", website="", industry="", size="", locations="")
        assert cleared.status_code == 200
        header = re.search(r'<div id="company-header"[^>]*>.*?</div>', cleared.text, re.S)
        assert header is not None and 'hx-swap-oob="true"' in header.group(0)
        assert "Website:" not in header.group(0) and "company-summary" not in header.group(0)
        # A refused save does not resend the header.
        refused = post(r, EDIT, version="3", website="ftp://example.test")
        assert refused.status_code == 422 and 'id="company-header"' not in refused.text


def test_company_summary_is_escaped_plain_text(tmp_path):
    with refining(tmp_path) as r:
        sort_seeded(r)
        saved = post(r, EDIT, version="1", size="<b>Big</b>", locations="<i>Porto</i>")
        assert saved.status_code == 200
        for shown in (saved.text, get(r, f"/companies/{A}").text):
            assert "<b>Big" not in shown and "<i>Porto" not in shown
            assert "Size: &lt;b&gt;Big&lt;/b&gt;" in shown


def test_company_name_without_letters_or_numbers_is_refused(tmp_path):
    with refining(tmp_path) as r:
        sort_seeded(r)
        shown = post(r, EDIT, version="1", name="!!!")
        assert shown.status_code == 422 and 'id="company-edit-name-error"' in shown.text
        assert "letters or numbers" in shown.text
        assert company(r)["name"] == "Birch Fictional Farms" and company(r)["version"] == 1


def test_company_locations_limited_to_100_places(tmp_path):
    with refining(tmp_path) as r:
        sort_seeded(r)
        too_many = "\n".join(f"Place {n}" for n in range(101))
        shown = post(r, EDIT, version="1", locations=too_many)
        assert shown.status_code == 422 and 'id="company-edit-locations-error"' in shown.text
        assert company(r)["version"] == 1
        exactly = "\n".join(f"Place {n}" for n in range(100))
        assert post(r, EDIT, version="1", locations=exactly).status_code == 200
        assert len(json.loads(company(r)["locations"])) == 100


def test_company_edit_that_changes_nothing_keeps_the_version_and_writes_nothing(tmp_path):
    with refining(tmp_path) as r:
        sort_seeded(r)
        before = company(r)
        same = post(
            r, EDIT, version="1", name="Birch Fictional Farms", website="", size="", notes=""
        )
        assert same.status_code == 200 and 'name="version" value="1"' in same.text
        assert company(r) == before, "no write: version and updated_at are unchanged"


def test_stored_industry_outside_the_list_is_kept_and_stays_valid(tmp_path):
    with refining(tmp_path) as r:
        sort_seeded(r)
        r.run.execute("UPDATE companies SET industry='legacy_sector' WHERE id=?", (A,))
        page = get(r, f"/companies/{A}").text
        select = re.search(r'<select[^>]*id="company-edit-industry".*?</select>', page, re.S)
        assert select is not None
        options = re.findall(r'<option[^>]*value="([^"]*)"([^>]*)>', select.group(0))
        assert any(v == "legacy_sector" and "selected" in rest for v, rest in options)
        assert any(v == "agriculture" and "selected" not in rest for v, rest in options)
        # Posting it unchanged is valid; another value outside the list is not.
        kept = post(r, EDIT, version="1", industry="legacy_sector", size="11-50")
        assert kept.status_code == 200 and company(r)["industry"] == "legacy_sector"
        refused = post(r, EDIT, version="2", industry="other_sector")
        assert refused.status_code == 422 and company(r)["industry"] == "legacy_sector"


@pytest.mark.parametrize(
    "char,allowed",
    [
        ("\u200d", True),
        ("\u200c", True),
        ("\u200b", False),
        ("\u200e", False),
        ("\u202e", False),
        ("\u2066", False),
        ("\ufeff", False),
        ("\u2028", False),
        ("\x07", False),
    ],
    ids=["zwj", "zwnj", "zero-width-space", "lrm", "rlo", "isolate", "bom", "line-sep", "bell"],
)
@pytest.mark.parametrize("field", ["name", "size"])
def test_company_text_allows_joiners_but_no_other_invisible_characters(
    tmp_path, field, char, allowed
):
    with refining(tmp_path) as r:
        sort_seeded(r)
        value = f"Fictional{char}Co"
        shown = post(r, EDIT, version="1", **{field: value})
        if allowed:
            assert shown.status_code == 200 and company(r)[field] == value
        else:
            assert shown.status_code == 422 and f"company-edit-{field}-error" in shown.text
            assert company(r)["version"] == 1


@pytest.mark.parametrize(
    "stored", ["not json at all", '{"a": 1}', '"text"', "7", '[1, null, "Porto"]']
)
def test_corrupt_stored_locations_are_no_places_not_a_server_error(tmp_path, stored):
    with refining(tmp_path) as r:
        sort_seeded(r)
        with r.run.app.state.workspace.db.transaction() as conn:
            conn.execute("PRAGMA ignore_check_constraints=ON")
            conn.execute("UPDATE companies SET locations=? WHERE id=?", (stored, A))
            conn.execute("PRAGMA ignore_check_constraints=OFF")
        assert company(r)["locations"] == stored, "the corrupt value is really stored"
        for shown in (get(r, f"/companies/{A}"), r.client.get(f"/companies/{A}")):
            assert shown.status_code == 200
        edited = post(r, EDIT, version="1", notes="Fictional note.")
        assert edited.status_code == 200
        expected = ["Porto"] if stored.startswith("[1") else []
        assert json.loads(company(r)["locations"]) == expected


def test_unsafe_ids_are_plain_text_not_links(tmp_path):
    with refining(tmp_path) as r:
        sort_seeded(r)
        _insert_unsafe_company(r)
        r.run.execute(
            "INSERT INTO jobs (id, company_id, title, snapshot_text, captured_at, found_at, "
            "created_at, updated_at) SELECT 'bad!job', company_id, 'Unsafe Fictional Role', "
            "snapshot_text, captured_at, found_at, created_at, updated_at FROM jobs "
            "WHERE id='company-a-job-1'"
        )
        r.run.execute("UPDATE jobs SET company_id='bad!id' WHERE id='company-a-job-3'")
        listed = get(r, "/companies").text
        assert "Unsafe Fictional Co" in listed and "/companies/bad" not in listed
        assert f'href="/companies/{A}"' in listed
        page = get(r, f"/companies/{A}").text
        assert "Unsafe Fictional Role" in page and "/jobs/bad" not in page
        assert 'href="/jobs/company-a-job-1"' in page
        card = get(r, "/inbox/jobs/company-a-job-3/card").text
        assert "Unsafe Fictional Co" in card and "/companies/bad" not in card


def test_list_page_links_announce_the_page_and_ask_for_focus(tmp_path):
    with refining(tmp_path) as r:
        seed_many(r.run.app.state.workspace.db, 55)
        first = get(r, "/companies?sort=name&dir=asc")
        assert "Companies. Sorted by Name, A to Z." in " ".join(first.text.split())
        nxt = re.search(r'<a[^>]*hx-get="([^"]*)"[^>]*>\s*Next page', first.text)
        assert nxt is not None and query(nxt.group(1).replace("&amp;", "&"))["focus"] == "page"
        assert "HX-Trigger-After-Settle" not in first.headers
        second = get(r, nxt.group(1).replace("&amp;", "&"))
        assert json.loads(second.headers["HX-Trigger-After-Settle"]) == {"announce": "Page 2 of 2."}
        assert "data-autofocus" in second.text
        assert not re.search(r"<h1[^>]*data-autofocus", second.text)
        back = re.search(r'<a[^>]*hx-get="([^"]*)"[^>]*>\s*Previous page', second.text)
        assert back is not None
        assert json.loads(
            get(r, back.group(1).replace("&amp;", "&")).headers["HX-Trigger-After-Settle"]
        ) == {"announce": "Page 1 of 2."}
        # The pushed address has no focus parameter, and a full page announces nothing.
        assert "focus=page" not in re.findall(r'hx-push-url="([^"]*)"', first.text).__str__()
        assert "HX-Trigger-After-Settle" not in r.client.get("/companies?page=2&focus=page").headers


def test_companies_empty_state_offers_the_job_inbox(tmp_path):
    with refining(tmp_path) as r:
        empty = r.client.get("/companies")
        assert re.search(r'<a href="/inbox">\s*Add a job in the Job inbox\s*</a>', empty.text)


def test_companies_caption_names_the_sort_direction(tmp_path):
    with refining(tmp_path) as r:
        sort_seeded(r)
        for path, said in (
            ("/companies?sort=open_roles&dir=desc", "Sorted by Open roles, most first."),
            ("/companies?sort=open_roles&dir=asc", "Sorted by Open roles, fewest first."),
            ("/companies?sort=name&dir=desc", "Sorted by Name, Z to A."),
        ):
            caption = re.search(r"<caption[^>]*>(.*?)</caption>", get(r, path).text, re.S)
            assert caption is not None and said in caption.group(1), path
