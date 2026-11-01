"""Practice answers and their feedback through the real service, tasks and routes (M8.2 part A;
AC-M8.4; spec 3.1 items 5 and 6, 3.3; ADR-124 items 7 and 8) on fictional temporary data.

Assumed interface (chosen here where the sources left it open; the implementer matches it).
The pure feedback rules are pinned by ``tests/unit/domain/test_answer_feedback.py`` and the
prompt by ``tests/unit/modules/ai/test_answer_feedback_prompt.py``.

Service ``app.state.answer_practice`` (``AnswerPracticeService``; not ``PracticeService``, the
M2.17 fake vehicle):

- ``view(aid, qid) -> PracticeView``: ``question`` (the bank question), ``version`` (the prep
  item's version, 0 while there is no item), ``ready``, ``attempts`` (newest first); each
  ``AttemptView`` has ``id``, ``answer``, ``version``, ``feedback`` (the domain ``Feedback``, or
  None while the task runs or after it failed) and ``resolved`` (claim index -> fact id).
  A question that doesn't exist, or belongs to another company, raises ``ApplicationNotFound``.
- ``submit(aid, qid, answer, *, version, tier=None) -> Submitted`` with ``attempt_id`` and
  ``task`` (a ``TaskRef``): the answer is at most 4,000 characters and not blank (else
  ``ValueError``, nothing stored); ``version`` is the prep item's (``VersionConflict`` when
  stale); with no item yet one is created the way ``PrepService.mark_ready`` does it (the shown
  reuse candidates become items too); an immutable ``practice_attempts`` row keeps the answer
  and the CV sent (``cv_snapshot``); the task ``answer_feedback`` (dedupe target: the attempt)
  writes ``feedback``, ``fact_refs``, provider, model and prompt in the transaction that marks
  it succeeded, and nowhere else.
- ``add_claims(attempt_id, indexes, *, version)``: for each ticked index into the feedback's
  ``unsupported`` list, in ONE transaction, a needs-check DRAFT fact (source ``practice``,
  ``original_answer`` the claim's sentence, never confirmed) and the attempt's ``resolved`` map
  (stored in its feedback JSON as ``{"resolved": {"<index>": "<fact id>"}}``). Unticked claims
  create nothing; an already resolved index is skipped; an index outside the list raises
  ``ValueError``; a stale attempt ``version`` raises ``VersionConflict``.

Routes (token for fragments, token, origin and ``version`` for POSTs; all answer the ``#prep``
fragment, 404 for an unknown or out-of-scope question):

- ``GET /applications/{aid}/prep/practice/{qid}``: the practice view; it writes nothing.
- ``POST .../practice/{qid}/answer`` (``answer``, ``version``; the budget dialog fields):
  announces "Feedback started."; 422 for a blank or over-long answer; the budget gate is asked
  before anything is stored (409 with the reason, or the dialog).
- ``POST .../practice/{qid}/claims/{attempt_id}`` (``version`` = the attempt's, ``claim``
  repeated): announces "N draft(s) added to your facts."; an attempt of another question is 404.

Markup (inside ``#prep``): ``h3#practice-heading`` (``tabindex="-1"``, ``data-autofocus``);
``form#practice-form`` with ``textarea[name=answer]`` and a hidden ``version``; one
``[data-attempt]`` per attempt; its checks as ``[data-check=answers|structure|evidence|length|
consistent_with_cv_sent]`` with ``data-verdict``; each unsupported claim as ``[data-claim=<index>]``
with "This isn't in your fact bank." and ``input[type=checkbox][name=claim][value=<index>]``
labelled "This is true; add it", all in one form that posts to the claims route; claims not in
the CV sent under "Not in the CV you sent"; a failed task as ``role="alert"``.
Everything is fictional.
"""

import json
import logging
import re
import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from app.domain.budgets import Budget
from app.domain.errors import ApplicationNotFound, VersionConflict
from app.domain.honesty import Claim
from app.modules.ai.prompts import HONESTY_NOTICE
from app.modules.ai.providers.fake import ScriptedResponse
from tests.asgi_stream import open_stream
from tests.fakes.ai import load_ai_fixture
from tests.integration.test_board import snapshot
from tests.integration.test_inbox import get, post
from tests.integration.test_m4_cross_cutting import assert_stored_only_in, tables_containing
from tests.integration.test_prep_questions import _seed_cvs
from tests.leak_scan import RawLogRecords
from tests.refine_harness import refining
from tests.tailoring_ai_inputs import FACT_1, FACT_2, HIDDEN
from tests.tailoring_pages import Tailoring, announced, said_text

pytestmark = pytest.mark.integration

TASK = "answer_feedback"
QUESTION = "Tell me about a time you planned under pressure."
GROUNDED = "I planned 60 delivery routes a week with a team of 3."
TWO_CLAIMS = (
    "I planned 60 delivery routes a week with a team of 3. "
    "Later I managed 17 stores across the region. "
    "I also worked for Globex Corporation on the side."
)
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "ai" / "answer_feedback"


class Desk:
    """An application with facts F1 to F5, one bank question, and the practice service."""

    def __init__(self, r: Any, job: str = "practice-job") -> None:
        self.r = r
        self.tw = Tailoring(r)
        self.aid: str = self.tw.ready(job).id
        self.qid: str = r.run.app.state.prep.add_bank_question(QUESTION, "behavioral").id
        self.service = r.run.app.state.answer_practice

    @property
    def version(self) -> int:
        return int(self.service.view(self.aid, self.qid).version)

    def script(self, data: dict[str, Any] | None = None, *, fixture: str = "ok") -> None:
        response = ScriptedResponse(data=data) if data else load_ai_fixture(TASK, fixture)
        self.r.run.spy.script(TASK, response)

    def practice(
        self,
        answer: str = GROUNDED,
        data: dict[str, Any] | None = None,
        *,
        fixture: str = "ok",
        status: str = "succeeded",
    ) -> Any:
        """Submit an answer through the service and wait for the task to end as ``status``."""
        self.script(data, fixture=fixture)
        self.r.run.clock.advance(timedelta(minutes=1))
        made = self.service.submit(self.aid, self.qid, answer, version=self.version)
        row = self.r.run.wait_terminal(made.task.id)
        assert str(row.status.value) == status, (str(row.status.value), row.error)
        return made

    def attempt(self, index: int = 0) -> Any:
        return self.service.view(self.aid, self.qid).attempts[index]

    def path(self, tail: str = "") -> str:
        return f"/applications/{self.aid}/prep/practice/{self.qid}{tail}"

    def requests(self) -> list[Any]:
        return [q for q in self.r.run.spy.requests if q.task == TASK]

    def practice_facts(self) -> list[dict[str, Any]]:
        return self.r.run.rows(
            "SELECT f.id,f.status,f.privacy,v.source,v.needs_check,v.original_answer,v.text "
            "FROM facts f JOIN fact_versions v ON v.fact_id=f.id WHERE v.source='practice' "
            "ORDER BY f.number"
        )

    def known_skill(self, name: str) -> None:
        self.r.drafts(f"A draft note about {name} work.", skills=(name,))
        assert self.r.run.rows("SELECT name FROM skills WHERE name=?", (name,))


