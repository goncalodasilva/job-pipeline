"""M6.7 service half: generating a tailored CV through ``DocumentService`` and the real task runner
(AC-M6.1, AC-M6.2; spec 3.1, 3.6; ADR-106 items 5, 7, 8, 11, 12; ADR-110 item 8).

Assumed interface: see the docstring of ``tests/document_pages.py`` (``app.state.documents`` is a
``DocumentService``: ``plan_start``, ``start_generate``, ``view``, ``approve``; the task type
``tailor_cv`` with the dedupe key ``tailor_cv:<application id>`` is run by
``app/tasks/documents.py::tailor_cv_handler`` and writes with ``ctx.complete``). The fixtures are
those of ``tests/fixtures/ai/tailor_cv`` (their fact ids are F1 to F8 as ``seed_cv_facts`` makes
them). Real SQLite in a temp folder, the fake provider behind the spy, no network, no fixed waits.
The golden prompt test already exists at the AI layer and is not repeated here.
"""

import json

import pytest

from app.domain.errors import ApplicationNotFound, InvalidModelOutput, message_for_kind
from app.domain.fact_selection import GENERALIZE_MARK
from app.modules.ai.providers.fake import FAKE_THOROUGH_MODEL
from app.services.tailoring import NO_FACTS, NO_REQUIREMENTS, NO_SUMMARY
from tests.document_pages import (
    CV_TABLES,
    LANGUAGE_UNIT,
    ROLE_A,
    ROLE_B,
    SUMMARY_UNIT,
    TAILOR_CV,
    Documents,
    documents_app,
    injection_text,
    template_json,
)
from tests.tailoring_ai_inputs import (
    DETAILS,
    FACT_1,
    FACT_2,
    FACT_3,
    FACT_7,
    FACT_8,
    HIDDEN,
    OTHER_ROLE,
    ROLE,
    TEMPLATE,
    personal_values,
)
from tests.tailoring_pages import ARCHIVED_TEXT, DRAFT_TEXT, seed_summary

pytestmark = pytest.mark.integration


@pytest.fixture
def d(tmp_path):
    with documents_app(tmp_path) as desk:
        yield desk


INVENTED_ROLE = "Head of Planning at Globex Corporation"
GLOBEX = "Led route planning at Globex Corporation."
INFLATED = "Cut invoice processing time by 85% across 14 warehouses."
UNKNOWN_ID_UNIT = "Managed a regional planning office."
UNCITED = "A seasoned leader known for turning around struggling teams."
STRUCTURE = "Eight years of leadership at Initech"
VANS = "Scheduled 12 vans for the night shift."
AUDIT = "Audited the accounts of Lakeshore Bank over 2 years."
OK_TEXTS = [SUMMARY_UNIT, FACT_1, FACT_2, FACT_7, LANGUAGE_UNIT]

# fixture -> (kind, claim, text of the unit that carries the flag), in unit order.
ADVERSARIAL = {
    "invented_employer": [
        ("needs_check", "Globex Corporation", GLOBEX),
        ("unsupported", INVENTED_ROLE, FACT_1),  # the invented role header is dropped and flagged
    ],
    "unknown_id": [("unsupported", "F999@1", UNKNOWN_ID_UNIT)],
    "inflated_number": [
        ("needs_check", "85%", INFLATED),
        ("needs_check", "14", INFLATED),
    ],
    "uncited_paragraph": [("unsupported", UNCITED, UNCITED)],
    "structure_claim": [("unsupported", STRUCTURE, STRUCTURE)],
    "wrong_role": [("needs_check", "F7@1", VANS)],
    "generalized_name": [("needs_check", "Lakeshore Bank", AUDIT)],
}


# --- AC-M6.1, AC-M6.2: what is stored and flagged ---------------------------------------------


