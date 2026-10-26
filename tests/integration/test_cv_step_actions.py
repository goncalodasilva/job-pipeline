"""M6.8 the CV step's actions through its routes: starting and regenerating (note, budget gate,
"Replace everything, including my edits"), inline edit, add, remove and "This is true: save it as a
fact", the "Save this as a fact?" question, approval, the errors in plain words, a stale version
(409 with the shared reload prompt and the typed text kept) and how the step refreshes itself
(AC-M6.1, AC-M6.3, AC-M6.4 in the UI; spec 3.1; ADR-106 items 8, 9 and 11; ADR-113 items 4, 8, 10).

Assumed interface: see the docstring of ``tests/cv_step_pages.py`` (routes, fields, markup hooks
and the announcement strings). The service half is M6.7's. Real SQLite in a temp folder, the fake
provider behind the spy, no network, no fixed waits.
"""

import re

import pytest

from app.domain.budgets import Budget
from app.domain.errors import VersionConflict, message_for_kind
from app.services.documents import (
    IN_PROGRESS,
    NO_TEMPLATE,
    NOTE_BAD_CHARACTERS,
    NOTE_TOO_LONG,
    NOTHING_TO_APPROVE,
    REPLACE_NOT_CONFIRMED,
)
from app.services.tailoring import NO_FACTS, NO_SUMMARY
from tests.board_pages import elements
from tests.cv_step_pages import (
    ANNOUNCE,
    CONFIRM_BUTTON,
    NOT_IN_FACT_BANK,
    REPLACE_ALL,
    SAVE_AS_FACT,
    CvStep,
    add_form,
    autofocus_elements,
    control,
    cv_step_app,
    cv_url,
    described_by,
    fact_links,
    flags_of,
    hidden_inputs,
    line_by_unit,
    lines,
    mark_text,
    state,
    step,
    tailored_lines,
    unit_url,
    version_id,
)
from tests.document_pages import LANGUAGE_UNIT, NOTE, ROLE_A, TAILOR_CV
from tests.integration.test_board import snapshot
from tests.integration.test_documents_generation import GLOBEX, OK_TEXTS, UNCITED
from tests.settings_board_pages import accessible_name
from tests.tailoring_ai_inputs import FACT_1, FACT_2, KEPT_TEXT
from tests.tailoring_pages import announced, by_id, fragment_url, has_id, inside, said_text, stepper

pytestmark = pytest.mark.integration

NEW_CLAIM = "Planned 60 delivery routes a week with a team of 9 and a budget of $2m."


@pytest.fixture
def cv(tmp_path):
    with cv_step_app(tmp_path) as desk:
        yield desk


def options(page: str, select_name: str) -> list[tuple[str, str]]:
    box = control(page, add_form(page), select_name)
    return [
        (str(o.attrs.get("value", "")), o.text)
        for o in elements(page)
        if o.tag == "option" and o.within(box)
    ]


def tasks(cv: CvStep, aid: str | None = None) -> list[dict]:
    found = cv.rows("SELECT * FROM tasks WHERE type='tailor_cv' ORDER BY rowid")
    return [row for row in found if aid is None or row["dedupe_key"] == f"tailor_cv:{aid}"]


def rows_checked(page: str, element_id: str) -> bool:
    return "checked" in by_id(page, element_id).attrs


# --- Start: the form, the budget gate, one running task -------------------------------------------


