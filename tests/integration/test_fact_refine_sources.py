"""Acceptance uses the sources of the suggestion's request (ADR-089 item 5)."""

from pathlib import Path

import pytest

from tests.refine_harness import ACCEPTED, refining

pytestmark = pytest.mark.integration

WORDING = "Cut invoice processing time by 35% across 4 warehouses, handling 1200 invoices."


def test_accept_does_not_gain_sources_created_after_the_request(tmp_path: Path) -> None:
    with refining(tmp_path) as r:
        fact = r.target()
        r.answer_with("ok")
        r.started(fact.id)
        r.typed("Handled 1200 invoices.")
        before = r.fact_tables()

        refused = r.accept(fact.id, text=WORDING)

        assert refused.status_code == 422
        assert "1200" in refused.text
        assert r.fact_tables() == before
        assert r.open_session(fact.id)["state"] == "open"


def test_accept_keeps_sources_sent_even_if_the_context_is_later_archived(tmp_path: Path) -> None:
    with refining(tmp_path) as r:
        fact = r.target()
        context = r.typed("Handled 1200 invoices.")
        r.answer_with("ok")
        r.started(fact.id)
        r.facts.archive(context.id, expected_version=context.version)

        accepted = r.accept(fact.id, text=WORDING)

        assert accepted.status_code in ACCEPTED
        assert r.versions(fact.id)[-1]["text"] == WORDING


def test_budget_warning_keeps_the_answer_until_the_user_continues(tmp_path: Path) -> None:
    from tests.integration.test_budget_warning_dialog import (
        CONTINUE,
        control,
        crossing,
        submitted_by,
        the_dialog,
    )
    from tests.refine_harness import refine_path

    with refining(tmp_path) as r:
        fact = r.target()
        r.answer_with("questions_only")
        r.started(fact.id)
        crossing(r.run, 9)
        answer = "We handled 1200 invoices a month."

        warning = r.post(refine_path(fact.id, "answer"), {"answer": answer}, htmx=True)

        elements, dialog = the_dialog(warning)
        path, fields = submitted_by(elements, control(elements, dialog, CONTINUE))
        assert path == refine_path(fact.id, "answer") and fields["answer"] == answer
        assert r.open_session(fact.id)["answer_rounds"] == 0
        assert len(r.requests()) == 1
        r.ended_turn(lambda: r.post(path, fields, htmx=True))
        assert r.open_session(fact.id)["answer_rounds"] == 1
        assert len(r.requests()) == 2
