"""Removal suggestions and reversible rules through the real inbox routes."""

import re

import pytest

from tests.integration.test_inbox import get, post, seeded
from tests.refine_harness import refining

pytestmark = pytest.mark.integration
SUGGESTIONS = "/inbox/removal-suggestions"
PREFERENCES = "/settings/preferences/removal-rules"


def remove_onsite(r) -> None:
    seeded(r, 5)
    r.run.execute("UPDATE jobs SET work_mode='on_site' WHERE id!='job-0005'")
    for index in range(1, 4):
        reply = post(
            r,
            f"/inbox/jobs/job-{index:04d}/triage",
            version="1",
            action="remove",
            reason="location",
        )
        assert reply.status_code == 200


def suggestion_fields(r) -> dict[str, str]:
    reply = get(r, SUGGESTIONS)
    assert reply.status_code == 200
    fields = {}
    for field in ("signature", "preview_version"):
        found = re.search(rf'name="{field}" value="([0-9a-f]{{64}})"', reply.text)
        assert found is not None
        fields[field] = found[1]
    return fields


def test_preview_counts_affected_inbox_jobs(tmp_path):
    with refining(tmp_path) as r:
        remove_onsite(r)
        reply = get(r, SUGGESTIONS)
        assert reply.status_code == 200
        assert "You removed 3 jobs" in reply.text
        assert "1 inbox job matches" in reply.text
        assert r.run.rows("SELECT * FROM removal_rules") == []


def test_ac_m9_1_flag_is_default_mode(tmp_path):
    with refining(tmp_path) as r:
        remove_onsite(r)
        reply = post(r, SUGGESTIONS, action="accept", **suggestion_fields(r))
        assert reply.status_code == 200
        assert r.run.rows("SELECT mode FROM removal_rules")[0]["mode"] == "flag"
        assert "Matches your rule: Work mode: Onsite" in get(r, "/inbox/jobs/job-0004/card").text


def test_ac_m9_1_rule_visible_editable_and_reversible(tmp_path):
    with refining(tmp_path) as r:
        remove_onsite(r)
        assert post(r, SUGGESTIONS, action="hide", **suggestion_fields(r)).status_code == 200
        rule = r.run.rows("SELECT id FROM removal_rules")[0]["id"]
        rules = get(r, PREFERENCES)
        assert rules.status_code == 200 and "Work mode: Onsite" in rules.text
        assert "fixture0001" in rules.text
        assert (
            post(
                r, f"{PREFERENCES}/{rule}", version="1", action="save", mode="flag", active="1"
            ).status_code
            == 200
        )
        assert (
            post(r, f"{PREFERENCES}/{rule}", version="2", action="save", mode="flag").status_code
            == 200
        )
        assert "Matches your rule" not in get(r, "/inbox/jobs/job-0004/card").text
        assert post(r, f"{PREFERENCES}/{rule}", version="3", action="delete").status_code == 200
        assert r.run.rows("SELECT * FROM removal_rules") == []


def test_ac_m9_1_hidden_jobs_reachable_with_count(tmp_path):
    with refining(tmp_path) as r:
        remove_onsite(r)
        assert post(r, SUGGESTIONS, action="hide", **suggestion_fields(r)).status_code == 200
        queue = get(r, "/inbox/queue")
        assert "Hidden by your rules (1)" in queue.text
        assert "fixture0004" not in queue.text
        hidden = get(r, "/inbox/queue?view=hidden")
        assert "fixture0004" in hidden.text and "fixture0005" not in hidden.text


def test_rule_never_hides_jobs_with_applications(tmp_path):
    with refining(tmp_path) as r:
        remove_onsite(r)
        assert post(r, SUGGESTIONS, action="hide", **suggestion_fields(r)).status_code == 200
        assert (
            post(r, "/inbox/jobs/job-0004/triage", version="1", action="to_apply").status_code
            == 200
        )
        assert r.run.rows("SELECT job_id FROM applications")[0]["job_id"] == "job-0004"
        assert "fixture0004" not in get(r, "/inbox/queue?view=hidden").text
        assert "fixture0004" in get(r, "/inbox/queue?view=to_apply").text