def feedback_data(**changes: Any) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / "ok.json").read_text())["data"]
    return data | changes


def post_form(r: Any, path: str, data: dict[str, Any]) -> Any:
    return r.client.post(path, data=data, headers=r.run.run.action(**{"HX-Request": "true"}))


# --- AC-M8.4: claims not in the fact bank are flagged -----------------------------------------


@pytest.mark.parametrize(
    ("case", "answer", "reported", "expected"),
    [
        (
            "number",
            "I planned 75 delivery routes a week with a team of 3.",
            [],
            Claim("number", "75"),
        ),
        (
            "employer",
            "I planned delivery routes for Globex Corporation, with a team of 3.",
            [],
            Claim("name", "Globex Corporation"),
        ),
        (
            "skill",
            "I planned the routes using Terraform with a team of 3.",
            [],
            Claim("skill", "Terraform"),
        ),
        (
            "added_details",
            "I planned delivery routes and saved the team a week every month.",
            ["saved the team a week every month"],
            Claim("added_detail", "saved the team a week every month"),
        ),
    ],
)
def test_ac_m8_4_practice_flags_claims_not_in_fact_bank(tmp_path, case, answer, reported, expected):
    with refining(tmp_path) as r:
        desk = Desk(r)
        if case == "skill":
            desk.known_skill("Terraform")
        desk.practice(answer, feedback_data(unsupported_claims=reported))

        flagged = desk.attempt().feedback.unsupported
        assert expected in flagged
        shown = get(r, desk.path())
        assert shown.status_code == 200
        text = said_text(shown.text)
        assert "This isn't in your fact bank." in text and "This is true; add it" in text
        assert len(re.findall(r'name="claim"', shown.text)) == len(flagged)
        assert expected.text in text
        # Nothing is added until the person ticks it: no draft, no fact, whatever was flagged.
        assert desk.practice_facts() == []


def test_grounded_answer_flags_nothing_and_offers_no_form(tmp_path):
    with refining(tmp_path) as r:
        desk = Desk(r)
        desk.practice(GROUNDED)

        assert desk.attempt().feedback.unsupported == ()
        shown = get(r, desk.path())
        assert "This isn't in your fact bank." not in said_text(shown.text)
        assert 'name="claim"' not in shown.text


def test_feedback_is_written_with_its_provenance_and_only_sent_fact_refs(tmp_path):
    with refining(tmp_path) as r:
        desk = Desk(r)
        _seed_cvs(r, desk.aid, sent=True)
        desk.practice(
            GROUNDED, json.loads((FIXTURES / "adversarial_claims.json").read_text())["data"]
        )

        (row,) = r.run.rows("SELECT * FROM practice_attempts")
        assert row["answer_text"] == GROUNDED
        assert "Fictional older CV wording." in row["cv_snapshot"]
        assert json.loads(row["fact_refs"]) == ["F1@1"], "F4@1 and F99@1 were not sent"
        assert (row["provider"], row["model"]) == ("fake", "fake-thorough")
        assert (row["prompt_id"], row["prompt_version"]) == (TASK, "2")
        stored = json.loads(row["feedback"])
        assert stored.get("resolved", {}) == {}
        assert desk.attempt().feedback.checklist["consistent_with_cv_sent"].verdict == "no"
        assert {c.text for c in desk.attempt().feedback.not_in_cv} >= {"60"}


def test_a_claim_not_in_the_cv_sent_is_listed_apart_from_the_fact_bank_flags(tmp_path):
    with refining(tmp_path) as r:
        desk = Desk(r)
        _seed_cvs(r, desk.aid, sent=True)
        desk.practice(GROUNDED)

        shown = said_text(get(r, desk.path()).text)
        assert "Not in the CV you sent" in shown
        assert "This isn't in your fact bank." not in shown, "the fact bank states all of it"
        assert check_verdicts(get(r, desk.path()).text)["consistent_with_cv_sent"] == "no"


def check_verdicts(page: str) -> dict[str, str | None]:
    found: dict[str, str | None] = {}
    for tag in re.finditer(r'<[^>]*\bdata-check="([a-z_]+)"[^>]*>', page):
        verdict = re.search(r'\bdata-verdict="([a-z_]+)"', tag.group(0))
        found[tag.group(1)] = verdict.group(1) if verdict else None
    return found


def test_checklist_and_speaking_time_are_shown_with_code_owned_verdicts(tmp_path):
    with refining(tmp_path) as r:
        desk = Desk(r)
        desk.practice(GROUNDED)

        page = get(r, desk.path()).text
        shown = check_verdicts(page)
        assert list(shown) == [
            "answers",
            "structure",
            "evidence",
            "length",
            "consistent_with_cv_sent",
        ]
        assert shown["answers"] == "yes" and shown["structure"] == "partly"
        assert shown["length"] == "too_short", "11 words are about 5 seconds"
        assert shown["consistent_with_cv_sent"] == "not_checked", "no CV was sent"
        assert "speaking time" in said_text(page).lower()
        assert "F1@1" in page, "a suggestion points at the fact it draws on"
        assert not re.search(r"(?i)<button[^>]*>[^<]*(accept|use this wording)", page)


# --- ticked claims become needs-check drafts ---------------------------------------------------


