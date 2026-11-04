"""M9.2 part C: every path that starts an AI task honours the per-task tier override, and the
budget gate's plan and the run agree (AC-M9.2; ADR-089 item 9; spec 3.7; TD-165).

A task's tier is the user's override in ``AIProviderSettings.task_tiers``, else the default in
``app.domain.tiers.DEFAULT_TIERS`` (``task_tier``). The paths that ignored an override before
M9.2 are ``extract_cv`` (a tier constant of its own, TD-165, now removed), ``refine_fact`` (a
fixed ``FAST``) and the session questions and answers (``tier="fast"`` on every call); every path
now reads ``task_tier`` (``tests/unit/modules/ai/test_default_tiers_match_prompts.py`` guards
that); the ones that already read the setting keep
their pins here (``summarize_job``, the debrief's ``structure_answer``). Without an override every
default is unchanged. The real runner, the fake provider through the spy, a temp database and a
frozen clock; no network. Every value is fictional.
"""

from pathlib import Path
from typing import Any

import pytest

from app.domain.budgets import Budget
from app.domain.settings import AI_PROVIDER, AIProviderSettings
from app.modules.ai.providers.fake import FAKE_FAST_MODEL, FAKE_THOROUGH_MODEL
from app.services.sessions import SessionFocus
from app.services.settings import SettingsService
from tests.cv_import_harness import ACCEPTED, importing, said
from tests.debrief_pages import PARAGRAPH_ONE
from tests.integration.test_cv_import import CV_TEXT
from tests.integration.test_debrief import TASK as DEBRIEF_TASK
from tests.integration.test_debrief import Desk
from tests.integration.test_job_summary import answer, seed, start
from tests.integration.test_session_answers import payload, submit
from tests.integration.test_sessions import current, seed_note
from tests.integration.test_sessions import service as sessions_service
from tests.integration.test_sessions import start as start_session
from tests.refine_harness import refining

pytestmark = pytest.mark.integration

MODEL = {"fast": FAKE_FAST_MODEL, "thorough": FAKE_THOROUGH_MODEL}
HTMX = {"HX-Request": "true"}
OTHER = {"fast": "thorough", "thorough": "fast"}


def provider(**overrides: str) -> AIProviderSettings:
    return AIProviderSettings(kind="fake", task_tiers=overrides)  # type: ignore[arg-type]


def requests_of(run: Any, task: str) -> list[Any]:
    return [r for r in run.spy.requests if r.task == task]


def tiers_used(run: Any, task: str) -> list[str]:
    """The tier of each counted request of ``task``, in the order they were made."""
    rows = run.rows("SELECT tier FROM usage_records WHERE task_type = ? ORDER BY rowid", (task,))
    return [str(row["tier"]) for row in rows]


def cap_thorough(run: Any, *, task: str) -> None:
    """A budget on the fake service whose Thorough cap is used up: a Thorough start is refused
    (Run once on Fast is offered), a Fast start is not."""
    usage = run.app.state.usage
    usage.save_budget(
        "fake",
        Budget(
            allowance=10,
            first_warning=4,
            second_warning=6,
            block_at=8,
            reset_day=1,
            thorough_cap=1,
        ),
        expected_version=usage.load_budgets().version,
    )
    record = usage.reserve("fake", tier="thorough", task_type=task)
    usage.complete(record, success=True, tokens=None)


def set_task_tiers(run: Any, overrides: dict[str, str]) -> None:
    settings = SettingsService(run.app.state.workspace.db, run.clock)
    loaded = settings.load(AI_PROVIDER)
    changed = loaded.value.model_copy(update={"task_tiers": overrides})
    settings.save(AI_PROVIDER, changed, expected_version=loaded.version)


# --- summarize_job: the executor and the gate agree (already read the setting; pinned) -----------


@pytest.mark.parametrize("override", [None, "thorough"], ids=["default", "moved"])
def test_moving_task_to_other_tier_used_by_executor_and_budget_gate(
    tmp_path: Path, override: str | None
) -> None:
    """The plan the budget gate is asked about, the model the executor asks the provider for and
    the tier the usage is counted on are one tier: the override, else the default."""
    tier = override or "fast"
    with importing(
        tmp_path, provider=provider(**({"summarize_job": override} if override else {}))
    ) as run:
        answer(run)
        job_id = seed(run)
        assert run.app.state.job_summary.plan_start(job_id).tier == tier
        start(run, job_id)
        (request,) = requests_of(run, "summarize_job")
        assert request.model == MODEL[tier], "the executor follows the override"
        assert tiers_used(run, "summarize_job") == [tier], "and so does the usage record"


