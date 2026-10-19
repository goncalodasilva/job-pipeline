"""Regressions from the independent M3.8 reviews. Fictional data and real local runner."""

from pathlib import Path

import pytest

from app.modules.ai.providers.fake import ScriptedResponse
from tests.refine_harness import ACCEPTED, REFINE, refine_path, refining

pytestmark = pytest.mark.integration
PROMPT = Path(__file__).resolve().parents[2] / "prompts/refine_fact/prompt.md"
# How the shipped prompt shows the context facts (untrusted, since prompt version 3).
FACTS_EXPRESSION = '{{ facts | untrusted("facts", fact_id) }}'


@pytest.mark.parametrize("generalized", ["target", "context"])
@pytest.mark.parametrize(
    "replacement",
    [
        "",
        '{{ facts | untrusted("other_facts", fact_id) }}',
        f"{{# {FACTS_EXPRESSION} #}}",
        f"{{% if false %}}{FACTS_EXPRESSION}{{% endif %}}",
    ],
)
def test_generalize_policy_survives_valid_prompt_overrides(
    tmp_path: Path, generalized: str, replacement: str
) -> None:
    from app.domain.fact_selection import GENERALIZE_INSTRUCTION

    assert PROMPT.read_text(encoding="utf-8").count(FACTS_EXPRESSION) == 1
    with refining(tmp_path) as r:
        target = r.target(privacy="generalize" if generalized == "target" else "normal")
        r.typed(
            "Worked for Brightwater Trust.",
            privacy="generalize" if generalized == "context" else "normal",
        )
        override = r.run.data_dir / "prompts/overrides/refine_fact"
        override.mkdir(parents=True)
        override.joinpath("prompt.md").write_text(
            PROMPT.read_text(encoding="utf-8").replace(FACTS_EXPRESSION, replacement),
            encoding="utf-8",
        )
        r.answer_with("ok")

        r.started(target.id)

        (request,) = r.requests()
        assert request.prompt_version.endswith("+override")
        assert request.system.count(GENERALIZE_INSTRUCTION) == 1
        assert GENERALIZE_INSTRUCTION not in request.user


def test_never_use_context_is_not_replayed_through_prior_ai_questions(tmp_path: Path) -> None:
    with refining(tmp_path) as r:
        target = r.target()
        context = r.typed("Handled the Brightwater Trust audit.")
        r.run.spy.script(
            REFINE,
            ScriptedResponse(
                data={
                    "questions": ["How did the Brightwater Trust audit affect processing?"],
                    "suggestion": None,
                    "added_details": [],
                }
            ),
        )
        r.started(target.id)
        assert "Brightwater Trust" in r.open_session(target.id)["turns"]
        r.facts.set_privacy(context.id, "never_use", expected_version=context.version)

        refused = r.answer(target.id, "It improved processing.")

        assert refused.status_code == 409
        assert len(r.requests()) == 1
        assert r.open_session(target.id)["answer_rounds"] == 0
        assert r.discard(target.id).status_code in ACCEPTED
        r.answer_with("ok")
        r.started(target.id)
        assert "Brightwater Trust" not in r.requests()[-1].user


def test_start_waits_for_the_discarded_turn_to_settle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with refining(tmp_path) as r:
        target = r.target()
        r.run.hold.hold = True
        r.answer_with("ok")
        assert r.start(target.id).status_code in ACCEPTED
        r.run.wait_entered(1)
        task = r.tasks()[0]
        tasks = r.run.app.state.task_service
        cancel = tasks.request_cancel
        monkeypatch.setattr(tasks, "request_cancel", lambda _task_id: None)
        assert r.discard(target.id).status_code in ACCEPTED
        try:
            refused = r.start(target.id)
            assert refused.status_code == 409
            assert all(s["state"] == "discarded" for s in r.sessions(target.id))
        finally:
            cancel(task.id)
            r.run.hold.open()
        assert r.run.wait_terminal(task.id).status.value == "cancelled"
        r.started(target.id)
        assert r.open_session(target.id)["suggestion"] is not None


def test_accept_failure_rolls_back_fact_event_version_and_assertions(tmp_path: Path) -> None:
    with refining(tmp_path) as r:
        target = r.target()
        r.answer_with("adversarial_invented_claims")
        r.started(target.id)
        from tests.refine_harness import claim_texts

        session = r.open_session(target.id)
        before = r.fact_tables()
        r.run.execute(
            "CREATE TRIGGER fictional_refine_close BEFORE UPDATE OF state ON refine_sessions "
            "WHEN NEW.state = 'accepted' BEGIN SELECT RAISE(ABORT, 'fictional failure'); END"
        )
        suggestion = (
            "Cut invoice processing time by 85% across 14 warehouses at Globex Corporation."
        )
        acknowledged = claim_texts(session["flagged_claims"])

        failed = r.accept(target.id, text=suggestion, acknowledged=acknowledged)

        assert failed.status_code == 500
        assert r.fact_tables() == before
        assert r.open_session(target.id) == session
        r.run.execute("DROP TRIGGER fictional_refine_close")
        assert (
            r.accept(target.id, text=suggestion, acknowledged=acknowledged).status_code in ACCEPTED
        )
        assert len(r.versions(target.id)) == 2


def test_unchanged_accept_does_not_announce_a_new_version(tmp_path: Path) -> None:
    with refining(tmp_path) as r:
        target = r.target()
        r.answer_with("ok")
        r.started(target.id)

        response = r.post(
            refine_path(target.id, "accept"),
            {
                "version": str(target.version),
                "text": target.content.text,
            },
            htmx=True,
        )

        assert response.status_code == 200
        assert len(r.versions(target.id)) == 1
        assert "new version" not in response.text
        assert "new version" not in response.headers["HX-Trigger"]
