"""M8.1 review regressions: reuse after Mark ready, Show more, company scope, honest messages."""

import json
from pathlib import Path

import pytest

from app.domain.budgets import Budget
from app.modules.ai.providers.fake import ScriptedResponse
from tests.integration.test_inbox import get, post
from tests.integration.test_m4_cross_cutting import tables_containing
from tests.integration.test_prep_web_questions import web_ready
from tests.refine_harness import refining
from tests.tailoring_pages import Tailoring

pytestmark = pytest.mark.integration


def _question(text, rid, company_specific=False):
    return {
        "text": text,
        "type": "behavioral",
        "requirement_ids": [rid],
        "company_specific": company_specific,
    }


def test_mark_ready_keeps_the_other_reused_candidates(tmp_path):
    with refining(tmp_path) as r:
        aid = Tailoring(r).ready("reuse-job", facts=False).id
        service = r.run.app.state.prep
        made = [
            service.add_bank_question(f"Fictional general question number {n}?", "behavioral")
            for n in range(4)
        ]
        before = service.view(aid)
        assert len(before.questions) == 4
        service.mark_ready(aid, made[0].id, ready=True, version=0)
        after = service.view(aid)
        assert len(after.already_prepared) == 1
        assert {row.question.id for row in after.questions} == {q.id for q in made[1:]}


def test_show_more_runs_when_the_first_batch_covered_every_key_requirement(tmp_path):
    with refining(tmp_path) as r:
        aid = Tailoring(r).ready("more-covered", facts=False).id
        service = r.run.app.state.prep
        reqs = r.run.rows("SELECT id,text FROM job_requirements WHERE kind IN ('must_have','key')")
        bank = service.add_bank_question(
            "Which fictional approach covers everything?",
            "technical",
            requirements=tuple(q["text"] for q in reqs),
        )
        assert service.start_questions(aid, version=service.view(aid).version) is None
        r.run.spy.script(
            "interview_questions",
            ScriptedResponse(
                data={
                    "questions": [
                        _question(f"What would you do about {word} first?", reqs[0]["id"])
                        for word in ("planning", "testing", "scoping", "reviewing", "learning")
                    ],
                    "ask_them": [],
                }
            ),
        )
        ref = service.start_questions(aid, version=service.view(aid).version, action="more")
        assert ref is not None
        assert r.run.wait_terminal(ref.id).status.value == "succeeded"
        request = next(q for q in r.run.spy.requests if q.task == "interview_questions")
        assert reqs[0]["id"] in request.user
        assert bank.text in request.user
        assert len(r.run.rows("SELECT id FROM bank_questions WHERE origin='generated'")) == 5


def _without_company(r):
    with r.run.app.state.workspace.db.transaction() as conn:
        conn.execute("UPDATE jobs SET company_id=NULL")


def test_job_without_company_gets_no_web_search_and_no_global_company_questions(tmp_path):
    with refining(tmp_path) as r:
        aid, service = web_ready(r)
        _without_company(r)
        view = service.view(aid)
        assert view.company == ""
        assert service.plan_start(aid, web=True) is None
        shown = get(r, f"/applications/{aid}/prep")
        assert 'id="prep-web-start"' in shown.text
        assert "needs a company name" in shown.text
        assert 'aria-disabled="true" aria-describedby="prep-web-reason"' in shown.text
        assert not [q for q in r.run.spy.requests if q.task == "interview_web_questions"]
        rid = r.run.rows("SELECT id FROM job_requirements WHERE kind='must_have'")[0]["id"]
        r.run.spy.script(
            "interview_questions",
            ScriptedResponse(
                data={
                    "questions": [
                        _question("Why this fictional employer?", rid, company_specific=True),
                        _question("A general fictional question?", rid),
                    ],
                    "ask_them": [],
                }
            ),
        )
        ref = service.start_questions(aid, version=service.view(aid).version)
        assert r.run.wait_terminal(ref.id).status.value == "succeeded"
        texts = [row["text"] for row in r.run.rows("SELECT text FROM bank_questions")]
        assert texts == ["A general fictional question?"]


def test_generate_with_nothing_new_says_so(tmp_path):
    with refining(tmp_path) as r:
        aid = Tailoring(r).ready("nothing-new", facts=False).id
        service = r.run.app.state.prep
        reqs = r.run.rows("SELECT text FROM job_requirements WHERE kind IN ('must_have','key')")
        service.add_bank_question(
            "Which fictional approach covers everything?",
            "technical",
            requirements=tuple(q["text"] for q in reqs),
        )
        path = f"/applications/{aid}/prep/generate"
        first = post(r, path, version=str(service.view(aid).version))
        assert json.loads(first.headers["HX-Trigger"])["announce"] == (
            "Questions reused from your bank."
        )
        second = post(r, path, version=str(service.view(aid).version))
        said = json.loads(second.headers["HX-Trigger"])["announce"]
        assert said == "No new questions to add right now."


