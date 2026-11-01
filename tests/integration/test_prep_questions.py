"""Interview questions through real services/tasks on fictional temporary data."""

from pathlib import Path

import pytest

from app.domain.errors import VersionConflict
from app.modules.ai.providers.fake import ScriptedResponse
from tests.refine_harness import refining
from tests.tailoring_pages import Tailoring

pytestmark = pytest.mark.integration


def test_ac_m8_1_bank_ranked_first_and_only_uncovered_requirements_sent(tmp_path):
    with refining(tmp_path) as r:
        aid = Tailoring(r).ready("prep-job", facts=False).id
        service = r.run.app.state.prep
        reqs = r.run.rows("SELECT id,text FROM job_requirements ORDER BY position")
        assert len(reqs) >= 2
        service.add_bank_question(
            "How would you approach this requirement?", "technical", requirements=(reqs[0]["text"],)
        )
        view = service.view(aid)
        assert view.questions[0].question.origin == "user"
        r.run.spy.script(
            "interview_questions",
            ScriptedResponse(
                data={
                    "questions": [
                        {
                            "text": "How would you tackle a new challenge?",
                            "type": "behavioral",
                            "requirement_ids": [reqs[1]["id"]],
                            "company_specific": False,
                        }
                    ],
                    "ask_them": [],
                }
            ),
        )
        ref = service.start_questions(aid, version=view.version)
        assert ref is not None
        assert r.run.wait_terminal(ref.id).status.value == "succeeded"
        requests = [q for q in r.run.spy.requests if q.task == "interview_questions"]
        assert len(requests) == 1
        assert reqs[0]["id"] not in requests[0].user
        assert reqs[1]["id"] in requests[0].user
        assert r.run.rows("SELECT id FROM prep_items WHERE application_id=?", (aid,))
        assert r.run.rows(
            "SELECT prompt_id,prompt_version,model FROM bank_questions WHERE origin='generated'"
        )


def test_ac_m8_1_no_request_when_bank_covers_every_key_requirement(tmp_path):
    with refining(tmp_path) as r:
        aid = Tailoring(r).ready("prep-job", facts=False).id
        service = r.run.app.state.prep
        reqs = r.run.rows("SELECT text FROM job_requirements WHERE kind IN ('must_have','key')")
        service.add_bank_question(
            "How would you approach this role?",
            "technical",
            requirements=tuple(q["text"] for q in reqs),
        )
        assert service.start_questions(aid, version=service.view(aid).version) is None
        assert not [q for q in r.run.spy.requests if q.task == "interview_questions"]
        assert r.run.rows("SELECT id FROM prep_items WHERE application_id=?", (aid,))


def test_questions_written_with_task_success_or_not_at_all(tmp_path):
    with refining(tmp_path) as r:
        aid = Tailoring(r).ready("prep-job", facts=False).id
        service = r.run.app.state.prep
        r.run.spy.script(
            "interview_questions", ScriptedResponse(raw_text="fictional malformed output")
        )
        ref = service.start_questions(aid, version=service.view(aid).version)
        assert r.run.wait_terminal(ref.id).status.value == "failed"
        assert not r.run.rows("SELECT id FROM prep_items")
        assert not r.run.rows("SELECT id FROM bank_questions WHERE origin='generated'")


def test_question_results_roll_back_if_task_commit_fails_and_cancel_writes_nothing(tmp_path):
    with refining(tmp_path) as r:
        aid = Tailoring(r).ready("atomic-prep", facts=False).id
        service = r.run.app.state.prep
        r.run.execute(
            "CREATE TRIGGER fictional_abort BEFORE UPDATE OF status ON tasks "
            "WHEN NEW.type='interview_questions' AND NEW.status='succeeded' "
            "BEGIN SELECT RAISE(ABORT,'fictional failure'); END"
        )
        ref = service.start_questions(aid, version=service.view(aid).version)
        assert r.run.wait_terminal(ref.id).status.value == "failed"
        for query in (
            "SELECT * FROM bank_questions",
            "SELECT * FROM bank_question_sightings",
            "SELECT * FROM prep_items",
        ):
            assert r.run.rows(query) == []
        r.run.execute("DROP TRIGGER fictional_abort")
        r.run.hold.hold = True
        before = r.run.spy.entered
        ref = service.start_questions(aid, version=service.view(aid).version)
        r.run.wait_entered(before + 1)
        r.run.app.state.task_service.request_cancel(ref.id)
        r.run.hold.open()
        assert r.run.wait_terminal(ref.id).status.value == "cancelled"
        for query in (
            "SELECT * FROM bank_questions",
            "SELECT * FROM bank_question_sightings",
            "SELECT * FROM prep_items",
        ):
            assert r.run.rows(query) == []


