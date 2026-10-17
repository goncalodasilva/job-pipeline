"""Refine with AI: which facts can be refined, and the htmx answers (M3.8; AC-M3.5;
ADR-089 item 6). A draft or a confirmed fact can be refined; a discarded or an archived fact
is refused with its reason and no request. A draft's wording is sent as untrusted, never as
a confirmed fact. The routes are described in ``tests/refine_harness.py``; every value is
fictional."""

from pathlib import Path

import pytest

from tests.cv_import_harness import said
from tests.refine_harness import ACCEPTED, CONTEXT, refine_path, refining

pytestmark = pytest.mark.integration

OK_SUGGESTION = "Reduced invoice processing time by 35% across 4 warehouses."
DRAFT = "Draft line about the Larkspur depot."


def test_discarded_and_archived_facts_refused_409_without_a_request(tmp_path: Path) -> None:
    with refining(tmp_path) as r:
        (dropped,) = r.drafts("Discarded line about Corran Quay.")
        r.facts.discard(dropped.id, expected_version=dropped.version)
        archived = r.typed("Archived line about the Mapleford hub.")
        r.facts.archive(archived.id, expected_version=archived.version)
        before = r.fact_tables()

        for fact_id in (dropped.id, archived.id):
            for htmx in (False, True):
                refused = r.post(refine_path(fact_id), htmx=htmx)

                assert refused.status_code == 409, refused.status_code
                assert "restore" in said(refused).casefold()
            assert r.sessions(fact_id) == []
        assert r.tasks() == [] and r.requests() == []
        assert r.fact_tables() == before


def test_a_draft_can_be_refined_and_its_wording_is_untrusted(tmp_path: Path) -> None:
    with refining(tmp_path) as r:
        (draft,) = r.drafts(DRAFT)
        r.typed(CONTEXT)
        r.answer_with("questions_only")

        r.started(draft.id)

        (request,) = r.requests()
        at = request.user.index(DRAFT)
        opened = request.user.rindex("<untrusted_document", 0, at)
        assert "</untrusted_document>" not in request.user[opened:at]
        assert DRAFT not in request.system and CONTEXT in request.user
        assert r.current(draft.id).status == "draft"


def test_htmx_answers_are_the_refine_fragment(tmp_path: Path) -> None:
    with refining(tmp_path) as r:
        fact = r.target()
        r.answer_with("questions_only", "ok")

        started, _task = r.ended_turn(lambda: r.post(refine_path(fact.id), htmx=True))
        assert started.status_code == 200
        assert 'id="fact-refine"' in started.text and "announce" in started.headers["HX-Trigger"]

        answered, _task = r.ended_turn(
            lambda: r.post(refine_path(fact.id, "answer"), {"answer": "About 1,200."}, htmx=True)
        )
        assert answered.status_code == 200
        shown = r.client.post(
            refine_path(fact.id, "accept"),
            data={"version": "1", "text": f"{OK_SUGGESTION[:-1]} at Globex Corporation."},
            headers=r.run.run.action(**{"HX-Request": "true"}),
        )
        assert shown.status_code == 422
        assert "Globex Corporation" in said(shown) and 'id="fact-refine-claims"' in shown.text
        assert OK_SUGGESTION in said(shown), "the suggestion is shown"

        accepted = r.post(
            refine_path(fact.id, "accept"), {"version": "1", "text": OK_SUGGESTION}, htmx=True
        )
        assert accepted.status_code == 200
        assert r.versions(fact.id)[-1]["text"] == OK_SUGGESTION

        r.started(fact.id)
        discarded = r.post(refine_path(fact.id, "discard"), htmx=True)
        assert discarded.status_code == 200 and "unchanged" in said(discarded)
        assert r.post(refine_path(fact.id, "discard"), htmx=True).status_code == 409


def test_blank_or_unchanged_wording_on_accept(tmp_path: Path) -> None:
    with refining(tmp_path) as r:
        fact = r.target()
        r.answer_with("ok")
        r.started(fact.id)

        assert r.accept(fact.id, text="   ").status_code == 422
        unchanged = r.accept(fact.id, text=r.current(fact.id).content.text)

        assert unchanged.status_code in ACCEPTED
        assert len(r.versions(fact.id)) == 1, "the same wording adds no version"
        assert [row["state"] for row in r.sessions(fact.id)] == ["accepted"]
