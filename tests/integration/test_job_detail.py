"""M4.8 detail reads, corrected fields, current fact citations and protected writes."""

import json

import pytest

from app.services.facts import FactInput
from tests.integration.test_inbox import get, post, seeded
from tests.integration.test_job_fit import assess, prepare, reference, response
from tests.refine_harness import refining

pytestmark = pytest.mark.integration
DETAIL = "/inbox/jobs/job-0001/detail"
EDIT = "/inbox/jobs/job-0001/edit"


def test_drawer_fragment_needs_token(tmp_path):
    with refining(tmp_path) as r:
        seeded(r, 1)
        assert r.client.get(DETAIL).status_code == 403
        shown = get(r, DETAIL)
        assert shown.status_code == 200 and "<html" not in shown.text
        assert 'class="job-drawer"' in shown.text and "How you match" in shown.text


@pytest.mark.parametrize("id", ["job-0001", "missing", "bad!id", "x" * 65])
def test_job_page_by_safe_id_only_fixed_404(tmp_path, id):
    with refining(tmp_path) as r:
        seeded(r, 1)
        shown = r.client.get(f"/jobs/{id}")
        if id == "job-0001":
            assert shown.status_code == 200 and "<html" in shown.text
            assert 'id="job-detail"' in shown.text
            assert r.client.head(f"/jobs/{id}").status_code == 200
            assert r.client.get(f"/jobs/{id}", headers={"HX-Request": "true"}).status_code == 403
            assert "<html" not in get(r, f"/jobs/{id}").text
        else:
            assert (
                shown.status_code == 404
                and shown.text == "This job wasn't found. It may have been removed."
            )
            assert id not in shown.text


def test_detail_gets_change_nothing(tmp_path):
    with refining(tmp_path) as r:
        seeded(r, 1)
        before = r.run.rows("SELECT * FROM jobs")
        for path in (DETAIL, "/jobs/job-0001", "/inbox/jobs/job-0001/header"):
            assert get(r, path).status_code == 200
        assert r.run.rows("SELECT * FROM jobs") == before
        assert not r.run.spy.requests


def test_field_edit_marks_corrected_and_versions(tmp_path):
    with refining(tmp_path) as r:
        seeded(r, 1)
        saved = post(
            r,
            EDIT,
            version="1",
            title="Fictional gardener",
            company="New Orchard",
            salary_min="20.50",
            salary_max="30",
            locations="Paris\nLyon",
        )
        assert saved.status_code == 200 and "Saved." in saved.text
        row = r.run.rows("SELECT * FROM jobs")[0]
        assert row["version"] == 2 and row["title"] == "Fictional gardener"
        assert row["salary_min"] == 20.5 and json.loads(row["locations"]) == ["Paris", "Lyon"]
        corrected = json.loads(row["user_corrected"])
        assert set(corrected) == {"title", "company", "salary_min", "salary_max", "locations"}
        assert "Corrected by you" in saved.text
        assert r.run.rows("SELECT name FROM companies WHERE name='New Orchard'")


@pytest.mark.parametrize(
    "field,value",
    [
        ("title", " "),
        ("salary_min", "nan"),
        ("salary_max", "inf"),
        ("experience_min", "1.5"),
        ("experience_max", "-1"),
        ("posted_date", "2026-02-30"),
        ("notes", "x" * 10001),
    ],
)
def test_field_edit_422_ties_errors_to_fields(tmp_path, field, value):
    with refining(tmp_path) as r:
        seeded(r, 1)
        shown = post(r, EDIT, version="1", **{field: value})
        assert shown.status_code == 422
        assert f'aria-describedby="job-edit-{field}-error"' in shown.text
        assert 'aria-invalid="true"' in shown.text and "data-autofocus" in shown.text
        assert r.run.rows("SELECT version FROM jobs")[0]["version"] == 1


def test_edit_range_validation_is_atomic_and_notes_newlines_normalized(tmp_path):
    with refining(tmp_path) as r:
        seeded(r, 1)
        bad = post(r, EDIT, version="1", salary_min="90", salary_max="30", notes="fictional")
        assert bad.status_code == 422 and "job-edit-salary_max-error" in bad.text
        assert r.run.rows("SELECT notes FROM jobs")[0]["notes"] is None
        ok = post(r, EDIT, version="1", notes="Line one\r\nLine two")
        assert ok.status_code == 200
        assert r.run.rows("SELECT notes FROM jobs")[0]["notes"] == "Line one\nLine two"


def test_stale_field_edit_reload_prompt(tmp_path):
    with refining(tmp_path) as r:
        seeded(r, 1)
        assert post(r, EDIT, version="1", title="Saved title").status_code == 200
        shown = post(r, EDIT, version="1", title="Stale title")
        assert shown.status_code == 409 and "Reload the page" in shown.text
        assert r.run.rows("SELECT title FROM jobs")[0]["title"] == "Saved title"


def test_notes_saved_and_escaped(tmp_path):
    with refining(tmp_path) as r:
        seeded(r, 1)
        saved = post(r, EDIT, version="1", notes="<script>Fictional note</script>")
        assert saved.status_code == 200
        assert "<script>Fictional" not in saved.text and "&lt;script&gt;Fictional" in saved.text
        assert r.run.rows("SELECT user_corrected FROM jobs")[0]["user_corrected"] == "[]"


