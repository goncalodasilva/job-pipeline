"""M6.10 exports: the approved CV and cover letter rendered to PDF and Word (AC-M6.6; AC-M6.5 photo;
ADR-106 items 3, 4 and 10; ADR-108; ADR-110 item 3; spec 2.9 rules 2, 12 and 13).

Assumed interface: the docstring of ``tests/export_pages.py`` (``POST .../tailoring/5/export``,
``ExportService`` on ``app.state.exports``, ``Wiring.document_renderers``, the file and row rules).
The renderers are the real ones (typst and python-docx) unless a test wraps them in a ``Probe``;
the files are read back the way a user's reader would (pypdf, python-docx). Real SQLite in a temp
folder, the fake provider behind the spy, a frozen clock, no network, no fixed waits.
"""

import io
import os
import stat
from pathlib import Path

import pytest

from app.domain import errors
from tests.export_pages import (
    EXPORT_ANNOUNCE,
    GENERATED_NAME,
    PDF_UNAVAILABLE,
    Exporting,
    UnavailablePdf,
    assert_text_is_exactly,
    exporting_app,
    exports_module,
    probed_app,
    real_renderers,
    text_of,
)
from tests.integration.test_board import snapshot
from tests.render_support import docx_media, pdf_image_count, pdf_page_count
from tests.tailoring_ai_inputs import KEPT_TEXT
from tests.tailoring_pages import announced, by_id, has_id, said_text

pytestmark = pytest.mark.integration

FIRST_LINE = "Planned 60 delivery routes a week with a team of 3."
LANGUAGE_LINE = "English, used at work every day."


@pytest.fixture
def d(tmp_path):
    with exporting_app(tmp_path) as desk:
        yield desk


@pytest.fixture
def pd(tmp_path):
    with probed_app(tmp_path) as desk:
        yield desk


def edited_and_trimmed(d: Exporting, job_id: str = "export-job"):
    """A CV the user changed before approving it: one line reworded, one removed. The first
    approved version is therefore the third; the removed line must not be in the files."""
    app = d.export_ready(job_id, cv_approved=False)
    first = d.current_version_id(app.id)
    reworded = d.service.edit_unit(app.id, first, d.unit_with(first, FIRST_LINE)["id"], KEPT_TEXT)
    second = reworded.version_id
    third = d.service.remove_unit(app.id, second, d.unit_with(second, LANGUAGE_LINE)["id"])
    d.service.approve(app.id, third)
    return app, third


def check_both_documents(d: Exporting, app, fmt: str) -> None:
    for kind in ("cv", "cover_letter"):
        row = d.export_of(app.id, kind, fmt)
        extracted = text_of(d.file_bytes(row), fmt)
        pieces, known = (d.cv_pieces if kind == "cv" else d.letter_pieces)(app.id)
        assert_text_is_exactly(extracted, pieces, known=known)


# --- the files hold exactly the approved content -------------------------------------------------


def test_ac_m6_6_pdf_export_text_equals_approved_content(d: Exporting):
    app, version = edited_and_trimmed(d)
    d.export_ok(app.id)

    check_both_documents(d, app, "pdf")
    cv_text = text_of(d.file_bytes(d.export_of(app.id, "cv", "pdf")), "pdf")
    assert "mostlyatnight" in cv_text  # the user's edit is in the file
    assert "Englishusedatworkeveryday" not in cv_text.replace(",", "").replace(".", "")
    assert d.exports(app.id)[0]["number"] == d.version_row(version)["number"]


def test_ac_m6_6_word_export_text_equals_approved_content(d: Exporting):
    app, _version = edited_and_trimmed(d)
    d.export_ok(app.id)

    check_both_documents(d, app, "docx")
    cv_text = text_of(d.file_bytes(d.export_of(app.id, "cv", "docx")), "docx")
    assert "mostlyatnight" in cv_text
    assert "Englishusedatworkeveryday" not in cv_text.replace(",", "").replace(".", "")


