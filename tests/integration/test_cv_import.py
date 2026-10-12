"""CV import: the service, the task and the routes (M3.4; AC-M3.1, AC-M3.7; spec 2.9 rules 5
and 12, 2.10, 2.11, 3.1, 3.3; ADR-088, ADR-089 items 8 and 11, ADR-090 items 7 and 8, ADR-091;
background-task skill section 8).

Assumed interface (chosen here where the sources left it open):

- ``app/services/cv_import.py``: ``CvImportService`` with the constants ``EXTRACT_CV``
  (``"extract_cv"``: the task type, the AI task and the prompt id), ``EXTRACT_CV_LABEL``
  (``"CV import"``), ``MAX_CV_TEXT_CHARS`` (60000) and ``PARSE_TIMEOUT_SECONDS`` (30). The
  tests reach the service only through the routes and the task runner.
- Routes (launch token and origin; none is a page): ``POST /facts/import`` (multipart, read
  with ``read_upload``: one ``file`` part or a ``text`` field) stores the upload and then
  tries to start; ``POST /facts/import/{id}/start`` (URL-encoded; the budget dialog's fields)
  starts a stored upload; ``POST /facts/import/{id}/remove`` deletes its file and row.
  A start or a removal answers 200 or 303. Refusals: 413 ``UploadTooLarge``; 400 for every
  other document error (``UnsupportedDocumentType``, ``UnreadableDocument``,
  ``EncryptedDocument``, ``NoTextFound``, ``ParseTimedOut``, ``UploadNotAccepted`` and the
  new ``CvTextTooLong``); 404 for an id that is no import; 409 for a removal while the
  import's task is queued or running, for a start of an import that is already done, and for
  a start the budget gate refuses (for htmx a warning is 200 with the budget dialog, whose
  "Continue" posts to the start route). Every refusal's text holds the error's
  ``user_message``.
- The task: type ``extract_cv``, dedupe target ``cv_import:<import id>``, counted on the
  saved provider's usage service, the prompt's own tier (Thorough). Its failures are stored
  by kind: ``InvalidModelOutput``, the provider's own kinds, and the new ``NothingExtracted``
  for an answer without units.
- New error classes in ``app/domain/errors.py``: ``CvTextTooLong`` (its message says to
  paste the part that matters) and ``NothingExtracted``.

Real SQLite and files under ``tmp_path``; the fake provider through wiring's own scripts
(``tests/cv_import_harness.py``); no network; no fixed waits; every value is fictional.
"""

import importlib
import logging
import re
import stat
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import Request

from app.domain.errors import AppError, InvalidModelOutput, message_for_kind
from app.domain.settings import AIProviderSettings
from app.modules.ai.prompts import PromptLibrary, render_prompt
from app.modules.ai.providers import fake_scripts
from app.modules.ai.providers.fake import FAKE_THOROUGH_MODEL, FakeAIProvider
from app.wiring import TASK_TIMEOUT_FLOOR
from tests.cv_fixtures import fixture_path
from tests.cv_import_harness import (
    ACCEPTED,
    EXTRACT_CV,
    IMPORT_PATH,
    Importing,
    dedupe_target,
    importing,
    message,
    remove_path,
    said,
    start_path,
)
from tests.fakes.ai import NotCancelled, make_request
from tests.integration.test_budget_warning_dialog import (
    CONTINUE,
    NOT_NOW,
    acknowledged,
    blocked_message,
    control,
    crossing,
    press,
    press_not_now,
    set_budget,
    submitted_by,
    the_dialog,
    used,
)
from tests.integration.test_practice_task import FAKE_SERVICE, NO_FAKES
from tests.integration.test_setup_provider import make_wiring
from tests.integration.test_setup_redirect import ORIGIN, TOKEN_HEADER
from tests.integration.test_setup_reopen_banner import TOKEN_KIND
from tests.upload_bodies import CONTENT_TYPE, FieldPart, FilePart, multipart_body

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPTS = REPO_ROOT / "prompts"
CV_TEXT = fixture_path("cv.txt").read_text(encoding="utf-8")
INJECTION_CV = (REPO_ROOT / "tests/fixtures/ai/extract_cv/injection_cv.txt").read_text(
    encoding="utf-8"
)
PROMPT_VERSION = re.search(
    r"^version:\s*(\S+)", (PROMPTS / EXTRACT_CV / "prompt.md").read_text("utf-8"), re.MULTILINE
)

LABEL = "CV import"
MAX_CHARS = 60_000
MIB = 1024 * 1024
STORED_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*\.(txt|pdf|docx)")
EXTENSIONS = {"text": ".txt", "pdf": ".pdf", "docx": ".docx"}

# What the fake's well-behaved answer holds (app/modules/ai/providers/fake_scripts.py).
INVOICES = "Cut invoice processing time by 35% across 4 warehouses."
ROUTES = "Planned 60 delivery routes a week with a team of 3."
DEGREE = "BSc in Industrial Management, Example University, 2018"
WIDGETS = ("Example Widgets Ltd", "Operations Analyst", "2021", "2024")
LOGISTICS = ("Sample Logistics SA", "Junior Planner", "2018", "2021")
INVENTED_EMPLOYER = "Imaginary Aerospace Corp"
PLANTED_CLAIM = "Led a team of 50 engineers at Imaginary Aerospace Corp."
# Text of the fictional CV that must never reach a task row, an event or a log.
CV_MARKERS = (
    "Alex Example",
    "alex@example.com",
    "Example Widgets",
    "Sample Logistics",
    "invoice processing",
    "delivery routes",
    "Industrial Management",
)


# --- helpers -----------------------------------------------------------------------------------


def with_keyword(keyword: str, text: str = CV_TEXT) -> str:
    """The fictional CV with a line that makes the fake choose another answer."""
    return f"{text}\nReference code: {keyword}\n"


def role(key: str, employer: str | None, title: str | None, *dates: str | None) -> dict[str, Any]:
    start, end = (*dates, None, None)[:2]
    return {"key": key, "employer": employer, "title": title, "start_date": start, "end_date": end}


def fact(role_key: str | None, text: str, quote: str, *skills: str) -> dict[str, Any]:
    return {
        "type": "fact",
        "role_key": role_key,
        "text": text,
        "situation": None,
        "action": None,
        "result": None,
        "skills": list(skills),
        "source_quote": quote,
    }


def story(role_key: str | None, action: str, result: str, quote: str) -> dict[str, Any]:
    return {
        "type": "story",
        "role_key": role_key,
        "text": None,
        "situation": None,
        "action": action,
        "result": result,
        "skills": [],
        "source_quote": quote,
    }


def role_fields(row: dict[str, Any]) -> tuple[Any, ...]:
    return (row["employer"], row["title"], row["start_date"], row["end_date"])


def role_of(run: Importing, draft: dict[str, Any]) -> tuple[Any, ...] | None:
    if draft["role_id"] is None:
        return None
    (row,) = [found for found in run.roles() if found["id"] == draft["role_id"]]
    return role_fields(row)