def test_original_link_only_http_https(tmp_path):
    with refining(tmp_path) as r:
        seeded(r, 1)
        r.run.execute(
            "INSERT INTO job_sources (id,job_id,url,canonical_url,kind,found_at,created_at) "
            "VALUES ('src','job-0001','https://example.test/ad','https://example.test/ad',"
            "'link','2026-10-20','2026-10-20')"
        )
        shown = get(r, DETAIL)
        assert (
            'href="https://example.test/ad"' in shown.text
            and 'rel="noopener noreferrer"' in shown.text
        )
        # A legacy/corrupt value must not turn into an executable link even when its prefix
        # passed the database's deliberately simple CHECK.
        r.run.execute("UPDATE job_sources SET url='https://example.test/' || char(10) || 'junk'")
        assert 'href="https://example.test/' not in get(r, DETAIL).text


def test_fit_uses_current_wording_links_and_marks_changed_fact(tmp_path):
    with refining(tmp_path) as r:
        job_id = prepare(r)
        fact = r.typed("Worked with fictional Python deployments.")
        assess(r, job_id, response(r, job_id, [reference(fact)]))
        r.facts.edit(
            fact.id,
            FactInput(text="Maintained fictional Python deployments."),
            expected_version=fact.version,
        )
        shown = get(r, f"/inbox/jobs/{job_id}/detail")
        assert shown.status_code == 200
        assert "Maintained fictional Python deployments." in shown.text
        assert f'href="/facts/{fact.id}/edit"' in shown.text
        assert "changed since this assessment" in shown.text
        assert "Worked with fictional Python deployments." not in shown.text
        assert "Practice data" in shown.text


def test_missing_requirement_shown_without_invented_explanation(tmp_path):
    with refining(tmp_path) as r:
        job_id = prepare(r)
        r.typed("Maintained fictional greenhouses.")
        assess(r, job_id, response(r, job_id, ["F999@1"]))
        shown = get(r, f"/inbox/jobs/{job_id}/detail")
        assert (
            "Missing" in shown.text
            and "/facts/" not in shown.text.split("How you match")[1].split("</section>")[0]
        )


def test_resummarize_and_assess_go_through_budget_gate(tmp_path, monkeypatch):
    from app.services.budget_gate import BlockedAction

    with refining(tmp_path) as r:
        job_id = prepare(r)
        r.typed("Worked with fictional deployments.")
        calls = []
        gate = r.run.app.state.budget_gate

        def blocked(service, **kwargs):
            calls.append(service)
            return BlockedAction("Budget reached. Open Settings to change it.")

        monkeypatch.setattr(gate, "check", blocked)
        shown = get(r, f"/inbox/jobs/{job_id}/detail")
        assert (
            shown.status_code == 200
            and "Summarize again" in shown.text
            and "Assess fit" in shown.text
        )
        assert len(calls) >= 2
        assert 'aria-disabled="true"' in shown.text and "Budget reached" in shown.text
        before = r.run.rows("SELECT * FROM tasks")
        for suffix in ("summarize", "assess-fit"):
            refused = post(r, f"/inbox/jobs/{job_id}/{suffix}")
            assert refused.status_code == 409 and "Budget reached" in refused.text
        assert r.run.rows("SELECT * FROM tasks") == before


@pytest.mark.parametrize("suffix", ["edit", "detail-triage", "split"])
def test_detail_writes_need_token_origin_and_strict_form(tmp_path, suffix):
    with refining(tmp_path) as r:
        seeded(r, 1)
        path = f"/inbox/jobs/job-0001/{suffix}"
        assert r.client.post(path, data={"version": "1"}).status_code == 403
        assert (
            r.client.post(
                path,
                data={"version": "1"},
                headers={**r.run.run.htmx(), "Origin": "https://hostile.example.test"},
            ).status_code
            == 403
        )
        assert post(r, path, version="1", extra="bad").status_code == 400


def test_detail_unknown_ids_404_before_field_validation(tmp_path):
    with refining(tmp_path) as r:
        for id in ("missing", "bad!id", "x" * 65):
            shown = post(r, f"/inbox/jobs/{id}/edit", version="1", title="")
            assert shown.status_code == 404 and id not in shown.text


def test_triage_reports_only_its_committed_version(tmp_path, monkeypatch):
    from app.api import job_detail

    with refining(tmp_path) as r:
        seeded(r, 1)
        original = job_detail._perform

        def intervening(*args):
            result = original(*args)
            r.run.app.state.job_detail.edit(
                "job-0001", {"title": "Concurrent correction"}, expected_version=result.version
            )
            return result

        monkeypatch.setattr(job_detail, "_perform", intervening)
        moved = post(r, "/inbox/jobs/job-0001/detail-triage", version="1", action="to_apply")
        assert moved.status_code == 200
        event = json.loads(moved.headers["HX-Trigger-After-Settle"])["job:changed"]
        assert event["previous_version"] == 1 and event["version"] == 2
        assert 'data-version="3"' in moved.text
        stale = post(r, EDIT, version=str(event["version"]), title="Old editor title")
        assert stale.status_code == 409
        assert r.run.rows("SELECT title FROM jobs")[0]["title"] == "Concurrent correction"


def test_detail_triage_422_tied_to_date_and_reason(tmp_path):
    with refining(tmp_path) as r:
        seeded(r, 1)
        post(r, "/inbox/jobs/job-0001/seen", version="1")
        for values, field in (
            ({"action": "snooze", "choice": "date", "picked": "bad"}, "picked"),
            ({"action": "remove", "reason": "other", "text": "x" * 501}, "text"),
        ):
            refused = post(r, "/inbox/jobs/job-0001/detail-triage", version="2", **values)
            assert refused.status_code == 422
            assert f'aria-describedby="detail-{field}-error"' in refused.text
            assert 'aria-invalid="true"' in refused.text
