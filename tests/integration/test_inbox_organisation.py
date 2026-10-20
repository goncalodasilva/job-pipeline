"""M4.9 saved views, controlled-value decisions and bounded read-only inbox UI."""

import json
from statistics import median
from time import perf_counter
from urllib.parse import parse_qs, urlsplit

import pytest

from app.domain.errors import VersionConflict
from tests.integration.test_inbox import get, post, seeded
from tests.job_fixtures import generate_jobs, seed_jobs
from tests.refine_harness import refining

pytestmark = pytest.mark.integration


def queued(r, *, field="industry", value="Fictional cultivation", id="unknown-1"):
    r.run.execute(
        "INSERT INTO field_value_queue (id,field,value,created_at,updated_at) VALUES (?,?,?,?,?)",
        (id, field, value, "2026-10-20", "2026-10-20"),
    )
    r.run.execute(f"UPDATE jobs SET {field}=?", (value,))  # noqa: S608 - fixed test fields


def target(r, value="agriculture", field="industry"):
    row = r.run.rows(
        "SELECT id,version FROM controlled_values WHERE field=? AND value=?", (field, value)
    )[0]
    return f"{row['id']}@{row['version']}"


def decide(r, action="map", version="1", **fields):
    return post(r, "/inbox/values/unknown-1", action=action, version=version, **fields)


def test_ac_m4_4_mapping_unknown_value_adds_alias_and_updates_jobs(tmp_path):
    with refining(tmp_path) as r:
        seeded(r, 3)
        queued(r)
        shown = r.client.get("/inbox/values")
        assert shown.status_code == 200 and "Fictional cultivation" in shown.text
        assert "3 jobs" in shown.text
        saved = decide(r, target=target(r))
        assert saved.status_code == 200 and "Mapped." in saved.text
        assert {row["industry"] for row in r.run.rows("SELECT industry FROM jobs")} == {
            "agriculture"
        }
        row = r.run.rows("SELECT aliases,version FROM controlled_values WHERE value='agriculture'")[
            0
        ]
        assert "Fictional cultivation" in json.loads(row["aliases"]) and row["version"] == 2
        assert r.run.rows("SELECT status,version FROM field_value_queue")[0] == {
            "status": "mapped",
            "version": 2,
        }


def test_mapping_skips_user_corrected_fields(tmp_path):
    with refining(tmp_path) as r:
        seeded(r, 2)
        queued(r)
        r.run.execute("UPDATE jobs SET user_corrected='[\"industry\"]' WHERE id='job-0001'")
        assert decide(r, target=target(r)).status_code == 200
        assert r.run.rows("SELECT industry,version FROM jobs WHERE id='job-0001'")[0] == {
            "industry": "Fictional cultivation",
            "version": 1,
        }
        assert r.run.rows("SELECT industry,version FROM jobs WHERE id='job-0002'")[0] == {
            "industry": "agriculture",
            "version": 2,
        }


def test_add_as_new_value(tmp_path):
    with refining(tmp_path) as r:
        seeded(r, 1)
        queued(r)
        assert decide(r, "new").status_code == 200
        assert r.run.rows("SELECT value FROM controlled_values WHERE value='Fictional cultivation'")
        assert r.run.rows("SELECT status FROM field_value_queue")[0]["status"] == "mapped"


def test_ignore_value_keeps_text_as_written(tmp_path):
    with refining(tmp_path) as r:
        seeded(r, 1)
        queued(r)
        before = r.run.rows("SELECT * FROM jobs")
        assert decide(r, "ignore").status_code == 200
        assert r.run.rows("SELECT * FROM jobs") == before
        assert r.run.rows("SELECT status FROM field_value_queue")[0]["status"] == "ignored"


def test_mapping_stale_queue_and_target_are_atomic(tmp_path):
    with refining(tmp_path) as r:
        seeded(r, 1)
        queued(r)
        selected = target(r)
        r.run.execute("UPDATE controlled_values SET version=2 WHERE value='agriculture'")
        for version, choice in (("1", selected), ("9", target(r))):
            refused = decide(r, version=version, target=choice)
            assert refused.status_code == 409 and "Reload the page" in refused.text
            assert r.run.rows("SELECT industry,version FROM jobs")[0]["version"] == 1
            assert r.run.rows("SELECT status FROM field_value_queue")[0]["status"] == "pending"


def test_mapping_target_must_belong_to_same_field(tmp_path):
    with refining(tmp_path) as r:
        seeded(r, 1)
        queued(r)
        refused = decide(r, target=target(r, "remote", "work_mode"))
        assert refused.status_code == 422 and 'aria-invalid="true"' in refused.text
        assert r.run.rows("SELECT status FROM field_value_queue")[0]["status"] == "pending"