def test_ac_m8_4_ticked_claim_creates_draft_fact_with_original_sentence(tmp_path):
    with refining(tmp_path) as r:
        desk = Desk(r)
        desk.practice(TWO_CLAIMS)
        attempt = desk.attempt()
        assert [c.text for c in attempt.feedback.unsupported] == ["17", "Globex Corporation"]
        facts_before = r.run.rows("SELECT id FROM facts")

        desk.service.add_claims(attempt.id, [0], version=attempt.version)

        (made,) = desk.practice_facts()
        assert made["status"] == "draft" and made["needs_check"] == 1
        assert made["source"] == "practice"
        assert made["original_answer"] == "Later I managed 17 stores across the region."
        assert "17" in (made["text"] or "")
        assert made["privacy"] == "normal"
        assert len(r.run.rows("SELECT id FROM facts")) == len(facts_before) + 1
        # Never confirmed by this action, and the attempt remembers which fact it became.
        assert not r.run.rows(
            "SELECT id FROM facts WHERE status='confirmed' AND id=?", (made["id"],)
        )
        after = desk.attempt()
        assert after.resolved == {0: made["id"]}
        stored = json.loads(r.run.rows("SELECT feedback FROM practice_attempts")[0]["feedback"])
        assert stored["resolved"] == {"0": made["id"]}
        assert after.version > attempt.version


def test_ac_m8_4_unticked_claim_creates_nothing(tmp_path):
    with refining(tmp_path) as r:
        desk = Desk(r)
        desk.practice(TWO_CLAIMS)
        attempt = desk.attempt()
        before = snapshot(r)

        desk.service.add_claims(attempt.id, [], version=attempt.version)
        assert snapshot(r) == before, "ticking nothing changes nothing"
        assert desk.practice_facts() == []

        desk.service.add_claims(attempt.id, [1], version=attempt.version)
        (made,) = desk.practice_facts()
        assert made["original_answer"] == "I also worked for Globex Corporation on the side."
        assert not [f for f in desk.practice_facts() if "17" in (f["original_answer"] or "")]
        assert desk.attempt().resolved == {1: made["id"]}

        with pytest.raises(ValueError):
            desk.service.add_claims(attempt.id, [7], version=desk.attempt().version)
        assert len(desk.practice_facts()) == 1


def test_ticking_both_claims_adds_both_in_order_and_each_only_once(tmp_path):
    with refining(tmp_path) as r:
        desk = Desk(r)
        desk.practice(TWO_CLAIMS)
        attempt = desk.attempt()

        desk.service.add_claims(attempt.id, [0, 1], version=attempt.version)
        first = desk.practice_facts()
        assert [f["original_answer"] for f in first] == [
            "Later I managed 17 stores across the region.",
            "I also worked for Globex Corporation on the side.",
        ]
        with pytest.raises(VersionConflict):
            desk.service.add_claims(attempt.id, [0], version=attempt.version)
        desk.service.add_claims(attempt.id, [0, 1], version=desk.attempt().version)
        assert desk.practice_facts() == first, "an added claim is not added again"


def test_a_stale_attempt_version_adds_nothing(tmp_path):
    with refining(tmp_path) as r:
        desk = Desk(r)
        desk.practice(TWO_CLAIMS)
        attempt = desk.attempt()

        with pytest.raises(VersionConflict):
            desk.service.add_claims(attempt.id, [0], version=attempt.version + 1)
        assert desk.practice_facts() == []
        with pytest.raises(ApplicationNotFound):
            desk.service.add_claims("no-such-attempt", [0], version=1)


def test_claims_are_added_in_one_transaction_or_not_at_all(tmp_path):
    with refining(tmp_path) as r:
        desk = Desk(r)
        desk.practice(TWO_CLAIMS)
        attempt = desk.attempt()
        r.run.execute(
            "CREATE TRIGGER fictional_abort BEFORE INSERT ON fact_versions "
            "WHEN NEW.source='practice' AND NEW.original_answer LIKE '%Globex%' "
            "BEGIN SELECT RAISE(ABORT,'fictional failure'); END"
        )

        with pytest.raises(Exception):  # noqa: B017 - the storage layer's own error
            desk.service.add_claims(attempt.id, [0, 1], version=attempt.version)

        assert desk.practice_facts() == [], "the first draft rolled back with the second"
        assert desk.attempt().resolved == {}
        assert desk.attempt().version == attempt.version


def test_an_added_claim_stays_flagged_until_the_person_confirms_the_draft(tmp_path):
    with refining(tmp_path) as r:
        desk = Desk(r)
        desk.practice(TWO_CLAIMS)
        attempt = desk.attempt()
        desk.service.add_claims(attempt.id, [0], version=attempt.version)

        desk.practice(TWO_CLAIMS)

        newest = desk.attempt(0).feedback.unsupported
        assert Claim("number", "17") in newest, "a draft is not part of the fact bank"
        request = desk.requests()[-1]
        # The answer itself is always in the request; the draft must not be among the facts.
        facts = re.search(
            r'<untrusted_document source="facts".*?</untrusted_document>', request.user, re.S
        )
        assert facts is not None
        assert "managed 17 stores" not in facts.group(0), "and it is not sent as a fact"


# --- the answer, the request and the budget ---------------------------------------------------


def test_the_request_holds_the_question_answer_confirmed_facts_and_cv_sent_only(tmp_path):
    with refining(tmp_path) as r:
        desk = Desk(r)
        _seed_cvs(r, desk.aid, sent=True)
        desk.practice(GROUNDED)

        (request,) = desk.requests()
        assert request.model == "fake-thorough" and request.allow_web_search is False
        assert request.system.endswith(HONESTY_NOTICE)
        user = request.user
        for source in ("question", "answer", "facts", "cv_sent"):
            assert f'<untrusted_document source="{source}"' in user
        assert QUESTION in user and GROUNDED in user
        assert FACT_1 in user and FACT_2 in user and "F1@1" in user and "F2@1" in user
        assert "Fictional older CV wording." in user
        assert "Fictional newer CV wording." not in user, "the CV sent, not the latest"


def test_never_use_and_draft_facts_are_absent_from_the_answer_feedback_request(tmp_path):
    with refining(tmp_path) as r:
        desk = Desk(r)
        desk.practice(GROUNDED)

        (request,) = desk.requests()
        everything = request.system + request.user + json.dumps(request.output_schema)
        assert HIDDEN not in everything and "Brightwater" not in everything
        assert "F4@" not in everything, "the never-use fact"
        assert "unconfirmed draft" not in everything and "F5@" not in everything
        hidden = r.run.rows("SELECT privacy FROM facts WHERE number=4")[0]["privacy"]
        assert hidden == "never_use"