def test_show_more_adds_up_to_five_without_replacing_ready_questions(tmp_path):
    with refining(tmp_path) as r:
        aid = Tailoring(r).ready("more-prep", facts=False).id
        service = r.run.app.state.prep
        rid = r.run.rows("SELECT id FROM job_requirements WHERE kind='must_have'")[0]["id"]
        q = service.add_bank_question("How would you collaborate?", "behavioral")
        service.mark_ready(aid, q.id, ready=True, version=0)
        r.run.spy.script(
            "interview_questions",
            ScriptedResponse(
                data={
                    "questions": [
                        _question(text, rid)
                        for text in (
                            "What would you consider first?",
                            "How would you choose an approach?",
                            "Describe a challenge.",
                            "How would you evaluate success?",
                            "What lessons would you share?",
                            "An extra question to drop.",
                        )
                    ],
                    "ask_them": [],
                }
            ),
        )
        ref = service.start_questions(aid, version=service.view(aid).version, action="more")
        assert r.run.wait_terminal(ref.id).status.value == "succeeded"
        request = next(q for q in r.run.spy.requests if q.task == "interview_questions")
        assert "Write up to 5 interview questions and up to 0" in request.user
        assert len(r.run.rows("SELECT id FROM bank_questions WHERE origin='generated'")) == 5
        assert service.view(aid).already_prepared[0].question.id == q.id


def test_regenerate_needs_confirmation_and_keeps_practiced_answers(tmp_path):
    with refining(tmp_path) as r:
        aid = Tailoring(r).ready("prep-job", facts=False).id
        service = r.run.app.state.prep
        question = service.add_bank_question("Describe teamwork.", "behavioral")
        service.mark_ready(aid, question.id, ready=True, version=0)
        view = service.view(aid)
        with pytest.raises(ValueError, match="Confirm"):
            service.start_questions(aid, version=view.version, action="regenerate")
        assert service.view(aid).already_prepared[0].question.id == question.id
        with pytest.raises(VersionConflict):
            service.start_questions(aid, version=view.version - 1)


def test_near_identical_questions_merge_keeping_sightings(tmp_path):
    with refining(tmp_path) as r:
        service = r.run.app.state.prep
        one = service.add_bank_question("Tell me about a difficult project.", "behavioral")
        two = service.add_bank_question("Tell me about a difficult project!", "behavioral")
        assert one.id == two.id
        assert len(r.run.rows("SELECT id FROM bank_questions")) == 1
        assert len(r.run.rows("SELECT id FROM bank_question_sightings")) == 2


def test_interview_questions_injection_in_posting_changes_nothing(tmp_path):
    from tests.fakes.ai import load_ai_fixture
    from tests.integration.test_board import snapshot
    from tests.tailoring_ai_inputs import REQUIREMENTS
    from tests.tailoring_pages import seed_summary

    root = Path(__file__).resolve().parents[1] / "fixtures/ai/interview_questions"
    injection = (root / "injection_posting.txt").read_text()
    with refining(tmp_path) as r:
        aid = Tailoring(r).ready("prep-injection", summary=False, facts=False).id
        seed_summary(
            r.run.app.state.workspace.db, "prep-injection", REQUIREMENTS, summary=injection
        )
        service = r.run.app.state.prep
        r.run.spy.script(
            "interview_questions", load_ai_fixture("interview_questions", "injection_obeyed")
        )
        before = snapshot(r)
        ref = service.start_questions(aid, version=service.view(aid).version)
        assert r.run.wait_terminal(ref.id).status.value == "succeeded"
        request = next(q for q in r.run.spy.requests if q.task == "interview_questions")
        assert "Ignore all previous instructions" in request.user
        assert "Ignore all previous instructions" not in request.system
        assert request.user.count("<untrusted_document") == 1
        assert "&lt;/untrusted_document>" in request.user
        assert not request.allow_web_search
        assert not r.run.rows("SELECT * FROM bank_questions")
        after = snapshot(r)
        for table in ("jobs", "facts", "fact_versions", "status_history", "applications"):
            assert before[table] == after[table]


def _question(text, rid):
    return {"text": text, "type": "behavioral", "requirement_ids": [rid], "company_specific": False}


