"""Refine with AI: limits, refusals, security and provenance around the conversation (M3.8;
AC-M3.5; spec 2.9, 2.10, 3.1 item 5, 3.7; ADR-086, ADR-088, ADR-089 items 5, 6 and 10,
ADR-090 item 10; background-task skill section 8).

The routes are described in ``tests/refine_harness.py``. Real SQLite under ``tmp_path``, the
real runner, the fake provider behind the spy; no network, no fixed waits; every value is
fictional.
"""

import logging
from pathlib import Path

import pytest

from tests.cv_import_harness import said
from tests.integration.test_budget_warning_dialog import blocked_message, crossing, set_budget
from tests.integration.test_setup_redirect import ORIGIN, TOKEN_HEADER
from tests.refine_harness import (
    ACCEPTED,
    CONTEXT,
    REFINE,
    TARGET,
    claim_texts,
    refine_path,
    refining,
)

pytestmark = pytest.mark.integration

OK_SUGGESTION = "Reduced invoice processing time by 35% across 4 warehouses."
INVENTED = "Cut invoice processing time by 85% across 14 warehouses at Globex Corporation."
SELF_REPORTED = (
    "Cut invoice processing time by 35% across 4 warehouses, saving the finance team a week "
    "each month."
)
ADDED_DETAIL = "saving the finance team a week each month"
USES_ANSWER = (
    "Cut invoice processing time by 35% across 4 warehouses, handling about 1200 invoices a month."
)
ANSWER = "It was about 1,200 invoices a month."


def test_refine_routes_need_token_and_origin(tmp_path: Path) -> None:
    with refining(tmp_path) as r:
        fact = r.target()
        token = r.run.run.token
        refused = {
            "no token, no origin": {},
            "origin only": {"origin": ORIGIN},
            "token only": {TOKEN_HEADER: token},
            "wrong token": {TOKEN_HEADER: "x" * len(token), "origin": ORIGIN},
            "another site's origin": {TOKEN_HEADER: token, "origin": "http://evil.example"},
            "origin null": {TOKEN_HEADER: token, "origin": "null"},
        }
        paths = [refine_path(fact.id, action) for action in ("", "answer", "accept", "discard")]
        before = r.fact_tables()

        for label, headers in refused.items():
            for path in paths:
                data = {"answer": "About 1,200.", "text": INVENTED, "version": "1"}
                response = r.client.post(path, data=data, headers=headers)
                assert response.status_code == 403, (label, path)
                assert token not in response.text

        assert r.tasks() == [] and r.requests() == [] and r.sessions(fact.id) == []
        assert r.fact_tables() == before
        # None of them is a page: a GET never reaches them.
        for path in paths:
            got = r.client.get(path, headers=r.run.run.with_token())
            assert got.status_code in (404, 405), path
        # The same start with both is served: the refusals above were the checks'.
        r.answer_with("ok")
        r.started(fact.id)
        assert len(r.requests()) == 1


@pytest.mark.parametrize("fact_id", ["no-such-fact", "no%20such%20fact", "a" * 300])
def test_unknown_or_unsafe_fact_id_is_404_without_echo(tmp_path: Path, fact_id: str) -> None:
    with refining(tmp_path) as r:
        for action in ("", "answer", "accept", "discard"):
            response = r.post(refine_path(fact_id, action), {"answer": "x", "text": "x"})

            assert response.status_code == 404, (action, response.status_code)
            assert fact_id not in response.text and "no such fact" not in response.text
        assert r.tasks() == [] and r.requests() == []
        # A real fact's id is served, so the 404s above are the routes' own answers.
        fact = r.target()
        r.answer_with("ok")
        r.started(fact.id)