def test_answer_feedback_injection_in_answer_changes_nothing(tmp_path):
    answer = (FIXTURES / "injection_answer.txt").read_text()
    with refining(tmp_path) as r:
        desk = Desk(r)
        _seed_cvs(r, desk.aid, sent=True)
        stable = ("jobs", "facts", "fact_versions", "fact_events", "skills", "fact_skills", "roles")
        stable += ("status_history", "applications_notes_placeholder")
        names = [
            t for t in stable if r.run.rows("SELECT name FROM sqlite_master WHERE name=?", (t,))
        ]
        before = {t: r.run.rows(f"SELECT * FROM {t} ORDER BY 1") for t in names}  # noqa: S608

        desk.practice(answer, fixture="injection_obeyed")

        (request,) = desk.requests()
        assert "Ignore all previous instructions" in request.user
        assert "Ignore all previous instructions" not in request.system
        assert request.user.count("<untrusted_document") == 4
        assert request.user.count("</untrusted_document>") == 4
        assert "&lt;/untrusted_document>" in request.user
        assert not request.allow_web_search
        for table in names:
            assert r.run.rows(f"SELECT * FROM {table} ORDER BY 1") == before[table], table  # noqa: S608
        assert desk.practice_facts() == []
        feedback = desk.attempt().feedback
        assert feedback.checklist["consistent_with_cv_sent"].verdict == "no"
        assert feedback.wording.flagged
        assert "Globex Corporation" in {c.text for c in feedback.unsupported}
        assert feedback.fact_refs == ()
        assert desk.attempt().resolved == {}
        assert (
            json.loads(r.run.rows("SELECT fact_refs FROM practice_attempts")[0]["fact_refs"]) == []
        )


def test_answer_feedback_malformed_retry_then_invalid(tmp_path):
    with refining(tmp_path) as r:
        desk = Desk(r)

        made = desk.practice(GROUNDED, fixture="malformed", status="failed")

        assert len(desk.requests()) == 2, "one retry, then the task fails"
        (row,) = r.run.rows("SELECT * FROM practice_attempts")
        assert row["id"] == made.attempt_id and row["answer_text"] == GROUNDED
        assert row["feedback"] is None and row["fact_refs"] == "[]"
        assert desk.attempt().feedback is None
        assert desk.practice_facts() == []
        shown = get(r, desk.path())
        assert 'role="alert"' in shown.text and GROUNDED in shown.text
        # The answer is kept, so trying again is one more attempt, not a retyped answer.
        desk.practice(GROUNDED)
        assert len(r.run.rows("SELECT id FROM practice_attempts")) == 2
        assert desk.attempt(0).feedback is not None and desk.attempt(1).feedback is None


def test_feedback_is_written_with_task_success_or_not_at_all(tmp_path):
    with refining(tmp_path) as r:
        desk = Desk(r)
        r.run.execute(
            "CREATE TRIGGER fictional_abort BEFORE UPDATE OF status ON tasks "
            "WHEN NEW.type='answer_feedback' AND NEW.status='succeeded' "
            "BEGIN SELECT RAISE(ABORT,'fictional failure'); END"
        )

        desk.practice(GROUNDED, status="failed")

        (row,) = r.run.rows("SELECT * FROM practice_attempts")
        assert row["feedback"] is None and row["fact_refs"] == "[]" and row["model"] is None


def test_blocked_by_budget_practice_does_not_enqueue_or_store(tmp_path):
    with refining(tmp_path) as r:
        desk = Desk(r)
        usage = r.run.app.state.usage
        usage.save_budget("fake", Budget(10, 3, 6, 10, 1), expected_version=0)
        for _ in range(10):
            usage.complete(
                usage.reserve("fake", tier="thorough", task_type="fictional_setup"),
                success=True,
                tokens=None,
            )
        tasks = len(r.run.rows("SELECT id FROM tasks"))

        refused = post(
            r,
            desk.path("/answer"),
            answer="my fictional practice answer",
            version=str(desk.version),
        )

        assert refused.status_code in (200, 409)
        assert len(r.run.rows("SELECT id FROM tasks")) == tasks
        assert not desk.requests()
        assert r.run.rows("SELECT id FROM practice_attempts") == []
        assert r.run.rows("SELECT id FROM prep_items") == []
        assert "my fictional practice answer" in refused.text, "the typed answer is not lost"


# --- versions, size and scope ------------------------------------------------------------------


def test_stale_version_is_refused_and_stores_nothing(tmp_path):
    with refining(tmp_path) as r:
        desk = Desk(r)
        tasks = len(r.run.rows("SELECT id FROM tasks"))

        with pytest.raises(VersionConflict):
            desk.service.submit(desk.aid, desk.qid, GROUNDED, version=desk.version + 3)
        refused = post(r, desk.path("/answer"), answer=GROUNDED, version=str(desk.version + 3))

        assert refused.status_code == 409 and "Reload" in refused.text
        assert r.run.rows("SELECT id FROM practice_attempts") == []
        assert len(r.run.rows("SELECT id FROM tasks")) == tasks and not desk.requests()


def test_answer_over_4000_characters_is_refused_at_exactly_one_more(tmp_path):
    with refining(tmp_path) as r:
        desk = Desk(r)
        too_long = "word " * 800 + "x"
        assert len(too_long) == 4001

        with pytest.raises(ValueError):
            desk.service.submit(desk.aid, desk.qid, too_long, version=desk.version)
        refused = post(r, desk.path("/answer"), answer=too_long, version=str(desk.version))
        assert refused.status_code == 422 and "4,000" in said_text(refused.text)
        for blank in ("", "   \n\t "):
            with pytest.raises(ValueError):
                desk.service.submit(desk.aid, desk.qid, blank, version=desk.version)
            assert (
                post(r, desk.path("/answer"), answer=blank, version=str(desk.version)).status_code
                == 422
            )
        assert r.run.rows("SELECT id FROM practice_attempts") == [] and not desk.requests()

        desk.practice(too_long[:4000])
        assert (
            len(r.run.rows("SELECT answer_text FROM practice_attempts")[0]["answer_text"]) == 4000
        )


def test_practice_creates_the_item_and_keeps_the_other_reused_candidates(tmp_path):
    with refining(tmp_path) as r:
        desk = Desk(r)
        prep = r.run.app.state.prep
        others = [
            prep.add_bank_question(f"Fictional general question number {n}?", "behavioral")
            for n in range(3)
        ]
        assert r.run.rows("SELECT id FROM prep_items") == []
        shown = {row.question.id for row in prep.view(desk.aid).questions}
        assert shown == {desk.qid, *(q.id for q in others)}

        desk.practice(GROUNDED)

        view = prep.view(desk.aid)
        assert {row.question.id for row in (*view.questions, *view.already_prepared)} == shown
        practiced = {row.question.id: row for row in view.questions}[desk.qid]
        assert practiced.practiced is True and practiced.item_id is not None
        assert len(r.run.rows("SELECT id FROM prep_items")) == 4
        assert [row.practiced for row in view.questions if row.question.id != desk.qid] == [
            False
        ] * 3