def test_removal_learning_routes_token_origin_version(tmp_path):
    with refining(tmp_path) as r:
        remove_onsite(r)
        assert r.client.get(SUGGESTIONS).status_code == 403
        assert r.client.get(PREFERENCES).status_code == 403
        fields = dict(action="accept", **suggestion_fields(r))
        assert r.client.post(SUGGESTIONS, data=fields).status_code == 403
        assert (
            r.client.post(
                SUGGESTIONS,
                data=fields,
                headers={**r.run.run.htmx(), "Origin": "https://hostile.example.test"},
            ).status_code
            == 403
        )
        assert post(r, SUGGESTIONS, **(fields | {"preview_version": "0" * 64})).status_code == 409
        assert post(r, SUGGESTIONS, **fields).status_code == 200
        rule = r.run.rows("SELECT id FROM removal_rules")[0]["id"]
        assert post(r, f"{PREFERENCES}/{rule}", version="2", action="delete").status_code == 409
        assert r.run.rows("SELECT version FROM removal_rules")[0]["version"] == 1


GROUPING = "/inbox/removal-suggestions/group"
TASK = "group_removal_reasons"
FREE_TEXT = "The fictional commute would take too long."


def remove_with_notes(r) -> None:
    seeded(r, 5)
    r.run.execute("UPDATE jobs SET work_mode='on_site' WHERE id!='job-0005'")
    for index in range(1, 4):
        reply = post(
            r,
            f"/inbox/jobs/job-{index:04d}/triage",
            version="1",
            action="remove",
            reason="other",
            text=FREE_TEXT,
        )
        assert reply.status_code == 200


def run_grouping(r) -> None:
    from tests.fakes.ai import load_ai_fixture

    remove_with_notes(r)
    r.run.spy.script(TASK, load_ai_fixture(TASK, "ok"))
    assert post(r, GROUPING).status_code == 200
    task = r.run.rows("SELECT id FROM tasks WHERE type=?", (TASK,))[0]["id"]
    assert r.run.wait_terminal(task).status.value == "succeeded"


def test_group_removal_reasons_only_on_request_through_gate(tmp_path):
    from app.domain.budgets import Budget
    from tests.fakes.ai import load_ai_fixture

    with refining(tmp_path) as r:
        remove_with_notes(r)
        r.run.spy.script(TASK, load_ai_fixture(TASK, "ok"))
        assert get(r, SUGGESTIONS).status_code == 200
        assert r.run.spy.requests == []
        assert r.run.rows("SELECT * FROM tasks") == []
        assert r.client.post(GROUPING).status_code == 403

        usage = r.run.app.state.usage
        usage.save_budget(
            "fake",
            Budget(allowance=3, first_warning=1, second_warning=2, block_at=3, reset_day=1),
            expected_version=0,
        )
        for _ in range(3):
            usage.reserve("fake", tier="fast", task_type="fictional_test")
        blocked = post(r, GROUPING)
        assert blocked.status_code == 409
        assert r.run.spy.requests == []
        assert r.run.rows("SELECT * FROM tasks") == []

        usage.save_budget("fake", None, expected_version=1)
        assert post(r, GROUPING).status_code == 200
        task = r.run.rows("SELECT id FROM tasks WHERE type=?", (TASK,))[0]["id"]
        assert r.run.wait_terminal(task).status.value == "succeeded"
        reply = get(r, SUGGESTIONS)
        assert "AI grouped 3 removal notes" in reply.text
        assert "1 inbox job matches" in reply.text
        assert r.run.rows("SELECT * FROM removal_rules") == []
        assert post(r, SUGGESTIONS, action="accept", **suggestion_fields(r)).status_code == 200
        assert r.run.rows("SELECT origin FROM removal_rules")[0]["origin"] == "ai"