def test_cv_step_generate_form_budget_gate_note_and_one_running_task(cv: CvStep):
    app = cv.cv_ready()
    second = cv.cv_ready("second-cv-job")
    usage = cv.r.run.app.state.usage

    shown = cv.step3(app.id).text
    form = by_id(shown, "cv-generate-form")
    assert form.attrs["action"] == cv_url(app.id, "generate") and form.attrs["method"] == "post"
    assert form.attrs["hx-target"] == "#tailoring"
    note = by_id(shown, "cv-note")
    assert note.tag == "textarea" and note.attrs["name"] == "note"
    assert accessible_name(shown, note) == "Note for the CV (optional)"
    assert by_id(shown, "cv-generate-start").text == "Generate CV"
    # Nothing to replace before there is a CV.
    assert not has_id(shown, "cv-replace-all") and not has_id(shown, "cv-confirm-replace")
    assert state(shown) == "none" and tailored_lines(shown) == []

    def set_budget(**limits: int) -> None:
        budget = Budget(reset_day=1, **limits)
        usage.save_budget("fake", budget, expected_version=usage.load_budgets().version)

    def use(count: int) -> None:
        for _ in range(count):
            record = usage.reserve("fake", tier="thorough", task_type=TAILOR_CV)
            usage.complete(record, success=True, tokens=None)

    # A warning is due: the dialog asks first, nothing starts, and it keeps what was typed.
    set_budget(allowance=10, first_warning=3, second_warning=5, block_at=8)
    use(2)
    warned = cv.post_cv(app.id, "generate", {"note": "shorter"})
    assert warned.status_code == 200 and "Continue" in warned.text
    assert has_id(warned.text, "tailoring-dialog-3")
    assert tasks(cv) == [] and cv.spy.requests == []
    fields = hidden_inputs(warned.text)
    assert fields["note"] == "shorter"

    # Continue starts exactly one task, announced, with focus on the heading.
    cv.r.run.hold.hold = True
    accepted = cv.post_fields(cv_url(app.id, "generate"), fields)
    assert accepted.status_code == 200 and announced(accepted) == [ANNOUNCE["started"]]
    assert [e.attrs.get("id") for e in autofocus_elements(accepted.text)] == ["cv-heading"]
    cv.r.run.wait_entered(1)
    (task,) = tasks(cv)
    assert task["dedupe_key"] == f"tailor_cv:{app.id}"

    # While it runs: a status line, no form and no edit control, and the step polls itself.
    for running in (cv.step3(app.id).text, cv.page3(app.id).text):
        assert state(running) == "running" and step(running).attrs["data-readonly"] == "true"
        status = by_id(running, "cv-running")
        assert status.attrs.get("role") == "status" and "hx-preserve" in status.attrs
        assert not has_id(running, "cv-generate-form") and not has_id(running, "cv-approve")
        assert "Running" in stepper(running)[3].text
        region = by_id(running, "tailoring")
        assert region.attrs["hx-get"] == fragment_url(app.id, 3)
        assert "every 2s" in str(region.attrs["hx-trigger"])
        assert "tasks:updated from:body" in str(region.attrs["hx-trigger"])
    # The same request again is the running task; a different note is refused in plain words.
    assert cv.post_cv(app.id, "generate", {"note": "shorter"}).status_code == 200
    assert [row["id"] for row in tasks(cv)] == [task["id"]]
    different = cv.post_cv(app.id, "generate", {"note": "longer"})
    assert different.status_code == 409
    assert by_id(different.text, "cv-error").text == IN_PROGRESS
    assert announced(different) == [IN_PROGRESS]
    assert [row["id"] for row in tasks(cv)] == [task["id"]]
    # Another application gets its own task.
    other = cv.service.start_generate(second.id)
    assert other.id != task["id"]
    cv.r.run.hold.open()
    cv.r.run.wait_terminal(task["id"])
    cv.r.run.wait_terminal(other.id)
    assert "shorter" in cv.requests(TAILOR_CV)[0].user  # the note went to the request
    tiers = cv.rows("SELECT DISTINCT tier FROM usage_records WHERE task_type=?", (TAILOR_CV,))
    assert {row["tier"] for row in tiers} == {"thorough"}

    # At the blocking point nothing starts: 409 with the reason and the button blocked.
    (count,) = cv.rows("SELECT count(*) AS n FROM usage_records")
    use(8 - count["n"])
    known, requests = len(tasks(cv)), len(cv.spy.requests)
    blocked = cv.post_cv(app.id, "generate", {"note": "again"})
    assert blocked.status_code == 409 and "Monthly limit reached" in said_text(blocked.text)
    assert len(tasks(cv)) == known and len(cv.spy.requests) == requests
    button = by_id(cv.step3(app.id).text, "cv-generate-start")
    assert button.attrs.get("aria-disabled") == "true" and "disabled" not in button.attrs
    reason = str(button.attrs["aria-describedby"])
    assert "Monthly limit reached" in by_id(cv.step3(app.id).text, reason).text