def test_alias_collision_refused_without_changing_jobs(tmp_path):
    with refining(tmp_path) as r:
        seeded(r, 1)
        queued(r, value="software")  # already an alias of technology
        assert decide(r, target=target(r)).status_code == 422
        assert decide(r, "new").status_code == 422
        assert r.run.rows("SELECT industry FROM jobs")[0]["industry"] == "software"


def test_saved_view_create_rename_delete_with_version(tmp_path):
    with refining(tmp_path) as r:
        org = r.run.app.state.inbox_organisation
        saved = org.create_view(
            " Remote, Strong ", {"band": "strong", "work_mode": "remote", "group_by": "company"}
        )
        assert saved["name"] == "Remote, Strong" and saved["version"] == 1
        assert parse_qs(urlsplit(saved["url"]).query)["work_mode"] == ["remote"]
        renamed = org.rename_view(saved["id"], "My remote roles", expected_version=1)
        assert renamed["version"] == 2
        with pytest.raises(VersionConflict):
            org.delete_view(saved["id"], expected_version=1)
        org.delete_view(saved["id"], expected_version=2)
        assert org.saved_views() == ()


def test_saved_view_unicode_name_collision_and_invalid_filters(tmp_path):
    with refining(tmp_path) as r:
        org = r.run.app.state.inbox_organisation
        org.create_view("Straße", {})
        for name, values in (
            ("STRASSE", {}),
            ("", {}),
            ("Invalid", {"sort": "DROP TABLE jobs"}),
            ("Invalid", {"group_by": "notes"}),
            ("Invalid", {"extra": "x"}),
        ):
            with pytest.raises(ValueError):
                org.create_view(name, values)
        assert len(org.saved_views()) == 1


ORGANISATION_TABLES = ("jobs", "saved_views", "field_value_queue", "controlled_values", "tasks")


def snapshot(r):
    return {t: r.run.rows(f"SELECT * FROM {t}") for t in ORGANISATION_TABLES}  # noqa: S608


def test_view_filters_are_read_only_get(tmp_path):
    with refining(tmp_path) as r:
        seeded(r, 60)
        queued(r)
        before = snapshot(r)
        shown = get(
            r, "/inbox?work_mode=remote&band=strong&group_by=company&search=greenhouse&sort=band"
        )
        assert shown.status_code == 200
        assert 'method="get"' in shown.text and 'id="inbox-filters"' in shown.text
        assert 'class="queue-group"' in shown.text
        assert 'name="filters"' in shown.text and "work_mode=remote" in shown.text
        queue = get(r, "/inbox/queue?work_mode=remote&group_by=company&push=1")
        assert queue.headers["HX-Push-Url"] == "/inbox?work_mode=remote&group_by=company&view=queue"
        assert get(r, "/inbox/queue?push=1").headers["HX-Push-Url"] == "/inbox?view=queue"
        assert snapshot(r) == before and not r.run.spy.requests


@pytest.mark.parametrize(
    "query",
    [
        "group_by=notes&view=bogus&band=x&sort=zz",
        "search=%00hidden",
        "search=" + "x" * 600,
        "page=999&selected=unknown-job",
        "flagged=maybe&work_mode=" + "y" * 600,
    ],
)
def test_garbage_filters_answer_200_and_change_nothing(tmp_path, query):
    with refining(tmp_path) as r:
        seeded(r, 5)
        before = snapshot(r)
        for path in ("/inbox", "/inbox/queue"):
            assert get(r, f"{path}?{query}").status_code == 200
        assert snapshot(r) == before


def test_filters_that_match_nothing_say_so_and_offer_clearing(tmp_path):
    with refining(tmp_path) as r:
        seeded(r, 5)
        shown = get(r, "/inbox/queue?search=nomatchwhatsoever&focus=1")
        assert "No jobs match these filters. Nothing was removed." in shown.text
        assert "No jobs yet" not in shown.text and "Clear filters" in shown.text
        assert "Filtered." in shown.headers["HX-Trigger-After-Settle"]


def test_grouped_page_labels_partial_groups(tmp_path):
    with refining(tmp_path) as r:
        seeded(r, 120)
        shown = get(r, "/inbox/queue?group_by=industry")
        assert " of 24 on this page)" in shown.text
        band = get(r, "/inbox/queue?group_by=band&view=queue")
        assert "Strong fit (" in band.text


def test_markup_in_values_company_and_view_names_is_escaped(tmp_path):
    hostile = '"><img src=x onerror=alert(1)>'
    with refining(tmp_path) as r:
        seeded(r, 3)
        r.run.execute("UPDATE companies SET name=? WHERE id='company-0001'", (hostile,))
        queued(r, value=hostile)
        org = r.run.app.state.inbox_organisation
        org.create_view("Hostile", {"search": hostile[:50]})
        for path in ("/inbox", "/inbox/queue?group_by=company", "/inbox/values"):
            assert "<img src=x" not in get(r, path).text, path