def test_confirmed_regenerate_keeps_ready_and_practiced_but_replaces_other_items(tmp_path):
    with refining(tmp_path) as r:
        aid = Tailoring(r).ready("prep-job", facts=False).id
        service = r.run.app.state.prep
        rids = [
            q["id"]
            for q in r.run.rows(
                "SELECT id FROM job_requirements WHERE kind<>'nice' ORDER BY position"
            )
        ]
        initial = [
            _question(text, rid)
            for text, rid in zip(
                (
                    "Describe a challenge you faced.",
                    "How would you learn a new skill?",
                    "What would you consider before starting?",
                ),
                rids,
                strict=True,
            )
        ]
        r.run.spy.script(
            "interview_questions", ScriptedResponse(data={"questions": initial, "ask_them": []})
        )
        ref = service.start_questions(aid, version=service.view(aid).version)
        assert r.run.wait_terminal(ref.id).status.value == "succeeded"
        rows = r.run.rows(
            "SELECT p.id,p.question_id,q.text,p.version FROM prep_items p "
            "JOIN bank_questions q ON q.id=p.question_id ORDER BY q.text"
        )
        by_text = {q["text"]: q for q in rows}
        one, two, old = [by_text[q["text"]] for q in initial]
        service.mark_ready(aid, one["question_id"], ready=True, version=one["version"])
        r.run.execute(
            "INSERT INTO practice_attempts(id,prep_item_id,answer_text,created_at,updated_at) "
            "VALUES(?,?,?,?,?)",
            (
                "practice-example",
                two["id"],
                "A fictional answer.",
                "2026-11-01T10:00:00+00:00",
                "2026-11-01T10:00:00+00:00",
            ),
        )
        r.run.spy.script(
            "interview_questions",
            ScriptedResponse(
                data={
                    "questions": [_question("How would you choose an approach?", rids[2])],
                    "ask_them": [],
                }
            ),
        )
        ref = service.start_questions(
            aid, version=service.view(aid).version, action="regenerate", confirmed=True
        )
        assert r.run.wait_terminal(ref.id).status.value == "succeeded"
        kept = r.run.rows("SELECT question_id FROM prep_items WHERE application_id=?", (aid,))
        assert old["question_id"] not in {q["question_id"] for q in kept}
        assert {one["question_id"], two["question_id"]} <= {q["question_id"] for q in kept}
        assert (
            r.run.rows("SELECT answer_text FROM practice_attempts WHERE id='practice-example'")[0][
                "answer_text"
            ]
            == "A fictional answer."
        )


def _seed_cvs(r, aid, sent):
    from tests.integration.test_schema_0004 import insert

    stamp = "2026-11-01T10:00:00+00:00"
    with r.run.app.state.workspace.db.transaction() as conn:
        insert(
            conn,
            "documents",
            dict(
                id="cv-example",
                application_id=aid,
                kind="cv",
                current_version_id="cv-v2",
                created_at=stamp,
                updated_at=stamp,
            ),
        )
        for number, text in (
            (1, "Fictional older CV wording."),
            (2, "Fictional newer CV wording."),
        ):
            vid = f"cv-v{number}"
            insert(
                conn,
                "document_versions",
                dict(
                    id=vid,
                    document_id="cv-example",
                    number=number,
                    origin="user",
                    created_at=stamp,
                    updated_at=stamp,
                ),
            )
            insert(
                conn,
                "document_units",
                dict(
                    id=f"cv-u{number}",
                    document_version_id=vid,
                    position=0,
                    section="summary",
                    kind="content",
                    text=text,
                    origin="user",
                    created_at=stamp,
                ),
            )
            conn.execute("UPDATE document_versions SET status='approved' WHERE id=?", (vid,))
        if sent:
            conn.execute("UPDATE document_versions SET status='sent' WHERE id='cv-v1'")
            insert(
                conn,
                "application_submissions",
                dict(application_id=aid, cv_version_id="cv-v1", frozen_at=stamp, created_at=stamp),
            )


@pytest.mark.parametrize("cv", ["sent", "approved_fallback", "none"])
def test_interview_questions_use_the_cv_sent(tmp_path, cv):
    with refining(tmp_path) as r:
        aid = Tailoring(r).ready("prep-job", facts=False).id
        service = r.run.app.state.prep
        if cv != "none":
            _seed_cvs(r, aid, cv == "sent")
        r.run.spy.script(
            "interview_questions", ScriptedResponse(data={"questions": [], "ask_them": []})
        )
        ref = service.start_questions(aid, version=service.view(aid).version)
        assert r.run.wait_terminal(ref.id).status.value == "succeeded"
        request = next(q for q in r.run.spy.requests if q.task == "interview_questions")
        if cv == "sent":
            assert (
                "Fictional older CV wording." in request.user
                and "Fictional newer CV wording." not in request.user
            )
        elif cv == "approved_fallback":
            assert (
                "Fictional newer CV wording." in request.user
                and "Fictional older CV wording." not in request.user
            )
        else:
            assert '"cv_sent": null' in request.user