@pytest.mark.parametrize("fixture", list(ADVERSARIAL))
def test_ac_m6_1_adversarial_cv_flagged(d: Documents, fixture: str):
    app = d.cv_ready()

    task = d.generate(app.id, f"adversarial_{fixture}")

    (version,) = d.versions(app.id)
    assert version["status"] == "draft" and version["number"] == 1
    assert version["parent_version_id"] is None and version["origin"] == "ai"
    assert version["task_id"] == task.id and version["provider"] == "fake"
    assert version["model"] == FAKE_THOROUGH_MODEL and version["prompt_id"] == TAILOR_CV
    # Each flag is stored as kind and claim on the unit it belongs to.
    assert d.flag_triples(version["id"]) == ADVERSARIAL[fixture]
    for row in d.units(version["id"]):
        assert all(set(flag) >= {"kind", "claim"} for flag in row["flag_list"])
    # The page-facing view carries the same flags and blocks approval; nothing is approved.
    view = d.service.view(app.id)
    shown = [(flag.kind, flag.claim) for unit in view.units for flag in unit.flags]
    assert shown == [(kind, claim) for kind, claim, _ in ADVERSARIAL[fixture]]
    assert {(item.kind, item.claim) for item in view.blockers} >= set(shown)
    with pytest.raises(d.docs.ApprovalBlocked):
        d.service.approve(app.id, version["id"])
    assert d.version_row(version["id"])["status"] == "draft"


def test_ac_m6_1_adversarial_units_keep_their_shape(d: Documents):
    app = d.cv_ready()
    d.generate(app.id, "adversarial_structure_claim")
    version_id = d.current_version_id(app.id)

    rows = d.units(version_id)
    # The model's own "Work history" heading is dropped (headings come from code); the other
    # structure line stays as a structure line that cites nothing, so the user can remove it.
    assert [row["kind"] for row in rows] == ["content", "structure"]
    assert "Work history" not in d.texts(version_id, removed=True)
    assert rows[1]["cited"] == [] and rows[1]["origin"] == "ai"

    d.generate(app.id, "adversarial_unknown_id")
    rows = d.units(d.current_version_id(app.id))
    flagged = [row for row in rows if row["flag_list"]]
    assert [row["text"] for row in flagged] == [UNKNOWN_ID_UNIT]
    assert flagged[0]["cited"] == []  # F999@1 names no stored fact version: no citation row

    d.generate(app.id, "adversarial_wrong_role")
    rows = d.units(d.current_version_id(app.id))
    (vans,) = [row for row in rows if row["text"] == VANS]
    assert vans["cited"] == ["F7@1"] and vans["role_title"] == ROLE_A[0]


def test_ac_m6_2_inflated_number_needs_check_and_blocks(d: Documents):
    app = d.cv_ready()
    d.generate(app.id, "adversarial_inflated_number")
    version_id = d.current_version_id(app.id)

    view = d.service.view(app.id)
    flagged = [unit for unit in view.units if unit.flags]
    assert [unit.text for unit in flagged] == [INFLATED]
    assert {flag.kind for flag in flagged[0].flags} == {"needs_check"}  # never "unsupported"
    assert [flag.claim for flag in flagged[0].flags] == ["85%", "14"]
    assert {item.kind for item in view.blockers} == {"needs_check"}
    with pytest.raises(d.docs.ApprovalBlocked) as blocked:
        d.service.approve(app.id, version_id)
    assert blocked.value.message == "1 line needs your check."
    assert d.version_row(version_id)["status"] == "draft"


def test_ok_cv_has_five_unflagged_units_and_can_be_approved(d: Documents):
    app = d.cv_ready()
    d.generate(app.id, "ok")
    version_id = d.current_version_id(app.id)

    rows = d.units(version_id)
    assert [row["text"] for row in rows] == OK_TEXTS
    assert [row["position"] for row in rows] == [0, 1, 2, 3, 4]
    assert [row["section"] for row in rows] == [
        "summary",
        "experience",
        "experience",
        "experience",
        "languages",
    ]
    assert [row["cited"] for row in rows] == [
        ["F1@1", "F2@1"],
        ["F1@1"],
        ["F2@1"],
        ["F7@1"],
        ["F3@1"],
    ]
    assert [row["role_title"] for row in rows] == [None, ROLE_A[0], ROLE_A[0], ROLE_B[0], None]
    assert {row["origin"] for row in rows} == {"ai"} and {row["removed"] for row in rows} == {0}
    assert all(row["flag_list"] == [] for row in rows)
    assert d.service.view(app.id).blockers == ()

    d.service.approve(app.id, version_id)
    assert d.version_row(version_id)["status"] == "approved"


