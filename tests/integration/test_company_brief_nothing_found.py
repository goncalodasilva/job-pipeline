"""A company brief run that keeps no usable section says so in plain words (M8.3 part B).

The note is read from the latest ``company_brief`` task of the company (it succeeded and kept
nothing), so it needs no extra table, is never a stored brief, and goes away with the next run.
"""

import pytest

from tests.brief_pages import (
    ABOUT,
    TASK,
    brief_path,
    brief_ready,
    run_brief,
    scripted,
    sections_of,
)
from tests.fakes.ai import load_ai_fixture
from tests.integration.test_board import snapshot
from tests.integration.test_inbox import get
from tests.refine_harness import refining
from tests.tailoring_pages import by_id, has_id, said_text

pytestmark = pytest.mark.integration
NOTE = "Nothing with a source was found this time."
NOTHING = ("adversarial_unsearched_url", "injection_obeyed")


def test_a_run_that_keeps_nothing_shows_the_plain_note_everywhere_the_brief_shows(tmp_path):
    with refining(tmp_path) as r:
        aid, cid, service = brief_ready(r)
        assert not has_id(get(r, brief_path(cid)).text, "company-brief-nothing")
        r.run.spy.script(TASK, scripted(load_ai_fixture(TASK, NOTHING[0]).data))
        run_brief(r, service, cid)
        for page in (
            get(r, brief_path(cid)).text,
            r.client.get(f"/companies/{cid}").text,
            get(r, f"/applications/{aid}/prep").text,
        ):
            assert said_text(by_id(page, "company-brief-nothing").text) == NOTE
            assert not sections_of(page)
            assert "Get a company brief" in by_id(page, "company-brief-start").text
        assert not r.run.rows("SELECT id FROM company_briefs")


def test_the_note_is_read_only_and_goes_away_with_the_next_run(tmp_path):
    with refining(tmp_path) as r:
        _aid, cid, service = brief_ready(r)
        r.run.spy.script(TASK, scripted(load_ai_fixture(TASK, NOTHING[1]).data))
        run_brief(r, service, cid)
        before = snapshot(r)
        assert has_id(get(r, brief_path(cid)).text, "company-brief-nothing")
        assert snapshot(r) == before, "reading the note writes nothing"
        r.run.spy.script(TASK, scripted())
        run_brief(r, service, cid)
        page = get(r, brief_path(cid)).text
        assert not has_id(page, "company-brief-nothing")
        assert ABOUT in said_text(page)


def test_the_note_sits_beside_an_older_saved_brief_without_replacing_it(tmp_path):
    with refining(tmp_path) as r:
        _aid, cid, service = brief_ready(r)
        run_brief(r, service, cid)
        r.run.spy.script(TASK, scripted(load_ai_fixture(TASK, NOTHING[0]).data))
        run_brief(r, service, cid)
        page = get(r, brief_path(cid)).text
        assert said_text(by_id(page, "company-brief-nothing").text) == NOTE
        assert (
            ABOUT in said_text(page)
            and "Refresh the brief" in by_id(page, "company-brief-start").text
        )
        assert len(r.run.rows("SELECT id FROM company_briefs")) == 1


def test_a_failed_run_does_not_claim_that_nothing_was_found(tmp_path):
    with refining(tmp_path) as r:
        _aid, cid, service = brief_ready(r)
        r.run.spy.script(TASK, load_ai_fixture(TASK, "malformed"))
        run_brief(r, service, cid, status="failed")
        assert not has_id(get(r, brief_path(cid)).text, "company-brief-nothing")


@pytest.mark.parametrize("storage", [None, False])
def test_a_held_brief_is_not_the_nothing_found_case(tmp_path, storage):
    with refining(tmp_path) as r:
        _aid, cid, service = brief_ready(r, storage)
        run_brief(r, service, cid)
        page = get(r, brief_path(cid)).text
        assert not has_id(page, "company-brief-nothing") and ABOUT in said_text(page)