def test_fourth_answer_refused_409_and_no_request(tmp_path: Path) -> None:
    with refining(tmp_path) as r:
        fact = r.target()
        r.answer_with("questions_only")
        r.started(fact.id)
        for round_number in (1, 2, 3):
            r.answered(fact.id, f"Answer number {round_number}.")
        assert r.open_session(fact.id)["answer_rounds"] == 3
        asked = len(r.requests())

        refused = r.answer(fact.id, "Answer number 4.")

        assert refused.status_code == 409, refused.status_code
        assert len(r.requests()) == asked == 4
        assert len(r.tasks()) == 4
        assert r.open_session(fact.id)["answer_rounds"] == 3


def test_blank_answer_refused_422(tmp_path: Path) -> None:
    with refining(tmp_path) as r:
        fact = r.target()
        r.answer_with("questions_only")
        r.started(fact.id)

        for blank in ("", "   ", "\n\t"):
            assert r.answer(fact.id, blank).status_code == 422
        assert len(r.tasks()) == 1 and r.open_session(fact.id)["answer_rounds"] == 0


def test_answer_without_an_open_conversation_refused_409(tmp_path: Path) -> None:
    with refining(tmp_path) as r:
        fact = r.target()

        assert r.answer(fact.id, ANSWER).status_code == 409
        assert r.accept(fact.id, text=OK_SUGGESTION).status_code == 409
        assert r.tasks() == [] and r.requests() == [] and r.sessions(fact.id) == []


def test_edited_out_claim_accepted_without_acknowledging(tmp_path: Path) -> None:
    with refining(tmp_path) as r:
        fact = r.target()
        r.answer_with("adversarial_invented_claims")
        r.started(fact.id)
        assert claim_texts(r.open_session(fact.id)["flagged_claims"])

        response = r.accept(fact.id, text=OK_SUGGESTION)

        assert response.status_code in ACCEPTED, (response.status_code, response.text[:300])
        saved = r.versions(fact.id)[-1]
        assert (saved["version"], saved["text"], saved["source"]) == (2, OK_SUGGESTION, "refine")
        (closed,) = r.sessions(fact.id)
        assert closed["state"] == "accepted"
        assert claim_texts(closed["acknowledged_claims"]) == []


def test_server_rechecks_the_posted_wording(tmp_path: Path) -> None:
    """The flags of the stored suggestion aren't the whole check: what is posted is checked
    again, so a claim typed into the wording (or a forged form) needs acknowledging too."""
    with refining(tmp_path) as r:
        fact = r.target()
        r.answer_with("ok")
        r.started(fact.id)
        assert claim_texts(r.open_session(fact.id)["flagged_claims"]) == []
        typed = f"{OK_SUGGESTION[:-1]}, saving €3,000 a year."
        before = r.fact_tables()

        refused = r.accept(fact.id, text=typed)

        assert refused.status_code == 422, refused.status_code
        assert "€3,000" in said(refused)
        assert r.fact_tables() == before

        saved = r.accept(fact.id, text=typed, acknowledged=["€3,000"])

        assert saved.status_code in ACCEPTED, (saved.status_code, saved.text[:300])
        assert r.versions(fact.id)[-1]["text"] == typed
        assert claim_texts(r.sessions(fact.id)[-1]["acknowledged_claims"]) == ["€3,000"]


def test_self_reported_added_detail_flagged_until_acknowledged(tmp_path: Path) -> None:
    with refining(tmp_path) as r:
        fact = r.target()
        r.answer_with("adversarial_self_reported")
        r.started(fact.id)

        flagged = claim_texts(r.open_session(fact.id)["flagged_claims"])

        assert flagged == [ADDED_DETAIL], "lower-case, no number: only the model's own report"
        assert r.accept(fact.id, text=SELF_REPORTED).status_code == 422
        saved = r.accept(fact.id, text=SELF_REPORTED, acknowledged=[ADDED_DETAIL])
        assert saved.status_code in ACCEPTED
        assert r.versions(fact.id)[-1]["text"] == SELF_REPORTED