# --- the request: facts for AI only, nothing personal -----------------------------------------


def test_tailor_cv_request_has_facts_for_ai_only_and_no_personal_details(d: Documents):
    app = d.cv_ready()
    d.save_details()
    d.generate(app.id, "ok")

    (request,) = d.requests(TAILOR_CV)
    assert request.model == FAKE_THOROUGH_MODEL and not request.allow_web_search
    for text in (FACT_1, FACT_2, FACT_3, FACT_7, FACT_8):
        assert text in request.user
    assert GENERALIZE_MARK in request.user  # F8 asks for its names to be withheld
    for withheld in (HIDDEN, DRAFT_TEXT, ARCHIVED_TEXT):
        assert withheld not in repr(request)
    for number in (4, 5, 6):
        assert f"F{number}@" not in repr(request)
    for value in personal_values():
        assert value not in repr(request), value
    # The sent set is what the request held, F1 F2 F3 F7 F8 at version 1.
    assert d.sent_pairs(d.current_version_id(app.id)) == {(1, 1), (2, 1), (3, 1), (7, 1), (8, 1)}
    # Nothing personal reached storage either; the saved details are only read by the view.
    stored = repr(d.cv_tables())
    for value in personal_values():
        assert value not in stored, value
    assert HIDDEN not in stored and DRAFT_TEXT not in stored


def test_tailor_cv_injection_posting_claim_flagged(d: Documents):
    posting = injection_text("injection_posting.txt")
    app = d.cv_ready(posting=posting)
    facts_before = d.table_counts()["fact_versions"]

    d.generate(app.id, "injection_posting")

    (request,) = d.requests(TAILOR_CV)
    # The posting is data inside its block; it is never in the system text.
    assert "Ignore all previous instructions" in request.user
    assert "Ignore all previous instructions" not in request.system
    assert "<system>" not in request.user
    version_id = d.current_version_id(app.id)
    triples = d.flag_triples(version_id)
    assert any("Globex Corporation" in claim for _, claim, _ in triples)
    assert {kind for kind, _, _ in triples} == {"needs_check"}
    # An obeyed instruction approves nothing and writes no fact.
    assert d.version_row(version_id)["status"] == "draft"
    assert d.table_counts()["fact_versions"] == facts_before
    with pytest.raises(d.docs.ApprovalBlocked):
        d.service.approve(app.id, version_id)


def test_cv_generation_uses_the_chosen_angle_and_the_note(d: Documents):
    app = d.cv_ready()
    tailoring = d.r.run.app.state.tailoring
    tailoring.save_angle(
        app.id, name="Steady hands", description="Lead with reliability.", version=0
    )

    d.generate(app.id, "ok", note="  Keep it plain.  ")

    (request,) = d.requests(TAILOR_CV)
    assert "Steady hands" in request.user and "Keep it plain." in request.user
    (version,) = d.versions(app.id)
    assert (
        "Steady hands" in version["angle_text"]
        and "Lead with reliability." in version["angle_text"]
    )
    assert version["note"] == "Keep it plain."


# --- refusals: nothing written ----------------------------------------------------------------


def test_cv_requires_approved_template(d: Documents):
    app = d.cv_ready(template=False)  # the country is PT, the finished setup's preference

    def refused() -> object:
        with pytest.raises(d.docs.GenerateNotReady) as error:
            d.service.start_generate(app.id)
        return error.value

    first = refused()
    assert first.reason == "template" and "template" in first.message.lower()
    for jargon in ("Traceback", "GenerateNotReady", "None", "cv_templates"):
        assert jargon not in first.message
    with pytest.raises(d.docs.GenerateNotReady):
        d.service.plan_start(app.id)
    # A draft is not an approved template.
    d.put_template("PT", approved=False)
    assert refused().reason == "template"
    # Another country's approved template does not count either.
    d.r.run.execute("DELETE FROM cv_templates WHERE country='PT'")
    d.put_template("DE", **{"max_pages": 1})
    assert refused().reason == "template"
    assert d.rows("SELECT * FROM tasks WHERE type='tailor_cv'") == []
    assert d.requests(TAILOR_CV) == [] and d.table_counts()["documents"] == 0

    # The workspace country wins over the preference: DE has an approved template.
    d.r.run.app.state.tailoring.save_country(app.id, cv_country="DE", version=0)
    d.generate(app.id, "ok")
    (version,) = d.versions(app.id)
    assert json.loads(version["template_snapshot"])["max_pages"] == 1