def test_cv_step_refreshing_after_the_task_says_the_cv_is_ready(cv: CvStep):
    app = cv.cv_ready()
    cv.generate_by_route(app.id, "ok")

    polled = cv.step3(app.id, **{"HX-Trigger": "tailoring"})
    plain = cv.step3(app.id)

    assert announced(polled) == [ANNOUNCE["ready"]]
    assert announced(plain) == []
    assert not autofocus_elements(polled.text)  # said, not focused: the user may be anywhere


# --- Not ready: plain words and a way forward ---------------------------------------------------


@pytest.mark.parametrize(
    "case", ["no_template", "draft_template", "other_country", "summary", "facts"]
)
def test_cv_step_not_ready_says_what_to_do_first(cv: CvStep, case: str):
    if case == "no_template":
        app = cv.cv_ready(template=False)
    elif case == "draft_template":
        app = cv.cv_ready(template=False)
        cv.put_template("PT", approved=False)
    elif case == "other_country":
        app = cv.cv_ready(template=False)
        cv.put_template("DE")
    elif case == "summary":
        app = cv.cv_ready(summary=False)
    else:
        app = cv.cv_ready(seed_facts=False)
        assert cv.rows("SELECT id FROM facts") == []
    message, link, href = {
        "summary": (NO_SUMMARY, "Open job", "/jobs/cv-job"),
        "facts": (NO_FACTS, "Open facts", "/facts"),
    }.get(case, (NO_TEMPLATE, "Draft the template", "#cv-template-heading"))

    for shown in (cv.step3(app.id).text, cv.page3(app.id).text):
        assert by_id(shown, "cv-not-ready").text == message
        anchor = by_id(shown, "cv-not-ready-link")
        assert (anchor.tag, anchor.text, anchor.attrs["href"]) == ("a", link, href)
        assert not has_id(shown, "cv-generate-form") and not has_id(shown, "cv-approve")
        assert state(shown) == "none"
    if href.startswith("#"):  # the template unit it points at is on the same step
        assert has_id(cv.step3(app.id).text, "cv-template-heading") or case == "no_template"

    before = snapshot(cv.r)
    refused = cv.post_cv(app.id, "generate", {"note": "shorter"})
    assert refused.status_code == 409
    assert by_id(refused.text, "cv-not-ready").text == message and announced(refused) == [message]
    assert snapshot(cv.r) == before and cv.spy.requests == [] and tasks(cv) == []


# --- AC-M6.3: regenerate keeps edits unless "replace everything" is confirmed ---------------------