@pytest.mark.parametrize("override", [None, "thorough"], ids=["default", "moved"])
def test_moving_task_to_other_tier_is_what_the_budget_gate_checks(
    tmp_path: Path, override: str | None
) -> None:
    """With the Thorough cap used up, a task moved to Thorough is stopped at the gate (Run once
    on Fast is offered, nothing is queued) and the same task on its default Fast is not."""
    with importing(
        tmp_path, provider=provider(**({"summarize_job": override} if override else {}))
    ) as run:
        answer(run)
        job_id = seed(run)
        cap_thorough(run, task="summarize_job")
        response = run.client.post(
            f"/inbox/jobs/{job_id}/summarize", headers=run.run.action(**HTMX)
        )
        assert response.status_code == 200, response.status_code
        queued = run.rows("SELECT id FROM tasks WHERE type='summarize_job'")
        if override:
            assert "Run once on Fast" in said(response)
            assert queued == []
        else:
            assert "Run once on Fast" not in said(response)
            assert len(queued) == 1
            run.wait_terminal(queued[0]["id"])


# --- extract_cv (TD-165) -----------------------------------------------------------------------


@pytest.mark.parametrize("override", [None, "fast"], ids=["default", "moved"])
def test_extract_cv_uses_task_tier_override(tmp_path: Path, override: str | None) -> None:
    """The plan a start is counted on (the budget gate asks about it), the model of the one
    request and the usage record are one tier: the override, else Thorough."""
    tier = override or "thorough"
    with importing(
        tmp_path, provider=provider(**({"extract_cv": override} if override else {}))
    ) as run:
        run.hold.hold = True  # the import is stored and its task is in flight while we look
        response = run.paste(CV_TEXT)
        assert response.status_code in ACCEPTED, response.status_code
        run.wait_entered(1)
        import_id = run.only_import()["id"]

        assert run.app.state.cv_import.plan_start(import_id).tier == tier
        (request,) = requests_of(run, "extract_cv")
        assert request.model == MODEL[tier]

        run.hold.open()
        run.wait_terminal(run.only_task().id)
        assert tiers_used(run, "extract_cv") == [tier]


@pytest.mark.parametrize("override", [None, "fast"], ids=["default", "moved"])
def test_extract_cv_budget_gate_follows_the_task_tier(tmp_path: Path, override: str | None) -> None:
    """With the Thorough cap used up, a CV import moved to Fast starts and the default Thorough
    one is stopped before anything is queued or sent (the upload is kept to retry)."""
    with importing(
        tmp_path, provider=provider(**({"extract_cv": override} if override else {}))
    ) as run:
        cap_thorough(run, task="extract_cv")
        response = run.paste(CV_TEXT)
        if override:
            assert response.status_code in ACCEPTED, response.status_code
            run.wait_terminal(run.only_task().id)
            assert tiers_used(run, "extract_cv")[-1] == "fast"
        else:
            assert response.status_code == 409, response.status_code
            assert run.tasks() == [] and run.spy.requests == []
            run.assert_upload_kept(run.only_import()["id"])


def test_extract_cv_run_once_on_fast_still_wins_over_the_override(tmp_path: Path) -> None:
    """A one-off Fast from the budget dialog is the caller's explicit tier: it beats a Thorough
    override for that start only; the override is what the next plain start plans."""
    with importing(tmp_path, provider=provider(extract_cv="thorough")) as run:
        run.hold.hold = True
        assert run.paste(CV_TEXT).status_code in ACCEPTED
        run.wait_entered(1)
        import_id = run.only_import()["id"]
        plan = run.app.state.cv_import.plan_start
        assert plan(import_id, tier="fast").tier == "fast"
        assert plan(import_id).tier == "thorough"
        run.hold.open()
        run.wait_terminal(run.only_task().id)


# --- refine_fact -------------------------------------------------------------------------------


@pytest.mark.parametrize("override", [None, "thorough"], ids=["default", "moved"])
def test_refine_fact_uses_task_tier_override(tmp_path: Path, override: str | None) -> None:
    tier = override or "fast"
    with refining(
        tmp_path, provider=provider(**({"refine_fact": override} if override else {}))
    ) as r:
        fact = r.target()
        r.answer_with("questions_only", "ok")
        assert r.run.app.state.fact_refine.plan_start(fact.id).tier == tier
        r.started(fact.id)
        (request,) = r.requests()
        assert request.model == MODEL[tier]
        assert tiers_used(r.run, "refine_fact") == [tier]
        # A later turn of the same conversation follows it as well.
        assert r.run.app.state.fact_refine.plan_answer(fact.id, "About 1,200 a month.").tier == tier
        r.answered(fact.id, "About 1,200 invoices a month.")
        assert [q.model for q in r.requests()] == [MODEL[tier]] * 2
        assert tiers_used(r.run, "refine_fact") == [tier, tier]


@pytest.mark.parametrize("override", [None, "thorough"], ids=["default", "moved"])
def test_refine_fact_budget_gate_follows_the_task_tier(
    tmp_path: Path, override: str | None
) -> None:
    with refining(
        tmp_path, provider=provider(**({"refine_fact": override} if override else {}))
    ) as r:
        fact = r.target()
        r.answer_with("questions_only", "ok")
        cap_thorough(r.run, task="refine_fact")
        if override:
            r.start(fact.id)
            assert r.tasks() == [] and r.requests() == [], "stopped at the gate"
        else:
            r.started(fact.id)
            assert len(r.requests()) == 1


