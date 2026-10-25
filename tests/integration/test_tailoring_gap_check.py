"""M6.5 step 1 of the workspace, the gap check, through the real task runner, the real prompt and
the fake provider (AC-M6.8; spec 3.1 item 5, 3.6; ADR-106 items 8 and 11).

Assumed interface: see ``tests/tailoring_pages.py`` (module, task, route and markup names). In
short: ``POST /applications/{id}/tailoring/1/check`` starts the ``gap_check`` task (dedupe
``gap_check:<application id>``); the task writes ``gap_checks`` and ``gap_check_facts`` with
``ctx.complete``; ``GET /applications/{id}?tab=tailoring&step=1`` and
``GET /applications/{id}/tailoring/1`` show ``li[data-requirement-id][data-verdict]``,
``#gap-check-message``, ``li[data-framing]``, "Add a fact" per gap and ``#gap-check-start``;
``POST .../1/facts`` (``text``) adds a confirmed fact.

The fixtures are those of ``tests/fixtures/ai/gap_check``; their requirement ids (req-1 to
req-4) and fact references (F1@1 to F3@1) are the ones ``tests/tailoring_ai_inputs.py`` seeds.
Nothing is real; no network.
"""

import json

import pytest

from app.domain.errors import InvalidModelOutput, message_for_kind
from app.modules.ai.providers.fake import FAKE_THOROUGH_MODEL, ScriptedResponse
from tests.board_pages import elements
from tests.integration.test_board import snapshot
from tests.tailoring_ai_inputs import (
    FACT_1,
    FACT_2,
    FACT_3,
    HIDDEN,
    REQUIREMENTS,
    Requirement,
)
from tests.tailoring_pages import (
    ARCHIVED_TEXT,
    DRAFT_TEXT,
    GAP_CHECK,
    OLD_STAMP,
    SUMMARY_TEXT,
    Tailoring,
    announced,
    by_id,
    framing_items,
    has_id,
    inside,
    requirement_items,
    said_text,
    seed_summary,
    stepper,
    tailoring_app,
    verdicts,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def tw(tmp_path):
    with tailoring_app(tmp_path) as desk:
        yield desk


NOT_IN_FACT_BANK = "This isn't in your fact bank."
MESSAGE = (
    "Your confirmed facts don't show 2 of this role's must-haves: "
    "Five years of route planning; Experience with Terraform. It may not be worth applying for."
)


def stored_result(t: Tailoring) -> dict:
    (row,) = t.rows("SELECT * FROM gap_checks")
    return json.loads(row["result"])


def test_ac_m6_8_missing_must_haves_says_may_not_be_worth_applying(tw: Tailoring):
    app = tw.ready()
    tw.check(app.id, "must_haves_missing")

    for shown in (tw.page(app.id, 1), tw.fragment(app.id, 1)):
        assert shown.status_code == 200
        assert by_id(shown.text, "gap-check-message").text == MESSAGE
        assert verdicts(shown.text) == {
            "req-1": "missing",
            "req-2": "missing",
            "req-3": "met",
            "req-4": "missing",
        }
        # The verdict is a word, and a met one shows the user's own fact wording (TD-241).
        items = requirement_items(shown.text)
        assert "Missing" in items["req-1"].text and "Met" in items["req-3"].text
        assert FACT_3 in items["req-3"].text
    assert stored_result(tw)["message"] == MESSAGE
    assert stored_result(tw)["missing_must_haves"] == ["req-1", "req-2"]


def test_ac_m6_8_no_missing_must_have_no_warning_sentence(tw: Tailoring):
    app = tw.ready()
    everything = {
        "requirements": [
            {"requirement_id": "req-1", "verdict": "met", "fact_ids": ["F1@1"]},
            {"requirement_id": "req-2", "verdict": "partial", "fact_ids": ["F2@1"]},
            {"requirement_id": "req-3", "verdict": "met", "fact_ids": ["F3@1"]},
            {"requirement_id": "req-4", "verdict": "missing", "fact_ids": []},
        ],
        "framing_ideas": [],
    }
    tw.spy.script(GAP_CHECK, ScriptedResponse(data=everything))
    tw.check(app.id, None)

    shown = tw.page(app.id, 1).text
    assert not has_id(shown, "gap-check-message")
    assert "may not be worth applying" not in shown
    assert stored_result(tw)["message"] is None
    # A not-needed nice-to-have is still a gap with its "Add a fact"; a met requirement is not.
    summaries = {
        rid: [e.text for e in inside(shown, item, lambda e: e.tag == "summary")]
        for rid, item in requirement_items(shown).items()
    }
    assert summaries["req-4"] == ["Add a fact"] and summaries["req-2"] == ["Add a fact"]
    assert summaries["req-1"] == [] and summaries["req-3"] == []


@pytest.mark.parametrize("fixture", ["adversarial_unsent_citation", "adversarial_uncited"])
def test_gap_check_counts_unsent_citation_as_missing(tw: Tailoring, fixture: str):
    app = tw.ready()
    tw.check(app.id, fixture)

    shown = tw.page(app.id, 1).text
    found = verdicts(shown)
    assert found["req-1"] == "missing" and found["req-2"] == "missing"  # F999 / F4 / no id
    assert by_id(shown, "gap-check-message").text.startswith(
        "Your confirmed facts don't show 2 of this role's must-haves"
    )
    assert "F999" not in shown and HIDDEN not in shown
    requirements = {item["requirement_id"]: item for item in stored_result(tw)["requirements"]}
    assert requirements["req-1"]["fact_ids"] == [] and requirements["req-1"]["downgraded"] is True


def test_gap_check_framing_claim_flagged(tw: Tailoring):
    app = tw.ready()
    facts_before = tw.rows("SELECT * FROM fact_versions ORDER BY id")
    tw.check(app.id, "adversarial_framing_invented")

    shown = tw.page(app.id, 1).text
    faithful, invented, unsent = framing_items(shown)
    assert NOT_IN_FACT_BANK not in faithful.text and FACT_1 in faithful.text
    assert NOT_IN_FACT_BANK in invented.text and "Globex Corporation" in invented.text
    assert NOT_IN_FACT_BANK in unsent.text
    # Flagged ideas are shown, never hidden or applied: the fact bank is untouched.
    assert tw.rows("SELECT * FROM fact_versions ORDER BY id") == facts_before
    assert "Worked on routing." in unsent.text


def test_gap_check_uses_current_summary_and_facts_for_ai_only(tw: Tailoring):
    app = tw.ready(summary=False)
    # An older summary with other requirements, then the current one.
    old = (Requirement("old-1", "must_have", "Old requirement text fictional"),)
    seed_summary(tw.db, "tailoring-job", old, summary_id="summary-old",
                 summary="Old summary text.", created_at=OLD_STAMP)  # fmt: skip
    seed_summary(tw.db, "tailoring-job")
    archived = tw.add_fact(ARCHIVED_TEXT)
    tw.r.facts.archive(archived.id, expected_version=archived.version)

    tw.check(app.id, "ok")

    (request,) = tw.requests(GAP_CHECK)
    everything = repr(request)
    assert request.model == FAKE_THOROUGH_MODEL and not request.allow_web_search
    assert request.user.count("<untrusted_document") == 2
    for item in REQUIREMENTS:
        assert item.id in request.user and item.text in request.user
    for text in (FACT_1, FACT_2, FACT_3):
        assert text in request.user
    for absent in ("Old requirement text", "old-1", HIDDEN, DRAFT_TEXT, ARCHIVED_TEXT):
        assert absent not in everything
    assert "F4@" not in everything and "F5@" not in everything and "F6@" not in everything
    (row,) = tw.rows("SELECT * FROM gap_checks")
    assert row["summary"] == SUMMARY_TEXT and row["application_id"] == app.id
    assert (row["prompt_id"], row["prompt_version"], row["provider"]) == ("gap_check", "2", "fake")
    sent = tw.rows(
        "SELECT f.number FROM gap_check_facts g JOIN facts f ON f.id=g.fact_id "
        "WHERE g.gap_check_id=? ORDER BY f.number",
        (row["id"],),
    )
    assert [item["number"] for item in sent] == [1, 2, 3]
    assert len(tw.rows("SELECT * FROM usage_records WHERE task_type='gap_check'")) == 1


def test_gap_check_injection_posting_changes_nothing_through_the_workspace(tw: Tailoring):
    from tests.tailoring_ai_inputs import ROOT

    posting = (ROOT / "tests/fixtures/ai/gap_check/injection_posting.txt").read_text("utf-8")
    app = tw.ready(posting=posting)
    before = snapshot(tw.r)
    tw.check(app.id, "injection_obeyed")

    (request,) = tw.requests(GAP_CHECK)
    assert "Ignore all previous instructions" in request.user
    assert "Ignore all previous instructions" not in request.system
    assert request.user.count("<untrusted_document") == 2
    shown = tw.page(app.id, 1).text
    # The obeyed "met" verdicts cite nothing, so none holds; the invented idea is flagged.
    assert set(verdicts(shown).values()) == {"missing"}
    (idea,) = framing_items(shown)
    assert NOT_IN_FACT_BANK in idea.text
    after = snapshot(tw.r)
    for table in ("applications", "jobs", "facts", "fact_versions", "status_history"):
        assert after[table] == before[table], table


@pytest.mark.parametrize("fixture", ["malformed", "schema_invalid"])
def test_gap_check_malformed_output_retry_then_invalid(tw: Tailoring, fixture: str):
    app = tw.ready()

    task = tw.check(app.id, fixture, status="failed")

    assert task.error == InvalidModelOutput.kind
    assert len(tw.requests(GAP_CHECK)) == 2  # the one retry
    assert tw.rows("SELECT * FROM gap_checks") == []
    assert tw.rows("SELECT * FROM gap_check_facts") == []
    shown = tw.page(app.id, 1).text
    alerts = [e for e in elements(shown) if e.attrs.get("role") == "alert"]
    assert [alert.text for alert in alerts] == [message_for_kind(task.error)]
    assert by_id(shown, "gap-check-start").text == "Check gaps"
    assert "Needs attention" in stepper(shown)[1].text
    assert verdicts(shown) == {}


def test_add_fact_from_gap_creates_confirmed_fact(tw: Tailoring):
    app = tw.ready()
    tw.check(app.id, "must_haves_missing")
    shown = tw.page(app.id, 1).text
    items = requirement_items(shown)
    assert by_id(shown, "gap-check-start").text == "Check again"
    for rid in ("req-1", "req-2", "req-4"):
        (summary,) = inside(shown, items[rid], lambda e: e.tag == "summary")
        assert summary.text == "Add a fact"
        (form,) = inside(shown, items[rid], lambda e: e.tag == "form")
        assert str(form.attrs["action"]).endswith(f"/applications/{app.id}/tailoring/1/facts")
        assert inside(shown, form, lambda e: e.tag == "textarea" and e.attrs.get("name") == "text")
    assert not inside(shown, items["req-3"], lambda e: e.tag == "summary")  # met: no gap

    requests_before = len(tw.spy.requests)
    tasks_before = tw.rows("SELECT id FROM tasks")
    text = "Led a fictional Terraform migration for 2 teams."
    answered = tw.post(app.id, 1, "facts", text=text, requirement_id="req-2")

    assert answered.status_code == 200
    assert "Fact added." in announced(answered)
    (fact,) = tw.rows(
        "SELECT f.id, f.number, f.status, v.version FROM facts f JOIN fact_versions v "
        "ON v.id=f.current_version_id WHERE v.text=?",
        (text,),
    )
    assert fact["status"] == "confirmed" and fact["version"] == 1  # no draft step
    assert by_id(answered.text, "gap-check-start").text == "Check again"
    # Adding a fact asks nothing of the AI and starts nothing: the user presses Check again.
    assert len(tw.spy.requests) == requests_before
    assert tw.rows("SELECT id FROM tasks") == tasks_before
    # An empty fact is refused and stores nothing.
    count = len(tw.rows("SELECT id FROM facts"))
    blank = tw.post(app.id, 1, "facts", text="   ")
    assert blank.status_code == 422
    assert len(tw.rows("SELECT id FROM facts")) == count

    tw.check(app.id, "ok")

    request = tw.requests(GAP_CHECK)[-1]
    assert text in request.user and f"F{fact['number']}@1" in request.user
    assert len(tw.rows("SELECT * FROM gap_checks")) == 2  # history, not overwritten


@pytest.mark.parametrize(
    ("summary", "facts", "words", "link"),
    [
        (False, True, "Summarize the job first.", "Open job"),
        (True, False, "Add confirmed facts", "Open facts"),
        (False, False, "Summarize the job first.", "Open job"),
    ],
)
def test_gap_check_without_summary_or_facts_says_what_to_do_first(
    tw: Tailoring, summary: bool, facts: bool, words: str, link: str
):
    app = tw.ready(summary=summary, facts=facts)

    for shown in (tw.page(app.id, 1).text, tw.fragment(app.id, 1).text):
        assert words in said_text(shown)
        assert not has_id(shown, "gap-check-start")
        assert any(e.text == link and e.tag == "a" for e in elements(shown))
    refused = tw.post(app.id, 1, "check")
    assert refused.status_code == 409
    assert tw.rows("SELECT id FROM tasks WHERE type='gap_check'") == []
    assert tw.requests(GAP_CHECK) == []
    assert tw.rows("SELECT * FROM gap_checks") == []