@pytest.mark.parametrize(
    ("case", "reason", "message"),
    [
        ("summary", "summary", NO_SUMMARY),
        ("requirements", "requirements", NO_REQUIREMENTS),
        ("facts", "facts", NO_FACTS),
    ],
)
def test_cv_start_not_ready_reasons_are_plain(d: Documents, case: str, reason: str, message: str):
    if case == "facts":
        app = d.cv_ready(seed_facts=False)
    else:
        app = d.cv_ready(summary=False)
        if case == "requirements":
            seed_summary(d.db, "cv-job", ())

    for start in (d.service.plan_start, d.service.start_generate):
        with pytest.raises(d.docs.GenerateNotReady) as error:
            start(app.id)
        assert error.value.reason == reason and error.value.message == message
    assert d.rows("SELECT * FROM tasks WHERE type='tailor_cv'") == [] and d.spy.requests == []


def test_tailor_cv_start_goes_through_the_budget_gate_and_is_metered(d: Documents):
    from app.domain.budgets import Budget
    from app.services.budget_gate import BlockedAction, Start

    app = d.cv_ready()
    usage, gate = d.r.run.app.state.usage, d.r.run.app.state.budget_gate

    plan = d.service.plan_start(app.id)
    assert plan.tier == "thorough" and d.service.plan_start(app.id, tier="fast").tier == "fast"
    assert isinstance(gate.check(plan.service, tier=plan.tier, confirmation=None), Start)
    d.generate(app.id, "ok")
    tiers = d.rows("SELECT DISTINCT tier, task_type FROM usage_records WHERE task_type='tailor_cv'")
    assert [(row["tier"], row["task_type"]) for row in tiers] == [("thorough", "tailor_cv")]

    # At the blocking point the gate refuses and, if a start comes anyway, the runner's reserve
    # refuses the request: the task fails and writes nothing.
    budget = Budget(reset_day=1, allowance=10, first_warning=3, second_warning=5, block_at=8)
    usage.save_budget("fake", budget, expected_version=usage.load_budgets().version)
    (count,) = d.rows("SELECT count(*) AS n FROM usage_records")
    for _ in range(8 - count["n"]):
        record = usage.reserve("fake", tier="thorough", task_type="tailor_cv")
        usage.complete(record, success=True, tokens=None)
    assert isinstance(gate.check(plan.service, tier=plan.tier, confirmation=None), BlockedAction)
    counts, requests = d.table_counts(), len(d.spy.requests)
    ref = d.service.start_generate(app.id)
    row = d.r.run.wait_terminal(ref.id)
    assert str(row.status.value) == "failed" and row.error == "BudgetBlocked"
    assert d.table_counts() == counts and len(d.spy.requests) == requests


def test_tailor_cv_dedupe_key_is_per_application(d: Documents):
    app = d.cv_ready()
    second = d.cv_ready("second-cv-job")
    d.r.run.hold.hold = True

    first = d.service.start_generate(app.id)
    again = d.service.start_generate(app.id)
    other = d.service.start_generate(second.id)

    assert again.id == first.id and again.created is False and other.id != first.id
    keys = {
        row["dedupe_key"] for row in d.rows("SELECT dedupe_key FROM tasks WHERE type='tailor_cv'")
    }
    assert keys == {f"tailor_cv:{app.id}", f"tailor_cv:{second.id}"}
    d.r.run.hold.open()
    d.r.run.wait_terminal(first.id)
    d.r.run.wait_terminal(other.id)


# --- invalid model output: nothing written ----------------------------------------------------


