"""M6.10 the export download: a token-protected, read-only GET of one stored file (ADR-102 item 8
extended by ADR-106 item 10; spec 2.9 rules 2 and 12). The path comes only from the export row and
is checked to be a regular file inside the application's folder with no link on the way.

Assumed interface: the docstring of ``tests/export_pages.py`` (``GET /applications/{id}/exports/
{export_id}``, the fixed 404 ``DOWNLOAD_NOT_FOUND``). Real SQLite in a temp folder, no network.
"""

import os
import shutil
from pathlib import Path

import pytest

from app.domain.errors import ApplicationNotFound
from tests.export_pages import (
    DOWNLOAD_NOT_FOUND,
    MEDIA,
    Exporting,
    download_url,
    exporting_app,
)
from tests.integration.test_board import snapshot

pytestmark = pytest.mark.integration

SECRET = b"a secret outside the data folder, never to be served"


@pytest.fixture
def d(tmp_path):
    with exporting_app(tmp_path) as desk:
        yield desk


def exported(d: Exporting):
    app = d.export_ready()
    d.r.run.execute("UPDATE jobs SET title=? WHERE id=?", ("Hostile <i>title</i>", "export-job"))
    d.export_ok(app.id)
    return app


def test_download_needs_token_read_only_fixed_name_and_type(d: Exporting):
    app = exported(d)
    rows = d.exports(app.id)
    assert len(rows) == 4
    before_tables, before_files = snapshot(d.r), d.folder_state(app.id)
    requests = len(d.spy.requests)

    for row in rows:
        path = download_url(app.id, row["id"])
        # No token, no file; a page-style request with only the htmx header is not enough either.
        assert d.r.client.get(path).status_code == 403
        assert d.r.client.get(path, headers={"HX-Request": "true"}).status_code == 403

        answer = d.download(app.id, row["id"])

        assert answer.status_code == 200
        assert answer.content == d.file_bytes(row)
        assert answer.headers["Content-Type"] == MEDIA[row["format"]]
        # The name is the code-built one from the row, not the job's title or anything typed.
        assert (
            answer.headers["Content-Disposition"] == f'attachment; filename="{row["stored_name"]}"'
        )
        assert answer.headers["Cache-Control"] == "no-store"
        # It only reads: no row, no file, no request to a provider, no task.
        assert snapshot(d.r) == before_tables and d.folder_state(app.id) == before_files
        assert len(d.spy.requests) == requests
        # Nothing but GET is allowed on it.
        for method in ("post", "put", "delete", "patch"):
            refused = getattr(d.r.client, method)(
                path, headers=d.r.run.run.action(**{"HX-Request": "true"})
            )
            assert refused.status_code in (404, 405), (method, refused.status_code)
        assert snapshot(d.r) == before_tables and d.folder_state(app.id) == before_files


def test_download_of_another_application_unknown_or_unsafe_ids_is_the_fixed_404(d: Exporting):
    app = exported(d)
    other = d.export_ready("other-export-job")
    d.export_ok(other.id)
    mine = d.exports(app.id)[0]
    theirs = d.exports(other.id)[0]

    wrong_application = d.download(app.id, theirs["id"])
    assert wrong_application.status_code == 404
    assert wrong_application.text == DOWNLOAD_NOT_FOUND
    assert theirs["stored_name"] not in wrong_application.text
    for export_id in ("missing", "bad.id", "<script>", "..", "a" * 400):
        answer = d.download(app.id, export_id)
        assert answer.status_code == 404 and answer.text == DOWNLOAD_NOT_FOUND
        assert export_id not in answer.text
    for application_id in ("missing", "bad.id", "<script>"):
        answer = d.download(application_id, mine["id"])
        assert answer.status_code == 404
        assert answer.text in (DOWNLOAD_NOT_FOUND, ApplicationNotFound.message)
        assert application_id not in answer.text and mine["stored_name"] not in answer.text


def test_download_refuses_paths_outside_folder_and_links(d: Exporting, tmp_path: Path):
    app = exported(d)
    secret = tmp_path / "outside-secret.pdf"
    secret.write_bytes(SECRET)
    pdf, docx = d.export_of(app.id, "cv", "pdf"), d.export_of(app.id, "cv", "docx")
    letter = d.export_of(app.id, "cover_letter", "pdf")
    folder = d.folder(app.id)

    def refused(row) -> None:
        answer = d.download(app.id, row["id"])
        assert answer.status_code == 404 and answer.text == DOWNLOAD_NOT_FOUND
        assert SECRET not in answer.content and row["stored_name"] not in answer.text

    # The stored file is replaced by a link to a file outside the data folder.
    target = folder / pdf["stored_name"]
    kept = target.read_bytes()
    target.unlink()
    os.symlink(secret, target)
    refused(pdf)
    target.unlink()
    target.write_bytes(kept)
    assert d.download(app.id, pdf["id"]).status_code == 200  # restored: served again

    # A link to another file inside the same folder is a link too.
    target = folder / docx["stored_name"]
    kept = target.read_bytes()
    target.unlink()
    os.symlink(folder / pdf["stored_name"], target)
    refused(docx)
    target.unlink()
    target.write_bytes(kept)

    # The file is gone, or is a folder, or a dangling link.
    target = folder / letter["stored_name"]
    kept = target.read_bytes()
    target.unlink()
    refused(letter)
    target.mkdir()
    refused(letter)
    target.rmdir()
    os.symlink(tmp_path / "does-not-exist", target)
    refused(letter)
    target.unlink()
    target.write_bytes(kept)

    # The application's whole folder is a link to a folder that holds same-named files.
    away = tmp_path / "away"
    shutil.copytree(folder, away)
    for path in away.iterdir():
        path.write_bytes(SECRET)
    moved = folder.with_name(folder.name + "-real")
    folder.rename(moved)
    os.symlink(away, folder)
    for row in d.exports(app.id):
        refused(row)
    os.unlink(folder)
    moved.rename(folder)
    for row in d.exports(app.id):
        assert d.download(app.id, row["id"]).status_code == 200


def test_download_reads_nothing_it_was_not_given(d: Exporting):
    """The id of an export row is the only thing the route takes from the address: a stored name
    can't be requested, and a query string changes nothing."""
    app = exported(d)
    row = d.exports(app.id)[0]

    by_name = d.r.client.get(
        f"/applications/{app.id}/exports/{row['stored_name']}",
        headers=d.r.run.run.with_token(),
    )
    with_query = d.r.client.get(
        download_url(app.id, row["id"]) + "?name=../../../etc/passwd&path=/etc/passwd",
        headers=d.r.run.run.with_token(),
    )

    assert by_name.status_code == 404
    assert with_query.status_code in (200, 400) and b"root:" not in with_query.content
    if with_query.status_code == 200:
        assert with_query.content == d.file_bytes(row)
