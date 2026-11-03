"""Prep page reads, explicit starts, filters, guards and safe source presentation."""

import json

import pytest

from app.domain.budgets import Budget
from tests.integration.test_board import snapshot
from tests.integration.test_inbox import get, post
from tests.integration.test_prep_web_questions import run_web, web_ready
from tests.refine_harness import refining
from tests.tailoring_pages import Tailoring

pytestmark = pytest.mark.integration


def seed(r):
    aid = Tailoring(r).ready("prep-page", facts=False).id
    return aid, r.run.app.state.prep


def test_prep_tab_third_tab_token_free_page_and_get_writes_nothing(tmp_path):
    with refining(tmp_path) as r:
        aid, _service = seed(r)
        before = snapshot(r)
        shown = r.client.get(f"/applications/{aid}?tab=prep")
        assert shown.status_code == 200
        assert 'id="prep"' in shown.text and "Generate questions" in shown.text
        assert 'href="/applications/' + aid + '?tab=prep" aria-current="page"' in shown.text
        assert get(r, f"/applications/{aid}/prep").status_code == 200
        assert snapshot(r) == before
        assert not [q for q in r.run.spy.requests if q.task.startswith("interview_")]


def test_prep_fragments_need_token_posts_need_origin_and_version(tmp_path):
    with refining(tmp_path) as r:
        aid, _service = seed(r)
        path = f"/applications/{aid}/prep"
        assert r.client.get(path).status_code == 403
        assert r.client.post(path + "/generate", data={"version": "1"}).status_code == 403
        assert (
            r.client.post(
                path + "/generate", data={"version": "1"}, headers={"X-JP-Token": r.run.run.token}
            ).status_code
            == 403
        )
        assert post(r, path + "/generate").status_code == 400
        stale = str(_service.view(aid).version + 1)
        assert post(r, path + "/generate", version=stale).status_code == 409
        assert "Reload" in post(r, path + "/generate", version=stale).text
        assert not [q for q in r.run.spy.requests if q.task == "interview_questions"]


def test_prep_text_escaped_type_filters_and_bank_search(tmp_path):
    with refining(tmp_path) as r:
        aid, service = seed(r)
        q = service.add_bank_question(
            "What about <script>alert(1)</script> teamwork?", "behavioral"
        )
        service.add_bank_question("How would you use Python?", "technical")
        shown = get(r, f"/applications/{aid}/prep?type=behavioral")
        assert "&lt;script&gt;" in shown.text and "<script>alert(1)</script>" not in shown.text
        assert "How would you use Python?" not in shown.text
        found = get(r, f"/applications/{aid}/prep?q=teamwork")
        assert q.id in found.text
        assert "How would you use Python?" not in found.text
        ready = post(r, f"/applications/{aid}/prep/questions/{q.id}/ready", version="0", ready="1")
        assert ready.status_code == 200 and "Already prepared" in ready.text
        assert json.loads(ready.headers["HX-Trigger"])["announce"] == "Question marked ready."


def test_ac_m8_3_source_links_http_only_with_noreferrer(tmp_path):
    with refining(tmp_path) as r:
        aid, service = web_ready(r)
        run_web(r, service, aid)
        shown = get(r, f"/applications/{aid}/prep")
        assert "Reported online · example.test · found " in shown.text
        assert 'rel="noopener noreferrer nofollow"' in shown.text
        assert "last spring (as stated by the source)" in shown.text


def test_ac_m8_2_blocked_web_button_disabled_with_reset_date_while_generate_works(tmp_path):
    with refining(tmp_path) as r:
        aid, service = web_ready(r)
        usage = r.run.app.state.usage
        usage.save_budget("web_search", Budget(10, 3, 6, 10, 1), expected_version=0)
        for _ in range(7):
            usage.reserve("web_search", tier="thorough", task_type="interview_web_questions")
        shown = get(r, f"/applications/{aid}/prep")
        assert "Monthly search limit reached. Resets on " in shown.text
        assert 'id="prep-web-start"' in shown.text
        assert 'aria-disabled="true" aria-describedby="prep-web-reason"' in shown.text
        assert "/settings/budgets#usage-budgets" in shown.text
        assert (
            post(
                r, f"/applications/{aid}/prep/web", version=str(service.view(aid).version)
            ).status_code
            == 409
        )
        assert (
            post(
                r, f"/applications/{aid}/prep/generate", version=str(service.view(aid).version)
            ).status_code
            == 200
        )


def test_regenerate_opens_safe_confirmation_without_starting(tmp_path):
    with refining(tmp_path) as r:
        aid, service = seed(r)
        shown = post(
            r, f"/applications/{aid}/prep/regenerate", version=str(service.view(aid).version)
        )
        assert shown.status_code == 200 and "data-modal" in shown.text
        assert "keeps practiced answers and ready marks" in shown.text
        assert not [q for q in r.run.spy.requests if q.task == "interview_questions"]


def test_web_controls_hidden_without_search_capable_provider(tmp_path):
    with refining(tmp_path) as r:
        aid, _service = seed(r)
        shown = get(r, f"/applications/{aid}/prep")
        assert 'id="prep-web-start"' not in shown.text
