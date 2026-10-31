"""M7.6 page, fragment and scoped action contracts with fictional data."""

import json
import re

import pytest

from app.modules.ai.providers.fake import ScriptedResponse
from tests.integration.test_session_answers import payload, submit
from tests.integration.test_sessions import current, seed_note, service, start
from tests.refine_harness import refining

pytestmark = pytest.mark.integration


@pytest.fixture
def desk(tmp_path):
    with refining(tmp_path) as r:
        yield r


def post(desk, sid, action, **fields):
    view = current(desk, sid)
    answer = view.answers[-1] if view.answers else None
    data = {"session_version": str(view.session.version)}
    if answer:
        data.update(answer_id=answer.id, version=str(answer.version))
    return desk.run.client.post(
        f"/sessions/{sid}/{action}",
        data={**data, **fields},
        headers=desk.run.run.action(**{"HX-Request": "true"}),
    )


def snapshot(desk):
    return {
        t: desk.run.rows(f"SELECT * FROM {t}")  # noqa: S608 -- fixed table names below
        for t in (
            "sessions",
            "session_answers",
            "enrichment_questions",
            "tasks",
            "facts",
            "fact_versions",
            "settings",
        )
    }


def test_session_pages_token_free_by_safe_id_read_only(desk):
    made = start(desk)
    before = snapshot(desk)
    for path in ("/sessions/new", f"/sessions/{made.session_id}"):
        for method in ("GET", "HEAD"):
            reply = desk.run.client.request(method, path)
            assert reply.status_code == 200 and reply.headers["content-type"].startswith(
                "text/html"
            )
        assert "<html" in desk.run.client.get(path).text
    assert snapshot(desk) == before
    for sid in ("missing-session", "bad%3Cid", "nested/id"):
        reply = desk.run.client.get(f"/sessions/{sid}")
        assert reply.status_code in (403, 404)
        assert "bad<id" not in reply.text


def test_session_fragments_need_token_posts_need_origin_and_version(desk):
    made = start(desk)
    path = f"/sessions/{made.session_id}"
    assert desk.run.client.get(path, headers={"HX-Request": "true"}).status_code == 403
    assert desk.run.client.get(path + "/panel").status_code == 403
    before = snapshot(desk)
    for action in ("answers", "skip", "not-sure", "later", "next", "end"):
        assert desk.run.client.post(path + "/" + action).status_code == 403
        assert (
            desk.run.client.post(
                path + "/" + action, headers={"X-JP-Token": desk.run.app.state.launch_token}
            ).status_code
            == 403
        )
    response = desk.run.client.post(
        path + "/skip",
        data={"session_version": "0", "version": "0"},
        headers=desk.run.run.action(**{"HX-Request": "true"}),
    )
    assert response.status_code == 409 and "Reload" in response.text
    assert snapshot(desk) == before


def test_session_start_form_and_post_budget_and_resume(desk):
    seed_note(desk)
    page = desk.run.client.get("/sessions/new")
    assert page.status_code == 200 and "Most useful" in page.text
    assert "up to 6 requests" in page.text
    response = desk.run.client.post(
        "/sessions",
        data={"focus": "most_useful", "length": "3"},
        headers=desk.run.run.action(**{"HX-Request": "true"}),
    )
    assert response.status_code == 200 and response.headers["HX-Redirect"].startswith("/sessions/")
    sid = response.headers["HX-Redirect"].split("/")[-1]
    assert service(desk).view(sid).session.length == 3
    again = desk.run.client.post(
        "/sessions",
        data={"focus": "most_useful", "length": "3"},
        headers=desk.run.run.action(**{"HX-Request": "true"}),
    )
    assert again.status_code == 409 and sid in again.text and "Continue" in again.text


def test_session_text_escaped(desk):
    hostile = '<img src=x onerror="alert(1)">'
    desk.run.spy.script(
        "enrichment_question",
        ScriptedResponse(data={"question": "Tell me about " + hostile + ".", "why": hostile}),
    )
    made = start(desk, seed_note(desk, hostile))
    response = desk.run.client.get(f"/sessions/{made.session_id}")
    assert "&lt;img" in response.text and hostile not in response.text
    desk.run.spy.script("structure_answer", ScriptedResponse(data=payload(text=hostile, skills=[])))
    reply = post(desk, made.session_id, "answers", answer=hostile)
    assert reply.status_code == 200
    desk.run.wait_terminal(current(desk, made.session_id).task.id)
    page = desk.run.client.get(f"/sessions/{made.session_id}")
    assert hostile not in page.text and "&lt;img" in page.text