def test_prep_buttons_and_links_use_the_visually_hidden_class(tmp_path):
    with refining(tmp_path) as r:
        aid, service = web_ready(r)
        service.add_bank_question("A fictional question?", "behavioral")
        ref = service.start_web(aid, version=service.view(aid).version)
        assert r.run.wait_terminal(ref.id).status.value == "succeeded"
        shown = get(r, f"/applications/{aid}/prep").text
        assert "sr-only" not in shown
        assert shown.count("visually-hidden") >= 2


def test_filter_and_search_announce_the_result(tmp_path):
    with refining(tmp_path) as r:
        aid = Tailoring(r).ready("announce-job", facts=False).id
        service = r.run.app.state.prep
        service.add_bank_question("A fictional teamwork question?", "behavioral")
        filtered = get(r, f"/applications/{aid}/prep?type=technical&focus=1")
        assert "No bank questions match" in json.loads(filtered.headers["HX-Trigger"])["announce"]
        found = get(r, f"/applications/{aid}/prep?q=teamwork&focus=1")
        assert "1 bank question" in json.loads(found.headers["HX-Trigger"])["announce"]
        assert "HX-Trigger" not in get(r, f"/applications/{aid}/prep?q=teamwork").headers


def test_ready_keeps_focus_on_the_question_it_moved(tmp_path):
    with refining(tmp_path) as r:
        aid = Tailoring(r).ready("focus-job", facts=False).id
        service = r.run.app.state.prep
        q = service.add_bank_question("A fictional focus question?", "behavioral")
        ready = post(r, f"/applications/{aid}/prep/questions/{q.id}/ready", version="0", ready="1")
        assert f'id="prep-ready-{q.id}"' in ready.text
        assert f'id="prep-ready-{q.id}" data-autofocus' in ready.text


def test_prep_without_a_set_disables_show_more_and_regenerate(tmp_path):
    with refining(tmp_path) as r:
        aid = Tailoring(r).ready("empty-set", facts=False).id
        shown = get(r, f"/applications/{aid}/prep").text
        for button in ("prep-more", "prep-regenerate"):
            marker = f'id="{button}"'
            tag = shown[shown.index(marker) : shown.index(">", shown.index(marker))]
            assert 'aria-disabled="true"' in tag and "aria-describedby" in tag


def test_regenerate_dialog_says_it_uses_a_request_without_a_limits_link(tmp_path):
    with refining(tmp_path) as r:
        aid = Tailoring(r).ready("regen-dialog", facts=False).id
        service = r.run.app.state.prep
        q = service.add_bank_question("A fictional regenerate question?", "behavioral")
        service.mark_ready(aid, q.id, ready=True, version=0)
        shown = post(
            r, f"/applications/{aid}/prep/regenerate", version=str(service.view(aid).version)
        )
        assert "This uses one AI request." in shown.text
        assert "Change limits" not in shown.text


def test_research_explains_how_to_get_web_questions_without_a_search_provider(tmp_path):
    with refining(tmp_path) as r:
        aid = Tailoring(r).ready("no-search", facts=False).id
        shown = get(r, f"/applications/{aid}/prep").text
        assert "Web questions need a search provider" in shown
        assert "/settings" in shown


def test_web_questions_with_unclear_storage_dont_spend_when_company_missing(tmp_path):
    with refining(tmp_path) as r:
        aid, service = web_ready(r, storage=None)
        _without_company(r)
        assert (
            post(
                r, f"/applications/{aid}/prep/web", version=str(service.view(aid).version)
            ).status_code
            == 409
        )
        assert not tables_containing(r.run, "interview-marker-web")


def test_web_button_states_use_and_month_usage(tmp_path):
    with refining(tmp_path) as r:
        aid, service = web_ready(r)
        usage = r.run.app.state.usage
        usage.save_budget("web_search", Budget(30, 10, 20, 30, 1), expected_version=0)
        shown = get(r, f"/applications/{aid}/prep").text
        assert "Web questions use AI and web search through" in shown
        assert "up to five searches" in shown
        assert "This month: 0 searches of 30." in shown
        ref = service.start_web(aid, version=service.view(aid).version)
        assert r.run.wait_terminal(ref.id).status.value == "succeeded"
        assert "This month: 2 searches of 30." in get(r, f"/applications/{aid}/prep").text