def wording(draft: dict[str, Any]) -> str:
    parts = (draft["text"], draft["situation"], draft["action"], draft["result"])
    return " ".join(part for part in parts if part)


def flagged(run: Importing) -> dict[str, bool]:
    """Every draft's wording, with whether it is marked ``needs_check``."""
    return {wording(draft): bool(draft["needs_check"]) for draft in run.facts()}


def assert_only_drafts(run: Importing) -> None:
    """AC-M3.1: nothing an import makes is confirmed, whatever the answer said."""
    statuses = {draft["status"] for draft in run.facts()}
    assert statuses <= {"draft"}, f"an import made a fact that is {statuses - {'draft'}}"
    role_statuses = {row["status"] for row in run.roles()}
    assert role_statuses <= {"draft"}, f"an import made a role that is {role_statuses}"
    events = {row["type"] for row in run.rows("SELECT DISTINCT type FROM fact_events")}
    assert "confirmed" not in events


def assert_the_ok_drafts(run: Importing, import_id: str) -> None:
    """The four drafts and two draft roles of the well-behaved answer, none flagged."""
    drafts = run.facts()
    assert [(d["type"], wording(d)) for d in drafts] == [
        ("story", INVOICES),
        ("fact", ROUTES),
        ("fact", DEGREE),
        ("fact", "English"),
    ]
    assert [role_of(run, d) for d in drafts] == [WIDGETS, LOGISTICS, None, None]
    assert sorted(role_fields(row) for row in run.roles()) == sorted([WIDGETS, LOGISTICS])
    assert [d["needs_check"] for d in drafts] == [0, 0, 0, 0], "every quote and number is in the CV"
    assert {d["cv_import_id"] for d in drafts} == {import_id}
    assert_only_drafts(run)


def assert_failed_and_wrote_nothing(run: Importing, row: Any, kind: str) -> dict[str, Any]:
    """A failed attempt: the kind is stored (never text), nothing was written and the upload
    is still there to retry or remove."""
    assert str(row.status.value) == "failed"
    assert row.error == kind
    assert row.result_ref is None
    run.assert_no_fact_rows()
    return run.assert_upload_kept(run.only_import()["id"])


# --- wiring ------------------------------------------------------------------------------------


def _extract_request(cv_text: str) -> Any:
    template = PromptLibrary(PROMPTS).load(EXTRACT_CV)
    rendered = render_prompt(template, {"cv_text": cv_text, "import_id": "imp-1"})
    return make_request(
        EXTRACT_CV,
        system=rendered.system,
        user=rendered.user,
        output_schema=template.output_schema,
        model=FAKE_THOROUGH_MODEL,
    )


async def test_real_tasks_scripted_only_when_fake_is_saved_provider(tmp_path: Path) -> None:
    wiring = make_wiring()
    request = _extract_request(CV_TEXT)

    # The saved provider is the fake: wiring scripts it with the fictional answers.
    saved = wiring.ai_provider(AIProviderSettings(kind="fake"), data_dir=tmp_path)
    assert isinstance(saved, FakeAIProvider)
    answer = await saved.run(request, NotCancelled())
    assert answer.data == fake_scripts.EXTRACT_CV_RESPONSES["ok"].data
    assert answer.model == FAKE_THOROUGH_MODEL
    assert saved.requests == [], "the app's fake keeps no request: prompts carry personal data"
    with pytest.raises(InvalidModelOutput):  # the keywords came with the script
        await saved.run(
            _extract_request(with_keyword(fake_scripts.KEYWORD_MALFORMED)), NotCancelled()
        )

    # The fake of the setup guide's connection test answers no real task.
    for key in (None, "fake-connected", fake_scripts.KEYWORD_INJECTION):
        probe = wiring.fake_for_key(key)
        with pytest.raises(InvalidModelOutput):
            await probe.run(request, NotCancelled())

    # Another saved provider is never the fake, and without the allow flag there is no fake.
    other = wiring.ai_provider(AIProviderSettings(kind=TOKEN_KIND), data_dir=tmp_path)
    assert not isinstance(other, FakeAIProvider)
    with pytest.raises(ValueError, match="isn't available"):
        make_wiring(NO_FAKES).ai_provider(AIProviderSettings(kind="fake"), data_dir=tmp_path)


@pytest.mark.parametrize("environ", [None, NO_FAKES], ids=["fakes_allowed", "fakes_not_allowed"])
def test_extract_cv_task_type_registered_with_label_and_timeout(
    tmp_path: Path, environ: dict[str, str] | None
) -> None:
    with importing(tmp_path, environ=environ) as run:
        specs = dict(run.app.state.task_registry.specs())

        assert EXTRACT_CV in specs, "a real task type: registered whether or not fakes are allowed"
        assert specs[EXTRACT_CV].label == LABEL
        assert specs[EXTRACT_CV].timeout > TASK_TIMEOUT_FLOOR


def test_service_limits_are_the_decided_ones() -> None:
    cv_import = importlib.import_module("app.services.cv_import")

    assert hasattr(cv_import, "CvImportService")
    assert cv_import.EXTRACT_CV == EXTRACT_CV
    assert cv_import.EXTRACT_CV_LABEL == LABEL
    assert cv_import.MAX_CV_TEXT_CHARS == MAX_CHARS
    assert cv_import.PARSE_TIMEOUT_SECONDS == 30


# --- AC-M3.1: an import makes drafts and nothing else ------------------------------------------


def test_ac_m3_1_import_creates_only_drafts(tmp_path: Path) -> None:
    """A pasted text import, end to end."""
    with importing(tmp_path) as run:
        task = run.finished(run.paste(CV_TEXT))

        stored = run.only_import()
        assert stored["kind"] == "text"
        assert stored["state"] == "done"
        assert stored["task_id"] == task.id, "the import names the task that made its drafts"
        assert task.result_ref, "a succeeded task names its result"
        assert_the_ok_drafts(run, stored["id"])
        assert [d["original_answer"] for d in run.facts()] == [INVOICES, ROUTES, DEGREE, "English"]
        skills = run.rows(
            "SELECT f.id AS fact_id, s.name FROM fact_skills AS fs "
            "JOIN skills AS s ON s.id = fs.skill_id JOIN facts AS f ON f.id = fs.fact_id"
        )
        assert [(row["fact_id"], row["name"]) for row in skills] == [
            (run.facts()[3]["id"], "English")
        ]
        # The uploaded bytes are what is kept (ADR-089 item 11).
        (kept,) = run.stored_files()
        assert kept.read_bytes() == CV_TEXT.encode("utf-8")

        # One request, without tools, on the prompt's own tier, counted once on the saved
        # provider's usage service.
        (request,) = run.spy.requests
        assert request.task == EXTRACT_CV
        assert request.allow_web_search is False
        assert request.model == FAKE_THOROUGH_MODEL
        (record,) = run.usage_rows()
        assert (record["provider"], record["tier"], record["success"]) == (
            FAKE_SERVICE,
            "thorough",
            1,
        )


