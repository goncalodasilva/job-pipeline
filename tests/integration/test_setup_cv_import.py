"""Setup step 4: import a CV from the guide (M3.7; AC-M3.8 Done-screen part; ADR-052,
ADR-089 items 3 and 4; detailed design "Today and the setup guide").

``GET /setup/cv_import`` (a page, like every guide step; no side effects) shows the step
"Import CV": a heading, a privacy notice ``#setup-cv-privacy`` (the CV's text goes to the AI
provider) and one upload form ``#setup-cv-form`` posting multipart to
``/setup/cv_import/upload`` (``action``, ``method="post"``,
``enctype="multipart/form-data"``, ``hx-post``, ``hx-encoding="multipart/form-data"``,
``hx-target="#setup-guide"``, ``hx-swap="outerHTML"``) with ``input[type=file][name=file]``
(``#setup-cv-file``) and ``textarea[name=text]`` (``#setup-cv-text``), both labelled, and the
button ``#setup-cv-start`` "Import and continue", described by the privacy notice. "Skip for
now" is ``button[name=action][value=skip]`` in a form posting to ``/setup/cv_import`` (the
ordinary step submit, swapping ``#setup-guide``).

- The upload goes through the same ``CvImportService`` as ``/facts/import`` (store, then start
  through the budget gate). Once stored, the step is done; the guide moves on to job
  preferences: 303, or for htmx the guide fragment with ``HX-Push-Url`` and "CV import
  started. Step 5 of 6: Job preferences" announced. The import runs in the background and
  makes drafts only.
- A refused upload answers 422 with the step again, the message in ``#setup-cv-error``, the
  file field ``aria-invalid="true"`` and described by it; nothing is stored and the step stays
  as it was.
- "Skip for now" marks the step skipped ("CV import skipped." announced before the next step);
  Done lists it as "Import CV: Skipped" with its link; finishing is allowed; no banner.
- The step marks itself done when facts or an import already exist (ADR-052), checked when the
  workspace opens. A setup finished before the step existed sees no banner.

A real migrated SQLite database under ``tmp_path``, the frozen clock, the fake provider saved
(scripted by wiring for real tasks), no network; every value is fictional.
"""

import json
from pathlib import Path
from typing import Any

import pytest
from httpx import Response

from app.onboarding.detection import StartupDecision
from tests.cv_fixtures import fixture_path
from tests.cv_import_harness import HTMX, Importing, importing, message
from tests.integration.test_setup_preferences_done import (
    DONE_PATH,
    PREFERENCES_PATH,
    SAVED,
    finish,
    live_statuses,
    seed,
    started,
    step_list_item,
    summary,
    title_of,
)
from tests.integration.test_setup_provider import only, required
from tests.integration.test_setup_redirect import (
    BASE_URL,
    Element,
    Running,
    assert_not_redirected,
    assert_see_other,
    outline,
)
from tests.integration.test_setup_reopen_banner import banners
from tests.upload_bodies import CONTENT_TYPE, FieldPart, FilePart, multipart_body

pytestmark = pytest.mark.integration

STEP = "/setup/cv_import"
UPLOAD = "/setup/cv_import/upload"
STEP_ID = "cv_import"
TITLE = "Import CV"
NEXT_ANNOUNCEMENT = "Step 5 of 6: Job preferences"
STARTED_SAID = "CV import started."
SKIPPED_SAID = "CV import skipped."
START_TEXT = "Import and continue"
SKIP_TEXT = "Skip for now"
OPTIONAL_TEXT = "Optional, not set up"
FORM_ID = "setup-cv-form"
FILE_ID = "setup-cv-file"
TEXT_ID = "setup-cv-text"
START_ID = "setup-cv-start"
PRIVACY_ID = "setup-cv-privacy"
ERROR_ID = "setup-cv-error"
BEFORE_STEP = ("welcome", "provider", "web_search")
ALL_DONE = ("welcome", "provider", "web_search", "preferences")
CV_TEXT = fixture_path("cv.txt").read_text(encoding="utf-8")
_PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + bytes(64)
NOTE = "Planned 60 delivery routes a week with a team of 3."


# --- helpers -------------------------------------------------------------------------------


def step_page(run: Running, *, htmx: bool = False) -> Response:
    response = run.client.get(STEP, headers=run.htmx() if htmx else {})
    assert response.status_code == 200, (
        f"GET {STEP} answered {response.status_code}: the CV import step isn't in the guide"
    )
    assert_not_redirected(response)
    return response