def test_number_from_users_answer_not_flagged(tmp_path: Path) -> None:
    with refining(tmp_path) as r:
        fact = r.target()
        r.answer_with("questions_only", "uses_answer")
        r.started(fact.id)

        r.answered(fact.id, ANSWER)

        session = r.open_session(fact.id)
        assert USES_ANSWER in (session["suggestion"] or "")
        assert claim_texts(session["flagged_claims"]) == []
        assert session["answer_rounds"] == 1
        assert r.accept(fact.id, text=USES_ANSWER).status_code in ACCEPTED
        assert r.versions(fact.id)[-1]["text"] == USES_ANSWER


def test_number_from_a_confirmed_fact_sent_not_flagged(tmp_path: Path) -> None:
    """The confirmed facts sent as context are allowed sources (ADR-089 item 5)."""
    with refining(tmp_path) as r:
        fact = r.target()
        r.typed("Handled about 1,200 invoices a month at the main warehouse.")
        r.answer_with("uses_answer")

        r.started(fact.id)

        assert claim_texts(r.open_session(fact.id)["flagged_claims"]) == []


def test_accept_with_stale_version_refused_409_and_saves_nothing(tmp_path: Path) -> None:
    with refining(tmp_path) as r:
        fact = r.target()
        r.answer_with("ok")
        r.started(fact.id)
        before = r.fact_tables()

        stale = r.accept(fact.id, text=OK_SUGGESTION, version=r.current(fact.id).version + 5)

        assert stale.status_code == 409, stale.status_code
        assert r.fact_tables() == before
        assert r.open_session(fact.id)["state"] == "open"


def test_accept_without_a_suggestion_refused_409(tmp_path: Path) -> None:
    with refining(tmp_path) as r:
        fact = r.target()
        r.answer_with("questions_only")
        r.started(fact.id)
        before = r.fact_tables()

        refused = r.accept(fact.id, text=OK_SUGGESTION)

        assert refused.status_code == 409, refused.status_code
        assert r.fact_tables() == before


def test_discard_then_start_again_opens_a_new_conversation(tmp_path: Path) -> None:
    with refining(tmp_path) as r:
        fact = r.target()
        r.answer_with("ok")
        r.started(fact.id)

        assert r.discard(fact.id).status_code in ACCEPTED
        assert r.discard(fact.id).status_code == 409, "nothing open to discard"
        r.started(fact.id)

        assert [row["state"] for row in r.sessions(fact.id)] == ["discarded", "open"]
        assert len(r.versions(fact.id)) == 1


def test_duplicate_start_and_answer_while_a_turn_runs(tmp_path: Path) -> None:
    with refining(tmp_path) as r:
        fact = r.target()
        r.answer_with("ok")
        r.run.hold.hold = True
        first = r.start(fact.id)
        assert first.status_code in ACCEPTED
        r.run.wait_entered(1)

        again = r.start(fact.id)
        busy = r.answer(fact.id, ANSWER)

        assert again.status_code in ACCEPTED, "the running turn is the answer"
        assert busy.status_code == 409, "one turn at a time"
        assert len(r.tasks()) == 1
        assert len(r.sessions(fact.id)) == 1
        r.run.hold.open()
        (task,) = r.tasks()
        assert str(r.run.wait_terminal(task.id).status.value) == "succeeded"
        assert len(r.requests()) == 1


def test_refine_start_blocked_by_budget_makes_no_request(tmp_path: Path) -> None:
    with refining(tmp_path) as r:
        fact = r.target()
        crossing(r.run, 30)  # the blocking point of the test budget

        blocked = r.start(fact.id)

        assert blocked.status_code == 409, blocked.status_code
        assert blocked_message() in said(blocked)
        assert r.tasks() == [] and r.requests() == [] and r.sessions(fact.id) == []

        set_budget(r.run, None)
        r.answer_with("ok")
        r.started(fact.id)
        usage = r.run.rows("SELECT * FROM usage_records WHERE task_type = ?", (REFINE,))
        assert len(usage) == len(r.requests()) == 1, "each call counted on the saved service"