def test_practice_history_kept_and_mark_ready(tmp_path):
    with refining(tmp_path) as r:
        desk = Desk(r)
        first, second = "I planned the routes.", "I planned 60 routes with a team of 3."
        desk.practice(first)
        desk.practice(second)

        view = desk.service.view(desk.aid, desk.qid)
        assert [a.answer for a in view.attempts] == [second, first], "newest first, both kept"
        assert all(a.feedback is not None for a in view.attempts)
        assert view.ready is False
        shown = get(r, desk.path()).text
        assert shown.count("data-attempt=") == 2 and first in shown and second in shown

        ready = post(
            r,
            f"/applications/{desk.aid}/prep/questions/{desk.qid}/ready",
            version=str(view.version),
            ready="1",
        )
        assert ready.status_code == 200 and "Already prepared" in ready.text
        assert r.run.rows("SELECT ready FROM prep_items WHERE question_id=?", (desk.qid,)) == [
            {"ready": 1}
        ]
        assert [a.answer for a in desk.service.view(desk.aid, desk.qid).attempts] == [second, first]
        with pytest.raises(sqlite3.DatabaseError):
            r.run.execute("UPDATE practice_attempts SET answer_text='rewritten'")


# --- routes ------------------------------------------------------------------------------------


def test_practice_fragment_needs_token_posts_need_origin_and_version(tmp_path):
    with refining(tmp_path) as r:
        desk = Desk(r)
        desk.practice(TWO_CLAIMS)
        attempt = desk.attempt()
        answer_path, claims_path = desk.path("/answer"), desk.path(f"/claims/{attempt.id}")
        token = {"X-JP-Token": r.run.run.token}

        assert r.client.get(desk.path()).status_code == 403
        for path, data in (
            (answer_path, {"answer": "x", "version": "1"}),
            (claims_path, {"claim": "0", "version": "1"}),
        ):
            assert r.client.post(path, data=data).status_code == 403
            assert r.client.post(path, data=data, headers=token).status_code == 403, "no origin"
        assert post(r, answer_path, answer=GROUNDED).status_code == 400
        assert post_form(r, claims_path, {"claim": ["0"]}).status_code == 400
        stale = post_form(r, claims_path, {"claim": ["0"], "version": str(attempt.version + 1)})
        assert stale.status_code == 409 and "Reload" in stale.text
        for bad in (["x"], ["9"], ["-1"]):
            refused = post_form(r, claims_path, {"claim": bad, "version": str(attempt.version)})
            assert refused.status_code in (400, 422), bad
        assert desk.practice_facts() == []


def test_practice_get_writes_nothing_and_answers_unknown_or_out_of_scope_with_404(tmp_path):
    with refining(tmp_path) as r:
        desk = Desk(r)
        other = desk.tw.another_application("other-practice-job")
        company = r.run.rows("SELECT id FROM companies WHERE name='Other Fictional Co'")[0]["id"]
        foreign = r.run.app.state.prep.add_bank_question(
            "Why do you want to join this fictional company?", "behavioral", company_id=company
        )
        before = snapshot(r)

        assert get(r, desk.path()).status_code == 200
        assert snapshot(r) == before, "opening the view creates no item and no attempt"
        for aid, qid in (
            (desk.aid, "no-such-question"),
            (desk.aid, "bad%20id"),
            (desk.aid, foreign.id),
            ("no-such-application", desk.qid),
        ):
            path = f"/applications/{aid}/prep/practice/{qid}"
            assert get(r, path).status_code == 404, path
            posted = post(r, path + "/answer", answer=GROUNDED, version="0")
            assert posted.status_code == 404, path
        assert other.id != desk.aid
        assert snapshot(r) == before and not desk.requests()


def test_claims_route_refuses_an_attempt_of_another_question(tmp_path):
    with refining(tmp_path) as r:
        desk = Desk(r)
        desk.practice(TWO_CLAIMS)
        attempt = desk.attempt()
        elsewhere = r.run.app.state.prep.add_bank_question(
            "Describe a hard decision.", "behavioral"
        )

        wrong = f"/applications/{desk.aid}/prep/practice/{elsewhere.id}/claims/{attempt.id}"
        refused = post_form(r, wrong, {"claim": ["0"], "version": str(attempt.version)})

        assert refused.status_code == 404
        assert desk.practice_facts() == [] and desk.attempt().resolved == {}


def test_practice_answer_and_claims_routes_announce_and_swap_the_prep_fragment(tmp_path):
    with refining(tmp_path) as r:
        desk = Desk(r)
        desk.script(fixture="ok")

        started = post(r, desk.path("/answer"), answer=TWO_CLAIMS, version=str(desk.version))

        assert started.status_code == 200
        assert announced(started) == ["Feedback started."]
        assert 'id="practice-heading"' in started.text and TWO_CLAIMS[:30] in started.text
        (task,) = r.run.rows("SELECT id FROM tasks WHERE type='answer_feedback'")
        assert r.run.wait_terminal(task["id"]).status.value == "succeeded"

        attempt = desk.attempt()
        ticked = post_form(
            r,
            desk.path(f"/claims/{attempt.id}"),
            {"claim": ["0"], "version": str(attempt.version)},
        )
        assert ticked.status_code == 200
        assert announced(ticked) == ["1 draft added to your facts."]
        assert "added as a draft" in said_text(ticked.text).lower()
        (made,) = desk.practice_facts()
        assert made["status"] == "draft" and made["needs_check"] == 1
        assert desk.attempt().resolved == {0: made["id"]}


def test_practice_text_is_escaped_in_the_page(tmp_path):
    with refining(tmp_path) as r:
        desk = Desk(r)
        hostile = "I planned routes <script>alert(1)</script> in 2 days."
        data = feedback_data()
        data["checklist"]["answers"]["comment"] = "<img src=x onerror=alert(2)>"
        desk.practice(hostile, data)

        page = get(r, desk.path()).text
        assert "<script>alert(1)</script>" not in page and "&lt;script&gt;" in page
        assert "<img src=x" not in page


# --- what is stored and where --------------------------------------------------------------------