def test_stored_view_that_no_longer_validates_does_not_break_the_inbox(tmp_path):
    with refining(tmp_path) as r:
        seeded(r, 3)
        r.run.execute(
            "INSERT INTO saved_views (id,name,filters_json,created_at,updated_at) "
            "VALUES ('bad-view','Odd',?,'2026-10-20','2026-10-20')",
            ('{"sort": "zz"}',),
        )
        shown = get(r, "/inbox")
        assert shown.status_code == 200 and "Odd" in shown.text


def test_values_page_without_a_workspace_is_empty(tmp_path):
    with refining(tmp_path) as r:
        service = r.run.app.state.inbox_organisation
        service._database = lambda: None
        assert service.values() == {"pending": (), "controlled": ()}


def test_rename_error_belongs_to_the_renamed_view(tmp_path):
    with refining(tmp_path) as r:
        org = r.run.app.state.inbox_organisation
        first = org.create_view("First", {})
        org.create_view("Second", {})
        failed = post(
            r, f"/inbox/saved-views/{first['id']}", action="rename", version="1", name="second"
        )
        assert failed.status_code == 422
        assert f'id="view-error-{first["id"]}"' in failed.text
        assert 'id="saved-view-error"' not in failed.text


def test_deleted_view_elsewhere_gets_a_reload_prompt_and_delete_offers_undo(tmp_path):
    with refining(tmp_path) as r:
        org = r.run.app.state.inbox_organisation
        saved = org.create_view("Remote", {"work_mode": "remote"})
        gone = post(r, "/inbox/saved-views/missing", action="delete", version="1")
        assert gone.status_code == 409 and "view-reload" in gone.text
        deleted = post(r, f"/inbox/saved-views/{saved['id']}", action="delete", version="1")
        assert deleted.status_code == 200 and "View deleted." in deleted.text
        assert "Undo delete of Remote" in deleted.text and "work_mode=remote" in deleted.text
        undone = post(r, "/inbox/saved-views", name="Remote", filters="work_mode=remote&view=queue")
        assert undone.status_code == 200 and len(org.saved_views()) == 1


def test_filter_context_survives_triage_undo_and_prefetch_links(tmp_path):
    with refining(tmp_path) as r:
        seeded(r, 60)
        page = get(r, "/inbox/queue?work_mode=remote&group_by=company")
        assert "work_mode=remote" in page.text and "group_by=company" in page.text
        removed = post(
            r,
            "/inbox/jobs/job-0001/triage",
            version="1",
            action="remove",
            filters="work_mode=remote&group_by=company",
        )
        assert removed.status_code == 200 and "work_mode=remote" in removed.text
        # Only remote jobs (every third fixture) stay listed: neighbours come from the filtered set.
        assert 'data-job-id="job-0002"' not in removed.text
        undo = post(
            r,
            "/inbox/jobs/job-0001/triage",
            version="2",
            action="undo",
            filters="work_mode=remote&group_by=company",
        )
        assert undo.status_code == 200 and "work_mode=remote" in undo.text
        card = get(r, "/inbox/jobs/job-0001/card?work_mode=remote&group_by=company")
        assert card.status_code == 200 and "work_mode=remote" in card.text


@pytest.mark.parametrize("path", ["/inbox/values"])
def test_organisation_fragments_need_token(tmp_path, path):
    with refining(tmp_path) as r:
        assert r.client.get(path, headers={"HX-Request": "true"}).status_code == 403
        assert get(r, path).status_code == 200


@pytest.mark.parametrize(
    "path", ["/inbox/values/unknown-1", "/inbox/saved-views", "/inbox/saved-views/missing"]
)
def test_organisation_writes_need_token_and_origin(tmp_path, path):
    with refining(tmp_path) as r:
        assert r.client.post(path).status_code == 403
        assert (
            r.client.post(
                path, headers={**r.run.run.htmx(), "Origin": "https://hostile.example.test"}
            ).status_code
            == 403
        )


@pytest.mark.slow
def test_ac_m4_10_inbox_fragments_under_150ms_with_1000_jobs(tmp_path):
    with refining(tmp_path) as r:
        seed_jobs(r.run.app.state.workspace.db, generate_jobs(1000))
        paths = (
            "/inbox/queue",
            "/inbox/jobs/job-1000/card",
            "/inbox/jobs/job-0999/card",
            "/inbox/queue?work_mode=remote",
            "/inbox/queue?group_by=company",
            "/inbox/queue?search=greenhouse",
        )
        for path in paths:
            times = []
            for _ in range(5):
                start = perf_counter()
                shown = get(r, path)
                times.append(perf_counter() - start)
                assert shown.status_code == 200
                if "group_by" in path:
                    assert 'class="queue-group"' in shown.text
            assert median(times) < 0.150, (path, times)