def _nothing_written(d: Documents, aid: str, task: object) -> None:
    assert task.error == InvalidModelOutput.kind  # type: ignore[attr-defined]
    assert len(d.requests(TAILOR_CV)) == 2  # the one retry
    assert d.cv_tables() == {table: [] for table in CV_TABLES}
    view = d.service.view(aid)
    assert view.version is None and view.units == () and view.versions == ()
    assert view.task.attention and view.task.status == "failed"
    assert message_for_kind(view.task.error)


def test_tailor_cv_schema_refuses_status_field(d: Documents):
    app = d.cv_ready()

    task = d.generate(app.id, "schema_invalid", status="failed")  # asks to set "status"

    _nothing_written(d, app.id, task)


def test_tailor_cv_malformed_output_retry_then_invalid(d: Documents):
    app = d.cv_ready()

    task = d.generate(app.id, "malformed", status="failed")

    _nothing_written(d, app.id, task)


def test_cv_result_written_with_task_success_or_not_at_all(d: Documents):
    app = d.cv_ready()
    d.script(TAILOR_CV, "ok")
    d.r.run.execute(
        "CREATE TRIGGER fictional_abort BEFORE UPDATE OF status ON tasks "
        "WHEN NEW.type='tailor_cv' AND NEW.status='succeeded' "
        "BEGIN SELECT RAISE(ABORT,'fictional failure'); END"
    )
    ref = d.service.start_generate(app.id)
    assert d.r.run.wait_terminal(ref.id).status.value == "failed"
    assert d.cv_tables() == {table: [] for table in CV_TABLES}  # units, citations, sent facts too
    d.r.run.execute("DROP TRIGGER fictional_abort")

    d.r.run.hold.hold = True
    ref = d.service.start_generate(app.id)
    d.r.run.wait_entered(2)
    d.r.run.app.state.task_service.request_cancel(ref.id)
    d.r.run.hold.open()
    assert d.r.run.wait_terminal(ref.id).status.value == "cancelled"
    assert d.cv_tables() == {table: [] for table in CV_TABLES}

    ref = d.service.start_generate(app.id)
    assert d.r.run.wait_terminal(ref.id).status.value == "succeeded"
    (version,) = d.versions(app.id)
    assert version["task_id"] == ref.id
    assert len(d.units(version["id"])) == 5 and d.sent_pairs(version["id"])
    document = d.document(app.id)
    assert document is not None and document["current_version_id"] == version["id"]
    assert [
        row["dedupe_key"] for row in d.rows("SELECT dedupe_key FROM tasks WHERE id=?", (ref.id,))
    ] == [f"tailor_cv:{app.id}"]


# --- the fake provider ------------------------------------------------------------------------


def test_fake_cv_labelled_practice_data(d: Documents):
    app = d.cv_ready()

    d.generate(app.id, None)  # the app's own fake answers, as in the demo

    view = d.service.view(app.id)
    assert view.practice_notice and "Practice data" in view.practice_notice
    assert view.version.provider == "fake"
    assert len(view.units) == 2
    assert all(unit.text.startswith("Practice data. ") for unit in view.units)
    assert {unit.text.removeprefix("Practice data. ") for unit in view.units} == {FACT_1, FACT_2}
    assert view.blockers == () and all(not unit.flags for unit in view.units)  # it cites the facts


# --- headings, role headers and the contact block come from code ------------------------------


def _marks(view: object) -> dict[str, str]:
    return {line.text: str(line.mark) for line in view.diff}  # type: ignore[attr-defined]