def test_ac_m6_3_cv_step_regenerate_keeps_edits_and_replace_everything_needs_confirmation(
    cv: CvStep,
):
    app = cv.cv_ready()
    cv.generate(app.id, "ok")
    assert cv.edit(app.id, cv.unit_id(app.id, FACT_1), KEPT_TEXT).status_code == 200
    shown = cv.step3(app.id).text
    assert by_id(shown, "cv-generate-start").text == "Regenerate"
    replace = by_id(shown, "cv-replace-all")
    confirm = by_id(shown, "cv-confirm-replace")
    for box, label in ((replace, REPLACE_ALL), (confirm, "Yes, replace my edits too")):
        assert box.tag == "input" and box.attrs["type"] == "checkbox" and box.attrs["value"] == "1"
        assert accessible_name(shown, box) == label and "checked" not in box.attrs
    assert replace.attrs["name"] == "replace_all" and confirm.attrs["name"] == "confirm_replace"

    # A short note: the user's edit survives the new version, which is labelled as theirs.
    started = cv.generate_by_route(app.id, "ok", note=NOTE)
    assert announced(started) == [ANNOUNCE["started"]]
    assert "Lead with logistics" in cv.requests(TAILOR_CV)[-1].user
    version = cv.current_version_id(app.id)
    assert KEPT_TEXT in cv.texts(version) and len(cv.versions(app.id)) == 3
    page = cv.step3(app.id).text
    kept = line_by_unit(page, cv.unit_id(app.id, KEPT_TEXT))
    assert kept.attrs["data-origin"] == "user" and "Edited by you" in kept.text

    # "Replace everything" without the confirmation is refused: nothing is queued, the typing and
    # the ticked box stay, and the confirmation is the invalid control.
    tasks_before, requests_before = len(tasks(cv)), len(cv.spy.requests)
    refused = cv.post_cv(app.id, "generate", {"note": "again", "replace_all": "1"})
    assert refused.status_code == 422
    assert announced(refused) == [REPLACE_NOT_CONFIRMED]
    box = by_id(refused.text, "cv-confirm-replace")
    assert box.attrs["aria-invalid"] == "true" and "data-autofocus" in box.attrs
    assert described_by(refused.text, box) == REPLACE_NOT_CONFIRMED
    assert by_id(refused.text, "cv-note").text == "again"
    assert rows_checked(refused.text, "cv-replace-all") and "checked" not in box.attrs
    assert len(autofocus_elements(refused.text)) == 1
    assert len(tasks(cv)) == tasks_before and len(cv.spy.requests) == requests_before
    assert KEPT_TEXT in cv.texts(cv.current_version_id(app.id))

    # Confirmed, it replaces everything, the edit included; the old versions stay.
    cv.generate_by_route(app.id, "ok", replace_all="1", confirm_replace="1")
    latest = cv.current_version_id(app.id)
    assert sorted(cv.texts(latest)) == sorted(OK_TEXTS) and KEPT_TEXT not in cv.texts(latest)
    assert KEPT_TEXT in cv.texts(version) and len(cv.versions(app.id)) == 4


def test_cv_step_note_is_checked_and_kept_when_refused(cv: CvStep):
    app = cv.cv_ready()
    for typed, message in (("a" * 501, NOTE_TOO_LONG), ("shorter‮", NOTE_BAD_CHARACTERS)):
        refused = cv.post_cv(app.id, "generate", {"note": typed})
        assert refused.status_code == 422 and announced(refused) == [message]
        box = by_id(refused.text, "cv-note")
        assert box.attrs["aria-invalid"] == "true" and described_by(refused.text, box) == message
        assert "data-autofocus" in box.attrs and len(autofocus_elements(refused.text)) == 1
        assert by_id(refused.text, "cv-note").text == typed.strip()
    assert tasks(cv) == [] and cv.spy.requests == []


# --- Inline edit, add, remove, confirm: each is a new draft version -----------------------------


