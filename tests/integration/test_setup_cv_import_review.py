"""Setup step 4 after review (M3.7; AC-M3.8): refusals tied to the field they belong to with
the pasted text shown back, the size cap, a start held back once the CV is stored, the step
marking itself done when the Fact bank is used, and a fact-bank check that fails at start-up.

Builds on ``tests/integration/test_setup_cv_import.py``. A real migrated SQLite database under
``tmp_path``, the frozen clock, no network; every value is fictional.
"""

import logging
import sqlite3
from pathlib import Path

import pytest

import app.main as main_module
from app import __version__
from app.api.uploads import MAX_UPLOAD_BYTES
from app.config import write_bootstrap
from app.onboarding.steps.cv_import import MSG_KEPT
from tests.cv_import_harness import importing, message
from tests.integration.test_setup_cv_import import (
    ALL_DONE,
    BEFORE_STEP,
    CV_TEXT,
    ERROR_ID,
    NEXT_ANNOUNCEMENT,
    STEP,
    TEXT_ID,
    assert_moved_to_preferences,
    file_field,
    paste_body,
    post_upload,
    status_of,
    upload_form,
)
from tests.integration.test_setup_preferences_done import SAVED, started
from tests.integration.test_setup_preferences_done import seed as seed_setup
from tests.integration.test_setup_provider import required
from tests.integration.test_setup_redirect import assert_not_redirected, outline
from tests.setup_harness import seed_workspace
from tests.upload_bodies import FieldPart, FilePart, multipart_body

pytestmark = pytest.mark.integration

TEXT_ERROR_ID = "setup-cv-text-error"
CHOOSE_ONE = "Choose one: upload a file or paste the text, not both."
HOSTILE_CV = '"></textarea><script id="injected">window.fictional=1</script>'
FICTIONAL_LINE = "Alex Example, fictional route planner at Example Freight Lda"


def padded_pdf(size: int, text: str) -> bytes:
    """A one-page PDF saying ``text``, padded with an unused stream to exactly ``size``
    bytes, its cross-reference table right."""

    def build(pad: int) -> bytes:
        content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
        objects = [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
            b"/Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length "
            + str(len(content)).encode()
            + b" >>\nstream\n"
            + content
            + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            b"<< /Length " + str(pad).encode() + b" >>\nstream\n" + b"0" * pad + b"\nendstream",
        ]
        out = bytearray(b"%PDF-1.4\n")
        offsets = []
        for number, body in enumerate(objects, start=1):
            offsets.append(len(out))
            out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
        xref = len(out)
        out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
        for offset in offsets:
            out += f"{offset:010d} 00000 n \n".encode()
        trailer = f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
        return bytes(out + trailer.encode())

    pad = size - len(build(0))
    for _ in range(5):
        got = len(build(pad))
        if got == size:
            return build(pad)
        pad += size - got
    raise AssertionError("no padding gives that size")