def test_cv_view_headings_role_headers_and_contact_come_from_code(d: Documents):
    app = d.cv_ready()
    d.save_details()
    d.generate(app.id, "ok")
    version_id = d.current_version_id(app.id)

    # Nothing the model could have said is a heading or a contact line: stored units are content.
    assert {row["kind"] for row in d.units(version_id)} == {"content"}
    stored = repr(d.units(version_id))
    for value in personal_values():
        assert value not in stored
    for heading in ("Summary", "Work history", "Education"):
        assert heading not in d.texts(version_id, removed=True)

    view = d.service.view(app.id)
    assert view.template == TEMPLATE
    assert [(s.id, s.heading) for s in view.sections] == [
        ("summary", "Summary"),
        ("experience", "Work history"),  # the template's own heading
        ("languages", "Languages"),
    ]
    summary, experience, languages = view.sections
    assert [block.header for block in summary.blocks] == [None]
    assert [block.header for block in languages.blocks] == [None]
    headers = [block.header for block in experience.blocks]
    assert [(h.title, h.employer, h.start_text, h.end_text, h.current) for h in headers] == [
        (ROLE_A[0], ROLE_A[1], "Mar 2019", "Jun 2022", False),  # most recent role first
        (ROLE_B[0], ROLE_B[1], "Jan 2017", "Feb 2019", False),
    ]
    assert headers[0].role_id == d.role_id(ROLE_A[0])
    assert [[u.text for u in block.units] for block in experience.blocks] == [
        [FACT_1, FACT_2],
        [FACT_7],
    ]

    # One unit as the review screen reads it.
    unit = experience.blocks[0].units[0]
    assert (unit.position, unit.section, unit.role, unit.kind, unit.origin) == (
        1,
        "experience",
        ROLE,
        "content",
        "ai",
    )
    assert unit.flags == () and unit.fact_ids == ("F1@1",) and unit.added_details == ()
    assert [w.text for w in unit.cited] == [FACT_1]
    assert experience.blocks[1].units[0].role == OTHER_ROLE

    # The contact block is read from the saved details at view time.
    assert view.contact.name == DETAILS.name
    lines = " ".join(view.contact.lines)
    for value in (DETAILS.email, DETAILS.phone, DETAILS.location, *DETAILS.links):
        assert value in lines

    # Version information for the screen.
    assert (view.version.number, view.version.status, view.version.parent_version_id) == (
        1,
        "draft",
        None,
    )
    assert [item.id for item in view.versions] == [version_id]

    # The master-CV diff, by fact number over the current facts.
    marks = _marks(view)
    assert marks[FACT_1] == marks[FACT_2] == marks[FACT_7] == "unchanged"
    assert marks[SUMMARY_UNIT] == "added"  # it cites several facts
    assert marks[LANGUAGE_UNIT] == "reworded"
    assert marks[FACT_8] == "removed"  # a usable fact this CV leaves out

    # The date format is the version's snapshot: a later template edit changes new versions only.
    d.r.run.execute(
        "UPDATE cv_templates SET spec=? WHERE country='PT'", (template_json(date_format="iso"),)
    )
    later = d.service.view(app.id).sections[1].blocks[0].header
    assert (later.start_text, later.end_text) == ("Mar 2019", "Jun 2022")
    d.generate(app.id, "ok")
    iso = d.service.view(app.id).sections[1].blocks[0].header
    assert (iso.start_text, iso.end_text) == ("2019-03", "2022-06")


def test_cv_view_without_a_version_is_empty_and_reads_only(d: Documents):
    app = d.cv_ready()
    before = d.cv_tables(), len(d.spy.requests)

    view = d.service.view(app.id)

    assert view.version is None and view.units == () and view.sections == ()
    assert view.blockers == () and view.versions == () and view.document_id is None
    assert view.contact.name == "" and view.contact.lines == ()
    assert (d.cv_tables(), len(d.spy.requests)) == before
    assert d.rows("SELECT * FROM tasks WHERE type='tailor_cv'") == []
    with pytest.raises(ApplicationNotFound):
        d.service.view("no-such-application")


# --- review fixes: provenance label, input checks, one request at a time ----------------------


def test_fake_cv_practice_label_survives_every_later_version(d: Documents):
    app = d.cv_ready()
    d.generate(app.id, None)  # the app's own fake answers
    v1 = d.current_version_id(app.id)
    service = d.service

    def labelled() -> None:
        view = service.view(app.id)
        assert view.practice_notice and "Practice data" in view.practice_notice

    labelled()
    v2 = service.edit_unit(app.id, v1, d.units(v1)[0]["id"], "Plans routes.").version_id
    labelled()
    added = service.add_unit(app.id, v2, "summary", "Plans routes weekly.", fact_ids=("F1@1",))
    labelled()
    v4 = service.remove_unit(app.id, added.version_id, added.unit_id)
    labelled()
    bare = service.add_unit(app.id, v4, "summary", "Keeps the depot tidy.")
    confirmed = service.resolve_flag_confirm(app.id, bare.version_id, bare.unit_id)
    labelled()
    assert d.service.view(app.id).version.id == confirmed.version_id
    assert service.view(app.id, v1).practice_notice  # the generated version itself, as before