def labelled(elements: list[Element], control: Element) -> bool:
    control_id = control.attrs.get("id")
    return any(e.tag == "label" and e.attrs.get("for") == control_id for e in elements)


def upload_form(elements: list[Element]) -> Element:
    form = required(elements, FORM_ID)
    assert form.tag == "form"
    assert form.attrs.get("action") == UPLOAD, form.attrs
    assert (form.attrs.get("method") or "").lower() == "post", form.attrs
    assert form.attrs.get("enctype") == "multipart/form-data", form.attrs
    assert form.attrs.get("hx-post") == UPLOAD, form.attrs
    assert form.attrs.get("hx-encoding") == "multipart/form-data", form.attrs
    assert form.attrs.get("hx-target") == "#setup-guide", form.attrs
    assert form.attrs.get("hx-swap") == "outerHTML", form.attrs
    return form


def file_field(elements: list[Element]) -> Element:
    field = required(elements, FILE_ID)
    assert field.tag == "input" and field.attrs.get("type") == "file", field.attrs
    assert field.attrs.get("name") == "file"
    return field


def skip_button(elements: list[Element]) -> Element:
    section = required(elements, "setup-step")
    button = only(
        [
            e
            for e in elements
            if e.tag == "button"
            and e.within(section)
            and e.attrs.get("name") == "action"
            and e.attrs.get("value") == "skip"
        ],
        "skip button",
    )
    assert button.text == SKIP_TEXT, button.text
    node = button.parent
    while node is not None and node.tag != "form":
        node = node.parent
    assert node is not None, "the skip button is in a form"
    assert node.attrs.get("action") == STEP and node.attrs.get("hx-post") == STEP, node.attrs
    assert node.attrs.get("hx-target") == "#setup-guide", node.attrs
    return button


def post_upload(run: Importing, body: bytes, *, htmx: bool = False) -> Response:
    headers = run.run.action(**{"content-type": CONTENT_TYPE, **(HTMX if htmx else {})})
    return run.client.post(UPLOAD, content=body, headers=headers)


def paste_body(text: str) -> bytes:
    """What a browser sends with the file left empty and the text typed in."""
    return multipart_body(FilePart("", b""), FieldPart(text))


def skip(run: Running, *, htmx: bool = False) -> Response:
    headers = run.action(**({"HX-Request": "true"} if htmx else {}))
    return run.client.post(STEP, data={"action": "skip"}, headers=headers)


def status_of(run: Any) -> str:
    return live_statuses(run).get(STEP_ID, "pending")


def assert_moved_to_preferences(response: Response, *, htmx: bool, said: str) -> None:
    if not htmx:
        assert_see_other(response, PREFERENCES_PATH)
        return
    assert response.status_code == 200, (response.status_code, response.text[:300])
    assert response.headers.get("hx-push-url") == PREFERENCES_PATH
    assert json.loads(response.headers["hx-trigger"]) == {"announce": f"{said} {NEXT_ANNOUNCEMENT}"}
    assert "<html" not in response.text
    assert title_of(response).startswith("Job preferences"), title_of(response)
    elements = outline(response.text)
    current = only([e for e in elements if e.attrs.get("aria-current") == "step"], "current")
    assert current.text.startswith("Job preferences")


def done_rows(response: Response) -> dict[str, str]:
    rows = summary(response)
    assert [href for href, _ in rows] == [
        "/setup/welcome",
        "/setup/provider",
        "/setup/web_search",
        STEP,
        "/setup/preferences",
    ], "Done lists the steps in order, CV import fourth, each with its link"
    texts = dict(rows)
    assert TITLE in texts[STEP], texts[STEP]
    return texts


def drafts_and_confirmed(run: Importing) -> tuple[int, int]:
    rows = run.facts()
    return (
        sum(1 for row in rows if row["status"] == "draft"),
        sum(1 for row in rows if row["status"] == "confirmed"),
    )


# --- the step --------------------------------------------------------------------------------