@pytest.mark.parametrize(
    ("case", "error"),
    [("too_long", "CvTextTooLong"), ("no_text", "NoTextFound"), ("both", None)],
)
def test_step4_pasted_text_refusal_is_tied_to_the_text_and_shown_back(
    tmp_path: Path, case: str, error: str | None
) -> None:
    long_text = (FICTIONAL_LINE + "\n") * (60_000 // len(FICTIONAL_LINE) + 2)
    bodies = {
        "too_long": (paste_body(long_text), long_text),
        "no_text": (paste_body("﻿\n"), "﻿\n"),
        "both": (multipart_body(FilePart("cv.txt", CV_TEXT.encode()), FieldPart(CV_TEXT)), None),
    }
    body, typed = bodies[case]
    said = CHOOSE_ONE if error is None else message(error)
    seed(tmp_path)
    with importing(tmp_path, seed=False) as run:
        response = post_upload(run, body, htmx=True)

        assert response.status_code == 422, (response.status_code, response.text[:300])
        elements = outline(response.text)
        text = required(elements, TEXT_ID)
        assert text.attrs.get("aria-invalid") == "true"
        assert TEXT_ERROR_ID in (text.attrs.get("aria-describedby") or "").split()
        assert required(elements, TEXT_ERROR_ID).text == said
        focused = [e.attrs.get("id") for e in elements if "data-autofocus" in e.attrs]
        assert focused == [TEXT_ID]
        assert "aria-invalid" not in file_field(elements).attrs
        if typed is not None:
            # The outline reads text with whitespace runs as one space.
            assert text.text.split() == typed.split(), "what was pasted is shown back"
        run.assert_nothing_stored()
        assert status_of(run) == "pending"


def test_step4_pasted_text_is_escaped_when_shown_back(tmp_path: Path) -> None:
    seed(tmp_path)
    with importing(tmp_path, seed=False) as run:
        refused_text = HOSTILE_CV + "\n" + FICTIONAL_LINE * (60_000 // len(FICTIONAL_LINE) + 2)
        response = post_upload(run, paste_body(refused_text), htmx=True)
        assert response.status_code == 422
        assert 'id="injected"' not in response.text
        assert "</textarea><script" not in response.text


def test_step4_file_refusal_stays_on_the_file_field(tmp_path: Path) -> None:
    seed(tmp_path)
    with importing(tmp_path, seed=False) as run:
        body = multipart_body(FilePart("cv.pdf", b"%PDF-1.4 truncated"), FieldPart(""))
        response = post_upload(run, body, htmx=True)
        assert response.status_code == 422
        elements = outline(response.text)
        assert file_field(elements).attrs.get("aria-invalid") == "true"
        assert "aria-invalid" not in required(elements, TEXT_ID).attrs
        required(elements, ERROR_ID)
        run.assert_nothing_stored()


def test_step4_upload_at_the_cap_is_read_and_one_byte_more_is_refused(tmp_path: Path) -> None:
    """Exactly 10 MB is taken whole (a fictional one-page PDF, padded); one byte more is
    refused on the file field as too large, and nothing is stored."""
    seed(tmp_path)
    with importing(tmp_path, seed=False) as run:
        over = padded_pdf(MAX_UPLOAD_BYTES + 1, FICTIONAL_LINE)
        refused = post_upload(run, multipart_body(FilePart("cv.pdf", over), FieldPart("")))
        assert refused.status_code == 422, (refused.status_code, refused.text[:300])
        elements = outline(refused.text)
        assert file_field(elements).attrs.get("aria-invalid") == "true"
        assert required(elements, ERROR_ID).text == message("UploadTooLarge")
        run.assert_nothing_stored()
        assert status_of(run) == "pending"

        at_cap = padded_pdf(MAX_UPLOAD_BYTES, FICTIONAL_LINE)
        assert len(at_cap) == MAX_UPLOAD_BYTES
        taken = post_upload(run, multipart_body(FilePart("cv.pdf", at_cap), FieldPart("")))
        assert_moved_to_preferences(taken, htmx=False, said="")
        stored = run.only_import()
        assert stored["kind"] == "pdf"
        assert status_of(run) == "done"
        run.wait_terminal(run.only_task().id)


def test_step4_without_a_provider_keeps_the_upload_and_moves_on(tmp_path: Path) -> None:
    """Once stored, a start that can't happen (no provider saved) never stops the guide: the
    step is done, the upload is kept for the Fact bank, no task is queued, and the
    announcement says so."""

    write_bootstrap(tmp_path / "cfg", data_dir=tmp_path / "data", version=__version__)
    seed_workspace(tmp_path / "data", done=BEFORE_STEP)
    with importing(tmp_path, seed=False) as run:
        response = post_upload(run, paste_body(CV_TEXT), htmx=True)

        assert response.status_code == 200, (response.status_code, response.text[:300])
        assert response.headers.get("hx-push-url") == "/setup/preferences"
        assert f"{MSG_KEPT} {NEXT_ANNOUNCEMENT}" in response.headers["hx-trigger"]
        row = run.only_import()
        assert row["state"] == "uploaded"
        assert [path.name for path in run.stored_files()] == [row["stored_name"]]
        assert run.tasks() == [] and run.spy.requests == []
        assert status_of(run) == "done"


# --- marks itself done when the Fact bank is used -------------------------------------------


def seed(tmp_path: Path, done: tuple[str, ...] = BEFORE_STEP) -> None:
    seed_setup(tmp_path, done=done)


def test_step4_done_after_a_fact_bank_import_without_restart(tmp_path: Path) -> None:
    """A guide finished before step 4 existed: importing from the Fact bank marks the step
    done at once (ADR-052), with no banner."""

    seed_setup(tmp_path, done=(*ALL_DONE, "done"), preferences=SAVED)
    with importing(tmp_path, seed=False) as run:
        assert status_of(run) == "pending"
        before = run.client.get(STEP)
        assert before.status_code == 200 and status_of(run) == "pending", "GET writes nothing"

        response = run.paste(CV_TEXT)

        assert response.status_code in (200, 303)
        assert status_of(run) == "done"
        assert run.app.state.onboarding.statuses.get("cv_import") == "done", "refreshed"
        run.wait_terminal(run.only_task().id)


def test_step4_done_after_a_fact_is_added(tmp_path: Path) -> None:

    seed_setup(tmp_path, done=(*ALL_DONE, "done"), preferences=SAVED)
    with importing(tmp_path, seed=False) as run:
        assert status_of(run) == "pending"
        page = run.client.get("/facts/new")
        assert page.status_code == 200 and status_of(run) == "pending"

        response = run.client.post(
            "/facts/new",
            data={"type": "fact", "text": FICTIONAL_LINE, "privacy": "normal"},
            headers=run.run.action(),
        )

        assert response.status_code == 303, (response.status_code, response.text[:300])
        assert status_of(run) == "done"
        assert run.app.state.onboarding.statuses.get("cv_import") == "done"


def test_failed_fact_bank_check_at_start_up_leaves_the_step_and_opens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The start-up check is a convenience for an optional step: when it can't be made the
    workspace opens anyway, the step is left as it is, and only the error's type is
    logged."""

    def raising(_db: object) -> bool:
        raise sqlite3.OperationalError("fictional failure at /Users/fictional/secret-path")

    monkeypatch.setattr(main_module, "fact_bank_started", raising)
    seed(tmp_path, done=("welcome", "provider"))
    with caplog.at_level(logging.WARNING), started(tmp_path) as run:
        assert run.app.state.workspace is not None
        page = run.client.get(STEP)
        assert page.status_code == 200
        assert_not_redirected(page)
        assert status_of(run) == "pending"
        upload_form(outline(page.text))
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "OperationalError" in logged
    assert "secret-path" not in logged and "fictional failure" not in logged