@pytest.mark.parametrize("name", ["single_column.pdf", "two_column.pdf"])
def test_pdf_upload_import_creates_unflagged_drafts(tmp_path: Path, name: str) -> None:
    data = fixture_path(name).read_bytes()
    with importing(tmp_path) as run:
        run.finished(run.upload("Alex Example CV.pdf", data))

        stored = run.only_import()
        assert (stored["kind"], stored["state"]) == ("pdf", "done")
        assert_the_ok_drafts(run, stored["id"])
        (kept,) = run.stored_files()
        assert kept.read_bytes() == data, "the task read the stored file and left it as it was"


def test_versions_carry_prompt_model_and_provider(tmp_path: Path) -> None:
    assert PROMPT_VERSION is not None
    with importing(tmp_path) as run:
        run.finished(run.paste(CV_TEXT))

        versions = run.rows("SELECT * FROM fact_versions ORDER BY rowid")
        assert len(versions) == 4
        for version in versions:
            assert version["source"] == "cv_import"
            assert version["provider"] == FakeAIProvider.name, "fake-made drafts can be labelled"
            assert version["model"] == FAKE_THOROUGH_MODEL
            assert version["prompt_id"] == EXTRACT_CV
            assert str(version["prompt_version"]) == PROMPT_VERSION.group(1)
            assert version["version"] == 1


@pytest.mark.parametrize("answer", ["obeyed", "status_field"])
def test_ac_m3_1_injection_text_cannot_confirm_anything(tmp_path: Path, answer: str) -> None:
    """The CV holds planted instructions. A model that obeyed them inside the schema only adds
    a draft; one that also returns a status is refused, and nothing is stored."""
    assert fake_scripts.KEYWORD_INJECTION in INJECTION_CV
    text = (
        INJECTION_CV
        if answer == "obeyed"
        else INJECTION_CV.replace(
            fake_scripts.KEYWORD_INJECTION, fake_scripts.KEYWORD_INJECTION_STATUS
        )
    )
    with importing(tmp_path) as run:
        response = run.paste(text)

        if answer == "status_field":
            task = run.finished(response, status="failed")
            assert_failed_and_wrote_nothing(run, task, "InvalidModelOutput")
            assert len(run.spy.requests) == 2, "the first call and its one retry"
            return
        run.finished(response)
        drafts = run.facts()
        assert PLANTED_CLAIM in [wording(d) for d in drafts], "the planted claim arrived"
        assert len(drafts) == 5
        assert_only_drafts(run)
        assert run.only_import()["state"] == "done"
        confirmed = run.rows("SELECT id FROM facts WHERE status <> 'draft'")
        assert confirmed == []


# --- needs_check -------------------------------------------------------------------------------


def test_adversarial_employer_and_number_marked_needs_check(tmp_path: Path) -> None:
    inflated = (
        "Cut invoice processing time by 85% across 14 warehouses at Imaginary Aerospace Corp."
    )
    with importing(tmp_path) as run:
        run.finished(run.paste(with_keyword(fake_scripts.KEYWORD_INVENTED_EMPLOYER)))

        assert flagged(run) == {
            ROUTES: False,  # faithful, under a role the CV names
            inflated: True,  # numbers the CV never states
            INVOICES: True,  # faithful wording, but under an employer the CV never names
        }
        assert INVENTED_EMPLOYER in [row["employer"] for row in run.roles()]
        assert_only_drafts(run)


def test_quote_not_in_cv_marked_needs_check(tmp_path: Path) -> None:
    with importing(tmp_path) as run:
        run.finished(run.paste(with_keyword(fake_scripts.KEYWORD_INVENTED_QUOTE)))

        assert flagged(run) == {ROUTES: False, "Trained new planners on the routing tool.": True}
        assert_only_drafts(run)


def test_number_not_in_cv_flagged_and_quote_compared_without_whitespace(tmp_path: Path) -> None:
    broken_quote = "Cut invoice processing time by 35%\n    across 4   warehouses."
    answer = {
        "roles": [role("r1", *WIDGETS), role("r2", *LOGISTICS)],
        "units": [
            fact("r1", INVOICES, broken_quote),
            fact("r1", "Cut invoice processing time by 45% across 4 warehouses.", INVOICES),
            story("r2", "Planned delivery routes.", "Saved 9 hours a week.", ROUTES),
            # 6 is not a number of the CV, though "60" holds the digit.
            fact("r2", "Planned 6 delivery routes a week.", ROUTES),
        ],
    }
    with importing(tmp_path) as run:
        run.spy.answer(answer)
        run.finished(run.paste(CV_TEXT))

        assert flagged(run) == {
            INVOICES: False,
            "Cut invoice processing time by 45% across 4 warehouses.": True,
            "Planned delivery routes. Saved 9 hours a week.": True,
            "Planned 6 delivery routes a week.": True,
        }


def test_numbers_moved_from_another_bullet_marked_needs_check(tmp_path: Path) -> None:
    """60 and 3 are numbers of the CV, but of the routes line, not of the quoted one."""
    with importing(tmp_path) as run:
        run.finished(run.paste(with_keyword(fake_scripts.KEYWORD_MOVED_NUMBER)))

        assert flagged(run) == {
            ROUTES: False,
            "Cut invoice processing time by 60% across 3 warehouses.": True,
        }
        assert_only_drafts(run)


def test_words_added_to_a_real_quote_marked_needs_check(tmp_path: Path) -> None:
    padded = (
        "Cut invoice processing time by 35% across 4 warehouses using SAP at "
        "Imaginary Aerospace Corp."
    )
    with importing(tmp_path) as run:
        run.finished(run.paste(with_keyword(fake_scripts.KEYWORD_INVENTED_WORDS)))

        assert flagged(run) == {ROUTES: False, padded: True}
        assert_only_drafts(run)


def test_invented_skill_flagged_and_never_stored(tmp_path: Path) -> None:
    with importing(tmp_path) as run:
        run.finished(run.paste(with_keyword(fake_scripts.KEYWORD_INVENTED_SKILL)))

        assert flagged(run) == {ROUTES: True, "English": False}
        assert [row["name"] for row in run.rows("SELECT name FROM skills")] == ["English"]
        linked = run.rows("SELECT fact_id FROM fact_skills")
        (english,) = [d for d in run.facts() if wording(d) == "English"]
        assert [row["fact_id"] for row in linked] == [english["id"]]
        assert "Kubernetes" not in repr(run.rows("SELECT * FROM fact_versions"))