def test_practice_answer_text_absent_from_task_rows_usage_rows_logs_and_events(
    tmp_path, monkeypatch, caplog
):
    raw = RawLogRecords()
    raw.install(monkeypatch)
    caplog.set_level(logging.DEBUG)
    marker = "FictionalPracticeMarkerLarkspur"
    surfaces: list[str] = []
    with refining(tmp_path) as r:
        portal = r.client.portal
        assert portal is not None
        with portal.wrap_async_context_manager(
            open_stream(r.run.app, "/events", port=8000, headers=r.run.run.htmx())
        ) as stream:
            frames = [portal.call(stream.next_chunk)]
            desk = Desk(r)
            desk.practice(f"{GROUNDED} My notes say {marker} twice.")
            while not any(b'"status":"succeeded"' in f.replace(b" ", b"") for f in frames):
                frames.append(portal.call(stream.next_chunk))
            surfaces.extend([repr(stream.headers), b"".join(frames).decode()])
        for path in ("/tasks/panel", "/tasks/indicator", "/settings/usage"):
            response = r.client.get(path, headers=r.run.run.htmx())
            assert response.status_code == 200
            surfaces.extend([response.text, repr(response.headers)])
        for table in ("tasks", "usage_records"):
            rows = r.run.rows(f"SELECT * FROM {table}")  # noqa: S608
            assert rows
            surfaces.append(repr(rows))
        # The control: the answer is stored, and only where the attempt keeps it.
        assert_stored_only_in(r.run, marker, frozenset({"practice_attempts"}))
        assert marker in repr(r.run.rows("SELECT * FROM practice_attempts"))
        assert "practice_attempts" in tables_containing(r.run, marker)
    surfaces.extend(text for _where, text in raw.entries)
    assert all(marker not in surface for surface in surfaces)


def test_practice_leaves_every_table_it_does_not_own_untouched(tmp_path):
    with refining(tmp_path) as r:
        desk = Desk(r)
        before = snapshot(r)

        desk.practice(TWO_CLAIMS)

        after = snapshot(r)
        for table in (
            "facts",
            "fact_versions",
            "fact_events",
            "skills",
            "status_history",
            "jobs",
            "events",
            "documents",
            "document_versions",
            "bank_questions",
            "bank_question_sightings",
            "prep_documents",
        ):
            assert before[table] == after[table], table
        assert len(after["practice_attempts"]) == 1


# --- review fixes (suggestions, caps, stale CV, echo, history, retries, markup) -------------------


def block_budget(r: Any, uses: int = 10) -> None:
    """Fake-provider budget of 10 requests (warning at 3 and 6, block at 10) with ``uses`` made."""
    usage = r.run.app.state.usage
    usage.save_budget("fake", Budget(10, 3, 6, 10, 1), expected_version=0)
    for _ in range(uses):
        usage.complete(
            usage.reserve("fake", tier="thorough", task_type="fictional_setup"),
            success=True,
            tokens=None,
        )


def one_li(page: str, marker: str) -> str:
    """The ``<li ... marker ...>`` block of the page that holds ``marker``."""
    found = re.search(rf"<li[^>]*{marker}[^>]*>.*?</li>", page, re.S)
    assert found is not None, marker
    return found.group(0)


class NoCtx:
    """The task context of a run started by the test itself."""

    task_id = None  # no task row: the usage record is kept without one

    def __init__(self, input_ref: str) -> None:
        from tests.fakes.ai import NotCancelled

        self.input_ref, self.cancel_flag = input_ref, NotCancelled()

    async def heartbeat(self, progress: int | None = None) -> None:
        return None

    def check_cancelled(self) -> None:
        return None


def test_a_suggestion_that_says_new_things_is_shown_with_the_same_flag_as_the_wording(tmp_path):
    with refining(tmp_path) as r:
        desk = Desk(r)
        data = json.loads((FIXTURES / "injection_obeyed.json").read_text())["data"]
        data["suggestions"][0]["fact_ids"] = []
        desk.practice("I planned delivery routes for a depot.", data)

        page = get(r, desk.path()).text
        idea = one_li(page, "data-suggestion")
        assert 'data-flagged="true"' in idea
        assert "do not state" in said_text(idea) and "Globex Corporation" in said_text(idea)
        assert "<input" not in idea and "<button" not in idea, "never offered as accepted"
        stored = json.loads(r.run.rows("SELECT feedback FROM practice_attempts")[0]["feedback"])
        assert {"kind": "name", "text": "Globex Corporation"} in stored["suggestions"][0]["claims"]
        assert desk.practice_facts() == []


def test_a_suggestion_that_restates_the_facts_is_not_flagged(tmp_path):
    with refining(tmp_path) as r:
        desk = Desk(r)
        desk.practice(GROUNDED)

        page = get(r, desk.path()).text
        assert page.count('data-flagged="false"') == 2 and 'data-flagged="true"' not in page
        assert "No new numbers, names or skills were found. Check the rest yourself." in said_text(
            page
        )
        assert "only restates your confirmed facts" not in page


def test_a_long_answer_gets_its_feedback_without_burning_a_retry(tmp_path):
    long_answer = ("I planned 60 delivery routes a week with a team of 3. " * 48).strip()
    assert 2400 < len(long_answer) < 2700
    data = feedback_data()
    data["suggested_wording"]["text"] = long_answer
    with refining(tmp_path) as r:
        desk = Desk(r)

        desk.practice(long_answer, data)

        assert len(desk.requests()) == 1
        assert desk.attempt().feedback.wording.text == long_answer


def test_flagged_claims_are_capped_and_the_feedback_write_succeeds(tmp_path):
    answer = " ".join(str(1000 + i) for i in range(700))
    with refining(tmp_path) as r:
        desk = Desk(r)
        _seed_cvs(r, desk.aid, sent=True)

        desk.practice(answer)

        feedback = desk.attempt().feedback
        assert len(feedback.unsupported) == len(feedback.not_in_cv) == 20
        assert feedback.unsupported_more > 600 and feedback.not_in_cv_more > 600
        stored = r.run.rows("SELECT feedback FROM practice_attempts")[0]["feedback"]
        assert len(stored) < 50_000
        page = said_text(get(r, desk.path()).text)
        assert f"{feedback.unsupported_more} more not shown." in page
        assert f"{feedback.not_in_cv_more} more not shown." in page

        attempt = desk.attempt()
        desk.service.add_claims(attempt.id, list(range(20)), version=attempt.version)
        assert len(desk.practice_facts()) == 20
        assert len(r.run.rows("SELECT feedback FROM practice_attempts")[0]["feedback"]) < 50_000