def test_ac_m6_6_exports_open_and_page_count_recorded(d: Exporting):
    import hashlib

    from pypdf import PdfReader

    app = d.export_ready()

    answer = d.export_ok(app.id)

    assert EXPORT_ANNOUNCE in announced(answer)
    rows = d.exports(app.id)
    assert sorted((row["kind"], row["format"]) for row in rows) == [
        ("cover_letter", "docx"),
        ("cover_letter", "pdf"),
        ("cv", "docx"),
        ("cv", "pdf"),
    ]
    for row in rows:
        content = d.file_bytes(row)
        assert row["sha256"] == hashlib.sha256(content).hexdigest()
        assert row["size_bytes"] == len(content) > 0
        if row["format"] == "pdf":
            assert content.startswith(b"%PDF-")
            assert (
                len(PdfReader(io.BytesIO(content)).pages) == row["pages"] == pdf_page_count(content)
            )
            assert row["pages"] >= 1
        else:
            assert row["pages"] is None  # Word has no fixed pages
            assert text_of(content, "docx")  # python-docx opens it and finds the words
    cv_pdf = d.export_of(app.id, "cv", "pdf")
    assert cv_pdf["pages"] <= 2  # the template allows 2


# --- the photo: only with a slot AND an upload (AC-M6.5) ------------------------------


@pytest.mark.parametrize("case", ["slot_and_photo", "slot_no_photo", "photo_no_slot"])
def test_ac_m6_5_photo_only_with_slot_and_upload(pd: Exporting, case: str):
    slot = case != "photo_no_slot"
    photo = case != "slot_no_photo"
    app = pd.export_ready(photo=photo, photo_slot=slot)

    pd.export_ok(app.id)

    expected = 1 if slot and photo else 0
    pdf_probe, word_probe = pd.probes
    cv_pdf = pd.file_bytes(pd.export_of(app.id, "cv", "pdf"))
    cv_docx = pd.file_bytes(pd.export_of(app.id, "cv", "docx"))
    assert pdf_image_count(cv_pdf) == len(docx_media(cv_docx)) == expected
    # The document handed to the renderers carries the stored (metadata-stripped) picture, or none;
    # of the two documents (CV and letter) only the CV can have it.
    for probe in (pdf_probe, word_probe):
        photos = [call.photo for call in probe.calls]
        assert len(photos) == 2
        assert sorted(photos, key=lambda p: p is not None) == (
            [None, pd.photo_bytes()] if expected else [None, None]
        )
    # A cover letter never carries a photo.
    letter_pdf = pd.file_bytes(pd.export_of(app.id, "cover_letter", "pdf"))
    letter_docx = pd.file_bytes(pd.export_of(app.id, "cover_letter", "docx"))
    assert pdf_image_count(letter_pdf) == len(docx_media(letter_docx)) == 0


# --- refusals ------------------------------------------------------------------------------------


def test_export_refused_for_unapproved_or_missing_name(pd: Exporting):
    exports = exports_module()
    before_files = pd.all_files_under_data()

    # No name saved: nothing is written, no renderer runs, the sentence says why.
    nameless = pd.export_ready("nameless-job", details=False)
    tables = snapshot(pd.r)
    refused = pd.export(nameless.id)
    assert refused.status_code == 409
    assert said_text(by_id(refused.text, "submit-error").text) == exports.NO_NAME
    assert pd.exports() == [] and snapshot(pd.r) == tables
    assert pd.all_files_under_data() == before_files
    assert all(probe.calls == [] for probe in pd.probes)

    # A name but a CV that is only a draft.
    pd.save_contact()
    draft = pd.export_ready("draft-job", cv_approved=False, letter=False)
    refused = pd.export(draft.id)
    assert refused.status_code == 409
    assert said_text(by_id(refused.text, "submit-error").text) == exports.NOT_APPROVED
    # No CV at all.
    empty = pd.cv_ready("empty-job", template=False)
    refused = pd.export(empty.id)
    assert refused.status_code == 409
    assert said_text(by_id(refused.text, "submit-error").text) == exports.NOT_APPROVED
    assert pd.exports() == [] and pd.all_files_under_data() == before_files
    assert all(probe.calls == [] for probe in pd.probes)

    # The same refusals when the unknown, unsafe or extra: ids are not found, fields are invalid.
    assert pd.export("missing-application").status_code == 404
    assert pd.export("bad.id").status_code == 404
    ready = pd.export_ready("ready-job", letter=False)
    assert pd.export(ready.id, {"format": "pdf"}).status_code == 400
    assert pd.exports() == []