@pytest.mark.parametrize(
    ("quote", "claim"),
    [
        ("Managed 5 people", "Managed 5k people"),
        ("Managed 5 people", "Managed 5 million people"),
        ("English", "Fluent English, used daily to lead a team of 50 engineers."),
        ("Handled 1,200 orders.", "Handled 1200 orders."),
    ],
    ids=["5k", "million", "one_word_quote", "number_format"],
)
def test_claim_beyond_its_quote_marked_needs_check(tmp_path: Path, quote: str, claim: str) -> None:
    text = f"{CV_TEXT}\n- Managed 5 people\n- Handled 1,200 orders.\n"
    answer = {
        "roles": [role("r1", *WIDGETS)],
        "units": [fact("r1", quote, quote), fact("r1", claim, quote)],
    }
    with importing(tmp_path) as run:
        run.spy.answer(answer)
        run.finished(run.paste(text))

        assert flagged(run) == {quote: False, claim: True}


def test_soft_hyphen_and_ligature_in_the_cv_do_not_flag_a_faithful_quote(tmp_path: Path) -> None:
    """The model is shown the CV without invisible characters and may spell a ligature out."""
    soft_hyphen, ligature_fi, en_dash = chr(0x00AD), chr(0xFB01), chr(0x2013)
    close = "Ran the finance close for 2 offices."
    text = (
        CV_TEXT.replace("warehouses", f"ware{soft_hyphen}houses").replace(
            "(2021 - 2024)", f"(2021 {en_dash} 2024)"
        )
        + f"- Ran the {ligature_fi}nance close for 2 offices.\n"
    )
    assert soft_hyphen in text and en_dash in text and close not in text
    answer = {
        "roles": [role("r1", *WIDGETS)],
        "units": [fact("r1", INVOICES, INVOICES), fact("r1", close, close)],
    }
    with importing(tmp_path) as run:
        run.spy.answer(answer)
        run.finished(run.paste(text))

        assert flagged(run) == {INVOICES: False, close: False}
        assert role_fields(run.roles()[0]) == WIDGETS


def test_quote_returned_with_escaped_lt_not_flagged_and_stored_unescaped(tmp_path: Path) -> None:
    """The prompt shows "<" as "&lt;"; an answer that copies the escape is still the CV's."""
    cover = "Kept stock cover < 5 days in 2 depots."
    escaped = cover.replace("<", "&lt;")
    answer = {
        "roles": [role("r1", *WIDGETS)],
        "units": [
            fact("r1", escaped, escaped),
            story("r1", "Kept stock cover &lt; 5 days", "in 2 depots.", escaped),
        ],
    }
    with importing(tmp_path) as run:
        run.spy.answer(answer)
        run.finished(run.paste(f"{CV_TEXT}\n- {cover}\n"))

        (request,) = run.spy.requests
        assert "stock cover &lt; 5 days" in request.user, "what the model was shown"
        drafts = run.facts()
        assert [d["needs_check"] for d in drafts] == [0, 0]
        assert [wording(d) for d in drafts] == [cover, cover]
        assert [d["original_answer"] for d in drafts] == [cover, cover]
        assert "&lt;" not in repr(run.rows("SELECT * FROM fact_versions"))


def test_role_year_not_in_cv_stored_as_null_and_its_units_flagged(tmp_path: Path) -> None:
    assert "1999" not in CV_TEXT
    answer = {
        "roles": [role("r1", WIDGETS[0], WIDGETS[1], "1999", "2024"), role("r2", *LOGISTICS)],
        "units": [fact("r1", INVOICES, INVOICES), fact("r2", ROUTES, ROUTES)],
    }
    with importing(tmp_path) as run:
        run.spy.answer(answer)
        run.finished(run.paste(CV_TEXT))

        assert sorted(role_fields(row) for row in run.roles()) == sorted(
            [(WIDGETS[0], WIDGETS[1], None, "2024"), LOGISTICS]
        )
        assert flagged(run) == {INVOICES: True, ROUTES: False}
        assert "1999" not in repr(run.roles())


def test_same_role_under_two_keys_is_one_draft_role(tmp_path: Path) -> None:
    answer = {
        "roles": [
            role("r1", *WIDGETS),
            role("r2", *LOGISTICS),
            role("r3", "Example  Widgets Ltd", " Operations Analyst", "2021", "2024"),
        ],
        "units": [
            fact("r1", INVOICES, INVOICES),
            fact("r2", ROUTES, ROUTES),
            fact("r3", "Cut invoice processing time", INVOICES),
        ],
    }
    with importing(tmp_path) as run:
        run.spy.answer(answer)
        run.finished(run.paste(CV_TEXT))

        assert sorted(role_fields(row) for row in run.roles()) == sorted([WIDGETS, LOGISTICS])
        drafts = run.facts()
        assert [role_of(run, d) for d in drafts] == [WIDGETS, LOGISTICS, WIDGETS]
        assert drafts[0]["role_id"] == drafts[2]["role_id"], "both keys name the one role"
        assert [d["needs_check"] for d in drafts] == [0, 0, 0]


def test_unknown_role_key_kept_without_role_and_flagged(tmp_path: Path) -> None:
    answer = {
        "roles": [role("r1", *WIDGETS)],
        "units": [fact("r1", INVOICES, INVOICES), fact("r7", ROUTES, ROUTES)],
    }
    with importing(tmp_path) as run:
        run.spy.answer(answer)
        run.finished(run.paste(CV_TEXT))

        by_wording = {wording(d): d for d in run.facts()}
        assert set(by_wording) == {INVOICES, ROUTES}, "the unit is kept"
        assert by_wording[ROUTES]["role_id"] is None
        assert by_wording[ROUTES]["needs_check"] == 1
        assert role_of(run, by_wording[INVOICES]) == WIDGETS
        assert by_wording[INVOICES]["needs_check"] == 0
        assert len(run.roles()) == 1


def test_impossible_date_stored_as_null(tmp_path: Path) -> None:
    """The schema's pattern lets 31 February through; the calendar doesn't."""
    answer = {
        "roles": [role("r1", WIDGETS[0], WIDGETS[1], "2021-02-31", "2024")],
        "units": [fact("r1", INVOICES, INVOICES)],
    }
    with importing(tmp_path) as run:
        run.spy.answer(answer)
        run.finished(run.paste(CV_TEXT))

        (stored,) = run.roles()
        assert role_fields(stored) == (WIDGETS[0], WIDGETS[1], None, "2024")
        assert [wording(d) for d in run.facts()] == [INVOICES]
        assert run.only_import()["state"] == "done"


def test_duplicate_role_keys_are_invalid_output_and_write_nothing(tmp_path: Path) -> None:
    answer = {
        "roles": [role("r1", *WIDGETS), role("r1", *LOGISTICS)],
        "units": [fact("r1", INVOICES, INVOICES)],
    }
    with importing(tmp_path) as run:
        run.spy.answer(answer)
        task = run.finished(run.paste(CV_TEXT), status="failed")

        assert_failed_and_wrote_nothing(run, task, "InvalidModelOutput")