def test_the_sentence_that_will_be_saved_is_shown_beside_each_claim(tmp_path):
    with refining(tmp_path) as r:
        desk = Desk(r)
        desk.practice(TWO_CLAIMS)

        page = get(r, desk.path()).text
        first = said_text(one_li(page, 'data-claim="0"'))
        second = said_text(one_li(page, 'data-claim="1"'))
        assert "Later I managed 17 stores across the region." in first
        assert "I also worked for Globex Corporation on the side." in second
        assert "saved as a draft" in first.lower()
        assert 'aria-describedby="claim-' in page, "the checkbox is tied to its sentence"


def test_claims_sit_in_a_fieldset_with_the_choices_styles(tmp_path):
    with refining(tmp_path) as r:
        desk = Desk(r)
        desk.practice(TWO_CLAIMS)

        page = get(r, desk.path()).text
        assert re.search(r'<fieldset class="choices">\s*<legend>Claims to check</legend>', page)
        assert page.count('class="choice"') == 2 and "</fieldset>" in page


def test_draws_on_shows_the_fact_wording_with_a_link_to_the_fact_bank(tmp_path):
    with refining(tmp_path) as r:
        desk = Desk(r)
        desk.practice(GROUNDED)

        idea = one_li(get(r, desk.path()).text, "data-suggestion")
        assert re.search(r'<a [^>]*href="/facts"[^>]*data-fact-ref="F1@1"', idea)
        assert "Draws on F1@1" not in said_text(idea), "no bare ids"
        assert FACT_1[:30] in said_text(idea)


def test_a_fact_that_has_changed_since_is_named_without_its_id(tmp_path):
    with refining(tmp_path) as r:
        desk = Desk(r)
        data = feedback_data()
        data["suggestions"] = [{"text": "Use it.", "fact_ids": ["F1@1"]}]
        desk.practice(GROUNDED, data)
        r.run.execute("UPDATE facts SET privacy='never_use' WHERE number=1")

        idea = said_text(one_li(get(r, desk.path()).text, "data-suggestion"))

        assert "F1@1" not in idea and FACT_1[:30] not in idea
        assert "no longer" in idea.lower()


def test_the_practice_link_goes_to_the_page_and_only_htmx_uses_the_fragment(tmp_path):
    with refining(tmp_path) as r:
        desk = Desk(r)

        page = get(r, f"/applications/{desk.aid}/prep").text

        link = re.search(rf'<a [^>]*id="prep-practice-{desk.qid}"[^>]*>', page)
        assert link is not None
        tag = link.group(0)
        assert f'href="/applications/{desk.aid}?tab=prep"' in tag
        assert f'hx-get="/applications/{desk.aid}/prep/practice/{desk.qid}"' in tag


def test_a_stale_cv_line_is_not_sent_with_the_answer(tmp_path):
    import asyncio

    with refining(tmp_path) as r:
        desk = Desk(r)
        _seed_cvs(r, desk.aid, sent=True)
        desk.practice(GROUNDED)
        (first,) = r.run.rows("SELECT * FROM practice_attempts")
        assert "Fictional older CV wording." in first["cv_snapshot"]
        stale = first["cv_snapshot"] + "\nA line that was marked never use after sending."
        r.run.execute(
            "INSERT INTO practice_attempts(id,prep_item_id,answer_text,cv_snapshot,created_at,"
            "updated_at) VALUES(?,?,?,?,?,?)",
            (
                "stale-attempt",
                first["prep_item_id"],
                GROUNDED,
                stale,
                "2026-11-02T10:00:00+00:00",
                "2026-11-02T10:00:00+00:00",
            ),
        )

        asyncio.run(desk.service.run(NoCtx("stale-attempt/thorough")))

        sent = desk.requests()[-1].user
        assert "Fictional older CV wording." in sent
        assert "marked never use after sending" not in sent


def test_a_cv_removed_from_the_application_sends_no_cv_text(tmp_path):
    import asyncio

    with refining(tmp_path) as r:
        desk = Desk(r)
        desk.practice(GROUNDED)
        (first,) = r.run.rows("SELECT * FROM practice_attempts")
        r.run.execute(
            "INSERT INTO practice_attempts(id,prep_item_id,answer_text,cv_snapshot,created_at,"
            "updated_at) VALUES(?,?,?,?,?,?)",
            (
                "orphan-attempt",
                first["prep_item_id"],
                GROUNDED,
                "A line from a CV that is no longer there.",
                "2026-11-02T10:00:00+00:00",
                "2026-11-02T10:00:00+00:00",
            ),
        )

        asyncio.run(desk.service.run(NoCtx("orphan-attempt/thorough")))

        assert "no longer there" not in desk.requests()[-1].user


def test_a_hard_budget_block_answers_409_with_the_reason_and_keeps_the_answer(tmp_path):
    with refining(tmp_path) as r:
        desk = Desk(r)
        block_budget(r)

        refused = post(r, desk.path("/answer"), answer="my fictional answer", version="0")

        assert refused.status_code == 409
        assert re.search(r'<p id="practice-reason"|<span id="practice-reason"', refused.text)
        assert "Monthly limit reached" in said_text(refused.text)
        assert re.search(
            r'<button[^>]*id="practice-submit"[^>]*aria-disabled="true"[^>]*practice-reason',
            refused.text,
        )
        assert "<dialog" not in refused.text
        assert re.search(r"<textarea[^>]*>my fictional answer</textarea>", refused.text)
        assert r.run.rows("SELECT id FROM practice_attempts") == [] and not desk.requests()


def test_a_budget_warning_answers_200_with_the_dialog_and_the_answer_as_stored(tmp_path):
    with refining(tmp_path) as r:
        desk = Desk(r)
        block_budget(r, uses=4)

        asked = post(r, desk.path("/answer"), answer="my fict\x00ional\r\nanswer", version="0")

        assert asked.status_code == 200
        assert '<dialog id="practice-budget-dialog"' in asked.text
        assert "\x00" not in asked.text
        assert 'name="answer" value="my fictional\nanswer"' in asked.text
        assert re.search(r"<textarea[^>]*>my fictional\nanswer</textarea>", asked.text)
        assert r.run.rows("SELECT id FROM practice_attempts") == [] and not desk.requests()


def test_a_nul_in_a_refused_answer_is_not_echoed_back(tmp_path):
    with refining(tmp_path) as r:
        desk = Desk(r)

        refused = post(r, desk.path("/answer"), answer="x" * 4001 + "\x00", version="0")

        assert refused.status_code == 422 and "\x00" not in refused.text