def test_tailor_cv_tier_must_be_fast_or_thorough(d: Documents):
    app = d.cv_ready()
    for start in (d.service.plan_start, d.service.start_generate):
        with pytest.raises(ValueError, match=r"Choose the fast or thorough tier\.") as error:
            start(app.id, tier="turbo")
        assert not isinstance(error.value, d.docs.GenerateNotReady)
    assert d.rows("SELECT * FROM tasks WHERE type='tailor_cv'") == []
    assert d.service.plan_start(app.id, tier="fast").tier == "fast"


def test_tailor_cv_note_refuses_control_and_bidi_characters(d: Documents):
    app = d.cv_ready()
    for note in ("Keep it plain.\u202e", "Keep\x07 it plain.", "Keep it\u2028plain.", "a\u200bb"):
        with pytest.raises(ValueError, match="Use plain text in the note") as error:
            d.service.start_generate(app.id, note=note)
        assert note not in str(error.value)
    assert d.rows("SELECT * FROM tasks WHERE type='tailor_cv'") == []

    # A browser posts CRLF and NUL may sneak in: both are cleaned, line breaks stay.
    d.generate(app.id, "ok", note="Line one.\r\nLine two.\x00")
    (version,) = d.versions(app.id)
    assert version["note"] == "Line one.\nLine two."


def test_tailor_cv_start_refuses_a_different_request_while_one_is_active(d: Documents):
    app = d.cv_ready()
    d.r.run.hold.hold = True
    first = d.service.start_generate(app.id, note="Keep it plain.")

    same = d.service.start_generate(app.id, note="  Keep it plain. ")
    assert same.id == first.id and same.created is False
    different = (
        {"note": "Something else."},
        {},
        {"note": "Keep it plain.", "replace_all": True, "confirm_replace": True},
    )
    for options in different:
        with pytest.raises(d.docs.GenerateInProgress) as error:
            d.service.start_generate(app.id, **options)
        assert error.value.message == "A CV is already being made. Wait for it to finish."
    assert len(d.rows("SELECT * FROM tasks WHERE type='tailor_cv'")) == 1

    d.r.run.hold.open()
    d.r.run.wait_terminal(first.id)
    d.generate(app.id, "ok", note="Something else.")  # once finished, a new request is welcome


def test_kept_lines_are_withheld_for_withdrawn_citations_and_capped():
    from dataclasses import replace

    from app.domain.documents import DocumentUnit, UnitOrigin
    from app.domain.fact_selection import facts_for_ai
    from app.services.tailoring_ai import MAX_KEPT_UNITS, _kept_for_ai
    from tests.tailoring_ai_inputs import CV_RECORDS

    assert MAX_KEPT_UNITS == 80
    records = [replace(r, version=2) if r.number == 2 else r for r in CV_RECORDS]
    facts = facts_for_ai(records)

    def unit(uid: str, *ids: str, text: str | None = None) -> DocumentUnit:
        return DocumentUnit(uid, "content", "summary", None, text or uid, UnitOrigin.USER, ids)

    sent, withheld = _kept_for_ai(
        [
            unit("plain"),
            unit("good", "F1@1"),
            unit("generalized", "F8@1"),
            unit("both", "F1@1", "F8@1"),
            unit("superseded", "F2@1"),
            unit("current", "F2@2"),
            unit("never-use", "F4@1"),
            unit("missing", "F99@1"),
        ],
        facts,
    )
    assert [item["text"] for item in sent] == ["plain", "good", "current"]
    assert withheld == ("generalized", "both", "superseded", "never-use", "missing")

    many = [unit(f"u{i:03d}", "F1@1") for i in range(85)]
    sent, withheld = _kept_for_ai(many, facts)
    assert len(sent) == MAX_KEPT_UNITS
    assert [item["text"] for item in sent] == [f"u{i:03d}" for i in range(5, 85)]
    assert withheld == tuple(f"u{i:03d}" for i in range(5))  # newest kept, the rest stay in the CV