@pytest.mark.parametrize("roles", [[], [role("r1", *WIDGETS)]], ids=["nothing", "roles_only"])
def test_empty_answer_fails_with_nothing_extracted(
    tmp_path: Path, roles: list[dict[str, Any]]
) -> None:
    with importing(tmp_path) as run:
        run.spy.answer({"roles": roles, "units": []})
        task = run.finished(run.paste(CV_TEXT), status="failed")

        assert_failed_and_wrote_nothing(run, task, "NothingExtracted")
        shown = message_for_kind("NothingExtracted")
        assert shown == message("NothingExtracted")
        assert shown != AppError.message, "the tasks panel explains it"
        assert "paste the text instead" in shown


# --- failures write nothing --------------------------------------------------------------------


def test_malformed_output_twice_leaves_no_facts_roles_or_skills(tmp_path: Path) -> None:
    with importing(tmp_path) as run:
        task = run.finished(
            run.paste(with_keyword(fake_scripts.KEYWORD_MALFORMED)), status="failed"
        )

        assert_failed_and_wrote_nothing(run, task, "InvalidModelOutput")
        assert len(run.spy.requests) == 2, "the first call and its one retry"
        records = run.usage_rows()
        assert [record["success"] for record in records] == [0, 0], "both calls counted, as failed"


@pytest.mark.parametrize(
    ("keyword", "kind"),
    [
        (fake_scripts.KEYWORD_USAGE_LIMIT, "UsageLimitReached"),
        (fake_scripts.KEYWORD_OFFLINE, "NetworkUnavailable"),
    ],
    ids=["usage_limit", "offline"],
)
def test_provider_error_writes_nothing_and_keeps_the_upload(
    tmp_path: Path, keyword: str, kind: str
) -> None:
    with importing(tmp_path) as run:
        task = run.finished(run.paste(with_keyword(keyword)), status="failed")

        assert_failed_and_wrote_nothing(run, task, kind)
        assert len(run.spy.requests) == 1, "a provider error is not retried"


def test_cancel_mid_call_leaves_no_records(tmp_path: Path) -> None:
    with importing(tmp_path) as run:
        run.hold.hold = True
        response = run.paste(CV_TEXT)
        assert response.status_code in ACCEPTED, response.status_code
        task_id = run.only_task().id
        run.wait_entered(1)

        cancelled = run.client.post(f"/tasks/{task_id}/cancel", headers=run.run.action())
        assert cancelled.status_code == 200, cancelled.status_code
        run.hold.open()

        row = run.wait_terminal(task_id)
        assert str(row.status.value) == "cancelled"
        assert row.result_ref is None and row.error is None
        run.assert_no_fact_rows()
        stored = run.assert_upload_kept(run.only_import()["id"])
        assert len(run.spy.requests) == 1
        assert [record["success"] is not None for record in run.usage_rows()] == [True], (
            "the cancelled call is counted and completed"
        )

        # A cancelled import can be removed.
        assert run.remove(stored["id"]).status_code in ACCEPTED
        assert run.imports() == [] and run.stored_files() == []


@pytest.mark.parametrize("how", ["retry", "start"])
def test_retry_after_failure_creates_drafts_once(tmp_path: Path, how: str) -> None:
    with importing(tmp_path) as run:
        run.spy.fail("network")
        failed = run.finished(run.paste(CV_TEXT), status="failed")
        import_id = assert_failed_and_wrote_nothing(run, failed, "NetworkUnavailable")["id"]
        run.spy.as_wired()

        if how == "retry":  # the tasks panel's Retry
            again = run.client.post(f"/tasks/{failed.id}/retry", headers=run.run.action())
            assert again.status_code == 200, again.status_code
            done_id = failed.id
        else:  # the import's own Start
            assert run.start(import_id).status_code in ACCEPTED
            (done_id,) = [row.id for row in run.tasks() if row.id != failed.id]
        done = run.wait_terminal(done_id)

        assert str(done.status.value) == "succeeded", done.error
        stored = run.only_import()
        assert (stored["id"], stored["state"], stored["task_id"]) == (import_id, "done", done.id)
        assert_the_ok_drafts(run, import_id)
        assert len(run.stored_files()) == 1, "the same upload: nothing was sent again"

        # A done import is not started again, so its drafts are made once.
        refused = run.start(import_id)
        assert refused.status_code == 409, refused.status_code
        assert all(str(row.status.value) in ("failed", "succeeded") for row in run.tasks())
        assert len(run.facts()) == 4 and len(run.roles()) == 2
        assert len(run.spy.requests) == 2


def test_duplicate_start_returns_the_existing_task(tmp_path: Path) -> None:
    with importing(tmp_path) as run:
        run.hold.hold = True
        assert run.paste(CV_TEXT).status_code in ACCEPTED
        task = run.only_task()
        import_id = run.only_import()["id"]
        run.wait_entered(1)

        again = run.start(import_id)

        assert again.status_code in ACCEPTED, again.status_code
        assert [row.id for row in run.tasks()] == [task.id], "no second task"
        ref = run.app.state.task_service.enqueue(EXTRACT_CV, dedupe_target(import_id))
        assert (ref.id, ref.created) == (task.id, False), "one active task per import"
        assert task.dedupe_key == f"{EXTRACT_CV}:{dedupe_target(import_id)}"

        run.hold.open()
        assert str(run.wait_terminal(task.id).status.value) == "succeeded"
        assert len(run.spy.requests) == 1
        assert len(run.facts()) == 4


def test_import_removed_mid_task_fails_cleanly(tmp_path: Path) -> None:
    with importing(tmp_path) as run:
        run.hold.hold = True
        assert run.paste(CV_TEXT).status_code in ACCEPTED
        task_id = run.only_task().id
        run.wait_entered(1)
        # The route refuses this while the task runs, so the row goes the way another process
        # (or a later bug) would take it.
        run.execute("DELETE FROM cv_imports")
        run.hold.open()

        row = run.wait_terminal(task_id)

        assert str(row.status.value) == "failed", "no result for an import that is gone"
        assert row.result_ref is None
        assert row.error and row.error == row.error.strip() and " " not in row.error, "a kind"
        run.assert_no_fact_rows()
        assert run.imports() == []


def test_import_done_by_another_attempt_mid_task_fails_cleanly(tmp_path: Path) -> None:
    with importing(tmp_path) as run:
        run.hold.hold = True
        assert run.paste(CV_TEXT).status_code in ACCEPTED
        task_id = run.only_task().id
        run.wait_entered(1)
        # While the call is in flight another attempt finishes the import (set directly:
        # the dedupe key allows no second task for it).
        run.execute("UPDATE cv_imports SET state = 'done', version = version + 1")
        run.hold.open()

        row = run.wait_terminal(task_id)

        assert str(row.status.value) == "failed", "a done import gets no second result"
        assert row.error in ("CvImportAlreadyDone", "CvImportNotFound")
        assert row.result_ref is None
        run.assert_no_fact_rows()
        stored = run.only_import()
        assert (stored["state"], stored["task_id"]) == ("done", None), "left as the other left it"
        assert len(run.spy.requests) == 1


# --- the budget gate ---------------------------------------------------------------------------


