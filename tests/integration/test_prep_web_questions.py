"""Explicit web starts and the search provider's retention rule, with offline fakes."""

import pytest

from app.modules.ai import AICapabilities
from app.modules.ai.providers.fake import ScriptedResponse
from tests.fakes.search import FakeSearchProvider
from tests.integration.test_m4_cross_cutting import tables_containing
from tests.refine_harness import refining
from tests.tailoring_pages import Tailoring

pytestmark = pytest.mark.integration
SOURCE = "https://example.test/interview-marker-web"
TEXT = "Describe a fictional web-marker challenge."


def web_ready(r, storage=True):
    aid = Tailoring(r).ready("web-prep-job", facts=False).id
    r.run.spy.capabilities = AICapabilities(web_search=True, reports_token_usage=True)
    service = r.run.app.state.prep
    service._search = lambda provider: FakeSearchProvider(storage_allowed=storage)
    r.run.spy.script(
        "interview_web_questions",
        ScriptedResponse(
            data={
                "questions": [
                    {
                        "text": TEXT,
                        "type": "behavioral",
                        "source_url": SOURCE,
                        "source_date": "last spring",
                    },
                    {
                        "text": "An unsearched question.",
                        "type": "technical",
                        "source_url": "https://unsearched.test/",
                        "source_date": None,
                    },
                ]
            },
            tool_calls=2,
            searched_urls=(SOURCE,),
        ),
    )
    return aid, service


def run_web(r, service, aid):
    ref = service.start_web(aid, version=service.view(aid).version)
    assert r.run.wait_terminal(ref.id).status.value == "succeeded"


def test_ac_m8_2_web_questions_only_on_explicit_post(tmp_path):
    with refining(tmp_path) as r:
        aid, service = web_ready(r)
        for _ in range(2):
            service.view(aid)
        assert not [q for q in r.run.spy.requests if q.task == "interview_web_questions"]
        run_web(r, service, aid)
        assert len([q for q in r.run.spy.requests if q.task == "interview_web_questions"]) == 1


def test_ac_m8_3_reported_online_questions_carry_source_and_date(tmp_path):
    with refining(tmp_path) as r:
        aid, service = web_ready(r)
        run_web(r, service, aid)
        rows = r.run.rows("SELECT * FROM bank_questions")
        assert len(rows) == 1
        assert rows[0]["text"] == TEXT
        assert rows[0]["origin"] == "reported_online"
        assert rows[0]["source_url"] == SOURCE
        assert rows[0]["source_date"] == "last spring"
        assert rows[0]["found_at"]
        assert rows[0]["prompt_id"] == "interview_web_questions"
        assert service.view(aid).questions[0].question.text == TEXT
        assert not service.view(aid).transient
        assert len(r.run.rows("SELECT id FROM usage_records WHERE service='web_search'")) == 2


@pytest.mark.parametrize("storage", [None, False])
def test_ac_m8_3_unclear_storage_output_not_cached(tmp_path, storage):
    with refining(tmp_path) as r:
        aid, service = web_ready(r, storage)
        for count in (1, 2):
            run_web(r, service, aid)
            view = service.view(aid)
            assert view.transient[0].text == TEXT
            assert view.storage_notice.startswith("Not saved:")
            assert not tables_containing(r.run, "web-marker")
            assert not tables_containing(r.run, "interview-marker-web")
            assert not r.run.rows("SELECT id FROM bank_questions")
            assert not r.run.rows("SELECT id FROM prep_items")
            assert (
                len([q for q in r.run.spy.requests if q.task == "interview_web_questions"]) == count
            )
            assert (
                len(r.run.rows("SELECT id FROM usage_records WHERE service='web_search'"))
                == 2 * count
            )


@pytest.mark.parametrize("remaining", [3, 20])
def test_ac_m8_2_search_refused_mid_call_at_budget_or_five_search_cap(tmp_path, remaining):
    from app.domain.budgets import Budget

    with refining(tmp_path) as r:
        aid, service = web_ready(r)
        usage = r.run.app.state.usage
        usage.save_budget("web_search", Budget(30, 10, 20, 30, 1), expected_version=0)
        for _ in range(30 - remaining):
            record = usage.reserve("web_search", tier="thorough", task_type="fictional_setup")
            usage.complete(record, success=True, tokens=None)
        r.run.spy.script(
            "interview_web_questions",
            ScriptedResponse(
                data={"questions": []},
                tool_calls=6,
            ),
        )
        run_web(r, service, aid)
        records = r.run.rows(
            "SELECT id FROM usage_records WHERE service='web_search' "
            "AND task_type='interview_web_questions'"
        )
        assert len(records) == min(remaining, 5)
        calls = r.run.spy._by_task["interview_web_questions"].tool_calls
        assert len(calls) == 6
        assert sum(allowed for _, allowed in calls) == min(remaining, 5)
        assert not r.run.rows("SELECT id FROM bank_questions")