def test_group_removal_reasons_sends_no_facts_or_personal_details(tmp_path, caplog):
    from tests.fakes.ai import load_ai_fixture

    with refining(tmp_path) as r:
        remove_with_notes(r)
        fact = "Fictional never-use detail from the fact bank."
        contact = "fictional-personal-marker@example.test"
        r.typed(fact, privacy="never_use")
        r.run.execute(
            "INSERT INTO settings (key,value,created_at,updated_at,version) VALUES "
            "('personal_details',?,'2026-11-04','2026-11-04',1)",
            ('{"email":"' + contact + '"}',),
        )
        r.run.spy.script(TASK, load_ai_fixture(TASK, "ok"))
        assert post(r, GROUPING).status_code == 200
        task = r.run.rows("SELECT id FROM tasks WHERE type=?", (TASK,))[0]["id"]
        assert r.run.wait_terminal(task).status.value == "succeeded"
        (request,) = r.run.spy.requests
        assert FREE_TEXT in request.user and "job-0001" in request.user
        assert fact not in request.user + request.system
        assert contact not in request.user + request.system
        assert "Fictional Greenhouse Engineer" not in request.user + request.system
        assert FREE_TEXT not in repr(r.run.rows("SELECT * FROM tasks"))
        assert FREE_TEXT not in caplog.text


def test_hide_rejects_a_stale_preview_even_when_the_affected_count_is_unchanged(tmp_path):
    with refining(tmp_path) as r:
        remove_onsite(r)
        fields = suggestion_fields(r)
        r.run.execute(
            "UPDATE jobs SET work_mode=CASE id WHEN 'job-0004' THEN 'remote' ELSE 'on_site' END "
            "WHERE id IN ('job-0004','job-0005')"
        )
        response = post(
            r,
            SUGGESTIONS,
            action="hide",
            **fields,
        )
        assert response.status_code == 409
        assert r.run.rows("SELECT * FROM removal_rules") == []


def test_removal_themes_never_in_logs_task_rows_or_events(tmp_path, caplog):
    from tests.asgi_stream import open_stream
    from tests.fakes.ai import load_ai_fixture
    from tests.integration.test_m4_cross_cutting import read_until_final

    with refining(tmp_path) as r:
        remove_with_notes(r)
        r.run.spy.script(TASK, load_ai_fixture(TASK, "ok"))
        portal = r.run.client.portal
        assert portal is not None
        with portal.wrap_async_context_manager(
            open_stream(r.run.app, "/events", port=8000, headers=r.run.run.htmx())
        ) as stream:
            initial = portal.call(stream.next_chunk)
            assert post(r, GROUPING).status_code == 200
            task = r.run.rows("SELECT id FROM tasks WHERE type=?", (TASK,))[0]["id"]
            assert r.run.wait_terminal(task).status.value == "succeeded"
            frames = initial + read_until_final(portal, stream, {task: "succeeded"})
            assert FREE_TEXT not in repr(stream.headers)
            assert FREE_TEXT.encode() not in frames
            assert b"on_site" not in frames
        (result,) = r.run.rows("SELECT * FROM removal_theme_runs")
        assert result["task_id"] == task
        assert result["prompt_id"] == TASK and result["prompt_version"] == "1"
        assert result["model"]
        assert "on_site" in result["themes"] and "job-0001" in result["themes"]
        assert FREE_TEXT not in repr(result)
        assert "on_site" not in repr(r.run.rows("SELECT * FROM tasks"))
        assert "on_site" not in caplog.text
        assert FREE_TEXT not in caplog.text