def test_cv_step_edit_add_remove_confirm_each_make_a_new_version_and_announce(cv: CvStep):
    app = cv.cv_ready()
    cv.generate(app.id, "ok")
    v1 = cv.current_version_id(app.id)
    before = cv.texts(v1)

    # Edit: saved, announced, and focus lands on the changed line.
    edited = cv.edit(app.id, cv.unit_id(app.id, FACT_1), KEPT_TEXT)
    assert edited.status_code == 200 and announced(edited) == [ANNOUNCE["edit"]]
    v2 = cv.current_version_id(app.id)
    assert v2 != v1 and cv.version_row(v2)["parent_version_id"] == v1
    assert (cv.version_row(v2)["origin"], cv.version_row(v2)["status"]) == ("user", "draft")
    assert cv.texts(v1) == before  # the earlier version is untouched
    line_id = cv.unit_id(app.id, KEPT_TEXT)
    (focus,) = autofocus_elements(edited.text)
    assert focus.tag == "li" and focus.attrs["data-unit-id"] == line_id
    assert focus.attrs["tabindex"] == "-1"
    assert state(edited.text) == "draft" and version_id(edited.text) == v2
    assert "dialog" not in edited.text.lower().replace("aria-", "")  # no question: no new claim

    # Add: the form offers the template's sections and the roles of the CV.
    shown = cv.step3(app.id).text
    assert options(shown, "section") == [
        ("summary", "Summary"),
        ("experience", "Work history"),
        ("education", "Education"),
        ("skills", "Skills"),
        ("languages", "Languages"),
    ]
    assert options(shown, "role")[0] == ("", "No role")
    assert (f"{ROLE_A[0]} at {ROLE_A[1]}") in [text for _, text in options(shown, "role")]
    added = cv.add(app.id, "languages", "Fluent in Portuguese.")
    assert added.status_code == 200 and announced(added) == [ANNOUNCE["add"]]
    mine = line_by_unit(added.text, cv.unit_id(app.id, "Fluent in Portuguese."))
    assert mine.attrs["data-mark"] == "added" and mine.attrs["data-flagged"] == "true"
    assert flags_of(added.text, mine) == [
        ("unsupported", NOT_IN_FACT_BANK, "Fluent in Portuguese.")
    ]
    assert [e.attrs["data-unit-id"] for e in autofocus_elements(added.text)] == [
        mine.attrs["data-unit-id"]
    ]
    assert any(a.attrs.get("data-section") == "languages" for a in mine.ancestors())
    with_role = cv.add(
        app.id, "experience", "Mentored new planners.", role=f"{ROLE_A[0]} at {ROLE_A[1]}"
    )
    assert with_role.status_code == 200
    stored = cv.unit_with(cv.current_version_id(app.id), "Mentored new planners.")
    assert stored["role_title"] == ROLE_A[0] and stored["origin"] == "user"

    # Confirm: "This is true — save it as a fact" makes a fact and the line cites it.
    facts = cv.table_counts()["facts"]
    confirmed = cv.confirm(
        app.id, mine.attrs["data-unit-id"] and cv.unit_id(app.id, "Fluent in Portuguese.")
    )
    assert confirmed.status_code == 200 and announced(confirmed) == [ANNOUNCE["confirm"]]
    assert cv.table_counts()["facts"] == facts + 1
    assert cv.fact_row(9)["status"] == "confirmed"
    done = line_by_unit(confirmed.text, cv.unit_id(app.id, "Fluent in Portuguese."))
    assert "data-flagged" not in done.attrs and flags_of(confirmed.text, done) == []
    assert fact_links(confirmed.text, done) == [
        (f"/facts/{cv.fact_id(9)}/edit", "Fluent in Portuguese.")
    ]
    assert [e.attrs["data-unit-id"] for e in autofocus_elements(confirmed.text)] == [
        done.attrs["data-unit-id"]
    ]

    # Remove: the line leaves the page, stays as a tombstone, and the heading takes focus.
    removed = cv.remove(app.id, cv.unit_id(app.id, LANGUAGE_UNIT))
    assert removed.status_code == 200 and announced(removed) == [ANNOUNCE["remove"]]
    assert LANGUAGE_UNIT not in [line.text for line in lines(removed.text)]
    assert [e.attrs.get("id") for e in autofocus_elements(removed.text)] == ["cv-heading"]
    tomb = [r for r in cv.units(cv.current_version_id(app.id)) if r["text"] == LANGUAGE_UNIT]
    assert [r["removed"] for r in tomb] == [1]
    assert len(cv.versions(app.id)) == 6  # generated, edited, two added, confirmed, removed


def test_cv_step_edit_of_an_approved_cv_makes_a_new_draft_and_says_so(cv: CvStep):
    app = cv.cv_ready()
    cv.generate(app.id, "ok")
    first = cv.current_version_id(app.id)
    assert cv.approve_now(app.id).status_code == 200
    shown = cv.step3(app.id).text
    assert state(shown) == "approved" and by_id(shown, "cv-approved")
    line = line_by_unit(shown, cv.unit_id(app.id, FACT_2))
    assert {"Edit", "Remove"} <= {
        e.text for e in inside(shown, line, lambda e: e.tag in ("summary", "button"))
    }

    edited = cv.edit(app.id, cv.unit_id(app.id, FACT_2), "Cut invoice processing time by 35%.")

    assert edited.status_code == 200 and announced(edited) == [ANNOUNCE["new_draft"]]
    assert state(edited.text) == "draft"
    assert cv.version_row(first)["status"] == "approved"  # what was approved is untouched
    assert cv.version_row(cv.current_version_id(app.id))["status"] == "draft"
    statuses = {
        i.attrs["data-version-id"]: i.attrs["data-version-status"]
        for i in inside(edited.text, by_id(edited.text, "cv-versions"), lambda e: e.tag == "li")
    }
    assert statuses[first] == "approved" and statuses[cv.current_version_id(app.id)] == "draft"


