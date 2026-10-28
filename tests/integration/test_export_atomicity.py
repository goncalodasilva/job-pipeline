"""Export commit failures and edits during rendering must not publish partial or stale files."""

import hashlib
from pathlib import Path

import pytest

from tests.export_pages import UnavailablePdf, exporting_app, probed_app, real_renderers
from tests.integration.test_board import snapshot
from tests.tailoring_ai_inputs import KEPT_TEXT

pytestmark = pytest.mark.integration


def test_export_database_failure_removes_all_new_files(tmp_path: Path):
    with exporting_app(tmp_path) as d:
        app = d.export_ready()
        d.r.run.execute(
            "CREATE TRIGGER export_boom BEFORE INSERT ON document_exports "
            "WHEN (SELECT count(*) FROM document_exports)>0 "
            "BEGIN SELECT RAISE(ABORT, 'boom'); END"
        )
        before = snapshot(d.r)
        answer = d.export(app.id)
        assert answer.status_code == 409
        assert snapshot(d.r) == before and d.folder_state(app.id) == {}


def test_export_exclusive_create_keeps_a_preexisting_file(tmp_path: Path):
    with exporting_app(tmp_path) as d:
        app = d.export_ready(letter=False)
        version = d.current_version_id(app.id)
        suffix = hashlib.sha256(version.encode()).hexdigest()[:24]
        folder = d.folder(app.id)
        folder.mkdir(parents=True)
        existing = folder / f"cv-v1-{suffix}.pdf"
        existing.write_bytes(b"preexisting bytes")
        before = snapshot(d.r)
        assert d.export(app.id).status_code == 409
        assert existing.read_bytes() == b"preexisting bytes"
        assert snapshot(d.r) == before and len(d.folder_files(app.id)) == 1


def test_export_edit_during_render_refuses_stale_files(tmp_path: Path, monkeypatch):
    with probed_app(tmp_path) as d:
        app = d.export_ready(letter=False)
        renderer = d.probes[0]
        render = renderer.render

        def edit_then_render(document):
            version = d.current_version_id(app.id)
            unit = d.units(version, removed=False)[1]
            d.service.edit_unit(app.id, version, unit["id"], KEPT_TEXT)
            return render(document)

        monkeypatch.setattr(renderer, "render", edit_then_render)
        answer = d.export(app.id)
        assert answer.status_code == 409
        assert 'id="submit-conflict"' in answer.text
        assert d.exports(app.id) == [] and d.folder_state(app.id) == {}


@pytest.mark.parametrize("sent", [False, True], ids=["approved", "sent"])
def test_partial_export_retry_preserves_original_content_after_details_change(
    tmp_path: Path, monkeypatch, sent: bool
):
    with exporting_app(
        tmp_path, renderers=lambda clock: (UnavailablePdf(), real_renderers(clock)[1])
    ) as d:
        app = d.export_ready()
        d.export_ok(app.id)
        if sent:
            d.move_ok(app.id, "applied")
        before = d.exports(app.id)
        original_files = d.folder_state(app.id)
        from tests.details_pages import Desk

        details = Desk(d.r.run)
        assert details.save(details.version(), name="Another Fictional Person").status_code == 200
        service = d.r.run.app.state.exports
        pdf, word = real_renderers(d.r.run.clock)
        rendered = []
        original_render = pdf.render

        def record(document):
            rendered.append(document)
            return original_render(document)

        monkeypatch.setattr(pdf, "render", record)
        service._renderers = (pdf, word)
        answer = d.export_ok(app.id)
        assert d.exports(app.id) == before and d.folder_state(app.id) == original_files
        assert rendered == []
        assert "new" in answer.text.lower() and "version" in answer.text.lower()
        assert "PDF" in answer.text
        # A newly approved version can export both formats with the changed details.
        version = d.current_version_id(app.id)
        unit = d.units(version, removed=False)[1]
        new = d.service.edit_unit(app.id, version, unit["id"], KEPT_TEXT).version_id
        d.service.approve(app.id, new)
        d.export_ok(app.id)
        assert {r["format"] for r in d.exports(app.id) if r["document_version_id"] == new} == {
            "pdf",
            "docx",
        }
        assert len(rendered) == 1 and rendered[0].author == "Another Fictional Person"
        assert {
            name: state for name, state in d.folder_state(app.id).items() if name in original_files
        } == original_files


def test_submit_targets_applied_after_active_columns_are_reordered(tmp_path: Path):
    with exporting_app(tmp_path) as d:
        app = d.cv_ready("reordered-job", template=False)
        d.r.run.execute("UPDATE application_statuses SET position=0 WHERE key='interviewing'")
        d.r.run.execute("UPDATE application_statuses SET position=99 WHERE key='applied'")
        assert d.mark_applied(app.id).status_code == 200
        assert d.application_row(app.id)["status_key"] == "applied"
