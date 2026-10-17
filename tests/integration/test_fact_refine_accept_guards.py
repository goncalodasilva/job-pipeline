"""Refine with AI: what Accept, Start and Answer refuse once the conversation has moved on
(M3.8 review fixes; AC-M3.4, AC-M3.5; ADR-089 items 5 and 6, ADR-090 item 10).

- Accept refuses (409) when a fact sent with the conversation became "never use", or when
  the fact changed after the suggestion was made; the fact is left as it is.
- Accept while a turn is queued or running cancels that turn, as Discard does; a turn that
  still ends after the conversation closed fails without writing anything.
- Start on a conversation that already had a turn, and an answer with no question asked,
  are refused (409) without a request.
- A name only a "generalize names" fact gives is flagged, while its numbers are allowed.

The routes are described in ``tests/refine_harness.py``. Real SQLite under ``tmp_path``, the
real runner, the fake provider behind the spy; no network, no fixed waits; every value is
fictional.
"""

from pathlib import Path
from typing import Any

import pytest

from app.domain.errors import message_for_kind
from app.modules.ai.providers.fake import ScriptedResponse
from app.modules.storage import refine_repo
from tests.cv_import_harness import said
from tests.refine_harness import ACCEPTED, REFINE, TARGET, Refining, claim_texts, refining

pytestmark = pytest.mark.integration

OK_SUGGESTION = "Reduced invoice processing time by 35% across 4 warehouses."
USES_ANSWER = (
    "Cut invoice processing time by 35% across 4 warehouses, handling about 1200 invoices a month."
)
EDITED = "Cut invoice processing time by 25% across 4 warehouses."
GENERALIZED = "Ran the Fernhill Bank payments migration for 1200 accounts."
FOR_THE_BANK = "Cut invoice processing time by 35% across 4 warehouses for Fernhill Bank."


def suggesting(r: Refining, text: str, *questions: str) -> None:
    """The refine turns from now on suggest ``text`` with these questions."""
    data = {
        "questions": list(questions),
        "suggestion": {"text": text, "situation": None, "action": None, "result": None},
        "added_details": [],
    }
    r.run.spy.script(REFINE, ScriptedResponse(data=data))


def held_second_turn(r: Refining, fact: Any) -> Any:
    """A conversation with a suggestion and a question, then an answer whose turn is held
    in its provider call. Returns that turn's task row."""
    r.answer_with("ok")
    first = r.started(fact.id)
    r.run.hold.hold = True
    assert r.answer(fact.id, "About 1,200 invoices a month.").status_code in ACCEPTED
    r.run.wait_entered(2)
    (second,) = [row for row in r.tasks() if row.id != first.id]
    return second


# --- Accept checks the conversation is still current -------------------------------------------


def test_accept_refused_when_a_context_fact_became_never_use(tmp_path: Path) -> None:
    with refining(tmp_path) as r:
        fact = r.target()
        context = r.typed("Handled about 1200 invoices a month at the main warehouse.")
        r.answer_with("uses_answer")
        r.started(fact.id)
        assert claim_texts(r.open_session(fact.id)["flagged_claims"]) == []
        r.facts.set_privacy(context.id, "never_use", expected_version=context.version)
        before = r.fact_tables()

        refused = r.accept(fact.id, text=USES_ANSWER)

        assert refused.status_code == 409, refused.status_code
        assert message_for_kind("RefineContextChanged") in said(refused)
        assert r.fact_tables() == before
        assert r.current(fact.id).content.text == TARGET
        assert r.open_session(fact.id)["state"] == "open"


def test_accept_of_a_suggestion_made_before_the_fact_changed_refused_409(tmp_path: Path) -> None:
    from app.services.facts import FactInput

    with refining(tmp_path) as r:
        fact = r.target()
        r.answer_with("ok")
        r.started(fact.id)
        edited = r.facts.edit(
            fact.id, FactInput(text=EDITED, role_id=fact.role_id), expected_version=fact.version
        )
        assert edited.content.text == EDITED
        before = r.fact_tables()

        # The version posted is the current one: only the suggestion is out of date.
        stale = r.accept(fact.id, text=OK_SUGGESTION, version=edited.version)

        assert stale.status_code == 409, stale.status_code
        assert message_for_kind("VersionConflict") in said(stale)
        assert r.fact_tables() == before
        assert r.current(fact.id).content.text == EDITED
        assert r.open_session(fact.id)["state"] == "open"


# --- Accept while a turn is on its way ---------------------------------------------------------


def test_accept_cancels_a_turn_still_on_its_way(tmp_path: Path) -> None:
    with refining(tmp_path) as r:
        fact = r.target()
        second = held_second_turn(r, fact)

        accepted = r.accept(fact.id, text=OK_SUGGESTION)

        assert accepted.status_code in ACCEPTED, (accepted.status_code, accepted.text[:300])
        r.run.hold.open()
        done = r.run.wait_terminal(second.id)
        assert done.status.value == "cancelled", (done.status.value, done.error)
        assert done.result_ref is None
        (closed,) = r.sessions(fact.id)
        assert closed["state"] == "accepted"
        assert r.versions(fact.id)[-1]["text"] == OK_SUGGESTION


