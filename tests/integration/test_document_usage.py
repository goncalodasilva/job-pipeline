"""M6.11 read-only document history, fact use and Today, with fictional data."""

import importlib

import pytest

from app.services.facts import FactInput
from tests.export_pages import exporting_app
from tests.tailoring_pages import by_id

pytestmark = pytest.mark.integration


@pytest.fixture
def d(tmp_path):
    with exporting_app(tmp_path) as data:
        yield data


def get(d, path):
    answer = d.r.client.get(path, headers=d.run_headers.htmx())
    assert answer.status_code == 200, answer.text[:300]
    return answer.text


def used(d, fact):
    return by_id(get(d, f"/facts/{fact}/edit"), "fact-used-in").text


def test_fact_library_lists_applications_that_used_it(d):
    app = d.export_ready(letter=False)
    fact = d.fact_row(1)["id"]
    for path in ("/facts?tab=library", f"/facts/{fact}/edit"):
        html = get(d, path)
        assert "Used in" in html
        assert f'href="/applications/{app.id}"' in html
    assert "Harbor Kite Fictional" in used(d, fact)
    assert "Sent" not in used(d, fact)


def test_used_in_notes_sent_version_differs_after_fact_edit(d):
    app = d.export_ready(letter=False)
    assert d.mark_applied(app.id).status_code == 200
    fact = d.fact_row(1)
    assert "Sent" in used(d, fact["id"])
    assert "The version sent differs" not in used(d, fact["id"])
    d.r.facts.edit(
        fact["id"],
        FactInput(text="Planned 70 delivery routes a week."),
        expected_version=fact["version"],
    )
    assert "The version sent differs from this one." in used(d, fact["id"])


def test_ac_m6_7_drawer_shows_documents_sent_with_versions_and_files(d):
    app = d.export_ready()
    d.export_ok(app.id)
    assert d.mark_applied(app.id).status_code == 200
    before = d.all_tables()
    for path in (f"/applications/{app.id}", f"/applications/{app.id}/detail"):
        html = get(d, path)
        box = by_id(html, "documents-sent")
        assert "Documents sent" in box.text
        assert "CV" in box.text and "Cover letter" in box.text
        assert "Version 1" in box.text and "Facts used" in box.text
        assert d.submissions()[0]["frozen_at"] in html
        for row in d.exports(app.id):
            assert f'data-file-download="/applications/{app.id}/exports/{row["id"]}"' in html
    assert d.all_tables() == before


def test_no_sent_documents_and_approved_without_export(d):
    app = d.export_ready(letter=False)
    assert "No documents from this app were recorded as sent." in get(d, f"/applications/{app.id}")
    assert d.mark_applied(app.id).status_code == 200
    html = get(d, f"/applications/{app.id}")
    assert "Version 1" in by_id(html, "documents-sent").text
    assert "No exported files" in html


def test_today_drafts_block_counts_document_drafts_with_links(d):
    app = d.export_ready(cv_approved=False, letter_approved=False)
    before = d.all_tables()
    html = get(d, "/")
    box = by_id(html, "today-drafts")
    assert "2 document drafts" in box.text
    for step in (3, 4):
        assert f"/applications/{app.id}?tab=tailoring&amp;step={step}" in html
    assert "Nothing needs your attention" not in html
    d.service.approve(app.id, d.current_version_id(app.id))
    d.letters.approve(app.id, d.current_letter_id(app.id))
    assert "document drafts" not in get(d, "/")
    # All GETs above the approvals left storage untouched.
    assert before["tasks"] == d.all_tables()["tasks"]


def test_used_in_and_sent_documents_escaped(d):
    app = d.export_ready(letter=False)
    d.r.run.execute(
        "UPDATE jobs SET title=? WHERE id=?", ('<script>alert("x")</script>', app.job_id)
    )
    assert d.mark_applied(app.id).status_code == 200
    for path in ("/facts?tab=library", f"/facts/{d.fact_row(1)['id']}/edit"):
        html = get(d, path)
        assert '<script>alert("x")</script>' not in html
        assert "&lt;script&gt;" in html


def test_used_in_query_uses_fact_index(d):
    d.export_ready(letter=False)
    repo = importlib.import_module("app.modules.storage.document_usage_repo")
    fact_id = d.fact_row(1)["id"]
    with d.db.read() as conn:
        queries = []
        conn.set_trace_callback(queries.append)
        found = repo.used_in(conn, (fact_id,))
        conn.set_trace_callback(None)
        assert found
        assert len(queries) == 1
        plan = conn.execute("EXPLAIN QUERY PLAN " + queries[0]).fetchall()
    assert any("document_unit_facts_fact_id" in str(tuple(row)) for row in plan)


def test_superseded_and_removed_units_are_not_current_use_but_sent_use_stays(d):
    app = d.export_ready(letter=False)
    fact = d.fact_row(1)["id"]
    while True:
        version = d.current_version_id(app.id)
        units = d.rows(
            "SELECT u.id FROM document_units u JOIN document_unit_facts uf "
            "ON uf.document_unit_id=u.id WHERE u.document_version_id=? "
            "AND uf.fact_id=? AND u.removed=0",
            (version, fact),
        )
        if not units:
            break
        d.service.remove_unit(app.id, version, units[0]["id"])
    assert "Harbor Kite Fictional" not in used(d, fact)
    # The approved earlier version is still selected by the first active move.
    assert d.mark_applied(app.id).status_code == 200
    assert "Harbor Kite Fictional" in used(d, fact)
    assert "Sent" in used(d, fact)


def test_sent_files_and_fact_versions_stay_frozen_after_document_edit(d):
    app = d.export_ready(letter=False)
    d.export_ok(app.id)
    assert d.mark_applied(app.id).status_code == 200
    sent = by_id(get(d, f"/applications/{app.id}"), "documents-sent").text
    version = d.current_version_id(app.id)
    unit = d.unit_with(version, "Planned 60 delivery routes a week with a team of 3.")
    d.service.edit_unit(app.id, version, unit["id"], "Planned delivery routes.")
    assert by_id(get(d, f"/applications/{app.id}"), "documents-sent").text == sent


def test_all_usage_pages_and_fragments_read_only_with_protected_fragments(d):
    app = d.export_ready(cv_approved=False, letter=False)
    fact = d.fact_row(1)["id"]
    before = d.all_tables()
    for path in ("/", "/facts", f"/facts/{fact}/edit", f"/applications/{app.id}"):
        answer = d.r.client.get(path)
        assert answer.status_code == 200
        assert "<!DOCTYPE html>" in answer.text or "<!doctype html>" in answer.text
    for path in (
        "/today/content",
        "/facts/library",
        f"/facts/{fact}/history",
        f"/applications/{app.id}/detail",
    ):
        assert d.r.client.get(path).status_code == 403
        get(d, path)
    assert d.all_tables() == before