def test_group_removal_reasons_result_rolls_back_when_task_success_fails(tmp_path):
    from tests.fakes.ai import load_ai_fixture

    with refining(tmp_path) as r:
        remove_with_notes(r)
        r.run.spy.script(TASK, load_ai_fixture(TASK, "ok"))
        r.run.execute(
            "CREATE TRIGGER abort_removal_success BEFORE UPDATE OF status ON tasks "
            "WHEN NEW.type='group_removal_reasons' AND NEW.status='succeeded' "
            "BEGIN SELECT RAISE(ABORT, 'fictional failure'); END"
        )
        assert post(r, GROUPING).status_code == 200
        task = r.run.rows("SELECT id FROM tasks WHERE type=?", (TASK,))[0]["id"]
        assert r.run.wait_terminal(task).status.value == "failed"
        assert r.run.rows("SELECT * FROM removal_theme_runs") == []


@pytest.mark.parametrize("ending", ["cancelled", "interrupted"])
def test_grouping_dedupes_and_discards_output_after_cancel_or_watchdog(tmp_path, ending):
    from datetime import timedelta

    from tests.fakes.ai import load_ai_fixture

    with refining(tmp_path) as r:
        remove_with_notes(r)
        r.run.spy.script(TASK, load_ai_fixture(TASK, "ok"))
        r.run.hold.hold = True
        try:
            assert post(r, GROUPING).status_code == 200
            r.run.wait_entered(1)
            assert post(r, GROUPING).status_code == 200
            (row,) = r.run.rows("SELECT id FROM tasks WHERE type=?", (TASK,))
            tasks = r.run.app.state.task_service
            if ending == "cancelled":
                tasks.request_cancel(row["id"])
            else:
                r.run.clock.advance(timedelta(minutes=5))
                assert tasks.interrupt_stale(TASK, timedelta(minutes=1)) == [row["id"]]
        finally:
            r.run.hold.open()
        assert r.run.wait_terminal(row["id"]).status.value == ending
        assert r.run.rows("SELECT * FROM removal_theme_runs") == []
        assert len(r.run.spy.requests) == 1


def test_grouping_rechecks_current_removals_before_acceptance(tmp_path):
    with refining(tmp_path) as r:
        run_grouping(r)
        fields = suggestion_fields(r)
        assert (
            post(r, "/inbox/jobs/job-0001/triage", version="2", action="restore").status_code == 200
        )
        assert post(r, SUGGESTIONS, action="accept", **fields).status_code == 409
        assert r.run.rows("SELECT * FROM removal_rules") == []


def test_merged_application_protects_canonical_job_and_blocks_matching_suggestion(tmp_path):
    from tests.integration.test_job_summary import answer, seed, start

    with refining(tmp_path) as r:
        a = seed(r.run, "A fictional farming role with no shared words")
        b = seed(r.run, "Other unrelated posting about imaginary equipment")
        answer(r.run)
        start(r.run, a)
        version = r.run.rows("SELECT version FROM jobs WHERE id=?", (b,))[0]["version"]
        r.run.app.state.applications.apply_to_job(b, expected_job_version=version)
        start(r.run, b)
        assert r.run.rows("SELECT merged_into_job_id FROM jobs WHERE id=?", (b,)) == [
            {"merged_into_job_id": a}
        ]
        assert r.run.rows("SELECT job_id FROM applications") == [{"job_id": b}]
        seeded(r, 4)
        r.run.execute("UPDATE jobs SET work_mode='remote' WHERE id LIKE 'job-%'")
        for index in range(1, 4):
            r.run.app.state.job_triage.remove(
                f"job-{index:04d}", expected_version=1, reason="location"
            )
        assert 'name="signature"' not in get(r, SUGGESTIONS).text
        r.run.execute(
            "INSERT INTO removal_rules (id,field,condition,mode,evidence_job_ids,origin,"
            "created_at,updated_at) "
            "VALUES ('rule-0001','work_mode',?,'hide','[]','pattern','x','x')",
            ('{"value":"remote"}',),
        )
        hidden = get(r, "/inbox/queue?view=hidden")
        assert f'data-job-id="{a}"' not in hidden.text
        assert "fixture0004" in hidden.text
        assert f'data-job-id="{a}"' in get(r, "/inbox/queue").text