# --- AC-M6.1: a flag is resolved by editing, removing or confirming the line --------------------


@pytest.mark.parametrize("how", ["edit", "remove", "confirm"])
def test_ac_m6_1_cv_step_flag_resolved_by_edit_remove_or_confirm(cv: CvStep, how: str):
    app = cv.cv_ready()
    cv.generate(app.id, "adversarial_invented_employer")  # two lines need a check
    uid = cv.unit_id(app.id, GLOBEX)

    if how == "edit":
        answer = cv.edit(app.id, uid, FACT_1)
    elif how == "remove":
        answer = cv.remove(app.id, uid)
    else:
        answer = cv.confirm(app.id, uid)

    assert answer.status_code == 200
    reason = by_id(answer.text, "cv-approve-reason")
    assert reason.text == "1 line needs your check."
    assert by_id(answer.text, "cv-approve").attrs["aria-disabled"] == "true"
    # The other line is the one still flagged.
    flagged = [line for line in tailored_lines(answer.text) if "data-flagged" in line.attrs]
    assert len(flagged) == 1


# --- AC-M6.4: a new claim opens "Save this as a fact?" ------------------------------------------


def test_ac_m6_4_cv_step_new_claim_opens_save_as_fact_question(cv: CvStep):
    app = cv.cv_ready()
    cv.generate(app.id, "ok")

    answer = cv.edit(app.id, cv.unit_id(app.id, FACT_1), NEW_CLAIM)

    assert answer.status_code == 200 and announced(answer) == [ANNOUNCE["offer"]]
    new_id = cv.unit_id(app.id, NEW_CLAIM)
    version = cv.current_version_id(app.id)
    dialog = by_id(answer.text, "cv-save-fact")
    assert dialog.tag == "dialog" and "data-modal" in dialog.attrs
    assert dialog.attrs["role"] == "dialog" and dialog.attrs["aria-modal"] == "true"
    assert dialog.attrs["aria-labelledby"] == "cv-save-fact-title"
    assert dialog.attrs["aria-describedby"] == "cv-save-fact-claims"
    assert by_id(answer.text, "cv-save-fact-title").text == SAVE_AS_FACT
    claims = inside(answer.text, by_id(answer.text, "cv-save-fact-claims"), lambda e: e.tag == "li")
    assert [c.text for c in claims] == ["9", "$2m"]
    (form,) = inside(answer.text, dialog, lambda e: e.tag == "form")
    assert form.attrs["action"] == unit_url(app.id, new_id, "confirm")
    assert hidden_inputs(answer.text)["version"] == version
    safe = [
        e for e in inside(answer.text, dialog, lambda e: e.tag == "button") if e.text == "Not now"
    ]
    assert len(safe) == 1 and "autofocus" in safe[0].attrs
    assert safe[0].attrs["data-return-focus"] == f"#line-{new_id}"
    assert [e.text for e in inside(answer.text, form, lambda e: e.tag == "button")] == [
        "Yes, save it as a fact"
    ]
    # The focus moves to the question, so nothing else asks for it.
    assert autofocus_elements(answer.text) == []
    # The line is flagged either way, so "Not now" leaves it blocking approval.
    line = line_by_unit(answer.text, new_id)
    assert [kind for kind, _, _ in flags_of(answer.text, line)] == ["needs_check", "needs_check"]
    plain = cv.step3(app.id).text
    assert not has_id(plain, "cv-save-fact")  # a read never asks again
    assert by_id(plain, "cv-approve").attrs["aria-disabled"] == "true"

    # Yes: a confirmed fact from the line, which then cites it.
    facts = cv.table_counts()["facts"]
    yes = cv.post_fields(
        unit_url(app.id, new_id, "confirm"), {"version": hidden_inputs(answer.text)["version"]}
    )
    assert yes.status_code == 200 and announced(yes) == [ANNOUNCE["confirm"]]
    assert cv.table_counts()["facts"] == facts + 1 and cv.fact_row(9)["status"] == "confirmed"
    saved = cv.unit_with(cv.current_version_id(app.id), NEW_CLAIM)
    assert saved["cited"] == ["F9@1"] and saved["flag_list"] == []
    assert "aria-disabled" not in by_id(yes.text, "cv-approve").attrs