def test_export_leaves_out_a_letter_that_is_still_a_draft(d: Exporting):
    app = d.export_ready(letter_approved=False)

    answer = d.export(app.id)

    assert answer.status_code == 200
    assert sorted((row["kind"], row["format"]) for row in d.exports(app.id)) == [
        ("cv", "docx"),
        ("cv", "pdf"),
    ]


# --- files: mode, names, folder ------------------------------------------------------------------


def test_export_files_0600_code_built_names_in_application_folder(d: Exporting):
    app = d.export_ready()
    d.r.run.execute(
        "UPDATE jobs SET title=? WHERE id=?", ("../../etc/<b>Route Planner</b>", "export-job")
    )
    before = d.all_files_under_data()

    d.export_ok(app.id)

    folder = d.folder(app.id)
    assert folder.is_dir() and not folder.is_symlink()
    assert stat.S_IMODE(folder.lstat().st_mode) == 0o700
    rows = d.exports(app.id)
    assert len(rows) == 4
    for row in rows:
        path = folder / row["stored_name"]
        info = path.lstat()
        assert stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600
        match = GENERATED_NAME.fullmatch(row["stored_name"])
        assert match is not None, row["stored_name"]
        prefix = "cv" if row["kind"] == "cv" else "cover-letter"
        assert (match.group(1), int(match.group(2)), match.group(4)) == (
            prefix,
            row["number"],
            row["format"],
        )
        for text in ("Route", "Planner", "Harbor", "Zara", "Quillfeather", "etc"):
            assert text.lower() not in row["stored_name"].lower()
    # Nothing but those four files was added, and all of them are in the application's folder.
    added = d.all_files_under_data() - before
    assert added == {f"documents/{app.id}/{row['stored_name']}" for row in rows}
    assert sorted(path.name for path in folder.iterdir()) == sorted(r["stored_name"] for r in rows)
    # Two applications never collide on a name (the schema keeps names unique across all of them).
    other = d.export_ready("second-export-job")
    d.export_ok(other.id)
    names = [row["stored_name"].lower() for row in d.exports()]
    assert len(names) == len(set(names)) == 8
    assert {row["application_id"] for row in d.exports()} == {app.id, other.id}


def test_export_refuses_a_linked_documents_or_application_folder(d: Exporting, tmp_path: Path):
    app = d.export_ready()
    outside = tmp_path / "outside-target"
    outside.mkdir()
    (d.data_dir / "documents").mkdir(exist_ok=True)

    # The application's folder is a link to somewhere else.
    os.symlink(outside, d.folder(app.id))
    refused = d.export(app.id)
    assert refused.status_code == 409
    assert list(outside.iterdir()) == [] and d.exports() == []
    os.unlink(d.folder(app.id))

    # The documents folder itself is a link.
    moved = d.data_dir / "documents-real"
    (d.data_dir / "documents").rename(moved)
    os.symlink(outside, d.data_dir / "documents")
    refused = d.export(app.id)
    assert refused.status_code == 409
    assert list(outside.iterdir()) == [] and d.exports() == []
    assert d.exports() == []