def test_budget_block_stops_the_call_and_keeps_the_upload(tmp_path: Path) -> None:
    with importing(tmp_path) as run:
        crossing(run, 30)  # the blocking point of the test budget

        response = run.paste(CV_TEXT)

        assert response.status_code == 409, response.status_code
        assert blocked_message() in said(response), "the reason, with the reset date"
        import_id = run.only_import()["id"]
        run.assert_upload_kept(import_id)
        assert run.tasks() == [] and run.spy.requests == []
        assert used(run) == 30, "no usage record"
        again = run.start(import_id)
        assert again.status_code == 409 and blocked_message() in said(again)
        assert run.tasks() == []

        # Once the budget allows it, the stored upload starts without being sent again.
        set_budget(run, None)
        assert run.start(import_id).status_code in ACCEPTED
        assert str(run.wait_terminal(run.only_task().id).status.value) == "succeeded"
        assert_the_ok_drafts(run, import_id)
        assert len(run.imports()) == 1


def test_warning_then_continue_starts_without_reupload(tmp_path: Path) -> None:
    with importing(tmp_path) as run:
        crossing(run, 9)  # the next request reaches the first warning

        shown = run.paste(CV_TEXT, htmx=True)

        elements, dialog = the_dialog(shown)
        import_id = run.only_import()["id"]
        run.assert_upload_kept(import_id)
        assert run.tasks() == [] and run.spy.requests == [], "a warning starts nothing"
        proceed = control(elements, dialog, CONTINUE)
        path, values = submitted_by(elements, proceed)
        assert path == start_path(import_id), "Continue starts the stored upload"
        assert "text" not in values and "file" not in values, "the CV is not sent again"

        answer = press(run, elements, proceed)

        assert answer.status_code in ACCEPTED, (answer.status_code, answer.text[:300])
        assert str(run.wait_terminal(run.only_task().id).status.value) == "succeeded"
        assert acknowledged(run) == frozenset({"first"})
        assert len(run.imports()) == 1 and len(run.stored_files()) == 1
        assert_the_ok_drafts(run, import_id)
        assert len(run.spy.requests) == 1


def test_warning_not_now_keeps_the_upload(tmp_path: Path) -> None:
    with importing(tmp_path) as run:
        crossing(run, 9)
        elements, dialog = the_dialog(run.paste(CV_TEXT, htmx=True))
        import_id = run.only_import()["id"]

        press_not_now(run, elements, control(elements, dialog, NOT_NOW))

        run.assert_upload_kept(import_id)
        assert run.tasks() == [] and run.spy.requests == []
        assert acknowledged(run) == frozenset()
        assert used(run) == 9
        # Not confirmed: starting it asks again; a request that can't show the dialog is
        # refused with the reason. Neither starts anything.
        the_dialog(run.start(import_id, htmx=True))
        plain = run.start(import_id)
        assert plain.status_code == 409, plain.status_code
        assert run.tasks() == []
        # And the kept upload can be removed instead.
        assert run.remove(import_id).status_code in ACCEPTED
        assert run.imports() == [] and run.stored_files() == []


# --- AC-M3.7: refused uploads leave nothing ----------------------------------------------------


def assert_refused(run: Importing, response: Any, status: int, error: str) -> None:
    assert response.status_code == status, (response.status_code, response.text[:300])
    assert message(error) in said(response), f"the answer says: {message(error)}"
    run.assert_nothing_stored()


def test_ac_m3_7_oversized_upload_friendly_and_nothing_stored(tmp_path: Path) -> None:
    with importing(tmp_path) as run:
        response = run.upload("cv.pdf", b"%PDF-1.4\n" + b"x" * (11 * MIB))

        assert_refused(run, response, 413, "UploadTooLarge")


_PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + bytes(64)
_EXE = b"MZ\x90\x00\x03\x00\x00\x00\x04\x00\x00\x00\xff\xff\x00\x00" + bytes(64)


@pytest.mark.parametrize("case", ["png_named_pdf", "exe", "zip"])
def test_ac_m3_7_wrong_type_by_content_refused(tmp_path: Path, case: str) -> None:
    """The name and the declared type say PDF or Word; the bytes decide."""
    uploads = {
        "png_named_pdf": ("cv.pdf", _PNG),
        "exe": ("cv.docx", _EXE),
        "zip": ("cv.docx", fixture_path("not_word.zip").read_bytes()),
    }
    name, data = uploads[case]
    with importing(tmp_path) as run:
        assert_refused(run, run.upload(name, data), 400, "UnsupportedDocumentType")


def test_ac_m3_7_malformed_pdf_and_docx_friendly_and_nothing_stored(tmp_path: Path) -> None:
    damaged = {
        "truncated.pdf": "UnreadableDocument",
        "oversized_entry.docx": "UnreadableDocument",
        "encrypted.pdf": "EncryptedDocument",
        "empty_body.docx": "NoTextFound",
    }
    with importing(tmp_path) as run:
        for name, error in damaged.items():
            response = run.upload(name, fixture_path(name).read_bytes())

            assert_refused(run, response, 400, error)


def test_scanned_pdf_says_paste_text_instead(tmp_path: Path) -> None:
    with importing(tmp_path) as run:
        response = run.upload("scan.pdf", fixture_path("image_only.pdf").read_bytes())

        assert_refused(run, response, 400, "NoTextFound")
        assert "paste the text instead" in said(response)


def test_ac_m3_7_request_that_is_not_one_file_or_text_refused(tmp_path: Path) -> None:
    pdf = fixture_path("single_column.pdf").read_bytes()
    bodies = {
        "two files": multipart_body(FilePart("a.pdf", pdf), FilePart("b.pdf", pdf)),
        "a file and a text": multipart_body(FilePart("a.pdf", pdf), FieldPart(CV_TEXT)),
        "neither": multipart_body(FieldPart("fictional", name="other")),
    }
    with importing(tmp_path) as run:
        for label, body in bodies.items():
            response = run.post_body(body)

            assert response.status_code == 400, label
            assert message("UploadNotAccepted") in said(response), label
            run.assert_nothing_stored()