def test_step4_shows_upload_paste_and_skip(tmp_path: Path) -> None:
    seed(tmp_path, done=BEFORE_STEP)
    with started(tmp_path) as run:
        elements = outline(step_page(run).text)
        assert required(elements, "step-heading").text == TITLE
        current = only([e for e in elements if e.attrs.get("aria-current") == "step"], "current")
        assert current.text.startswith(TITLE)
        form = upload_form(elements)
        file_input = file_field(elements)
        text = required(elements, TEXT_ID)
        assert text.tag == "textarea" and text.attrs.get("name") == "text"
        for control in (file_input, text):
            assert control.within(form)
            assert labelled(elements, control), control.attrs
        start = required(elements, START_ID)
        assert start.tag == "button" and start.within(form) and start.text == START_TEXT
        assert PRIVACY_ID in (start.attrs.get("aria-describedby") or "").split()
        assert "AI provider" in required(elements, PRIVACY_ID).text
        skip_button(elements)
        assert status_of(run) == "pending", "looking wrote nothing"


def test_ac_m3_8_step4_upload_starts_import_and_marks_done(tmp_path: Path) -> None:
    """AC-M3.8: pasting a CV in the guide stores it and starts the same import as the Fact
    bank; the step is done, the guide moves on, and the import makes drafts only."""
    seed(tmp_path, done=BEFORE_STEP)
    with importing(tmp_path, seed=False) as run:
        upload_form(outline(step_page(run.run).text))

        response = post_upload(run, paste_body(CV_TEXT), htmx=True)

        assert_moved_to_preferences(response, htmx=True, said=STARTED_SAID)
        assert status_of(run) == "done"
        assert run.run.decision == StartupDecision("guide", "preferences")
        assert "Done" in step_list_item(response, TITLE).text
        stored = run.only_import()
        assert [path.name for path in run.stored_files()] == [stored["stored_name"]]

        task = run.wait_terminal(run.only_task().id)
        assert str(task.status.value) == "succeeded", task.error
        drafts, confirmed = drafts_and_confirmed(run)
        assert drafts >= 1 and confirmed == 0, "AC-M3.1: drafts only"

        # Done lists the step as done, with its link.
        rows = done_rows(run.client.get(DONE_PATH))
        assert rows[STEP].endswith("Done"), rows[STEP]


def test_step4_plain_upload_redirects_to_preferences(tmp_path: Path) -> None:
    seed(tmp_path, done=BEFORE_STEP)
    with importing(tmp_path, seed=False) as run:
        response = post_upload(run, paste_body(CV_TEXT))
        assert_moved_to_preferences(response, htmx=False, said=STARTED_SAID)
        assert status_of(run) == "done"
        run.wait_terminal(run.only_task().id)


@pytest.mark.parametrize("htmx", [False, True], ids=["plain", "htmx"])
@pytest.mark.parametrize(
    ("case", "error"),
    [
        ("png_named_pdf", "UnsupportedDocumentType"),
        ("truncated_pdf", "UnreadableDocument"),
        ("nothing", "UploadNotAccepted"),
    ],
)
def test_step4_bad_upload_field_error_nothing_stored(
    tmp_path: Path, case: str, error: str, htmx: bool
) -> None:
    bodies = {
        "png_named_pdf": multipart_body(
            FilePart("cv.pdf", _PNG, content_type="application/pdf"), FieldPart("")
        ),
        "truncated_pdf": multipart_body(
            FilePart("cv.pdf", fixture_path("truncated.pdf").read_bytes()), FieldPart("")
        ),
        "nothing": paste_body(""),
    }
    seed(tmp_path, done=BEFORE_STEP)
    with importing(tmp_path, seed=False) as run:
        response = post_upload(run, bodies[case], htmx=htmx)

        assert response.status_code == 422, (response.status_code, response.text[:300])
        assert_not_redirected(response)
        assert "hx-push-url" not in response.headers
        assert ("<html" in response.text) is (not htmx), "htmx gets the guide fragment"
        elements = outline(response.text)
        current = only([e for e in elements if e.attrs.get("aria-current") == "step"], "current")
        assert current.text.startswith(TITLE), "the guide stays on the step"
        field = file_field(elements)
        assert field.attrs.get("aria-invalid") == "true"
        assert ERROR_ID in (field.attrs.get("aria-describedby") or "").split()
        assert required(elements, ERROR_ID).text == message(error)
        upload_form(elements)

        run.assert_nothing_stored()
        assert status_of(run) == "pending", "the step is unchanged"


def test_step4_upload_needs_token_and_origin(tmp_path: Path) -> None:
    seed(tmp_path, done=BEFORE_STEP)
    with importing(tmp_path, seed=False) as run:
        body = paste_body(CV_TEXT)
        no_token = run.client.post(
            UPLOAD, content=body, headers={"content-type": CONTENT_TYPE, "origin": BASE_URL}
        )
        bad_origin = run.client.post(
            UPLOAD,
            content=body,
            headers=run.run.action(
                **{"content-type": CONTENT_TYPE, "origin": "http://evil.example"}
            ),
        )
        assert no_token.status_code == 403
        assert bad_origin.status_code == 403
        run.assert_nothing_stored()
        assert status_of(run) == "pending"