def test_export_failure_leaves_no_file_or_row(tmp_path: Path):
    for number, error in enumerate(
        (errors.RenderFailed(), errors.RenderTooLarge(), errors.RenderTimedOut())
    ):
        folder = tmp_path / f"run-{number}"
        folder.mkdir()
        with probed_app(folder) as d:
            app = d.export_ready()
            # The second Word render (the letter, after the CV in both formats and the letter's
            # PDF have been rendered) fails: the first files must not survive it.
            d.probes[1].fail_on_call = 2
            d.probes[1].error = error
            before_files, before_tables = d.all_files_under_data(), snapshot(d.r)

            refused = d.export(app.id)

            assert refused.status_code == 409, error.kind
            assert said_text(by_id(refused.text, "submit-error").text) == error.message
            for name in ("RenderFailed", "RenderTooLarge", "RenderTimedOut", "Traceback"):
                assert name not in refused.text
            assert d.exports() == []
            assert d.all_files_under_data() == before_files
            assert [p for p in d.folder_files(app.id) if p.is_file()] == []
            assert snapshot(d.r) == before_tables
            # The renderers were not the problem next time: the same export now works whole.
            d.probes[1].fail_on_call = None
            assert d.export(app.id).status_code == 200
            assert len(d.exports(app.id)) == 4


def test_export_renders_off_the_event_loop(pd: Exporting):
    app = pd.export_ready()

    pd.export_ok(app.id)

    pdf_probe, word_probe = pd.probes
    assert len(pdf_probe.calls) == len(word_probe.calls) == 2  # the CV and the letter
    assert pdf_probe.on_loop == word_probe.on_loop == [False, False]


def test_an_exported_version_reuses_its_files_and_a_new_version_adds_new_ones(pd: Exporting):
    app = pd.export_ready()
    pd.export_ok(app.id)
    first_rows = pd.exports(app.id)
    state = pd.folder_state(app.id)
    tables = snapshot(pd.r)

    again = pd.export_ok(app.id)

    assert EXPORT_ANNOUNCE in announced(again)
    assert [len(p.calls) for p in pd.probes] == [2, 2]  # nothing was rendered again
    assert pd.exports(app.id) == first_rows and pd.folder_state(app.id) == state
    assert snapshot(pd.r) == tables

    # The user rewords one CV line and approves: only the CV is rendered again, as version 2 files.
    version = pd.current_version_id(app.id)
    unit = pd.unit_with(version, FIRST_LINE)
    second = pd.service.edit_unit(app.id, version, unit["id"], KEPT_TEXT).version_id
    pd.service.approve(app.id, second)
    pd.export_ok(app.id)

    assert [len(p.calls) for p in pd.probes] == [3, 3]
    rows = pd.exports(app.id)
    assert len(rows) == 6 and {r["number"] for r in rows if r["kind"] == "cv"} == {1, 2}
    for old in first_rows:  # the first files (they may have been sent) are untouched
        assert pd.file_bytes(old) == pd.file_bytes(next(r for r in rows if r["id"] == old["id"]))
    assert {name: pd.folder_state(app.id)[name] for name in state} == state
    check_new = text_of(pd.file_bytes(pd.export_of(app.id, "cv", "pdf", 2)), "pdf")
    assert "mostlyatnight" in check_new
    old_text = text_of(pd.file_bytes(pd.export_of(app.id, "cv", "pdf", 1)), "pdf")
    assert "mostlyatnight" not in old_text


def test_renderer_unavailable_keeps_word_export(tmp_path: Path):
    def renderers(clock):
        return UnavailablePdf(), real_renderers(clock)[1]

    with exporting_app(tmp_path, renderers=renderers) as d:
        app = d.export_ready()

        answer = d.export(app.id)

        assert answer.status_code == 200
        assert said_text(by_id(answer.text, "submit-unavailable").text) == PDF_UNAVAILABLE
        assert PDF_UNAVAILABLE in " ".join(announced(answer))
        assert "RendererUnavailable" not in answer.text
        rows = d.exports(app.id)
        assert sorted((r["kind"], r["format"]) for r in rows) == [
            ("cover_letter", "docx"),
            ("cv", "docx"),
        ]
        check_both_documents(d, app, "docx")
        assert sorted(p.suffix for p in d.folder_files(app.id)) == [".docx", ".docx"]
        # The button is still offered for the Word files, and nothing names a PDF.
        shown = d.step5(app.id).text
        assert has_id(shown, "submit-files")