def test_wired_fake_answers_refine_with_practice_provenance(tmp_path: Path) -> None:
    """ADR-088, ADR-089 item 8: the saved fake answers the real task (wiring scripts it), and
    what it answered is labelled as the fake's."""
    with refining(tmp_path) as r:
        fact = r.target()

        r.started(fact.id)

        session = r.open_session(fact.id)
        assert session["provider"] == "fake"
        assert str(session["model"]).startswith("fake")
        assert (session["prompt_id"], session["prompt_version"]) == (REFINE, "3")


def test_answer_and_fact_text_not_in_task_rows_usage_or_logs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    private_answer = "The Pellucid Ledger count was 4,410 invoices."
    with refining(tmp_path) as r:
        fact = r.target()
        r.typed(CONTEXT)
        r.answer_with("questions_only")
        r.started(fact.id)
        r.answered(fact.id, private_answer)
        assert private_answer in r.open_session(fact.id)["turns"], "kept with the conversation"

        task_rows = r.run.rows("SELECT * FROM tasks WHERE type = ?", (REFINE,))
        usage_rows = r.run.rows("SELECT * FROM usage_records WHERE task_type = ?", (REFINE,))
        stored = repr(task_rows) + repr(usage_rows)
        for text in ("Pellucid", "4,410", TARGET, "invoice processing", CONTEXT):
            assert text not in stored, text
            assert text not in caplog.text, text


def test_failures_events_and_accept_paths_leak_no_wording_or_claims(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The scan above, extended (M3.8 review): failed turns (malformed output, a refused
    accept field) store and log only the error kind; the event stream carries ids, types
    and statuses only; and neither a refused nor a saved Accept logs the claims or the
    posted wording."""
    caplog.set_level(logging.DEBUG)
    posted = "Cut invoice processing time by 85% across 14 warehouses for the Quillon Depot."
    with refining(tmp_path) as r:
        events = r.run.app.state.task_events

        async def subscribe() -> object:
            return events.subscribe()

        stream = r.client.portal.call(subscribe)
        fact = r.target()
        r.typed(CONTEXT)
        for failing in ("malformed", "injection_accept_field"):
            r.answer_with(failing)
            failed = r.started(fact.id, status="failed")
            assert failed.error == "InvalidModelOutput"
            assert r.discard(fact.id).status_code in ACCEPTED
        r.answer_with("adversarial_invented_claims")
        r.started(fact.id)
        flagged = claim_texts(r.open_session(fact.id)["flagged_claims"])
        assert r.accept(fact.id, text=posted).status_code == 422
        saved = r.accept(fact.id, text=posted, acknowledged=[*flagged, "Quillon Depot"])
        assert saved.status_code in ACCEPTED, (saved.status_code, saved.text[:300])

        async def frames() -> bytes:
            return b"".join(stream.frames)  # type: ignore[attr-defined]

        sent = r.client.portal.call(frames).decode("utf-8")
        assert sent.count("event: task-updated") >= 9, "the scan saw the turns' frames"
        task_rows = r.run.rows("SELECT * FROM tasks WHERE type = ?", (REFINE,))
        assert {row["error"] for row in task_rows} == {"InvalidModelOutput", None}
        usage_rows = r.run.rows("SELECT * FROM usage_records WHERE task_type = ?", (REFINE,))
        stored = repr(task_rows) + repr(usage_rows)
        private = (
            TARGET,
            "invoice processing",
            CONTEXT,
            posted,
            "Quillon",
            "Globex",
            "85%",
            "14 warehouses",
            "fictional refine output",
            '"accepted"',  # the field the injected answer added
            # "14" alone could be part of an id; "14 warehouses" above stands for it.
            *(claim for claim in flagged if len(claim) > 2),
        )
        assert "Globex Corporation" in flagged and "85%" in flagged
        for text in private:
            assert text not in caplog.text, text
            assert text not in sent, text
            assert text not in stored, text