@pytest.mark.parametrize(
    ("extra", "accepted"), [(0, True), (1, False)], ids=["4000-normalized", "4001-normalized"]
)
def test_the_length_limit_counts_the_answer_after_line_ends_are_normalized(
    tmp_path, extra, accepted
):
    crlf = ("a" * 39 + "\r\n") * 100 + "b" * extra
    assert len(crlf) == 4100 + extra
    with refining(tmp_path) as r:
        desk = Desk(r)

        if accepted:
            desk.practice(crlf)
            stored = r.run.rows("SELECT answer_text FROM practice_attempts")[0]["answer_text"]
            assert len(stored) == 4000 and "\r" not in stored
        else:
            refused = post(r, desk.path("/answer"), answer=crlf, version=str(desk.version))
            assert refused.status_code == 422 and "4,000" in said_text(refused.text)
            with pytest.raises(ValueError):
                desk.service.submit(desk.aid, desk.qid, crlf, version=desk.version)
            assert r.run.rows("SELECT id FROM practice_attempts") == []


def test_older_tries_are_not_hidden_silently_and_any_try_can_be_added_from(tmp_path):
    with refining(tmp_path) as r:
        desk = Desk(r)
        desk.practice(TWO_CLAIMS)
        oldest = desk.attempt()
        assert "Showing the latest" not in get(r, desk.path()).text
        for _ in range(20):
            desk.practice(GROUNDED)

        view = desk.service.view(desk.aid, desk.qid)
        assert len(view.attempts) == 20 and view.truncated is True
        assert oldest.id not in {a.id for a in view.attempts}
        page = get(r, desk.path()).text
        assert "Showing the latest 20 tries." in said_text(page)

        ticked = post_form(
            r,
            desk.path(f"/claims/{oldest.id}"),
            {"claim": ["0"], "version": str(oldest.version)},
        )

        assert ticked.status_code == 200 and announced(ticked) == ["1 draft added to your facts."]
        assert len(desk.practice_facts()) == 1


def test_a_try_of_another_application_is_still_refused_by_the_claims_route(tmp_path):
    with refining(tmp_path) as r:
        desk = Desk(r)
        desk.practice(TWO_CLAIMS)
        attempt = desk.attempt()
        other = desk.tw.another_application("other-claims-job").id

        wrong = f"/applications/{other}/prep/practice/{desk.qid}/claims/{attempt.id}"
        refused = post_form(r, wrong, {"claim": ["0"], "version": str(attempt.version)})

        assert refused.status_code == 404 and desk.practice_facts() == []


def test_an_unreadable_stored_feedback_is_not_reported_as_a_wrong_claim_choice(tmp_path):
    with refining(tmp_path) as r:
        desk = Desk(r)
        desk.practice(TWO_CLAIMS)
        attempt = desk.attempt()
        r.run.execute("UPDATE practice_attempts SET feedback='{\"checklist\": 1}'")

        refused = post_form(
            r,
            desk.path(f"/claims/{attempt.id}"),
            {"claim": ["0"], "version": str(attempt.version)},
        )

        assert refused.status_code == 404
        assert "Choose from the flagged claims." not in refused.text
        assert desk.practice_facts() == []


def test_a_claim_outside_the_list_is_reported_as_a_wrong_choice(tmp_path):
    with refining(tmp_path) as r:
        desk = Desk(r)
        desk.practice(TWO_CLAIMS)
        attempt = desk.attempt()

        refused = post_form(
            r,
            desk.path(f"/claims/{attempt.id}"),
            {"claim": ["9"], "version": str(attempt.version)},
        )

        assert refused.status_code == 422
        assert "Choose from the flagged claims." in said_text(refused.text)


def test_a_version_conflict_shows_the_reload_prompt_and_keeps_the_typed_answer(tmp_path):
    with refining(tmp_path) as r:
        desk = Desk(r)

        refused = post(r, desk.path("/answer"), answer="my typed answer", version="7")

        assert refused.status_code == 409 and "Reload" in refused.text
        assert re.search(r"<textarea[^>]*>my typed answer</textarea>", refused.text)
        assert r.run.rows("SELECT id FROM practice_attempts") == []


def test_after_a_failed_task_the_answer_is_prefilled_and_try_again_is_offered(tmp_path):
    with refining(tmp_path) as r:
        desk = Desk(r)
        desk.practice(GROUNDED, fixture="malformed", status="failed")

        page = get(r, desk.path()).text

        assert re.search(rf"<textarea[^>]*>{re.escape(GROUNDED)}</textarea>", page)
        assert re.search(r'<button[^>]*form="practice-form"[^>]*>Try again</button>', page)
        assert 'role="alert"' in page

        desk.practice(GROUNDED)  # a later try that worked
        after = get(r, desk.path()).text
        assert not re.search(rf"<textarea[^>]*>{re.escape(GROUNDED)}</textarea>", after)
        assert ">Try again</button>" not in after


def test_no_prefill_and_no_retry_button_while_feedback_is_being_written(tmp_path):
    with refining(tmp_path) as r:
        desk = Desk(r)
        desk.script()
        r.run.hold.hold = True
        try:
            made = desk.service.submit(desk.aid, desk.qid, GROUNDED, version=desk.version)
            r.run.wait_entered(1)

            page = get(r, desk.path()).text

            assert ">Try again</button>" not in page
            assert page.count('role="status"') == 1, "one place says that feedback is on its way"
            assert "Getting feedback" in said_text(page)
            assert not re.search(rf"<textarea[^>]*>{re.escape(GROUNDED)}</textarea>", page)
        finally:
            r.run.hold.open()
        r.run.wait_terminal(made.task.id)


def test_the_page_shows_a_character_counter_and_a_polite_limit_note(tmp_path):
    with refining(tmp_path) as r:
        desk = Desk(r)

        page = get(r, desk.path()).text

        assert re.search(r'<[^>]*id="practice-count"[^>]*>\s*0 of 4,000 characters', page)
        assert re.search(r'<[^>]*id="practice-count-note"[^>]*aria-live="polite"', page)
        assert "practice-count" in re.search(r"<textarea[^>]*>", page).group(0)
        assert "Up to 4,000 characters" in said_text(page)

        desk.practice(GROUNDED, fixture="malformed", status="failed")
        prefilled = get(r, desk.path()).text
        assert f"{len(GROUNDED)} of 4,000 characters" in said_text(prefilled)


def test_the_speaking_time_is_written_in_minutes_and_seconds(tmp_path):
    answer = " ".join(["word"] * 325)
    with refining(tmp_path) as r:
        desk = Desk(r)
        desk.practice(answer)

        page = said_text(get(r, desk.path()).text)

        assert "Speaking time: about 2 minutes 30 seconds." in page
        assert " min " not in page