def test_cv_step_edit_without_a_new_claim_asks_nothing(cv: CvStep):
    app = cv.cv_ready()
    cv.generate(app.id, "ok")
    facts = cv.table_counts()["facts"]

    answer = cv.edit(
        app.id, cv.unit_id(app.id, FACT_1), "Planned 60 delivery routes a week, with a team of 3."
    )

    assert answer.status_code == 200 and announced(answer) == [ANNOUNCE["edit"]]
    assert not has_id(answer.text, "cv-save-fact") and len(autofocus_elements(answer.text)) == 1
    assert cv.table_counts()["facts"] == facts


# --- The service's errors, in plain words -------------------------------------------------------


def test_cv_step_service_errors_map_to_plain_messages(cv: CvStep):
    app = cv.cv_ready()
    cv.generate(app.id, "ok")
    uid = cv.unit_id(app.id, FACT_1)
    version = cv.current_version_id(app.id)
    before = snapshot(cv.r)

    # UnitInvalid: 422, the editor open with what was typed and the error tied to its field.
    for typed, message in (
        ("x" * 401, "Keep the line to 400 characters or fewer."),
        ("   ", "Write the line before saving."),
        ("Tab\tseparated", "Use plain text on one line, without special characters."),
    ):
        refused = cv.edit(app.id, uid, typed)
        assert refused.status_code == 422 and announced(refused) == [message]
        line = line_by_unit(refused.text, uid)
        (box,) = inside(refused.text, line, lambda e: e.tag == "textarea")
        assert box.attrs["aria-invalid"] == "true" and "data-autofocus" in box.attrs
        assert described_by(refused.text, box) == message
        assert box.text == " ".join(typed.split())
        (disclosure,) = [
            e
            for e in inside(refused.text, line, lambda e: e.tag == "details")
            if "data-line-edit" in e.attrs
        ]
        assert "open" in disclosure.attrs
        assert len(autofocus_elements(refused.text)) == 1
    added = cv.add(app.id, "languages", "")
    assert added.status_code == 422 and announced(added) == ["Write the line before saving."]
    box = control(added.text, add_form(added.text), "text")
    assert box.attrs["aria-invalid"] == "true" and "data-autofocus" in box.attrs
    forged = cv.add(app.id, "no-such-section", "Fluent in Portuguese.")
    assert forged.status_code == 422
    assert announced(forged) == ["Choose one of the CV's sections."]
    chooser = control(forged.text, add_form(forged.text), "section")
    assert chooser.attrs["aria-invalid"] == "true"
    assert control(forged.text, add_form(forged.text), "text").text == "Fluent in Portuguese."
    assert snapshot(cv.r) == before  # nothing was written

    # UnitNotFound: a line that is not in this version.
    missing = cv.post_unit(app.id, "no-such-unit", "edit", {"version": version, "text": "Edited."})
    assert missing.status_code == 404 and missing.text == "That line wasn't found in this version."

    # Plain words: no class names, no stack, no ids.
    for text in (IN_PROGRESS, NOTHING_TO_APPROVE, REPLACE_NOT_CONFIRMED, NO_TEMPLATE):
        assert not re.search(r"Exception|Error\b|Traceback|\bF\d+@\d+", text)


# --- A stale version: the shared reload prompt, typing kept -------------------------------------