# --- Skip for now and the Done screen -----------------------------------------------------------


@pytest.mark.parametrize("htmx", [False, True], ids=["plain", "htmx"])
def test_step4_skip_moves_on(tmp_path: Path, htmx: bool) -> None:
    seed(tmp_path, done=BEFORE_STEP)
    with started(tmp_path) as run:
        response = skip(run, htmx=htmx)
        assert_moved_to_preferences(response, htmx=htmx, said=SKIPPED_SAID)
        assert status_of(run) == "skipped"
        assert run.decision == StartupDecision("guide", "preferences")


def test_ac_m3_8_skipped_step4_listed_on_done_with_link(tmp_path: Path) -> None:
    """AC-M3.8 (Done screen; Today's list is M5, ADR-089 item 3): a skipped step 4 reads
    "Skipped" on Done with a link back to it, never blocks finishing, and leaves no banner."""
    seed(tmp_path, done=ALL_DONE, preferences=SAVED)
    with started(tmp_path) as run:
        assert_moved_to_preferences(skip(run), htmx=False, said=SKIPPED_SAID)

        for shown in (run.client.get(DONE_PATH), run.client.get(DONE_PATH, headers=run.htmx())):
            assert shown.status_code == 200, shown.status_code
            texts = done_rows(shown)
            assert texts[STEP] == f"{TITLE}: Skipped", texts[STEP]
            assert "Skipped" in step_list_item(shown, TITLE).text

        assert_see_other(finish(run, "today"), "/")
        assert run.decision == StartupDecision("app")
        today = run.client.get("/")
        assert today.status_code == 200
        assert banners(today.text) == []
        assert status_of(run) == "skipped", "finishing never rewrites the step"

        # The link leads back to the step, which still offers the upload.
        reopened = outline(step_page(run).text)
        upload_form(reopened)
        assert "Skipped" in step_list_item(run.client.get(STEP), TITLE).text


# --- marks itself done; finished setups ---------------------------------------------------------


@pytest.mark.parametrize("existing", ["fact", "import"])
def test_step4_marks_itself_done_when_facts_exist(tmp_path: Path, existing: str) -> None:
    """ADR-052: facts or a kept import already in the workspace finish the step when the
    workspace opens; no GET writes anything."""
    seed(tmp_path, done=("welcome", "provider"))
    with started(tmp_path) as run:
        assert status_of(run) == "pending", "nothing in the fact bank yet"
        if existing == "fact":
            from app.services.facts import FactInput

            run.app.state.facts.create(FactInput(text=NOTE))
        else:
            with run.app.state.workspace.db.transaction() as conn:
                conn.execute(
                    "INSERT INTO cv_imports (id, kind, stored_name, state, task_id, created_at, "
                    "updated_at, version) VALUES ('imp-kept', 'text', 'imp-kept.txt', 'uploaded', "
                    "NULL, '2026-01-03T08:00:00+00:00', '2026-01-03T08:00:00+00:00', 1)"
                )

    with started(tmp_path) as run:
        assert status_of(run) == "done"
        assert run.decision == StartupDecision("guide", "preferences"), "required steps lead"
        assert "Done" in step_list_item(run.client.get(STEP), TITLE).text
        assert done_rows(run.client.get(DONE_PATH))[STEP].endswith("Done")


def test_finished_setup_from_before_step4_sees_no_banner(tmp_path: Path) -> None:
    """A guide finished before the step existed has no row for it: the app opens as before,
    and Done lists the step as optional with its link."""
    seed(tmp_path, done=(*ALL_DONE, "done"), preferences=SAVED)
    with started(tmp_path) as run:
        assert STEP_ID in [step.id for step in run.app.state.setup_registry.steps]
        assert run.decision == StartupDecision("app")
        for path in ("/", "/inbox", "/facts"):
            page = run.client.get(path)
            assert page.status_code == 200, (path, page.status_code)
            assert_not_redirected(page)
            assert banners(page.text) == [], f"no banner: {path}"
        rows = done_rows(run.client.get(DONE_PATH))
        assert rows[STEP].endswith(OPTIONAL_TEXT), rows[STEP]
        assert STEP_ID not in live_statuses(run), "looking wrote nothing"