def test_turn_ending_after_the_conversation_closed_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The writer's own check: a turn the cancel didn't reach fails as RefineNotOpen and
    leaves the closed conversation as it was."""
    with refining(tmp_path) as r:
        fact = r.target()
        second = held_second_turn(r, fact)
        tasks = r.run.app.state.task_service
        monkeypatch.setattr(tasks, "request_cancel", lambda _task_id: None)
        assert r.accept(fact.id, text=OK_SUGGESTION).status_code in ACCEPTED
        (closed,) = r.sessions(fact.id)
        versions = r.versions(fact.id)

        r.run.hold.open()
        done = r.run.wait_terminal(second.id)

        assert done.status.value == "failed" and done.error == "RefineNotOpen"
        assert done.result_ref is None
        assert r.sessions(fact.id) == [closed]
        assert r.versions(fact.id) == versions


# --- Start and Answer -------------------------------------------------------------------------


def test_start_again_after_a_turn_ran_refused_409_without_a_request(tmp_path: Path) -> None:
    with refining(tmp_path) as r:
        fact = r.target()
        r.answer_with("questions_only")
        r.started(fact.id)
        session = r.open_session(fact.id)

        for htmx in (False, True):
            refused = r.post(f"/facts/{fact.id}/refine", htmx=htmx)

            assert refused.status_code == 409, refused.status_code
        assert len(r.tasks()) == 1 and len(r.requests()) == 1
        assert r.open_session(fact.id) == session


def test_answer_before_any_question_refused_409(tmp_path: Path) -> None:
    with refining(tmp_path) as r:
        fact = r.target()
        r.answer_with("malformed")
        r.started(fact.id, status="failed")
        session = r.open_session(fact.id)
        asked = len(r.requests())

        refused = r.answer(fact.id, "About 1,200 invoices a month.")

        assert refused.status_code == 409, refused.status_code
        assert len(r.tasks()) == 1 and len(r.requests()) == asked
        assert r.open_session(fact.id) == session


@pytest.mark.parametrize("step", ["set_answers", "close_session"])
def test_a_conversation_write_that_matches_nothing_is_a_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, step: str
) -> None:
    """A conditional update of the conversation that changes no row is refused (409) and
    rolls back the whole step, never reported as done."""
    with refining(tmp_path) as r:
        fact = r.target()
        r.answer_with("ok")
        r.started(fact.id)
        session = r.open_session(fact.id)
        before = r.fact_tables()
        monkeypatch.setattr(refine_repo, step, lambda *_args, **_kwargs: False)

        if step == "set_answers":
            responses = [r.answer(fact.id, "About 1,200 invoices a month.")]
        else:
            responses = [r.accept(fact.id, text=OK_SUGGESTION), r.discard(fact.id)]

        for response in responses:
            assert response.status_code == 409, response.status_code
            assert message_for_kind("VersionConflict") in said(response)
        assert len(r.tasks()) == 1
        assert r.open_session(fact.id) == session
        assert r.fact_tables() == before


# --- generalize names ---------------------------------------------------------------------------


def test_name_only_a_generalized_fact_gives_is_flagged(tmp_path: Path) -> None:
    with refining(tmp_path) as r:
        fact = r.target()
        r.typed(GENERALIZED, privacy="generalize")
        suggesting(r, FOR_THE_BANK)
        r.started(fact.id)

        flagged = claim_texts(r.open_session(fact.id)["flagged_claims"])

        assert flagged == ["Fernhill Bank"], flagged
        before = r.fact_tables()
        assert r.accept(fact.id, text=FOR_THE_BANK).status_code == 422
        assert r.fact_tables() == before
        # Its numbers are still the user's own: only the name is withheld.
        with_count = f"{OK_SUGGESTION[:-1]}, on 1200 accounts."
        assert r.accept(fact.id, text=with_count).status_code in ACCEPTED
        assert r.versions(fact.id)[-1]["text"] == with_count


# --- second review round ---------------------------------------------------------------------


def test_answer_when_the_last_turn_asked_nothing_refused_409(tmp_path: Path) -> None:
    with refining(tmp_path) as r:
        fact = r.target()
        r.answer_with("uses_answer")  # a suggestion, and no question
        r.started(fact.id)
        session = r.open_session(fact.id)
        asked = len(r.requests())

        refused = r.answer(fact.id, "About 1,200 invoices a month.")

        assert refused.status_code == 409, refused.status_code
        assert message_for_kind("RefineBusy") in said(refused)
        assert len(r.tasks()) == 1 and len(r.requests()) == asked
        assert r.open_session(fact.id) == session


def test_accept_refused_when_a_context_fact_became_generalize(tmp_path: Path) -> None:
    with refining(tmp_path) as r:
        fact = r.target()
        context = r.typed("Handled about 1200 invoices a month for Fernhill Bank.")
        suggesting(r, FOR_THE_BANK)
        r.started(fact.id)
        assert claim_texts(r.open_session(fact.id)["flagged_claims"]) == []
        r.facts.set_privacy(context.id, "generalize", expected_version=context.version)
        before = r.fact_tables()

        refused = r.accept(fact.id, text=FOR_THE_BANK)

        assert refused.status_code == 409, refused.status_code
        assert message_for_kind("RefineContextChanged") in said(refused)
        assert r.fact_tables() == before
        assert r.current(fact.id).content.text == TARGET
        assert r.open_session(fact.id)["state"] == "open"