def test_export_writes_no_provider_request_and_no_task(pd: Exporting):
    app = pd.export_ready()
    requests = len(pd.spy.requests)
    tasks = pd.rows("SELECT id FROM tasks")

    pd.export_ok(app.id)
    pd.export_ok(app.id)

    assert len(pd.spy.requests) == requests and pd.rows("SELECT id FROM tasks") == tasks


# --- orphans from a crash between the file and its row -------------------------------------------


def test_orphan_export_files_removed_at_open_others_untouched(tmp_path: Path):
    outside = tmp_path / "outside-file.pdf"
    outside.write_bytes(b"outside file that a link points to")
    away = tmp_path / "away-folder"
    away.mkdir()
    (away / "cv-v4.pdf").write_bytes(b"in a folder that the application folder links to")

    with exporting_app(tmp_path) as first:
        app = first.export_ready()
        other = first.export_ready("second-export-job")
        first.export_ok(app.id)
        first.export_ok(other.id)
        rows = first.exports()
        named = {r["stored_name"]: first.file_bytes(r) for r in rows}
        folder = first.folder(app.id)
        sibling = first.folder(other.id)

        orphans = {
            "cv-v9.pdf": b"left by a kill before the row",
            "cover-letter-v9-abc123.docx": b"another one",
            "cv-v11.docx": b"and a third",
        }
        others = {
            "notes.txt": b"the user's own file",
            "cv-final.pdf": b"not a generated name",
            "cv-v7.pdf.bak": b"another extension",
            "my-cv-v3.pdf": b"a longer name",
            "Cv-v3.PDF": b"other case",
        }
        for name, data in {**orphans, **others}.items():
            (folder / name).write_bytes(data)
        (folder / "cv-v8.pdf").mkdir()
        (folder / "cv-v8.pdf" / "inside.txt").write_bytes(b"inside a folder")
        (folder / "nested").mkdir()
        (folder / "nested" / "cv-v6.pdf").write_bytes(b"only direct children")
        os.symlink(outside, folder / "cv-v5.pdf")
        # An orphan of another application is removed in its own folder only.
        (sibling / "cv-v12.pdf").write_bytes(b"orphan of the second application")
        # A link where an application folder should be: nothing behind it is swept.
        os.symlink(away, first.data_dir / "documents" / "linked-application")

    with exporting_app(tmp_path, seed=False) as reopened:  # opening the workspace sweeps
        left = sorted(p.name for p in folder.iterdir())
        assert not set(orphans) & set(left), f"orphans left: {set(orphans) & set(left)}"
        assert not (sibling / "cv-v12.pdf").exists()
        for row in rows:
            owner = folder if row["application_id"] == app.id else sibling
            assert (owner / row["stored_name"]).read_bytes() == named[row["stored_name"]]
        for name, data in others.items():
            assert (folder / name).read_bytes() == data, f"{name} was touched"
        assert (folder / "cv-v8.pdf" / "inside.txt").read_bytes() == b"inside a folder"
        assert (folder / "nested" / "cv-v6.pdf").read_bytes() == b"only direct children"
        assert (folder / "cv-v5.pdf").is_symlink() and outside.exists()
        assert outside.read_bytes() == b"outside file that a link points to"
        assert (away / "cv-v4.pdf").exists(), "a linked application folder is not swept"
        assert len(reopened.exports()) == len(rows) == 8
        # Exported files still download.
        row = reopened.exports(app.id)[0]
        got = reopened.download(app.id, row["id"])
        assert got.status_code == 200 and got.content == reopened.file_bytes(row)
        assert reopened.spy.requests == []