@pytest.mark.parametrize("phase", ["waiting", "answered", "failed_retry", "ended"])
def test_session_panel_states(desk, phase):
    made = start(desk)
    if phase == "answered":
        submit(desk, made, output=payload(missing=["result"]))
    elif phase in ("waiting", "failed_retry"):
        desk.run.hold.hold = phase == "waiting"
        if phase == "failed_retry":
            desk.run.spy.script("structure_answer", ScriptedResponse(raw_text="not JSON"))
        reply = post(desk, made.session_id, "answers", answer="I planned 12 routes.")
        assert reply.status_code == 200
        if phase == "failed_retry":
            desk.run.wait_terminal(current(desk, made.session_id).task.id)
    else:
        post(desk, made.session_id, "end")
    reply = desk.run.client.get(
        f"/sessions/{made.session_id}/panel", headers=desk.run.run.action(**{"HX-Request": "true"})
    )
    assert reply.status_code == 200 and "<html" not in reply.text
    expected = {
        "waiting": "Structuring your answer",
        "answered": "Next question",
        "failed_retry": "Try again",
        "ended": "Session summary",
    }[phase]
    assert expected in reply.text
    if phase == "answered":
        assert "result" in reply.text and "Practice data (fake provider)" in reply.text
    desk.run.hold.open()


@pytest.mark.parametrize("action", ["confirm", "discard"])
def test_end_summary_actions_are_scoped_versioned_and_announced(desk, action):
    made = start(desk)
    answer, _ = submit(desk, made, output=payload())
    post(desk, made.session_id, "end")
    fact = desk.facts.get(answer.draft_fact_id)
    response = desk.run.client.post(
        f"/sessions/{made.session_id}/drafts/{fact.id}/{action}",
        data={"version": str(fact.version)},
        headers=desk.run.run.action(**{"HX-Request": "true"}),
    )
    assert response.status_code == 200 and "announce" in json.loads(response.headers["HX-Trigger"])
    assert (
        desk.facts.get(fact.id).status.value
        == {"confirm": "confirmed", "discard": "discarded"}[action]
    )
    unrelated = desk.typed("Other fictional draft.")
    denied = desk.run.client.post(
        f"/sessions/{made.session_id}/drafts/{unrelated.id}/{action}",
        data={"version": str(unrelated.version)},
        headers=desk.run.run.action(**{"HX-Request": "true"}),
    )
    assert denied.status_code == 404


def test_summary_edit_leave_and_hint_merge_links(desk):
    from app.services.sessions import SessionFocus

    target = desk.target()
    made = start(desk, SessionFocus("fact", target.id))
    answer, _ = submit(desk, made, output=payload())
    post(desk, made.session_id, "end")
    page = desk.run.client.get(f"/sessions/{made.session_id}")
    assert f'href="/facts/{answer.draft_fact_id}/edit"' in page.text
    assert 'href="/facts?tab=drafts"' in page.text
    assert f"Merge into F{target.number}" in page.text
    assert f"target={target.id}" in page.text


def test_flagged_session_draft_needs_individual_confirm(desk):
    made = start(desk)
    answer, _ = submit(desk, made, output=payload(text="Planned 950 routes."))
    post(desk, made.session_id, "end")
    page = desk.run.client.get(f"/sessions/{made.session_id}")
    assert "This isn't in your answer" in page.text and "950" in page.text
    assert "Confirm all" not in page.text
    assert f"/drafts/{answer.draft_fact_id}/confirm" in page.text


def test_ask_later_menu_default_from_settings(desk):
    made = start(desk)
    page = desk.run.client.get(f"/sessions/{made.session_id}")
    select = re.search(r'<select[^>]*name="later_choice"[^>]*>(.*?)</select>', page.text, re.S)
    assert select and re.search(r'<option(?=[^>]*selected)(?=[^>]*value="week")', select[1])
    reply = post(desk, made.session_id, "later", later_choice="week")
    assert (
        reply.status_code == 200
        and "Ask me later" in json.loads(reply.headers["HX-Trigger"])["announce"]
    )


def test_session_answer_validation_retains_escaped_input(desk):
    made = start(desk)
    reply = post(desk, made.session_id, "answers", answer=" ")
    assert reply.status_code == 422 and 'aria-invalid="true"' in reply.text
    assert "data-autofocus" in reply.text


def test_fact_bank_offers_start_session(desk):
    page = desk.run.client.get("/facts")
    assert re.search(r'<a[^>]*href="/sessions/new"[^>]*>Start session</a>', page.text)


def test_session_budget_dialog_keeps_answer_and_versions_without_queuing(desk, monkeypatch):
    from app.services.budget_gate import ShowWarning

    made = start(desk)
    before = snapshot(desk)
    monkeypatch.setattr(
        desk.run.app.state.budget_gate,
        "check",
        lambda *args, **kwargs: ShowWarning(
            "First warning",
            "Fictional request limit.",
            {
                "budget_service": "fake",
                "budget_threshold": "first",
                "budget_cycle_start": "2026-04-01",
            },
        ),
    )
    response = post(desk, made.session_id, "answers", answer="I planned <fictional> routes.")
    assert response.status_code == 200 and "<dialog" in response.text
    assert 'name="answer" value="I planned &lt;fictional&gt; routes."' in response.text
    assert snapshot(desk) == before