def test_ac_m8_2_every_web_search_tool_call_counted(tmp_path):
    with refining(tmp_path) as r:
        aid, service = web_ready(r)
        ref = service.start_web(aid, version=service.view(aid).version)
        assert r.run.wait_terminal(ref.id).status.value == "succeeded"
        calls = r.run.spy._by_task["interview_web_questions"].tool_calls
        assert len(calls) == 2 and all(allowed for _, allowed in calls)
        records = r.run.rows(
            "SELECT id FROM usage_records WHERE service='web_search' "
            "AND task_type='interview_web_questions'"
        )
        assert len(records) == len(calls)


def test_show_more_adds_a_batch_through_the_gate(tmp_path):
    with refining(tmp_path) as r:
        aid = Tailoring(r).ready("more-gate", facts=False).id
        service = r.run.app.state.prep
        usage = r.run.app.state.usage
        service_name = service.plan_start(aid).service
        usage.save_budget(service_name, Budget(10, 3, 6, 10, 1), expected_version=0)
        for _ in range(10):
            record = usage.reserve(service_name, tier="thorough", task_type="fictional_setup")
            usage.complete(record, success=True, tokens=None)
        before = len(r.run.rows("SELECT id FROM tasks"))
        refused = post(r, f"/applications/{aid}/prep/more", version=str(service.view(aid).version))
        assert refused.status_code in (200, 409)
        assert len(r.run.rows("SELECT id FROM tasks")) == before
        assert not [q for q in r.run.spy.requests if q.task == "interview_questions"]


def _seed_never_use_cv(r, aid):
    from tests.integration.test_schema_0004 import insert

    stamp = "2026-11-01T10:00:00+00:00"
    fact = r.run.rows(
        "SELECT id AS fid, current_version_id AS vid FROM facts WHERE privacy='never_use'"
    )[0]
    with r.run.app.state.workspace.db.transaction() as conn:
        insert(
            conn,
            "documents",
            dict(
                id="cv-example",
                application_id=aid,
                kind="cv",
                current_version_id="cv-v1",
                created_at=stamp,
                updated_at=stamp,
            ),
        )
        insert(
            conn,
            "document_versions",
            dict(
                id="cv-v1",
                document_id="cv-example",
                number=1,
                origin="user",
                created_at=stamp,
                updated_at=stamp,
            ),
        )
        for number, text in (
            (0, "Fictional allowed CV wording."),
            (1, "Fictional hidden CV wording."),
        ):
            insert(
                conn,
                "document_units",
                dict(
                    id=f"cv-u{number}",
                    document_version_id="cv-v1",
                    position=number,
                    section="summary",
                    kind="content",
                    text=text,
                    origin="user",
                    created_at=stamp,
                ),
            )
        insert(
            conn,
            "document_unit_facts",
            dict(document_unit_id="cv-u1", fact_id=fact["fid"], fact_version_id=fact["vid"]),
        )
        conn.execute("UPDATE document_versions SET status='approved' WHERE id='cv-v1'")


def test_cv_units_citing_a_never_use_fact_are_not_sent(tmp_path):
    with refining(tmp_path) as r:
        aid = Tailoring(r).ready("never-use-cv", facts=True).id
        service = r.run.app.state.prep
        _seed_never_use_cv(r, aid)
        r.run.spy.script(
            "interview_questions", ScriptedResponse(data={"questions": [], "ask_them": []})
        )
        ref = service.start_questions(aid, version=service.view(aid).version)
        assert r.run.wait_terminal(ref.id).status.value == "succeeded"
        request = next(q for q in r.run.spy.requests if q.task == "interview_questions")
        assert "Fictional allowed CV wording." in request.user
        assert "Fictional hidden CV wording." not in request.user


def test_bank_search_query_not_in_access_log(tmp_path: Path) -> None:
    from tests.page_token import TOKEN_HEADER_NAME, page_token
    from tests.running_app import start_app

    marker = "CanaryPrepBankQuery81"
    app = start_app(tmp_path)
    try:
        with app.client() as client:
            token = page_token(client.get("/").text)
            headers = {TOKEN_HEADER_NAME: token, "HX-Request": "true"}
            for path in (
                f"/applications/app-0001/prep?q={marker}&type={marker}",
                f"/applications/app-0001/prep?{marker}={marker}&focus=1",
            ):
                assert client.get(path, headers=headers).status_code in (200, 404)
            assert client.get(f"/applications/app-0001?tab=prep&q={marker}").status_code in (
                200,
                404,
            )
    finally:
        app.stop()
    output = app.stdout_path.read_text() + app.stderr_path.read_text()
    output += "".join(p.read_text() for p in app.log_dir.glob("*.log*"))
    assert "GET /applications/app-0001/prep HTTP/1.1" in output
    assert marker not in output
    for fragment in ("?q=", "&type=", "&focus="):
        assert fragment not in output, fragment