def test_dismissal_covers_the_strongest_current_evidence_across_sources(tmp_path):
    from app.modules.ai.providers.fake import ScriptedResponse

    with refining(tmp_path) as r:
        seeded(r, 10)
        r.run.execute("UPDATE jobs SET work_mode='on_site'")
        for index in range(1, 10):
            r.run.app.state.job_triage.remove(
                f"job-{index:04d}",
                expected_version=1,
                reason="location" if index <= 3 else "other",
                text=None if index <= 3 else FREE_TEXT,
            )
        r.run.spy.script(
            TASK,
            ScriptedResponse(
                data={
                    "themes": [
                        {"field": "work_mode", "job_ids": [f"job-{i:04d}" for i in range(4, 10)]}
                    ]
                }
            ),
        )
        assert post(r, GROUPING).status_code == 200
        task = r.run.rows("SELECT id FROM tasks WHERE type=?", (TASK,))[0]["id"]
        assert r.run.wait_terminal(task).status.value == "succeeded"
        assert post(r, SUGGESTIONS, action="dismiss", **suggestion_fields(r)).status_code == 200
        assert 'name="signature"' not in get(r, SUGGESTIONS).text


def test_token_free_inbox_does_not_serialize_private_rule_data(tmp_path):
    with refining(tmp_path) as r:
        remove_onsite(r)
        assert post(r, SUGGESTIONS, action="hide", **suggestion_fields(r)).status_code == 200
        plain = r.client.get("/inbox?view=hidden")
        assert plain.status_code == 200 and "Loading your inbox" in plain.text
        assert "fixture0004" not in plain.text and "Hidden by your rules (1)" not in plain.text
        assert "fixture0004" in get(r, "/inbox/queue?view=hidden").text

        rule = r.run.rows("SELECT id FROM removal_rules")[0]["id"]
        assert (
            post(
                r, f"{PREFERENCES}/{rule}", version="1", action="save", mode="flag", active="1"
            ).status_code
            == 200
        )
        plain = r.client.get("/inbox?selected=job-0004")
        assert "Matches your rule" not in plain.text
        assert (
            "Matches your rule: Work mode: Onsite" in get(r, "/inbox/queue?selected=job-0004").text
        )


def test_grouping_retries_return_to_the_inbox_budget_gate(tmp_path):
    from app.domain.budgets import Budget
    from app.modules.ai.providers.fake import ScriptedResponse
    from tests.fakes.ai import load_ai_fixture

    with refining(tmp_path) as r:
        remove_with_notes(r)
        r.run.spy.script(TASK, ScriptedResponse(failure="network"), load_ai_fixture(TASK, "ok"))
        r.run.app.state.usage.save_budget(
            "fake",
            Budget(allowance=5, first_warning=2, second_warning=4, block_at=5, reset_day=1),
            expected_version=0,
        )
        assert post(r, GROUPING).status_code == 200
        task = r.run.rows("SELECT id FROM tasks WHERE type=?", (TASK,))[0]["id"]
        assert r.run.wait_terminal(task).status.value == "failed"
        warned = post(r, GROUPING)
        assert warned.status_code == 200 and "Continue" in warned.text
        refused = post(r, f"/tasks/{task}/retry")
        assert refused.status_code == 409
        assert "Inbox" in refused.text
        assert len(r.run.spy.requests) == 1
        assert r.run.rows("SELECT status,attempt FROM tasks") == [
            {"status": "failed", "attempt": 1}
        ]


def test_rule_threshold_error_preserves_the_value_and_identifies_the_field(tmp_path):
    with refining(tmp_path) as r:
        reply = post(r, f"{PREFERENCES}/threshold", version="0", threshold="101")
        assert reply.status_code == 422
        assert 'value="101"' in reply.text
        assert 'aria-invalid="true"' in reply.text
        assert 'aria-describedby="removal-threshold-error"' in reply.text