def test_summary_stale_action_and_open_session_action_write_nothing(desk):
    made = start(desk)
    answer, _ = submit(desk, made, output=payload())
    fact = desk.facts.get(answer.draft_fact_id)
    path = f"/sessions/{made.session_id}/drafts/{fact.id}/confirm"
    before = snapshot(desk)
    assert (
        desk.run.client.post(
            path,
            data={"version": str(fact.version)},
            headers=desk.run.run.action(**{"HX-Request": "true"}),
        ).status_code
        == 409
    )
    assert snapshot(desk) == before
    post(desk, made.session_id, "end")
    before = snapshot(desk)
    response = desk.run.client.post(
        path, data={"version": "0"}, headers=desk.run.run.action(**{"HX-Request": "true"})
    )
    assert response.status_code == 409 and "Reload" in response.text
    assert snapshot(desk) == before


def test_panel_unchanged_returns_204_and_unknown_or_duplicate_form_fields_refused(desk):
    made = start(desk)
    response = desk.run.client.get(
        f"/sessions/{made.session_id}/panel", headers=desk.run.run.action(**{"HX-Request": "true"})
    )
    match = re.search(r"hx-vals=\'([^\']+)\'", response.text)
    import html

    signature = json.loads(html.unescape(match[1]))["was"]
    repeat = desk.run.client.get(
        f"/sessions/{made.session_id}/panel",
        params={"was": signature},
        headers=desk.run.run.action(**{"HX-Request": "true"}),
    )
    assert repeat.status_code == 204
    before = snapshot(desk)
    bad = post(desk, made.session_id, "skip", unexpected="fictional")
    assert bad.status_code == 400 and snapshot(desk) == before


def test_summary_keeps_original_answer_after_user_edits_draft(desk):
    from app.services.facts import FactInput

    made = start(desk)
    answer, _ = submit(desk, made, "I planned twelve fictional orchard routes.", output=payload())
    post(desk, made.session_id, "end")
    fact = desk.facts.get(answer.draft_fact_id)
    desk.facts.edit(
        fact.id, FactInput(text="My revised orchard wording."), expected_version=fact.version
    )
    page = desk.run.client.get(f"/sessions/{made.session_id}")
    assert "My revised orchard wording." in page.text
    assert "I planned twelve fictional orchard routes." in page.text


def test_missing_session_with_invalid_answer_is_fixed_404(desk):
    response = desk.run.client.post(
        "/sessions/missing-session/answers",
        data={"answer": ""},
        headers=desk.run.run.action(**{"HX-Request": "true"}),
    )
    assert response.status_code == 404
    assert response.text == "This session wasn't found. Open Sessions to start another."


def test_refused_later_form_retains_escaped_choice_and_date(desk):
    made = start(desk)
    response = post(desk, made.session_id, "later", later_choice="date", later_date="2020-01-01")
    assert response.status_code == 422
    assert re.search(r'<option(?=[^>]*value="date")(?=[^>]*selected)', response.text)
    assert 'name="later_date" type="date" value="2020-01-01"' in response.text


def test_stale_answer_keeps_editor_versions_and_cannot_replace_other_tab_input(desk):
    made = start(desk)
    view = current(desk, made.session_id)
    first = view.answers[-1]
    desk.run.spy.script("structure_answer", ScriptedResponse(raw_text="not JSON"))
    other = service(desk).answer(
        made.session_id,
        first.id,
        "Other tab retained text.",
        expected_version=first.version,
        session_version=view.session.version,
    )
    desk.run.wait_terminal(other.task.id)
    fields = {
        "answer_id": first.id,
        "version": str(first.version),
        "session_version": str(view.session.version),
        "answer": "Local unsaved text.",
    }
    response = desk.run.client.post(
        f"/sessions/{made.session_id}/answers",
        data=fields,
        headers=desk.run.run.action(**{"HX-Request": "true"}),
    )
    assert response.status_code == 409
    for key in ("version", "session_version"):
        assert f'name="{key}" value="{fields[key]}"' in response.text
    assert 'aria-disabled="true"' in response.text
    repeated = desk.run.client.post(
        f"/sessions/{made.session_id}/answers",
        data=fields,
        headers=desk.run.run.action(**{"HX-Request": "true"}),
    )
    assert repeated.status_code == 409
    assert current(desk, made.session_id).answers[-1].answer_text == "Other tab retained text."