def test_cv_step_version_conflict_409_keeps_typed_text_and_uses_the_reload_prompt(cv: CvStep):
    app = cv.cv_ready()
    cv.generate(app.id, "ok")
    old = cv.current_version_id(app.id)
    uid = cv.unit_id(app.id, FACT_1)
    # Another tab changes the CV after this page was drawn.
    cv.service.edit_unit(
        app.id, old, cv.unit_id(app.id, FACT_2), "Cut invoice processing time by 35%."
    )
    new = cv.current_version_id(app.id)
    before = snapshot(cv.r)

    typed = "My careful rewording of this line, still unsaved."
    stale = cv.post_unit(app.id, uid, "edit", {"version": old, "text": typed})

    assert stale.status_code == 409 and announced(stale) == [VersionConflict.message]
    prompt = by_id(stale.text, "cv-conflict")
    assert prompt.attrs["role"] == "alert" and "data-autofocus" in prompt.attrs
    assert VersionConflict.message in prompt.text
    links = inside(stale.text, prompt, lambda e: e.tag == "a")
    assert [(a.text, a.attrs["href"]) for a in links] == [
        ("Reload the page", f"/applications/{app.id}?tab=tailoring&step=3")
    ]
    kept = by_id(stale.text, "cv-kept-text")
    assert kept.tag == "textarea" and "readonly" in kept.attrs and kept.text == typed
    assert accessible_name(stale.text, kept) == "Your unsaved text"
    assert version_id(stale.text) == new  # it shows the version that is current now
    assert len(autofocus_elements(stale.text)) == 1
    assert snapshot(cv.r) == before

    # Every other write says the same, with nothing to keep.
    for action, values in (("remove", {}), ("confirm", {})):
        answer = cv.post_unit(app.id, uid, action, {"version": old, **values})
        assert answer.status_code == 409 and has_id(answer.text, "cv-conflict")
        assert not has_id(answer.text, "cv-kept-text")
    approved = cv.post_cv(app.id, "approve", {"version": old})
    assert approved.status_code == 409 and has_id(approved.text, "cv-conflict")
    # An approved version cannot be approved again.
    assert cv.approve_now(app.id).status_code == 200
    again = cv.post_cv(app.id, "approve", {"version": cv.current_version_id(app.id)})
    assert again.status_code == 409 and has_id(again.text, "cv-conflict")


# --- The step refreshes itself, and the editor is never replaced --------------------------------


def test_cv_step_task_refresh_only_polls_while_running_and_protects_open_editors(cv: CvStep):
    app = cv.cv_ready()
    cv.generate(app.id, "ok")

    idle = by_id(cv.step3(app.id).text, "tailoring")

    # Task events refresh the step, but only a running task adds the 2 s poll.
    assert idle.attrs["hx-get"] == fragment_url(app.id, 3) and idle.attrs["hx-swap"] == "outerHTML"
    assert "tasks:updated from:body" in str(idle.attrs["hx-trigger"])
    assert "every" not in str(idle.attrs["hx-trigger"])
    # The part where the user types is never replaced by a refresh (app.js, data-keep-edits).
    assert "data-keep-edits" in idle.attrs
    # The forms inside answer into the whole step, so a save is never held back by the guard.
    shown = cv.step3(app.id).text
    for form in (e for e in elements(shown) if e.tag == "form" and e.within(step(shown))):
        if "data-line-form" in form.attrs or form.attrs.get("id") == "cv-add-form":
            assert form.attrs["hx-target"] == "#tailoring" and form.attrs["hx-swap"] == "outerHTML"
            assert "hx-disabled-elt" in form.attrs
    # While the step's own task runs, the editors are not drawn at all (nothing to lose).
    cv.r.run.hold.hold = True
    cv.service.start_generate(app.id, note="longer")
    cv.r.run.wait_entered(2)
    running = cv.step3(app.id).text
    assert not [e for e in elements(running) if e.tag == "textarea" and e.within(step(running))]
    cv.r.run.hold.open()
    cv.ended(TAILOR_CV, app.id)


def test_cv_step_failed_generation_shows_the_plain_error(cv: CvStep):
    app = cv.cv_ready()
    row = cv.generate(app.id, "malformed", status="failed")

    for shown in (cv.step3(app.id).text, cv.page3(app.id).text):
        alert = by_id(shown, "cv-error")
        assert alert.attrs["role"] == "alert" and alert.text == message_for_kind(row.error)
        assert state(shown) == "failed" and "Needs attention" in stepper(shown)[3].text
        assert has_id(shown, "cv-generate-start")  # try again
    assert mark_text and CONFIRM_BUTTON and UNCITED  # names the failure tests share