# --- sessions: questions and structured answers ------------------------------------------------


@pytest.mark.parametrize("override", [None, "thorough"], ids=["default", "moved"])
def test_session_question_and_answer_follow_the_task_tier(
    tmp_path: Path, override: str | None
) -> None:
    """``enrichment_question`` and ``structure_answer`` start without a tier of their own, so
    the override decides the model of each request and its usage record; the default stays
    Fast."""
    tier = override or "fast"
    overrides = {"enrichment_question": override, "structure_answer": override} if override else {}
    with refining(tmp_path, provider=provider(**overrides)) as desk:
        made = start_session(desk)
        (question,) = requests_of(desk.run, "enrichment_question")
        assert question.model == MODEL[tier]
        assert tiers_used(desk.run, "enrichment_question") == [tier]

        submit(desk, made, output=payload())
        (structure,) = requests_of(desk.run, "structure_answer")
        assert structure.model == MODEL[tier]
        assert tiers_used(desk.run, "structure_answer") == [tier]
        answered = current(desk, made.session_id).answers[-1]
        assert answered.structure_provider == "fake"


@pytest.mark.parametrize("override", [None, "thorough"], ids=["default", "moved"])
def test_session_next_question_follows_the_task_tier(tmp_path: Path, override: str | None) -> None:
    tier = override or "fast"
    overrides = {"enrichment_question": override} if override else {}
    with refining(tmp_path, provider=provider(**overrides)) as desk:
        seed_note(desk)
        desk.typed("Planned fictional garden routes.")
        made = start_session(desk, SessionFocus())
        view = current(desk, made.session_id)
        first = view.answers[0]
        sessions_service(desk).outcome(
            made.session_id,
            first.id,
            "later",
            expected_version=first.version,
            session_version=view.session.version,
            later_choice="next_session",
        )
        ref = sessions_service(desk).next(
            made.session_id, expected_version=current(desk, made.session_id).session.version
        )
        assert desk.run.wait_terminal(ref.task.id).status.value == "succeeded"
        assert [r.model for r in requests_of(desk.run, "enrichment_question")] == [MODEL[tier]] * 2
        assert tiers_used(desk.run, "enrichment_question") == [tier, tier]


@pytest.mark.parametrize("override", [None, "thorough"], ids=["default", "moved"])
def test_session_start_is_checked_on_the_task_tier(tmp_path: Path, override: str | None) -> None:
    """With the Thorough cap used up, a session question moved to Thorough is refused at the gate
    (no session task is queued) and the default Fast one starts."""
    overrides = {"enrichment_question": override} if override else {}
    with refining(tmp_path, provider=provider(**overrides)) as desk:
        cap_thorough(desk.run, task="enrichment_question")
        focus = seed_note(desk)
        result = sessions_service(desk).start(focus, 3)
        if override:
            assert result.refusal is not None and result.task is None
            assert desk.run.spy.requests == []
        else:
            assert result.refusal is None and result.task is not None
            desk.run.wait_terminal(result.task.id)


def test_session_start_route_without_a_tier_follows_the_override_and_fast_still_wins(
    tmp_path: Path,
) -> None:
    with refining(tmp_path, provider=provider(enrichment_question="thorough")) as desk:
        run = desk.run
        focus = seed_note(desk)
        form = {"focus": f"{focus.kind}:{focus.ref}", "length": "3"}
        sent = run.client.post("/sessions", data=form, headers=run.run.action())
        assert sent.status_code == 303, (sent.status_code, sent.text[:200])
        (queued,) = run.rows("SELECT id FROM tasks WHERE type='enrichment_question'")
        run.wait_terminal(queued["id"])
        assert [r.model for r in requests_of(run, "enrichment_question")] == [MODEL["thorough"]]


# --- the debrief's drafts are structure_answer requests ---------------------------------------


@pytest.mark.parametrize("override", [None, "thorough", "fast"], ids=["default", "up", "pinned"])
def test_debrief_drafts_plan_and_run_on_the_structure_answer_tier(
    tmp_path: Path, override: str | None
) -> None:
    with refining(tmp_path) as r:
        d = Desk(r)
        d.save(experiences=PARAGRAPH_ONE)
        if override:
            set_task_tiers(r.run, {"structure_answer": override})
        from app.domain.tiers import task_tier

        settings = SettingsService(d.db, r.run.clock).load(AI_PROVIDER).value
        expected = task_tier("structure_answer", settings)
        assert expected == (override or "fast")

        plan = r.run.app.state.debrief.plan_start(d.aid, d.event_id)
        d.drafted()

        assert plan.tier == expected
        assert set(tiers_used(r.run, DEBRIEF_TASK)) == {expected}
        assert {q.model for q in requests_of(r.run, DEBRIEF_TASK)} == {MODEL[expected]}