def test_ac_m3_7_text_over_60000_characters_refused_never_cut(tmp_path: Path) -> None:
    words = "Planned fictional delivery routes "
    too_long = (words * (MAX_CHARS // len(words) + 1))[:MAX_CHARS] + "x"
    assert len(too_long) == MAX_CHARS + 1
    with importing(tmp_path) as run:
        response = run.paste(too_long)

        assert_refused(run, response, 400, "CvTextTooLong")
        assert "paste" in message("CvTextTooLong").lower(), "it says what to do instead"

        # The limit counts characters, not bytes, and a text at the limit is taken whole.
        accented = ("é" * 9 + " ") * (MAX_CHARS // 10)
        assert len(accented) == MAX_CHARS and len(accented.encode("utf-8")) > MAX_CHARS
        run.finished(run.paste(accented))
        (kept,) = run.stored_files()
        assert kept.read_bytes() == accented.encode("utf-8"), "never cut"
        (request,) = run.spy.requests
        assert request.user.count("é") == 9 * (MAX_CHARS // 10), "the whole text was sent"


def test_ac_m3_7_failed_row_insert_removes_the_stored_file(tmp_path: Path) -> None:
    with importing(tmp_path) as run:
        run.execute(
            "CREATE TRIGGER fictional_refuse_import BEFORE INSERT ON cv_imports "
            "BEGIN SELECT RAISE(ABORT, 'fictional injected failure'); END"
        )

        response = run.paste(CV_TEXT)

        assert response.status_code >= 500, response.status_code
        assert "fictional injected failure" not in response.text
        run.assert_nothing_stored()


@pytest.mark.parametrize(
    "error",
    [
        "UnsupportedDocumentType",
        "UnreadableDocument",
        "EncryptedDocument",
        "NoTextFound",
        "ParseTimedOut",
        "UploadNotAccepted",
        "CvTextTooLong",
    ],
)
def test_document_errors_answer_400_with_their_message(tmp_path: Path, error: str) -> None:
    """The backstop for any route: a document error that escapes is a 400 with its message,
    never a 500 (``UploadTooLarge`` is 413; tests/unit/api/test_uploads.py)."""
    with importing(tmp_path) as run:

        async def probe(request: Request) -> None:
            errors = importlib.import_module("app.domain.errors")
            raise getattr(errors, error)(detail="fictional detail, never shown")

        run.app.add_api_route("/probe/document-error", probe, methods=["GET"])

        response = run.client.get("/probe/document-error", headers=run.run.with_token())

        assert response.status_code == 400, response.status_code
        assert said(response) == message(error)


# --- the stored file ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["text", "pdf", "docx"])
def test_stored_name_is_generated_and_0600(tmp_path: Path, kind: str) -> None:
    client_name = "Alex Example - CV (final).pdf"
    data = {
        "text": CV_TEXT.encode("utf-8"),
        "pdf": fixture_path("single_column.pdf").read_bytes(),
        "docx": fixture_path("with_tables.docx").read_bytes(),
    }[kind]
    with importing(tmp_path) as run:
        run.hold.hold = True  # the task is not what this test is about
        first = run.paste(CV_TEXT) if kind == "text" else run.upload(client_name, data)
        second = run.paste(CV_TEXT) if kind == "text" else run.upload(client_name, data)
        assert first.status_code in ACCEPTED and second.status_code in ACCEPTED

        rows = run.imports()
        assert len(rows) == 2, "the same bytes twice are two imports"
        names = [row["stored_name"] for row in rows]
        assert len(set(names)) == 2 and len({row["id"] for row in rows}) == 2
        for row in rows:
            name = row["stored_name"]
            assert row["kind"] == kind and row["state"] == "uploaded"
            assert STORED_NAME.fullmatch(name), name
            assert name.endswith(EXTENSIONS[kind]), "the extension follows the sniffed kind"
            assert "Alex" not in name and "final" not in name, "nothing of the client's name"
            path = run.imports_dir / name
            assert path.read_bytes() == data
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(run.imports_dir.stat().st_mode) == 0o700
        assert sorted(path.name for path in run.imports_dir.iterdir()) == sorted(names), (
            "nothing else is left in the folder (no temporary file)"
        )
        everything = repr(run.rows("SELECT * FROM cv_imports")) + repr(
            run.rows("SELECT * FROM tasks")
        )
        assert "Alex Example - CV" not in everything, "the client's file name is stored nowhere"


def test_stored_name_that_is_no_generated_file_name_is_never_joined(tmp_path: Path) -> None:
    """The name comes from the database. The table's CHECK already refuses a path; a name
    the service could not have given is still never read or deleted."""
    with importing(tmp_path) as run:
        run.spy.fail("network")
        run.finished(run.paste(CV_TEXT), status="failed")
        import_id = run.only_import()["id"]
        run.spy.as_wired()
        with pytest.raises(Exception, match="CHECK constraint"):
            run.execute("UPDATE cv_imports SET stored_name = ?", ("../fictional-keep.txt",))
        bystander = run.imports_dir / "fictional-keep.md"
        bystander.write_text("fictional", encoding="utf-8")
        run.execute("UPDATE cv_imports SET stored_name = ?", (bystander.name,))

        assert run.start(import_id).status_code == 404
        assert len(run.tasks()) == 1 and len(run.spy.requests) == 1, "nothing was started"
        assert run.remove(import_id).status_code in ACCEPTED

        assert run.imports() == []
        assert bystander.read_text(encoding="utf-8") == "fictional"
        run.assert_no_fact_rows()


# --- kept uploads, listed for the Fact bank page (M3.5) ----------------------------------------


def test_imports_listed_newest_first_without_file_names(tmp_path: Path) -> None:
    with importing(tmp_path) as run:
        service = run.app.state.cv_import
        assert service.imports() == ()

        done_task = run.finished(run.paste(CV_TEXT))
        first_at = run.clock.now()
        run.clock.advance(timedelta(minutes=5))
        run.spy.fail("network")
        assert run.upload("cv.pdf", fixture_path("single_column.pdf").read_bytes()).status_code in (
            ACCEPTED
        )
        (failed_id,) = [row.id for row in run.tasks() if row.id != done_task.id]
        run.wait_terminal(failed_id)

        listed = service.imports()

        older, newer = run.imports()
        assert [(item.id, item.kind, item.state, item.task_id) for item in listed] == [
            (newer["id"], "pdf", "uploaded", None),
            (older["id"], "text", "done", done_task.id),
        ]
        assert [item.created_at for item in listed] == [run.clock.now(), first_at]
        for item in listed:
            assert not hasattr(item, "stored_name")
        for name in (older["stored_name"], newer["stored_name"]):
            assert name not in repr(listed), "no file name leaves the service"

        # A removed import is no longer listed.
        assert run.remove(newer["id"]).status_code in ACCEPTED
        assert [item.id for item in service.imports()] == [older["id"]]


# --- the routes --------------------------------------------------------------------------------


def test_import_routes_need_token_and_origin(tmp_path: Path) -> None:
    with importing(tmp_path) as run:
        token = run.run.token
        refused = {
            "no token, no origin": {},
            "origin only": {"origin": ORIGIN},
            "token only": {TOKEN_HEADER: token},
            "wrong token": {TOKEN_HEADER: "x" * len(token), "origin": ORIGIN},
            "another site's origin": {TOKEN_HEADER: token, "origin": "http://evil.example"},
            "origin null": {TOKEN_HEADER: token, "origin": "null"},
        }
        body = multipart_body(FieldPart(CV_TEXT))
        for label, headers in refused.items():
            response = run.client.post(
                IMPORT_PATH,
                content=body,
                headers={"content-type": CONTENT_TYPE, **headers},
            )
            assert response.status_code == 403, label
        run.assert_nothing_stored()

        # An upload whose attempt failed: it can be started again or removed.
        run.spy.fail("network")
        run.finished(run.paste(CV_TEXT), status="failed")
        import_id = run.only_import()["id"]
        run.spy.as_wired()
        for label, headers in refused.items():
            for path in (start_path(import_id), remove_path(import_id)):
                response = run.client.post(path, headers=headers)
                assert response.status_code == 403, (label, path)
                assert token not in response.text
        run.assert_upload_kept(import_id)
        assert len(run.tasks()) == 1 and len(run.spy.requests) == 1, "nothing was started"

        # The same requests with both are served: the refusals above were the checks'.
        assert run.remove(import_id).status_code in ACCEPTED
        assert run.imports() == []
        # None of the three is a page: a GET never reaches them.
        for path in (IMPORT_PATH, start_path(import_id), remove_path(import_id)):
            assert run.client.get(path, headers=run.run.with_token()).status_code in (404, 405)


def test_remove_deletes_file_and_row(tmp_path: Path) -> None:
    with importing(tmp_path) as run:
        run.finished(run.paste(CV_TEXT))
        import_id = run.only_import()["id"]
        assert len(run.stored_files()) == 1

        response = run.remove(import_id)

        assert response.status_code in ACCEPTED, (response.status_code, response.text[:300])
        assert run.imports() == []
        assert run.stored_files() == []
        # The drafts stay, no longer tied to an import (ADR-090 item 7).
        drafts = run.facts()
        assert len(drafts) == 4 and len(run.roles()) == 2
        assert {d["cv_import_id"] for d in drafts} == {None}
        assert_only_drafts(run)
        # Removing it again finds nothing.
        assert run.remove(import_id).status_code == 404


def test_remove_refused_while_the_task_is_active(tmp_path: Path) -> None:
    with importing(tmp_path) as run:
        run.hold.hold = True
        assert run.paste(CV_TEXT).status_code in ACCEPTED
        task_id = run.only_task().id
        import_id = run.only_import()["id"]
        run.wait_entered(1)

        response = run.remove(import_id)

        assert response.status_code == 409, response.status_code
        run.assert_upload_kept(import_id)
        assert run.status(task_id) == "running", "the task was left alone"

        run.hold.open()
        assert str(run.wait_terminal(task_id).status.value) == "succeeded"
        assert run.remove(import_id).status_code in ACCEPTED
        assert run.imports() == [] and run.stored_files() == []
        assert len(run.facts()) == 4


def test_unknown_or_unsafe_import_id_is_404_and_touches_nothing(tmp_path: Path) -> None:
    with importing(tmp_path) as run:
        run.spy.fail("network")
        run.finished(run.paste(CV_TEXT), status="failed")
        stored = run.only_import()
        bystander = run.data_dir / "fictional-keep.txt"
        bystander.write_text("fictional", encoding="utf-8")
        ids = (
            "no-such-import",
            stored["stored_name"],  # a file name is not an id
            "..%2Ffictional-keep.txt",
            "%2E%2E%2F%2E%2E%2Fcfg",
            "x" * 300,
        )

        for import_id in ids:
            for path in (start_path(import_id), remove_path(import_id)):
                response = run.client.post(path, headers=run.run.action())
                assert response.status_code == 404, (path, response.status_code)
                assert "fictional-keep" not in response.text, "nothing sent is shown back"

        assert bystander.read_text(encoding="utf-8") == "fictional"
        run.assert_upload_kept(stored["id"])
        assert len(run.tasks()) == 1, "nothing was started"


# --- nothing of the CV in task rows, events or logs --------------------------------------------


def test_no_cv_text_in_task_rows_events_or_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The M3.4 part of AC-M1.4 for CV text (the cross-cutting scan is M3.10): a success, an
    invalid answer, an empty answer and a provider error."""
    events = importlib.import_module("app.api.events")
    frames: list[bytes] = []
    publish = events.TaskEvents.publish

    def recording(self: Any, change: Any) -> None:
        frames.append(events.task_updated_frame(change))
        frames.append(repr(change).encode())
        publish(self, change)

    monkeypatch.setattr(events.TaskEvents, "publish", recording)
    caplog.set_level(logging.DEBUG)

    shown: list[str] = []
    with importing(tmp_path) as run:
        run.finished(run.paste(CV_TEXT))
        for keyword in (fake_scripts.KEYWORD_MALFORMED, fake_scripts.KEYWORD_OFFLINE):
            before = {row.id for row in run.tasks()}
            assert run.paste(with_keyword(keyword)).status_code in ACCEPTED
            (new,) = [row.id for row in run.tasks() if row.id not in before]
            assert str(run.wait_terminal(new).status.value) == "failed"
        run.spy.answer({"roles": [], "units": []})
        before = {row.id for row in run.tasks()}
        assert run.paste(CV_TEXT).status_code in ACCEPTED
        (new,) = [row.id for row in run.tasks() if row.id not in before]
        assert str(run.wait_terminal(new).status.value) == "failed"

        task_rows = run.rows("SELECT * FROM tasks")
        usage_rows = run.rows("SELECT * FROM usage_records")
        assert len(task_rows) == 4 and len(usage_rows) >= 5
        for row in run.tasks():
            fragment = run.client.get(f"/tasks/{row.id}", headers=run.run.htmx())
            assert fragment.status_code == 200
            shown.append(fragment.text)
        shown.append(run.client.get("/tasks/panel", headers=run.run.htmx()).text)
        # Control: the markers are found where CV text belongs.
        versions = repr(run.rows("SELECT * FROM fact_versions"))
        assert "invoice processing" in versions and "delivery routes" in versions

    assert frames, "task events were published"
    logs = [
        f"{record.getMessage()} {record.args!r} {record.exc_text or ''}"
        for record in caplog.records
    ]
    log_files = [
        path.read_text(encoding="utf-8", errors="replace")
        for path in (tmp_path / "data" / "logs").rglob("*")
        if path.is_file()
    ]
    evidence = {
        "tasks rows": repr(task_rows),
        "usage rows": repr(usage_rows),
        "task events": b"\n".join(frames).decode("utf-8", errors="replace"),
        "task fragments": "\n".join(shown),
        "log records": "\n".join(logs),
        "log files": "\n".join(log_files),
    }
    for where, text in evidence.items():
        for marker in CV_MARKERS:
            assert marker not in text, f"CV text ({marker!r}) found in {where}"
    # Only fixed kinds are stored as the reason.
    assert {row["error"] for row in task_rows} == {
        None,
        "InvalidModelOutput",
        "NetworkUnavailable",
        "NothingExtracted",
    }


def test_htmx_and_plain_refusals_carry_the_same_message(tmp_path: Path) -> None:
    """A refused upload says why, whichever way the form was sent."""
    with importing(tmp_path) as run:
        scan = multipart_body(FilePart("scan.pdf", fixture_path("image_only.pdf").read_bytes()))
        for htmx in (False, True):
            response = run.post_body(scan, htmx=htmx)

            assert response.status_code == 400, (htmx, response.status_code)
            assert message("NoTextFound") in said(response)
        run.assert_nothing_stored()
