"""M6.9 review follow-ups for the cover letter (AC-M6.1, AC-M6.2 for letters; spec 3.1, 3.6):
what the checker really does with embellishment, a reported detail, a job title that carries
more names, and a tone or length outside the sets at run time. Assumed interface: see the
docstring of ``tests/letter_pages.py``. Real SQLite in a temp folder, the fake provider behind
the spy, no network, no fixed waits."""

import asyncio

import pytest

from app.services.documents import request_text
from app.services.tailoring_ai import TailoringJob, run_draft_cover_letter
from tests.letter_pages import COMPANY, DRAFT_COVER_LETTER, P1, P2, LetterDesk, letter_app

pytestmark = pytest.mark.integration

EMBELLISHED = "As a senior and experienced planner I led a large global team and cut costs."
ATTRIBUTED = "I delivered this work for Harbor Kite Fictional, where I managed 12 vans."


@pytest.fixture
def d(tmp_path):
    with letter_app(tmp_path) as desk:
        yield desk


def test_letter_embellishment_known_limit_only_numbers_are_flagged(d: LetterDesk):
    """Code finds numbers, names, skills and reported details. Seniority and scale adjectives, a
    stronger verb, and attributing the work to the job's company are the prompt's rules alone
    (the company is an allowed name in a letter), so only the invented number is flagged here.
    The person reads every word; this pins what the checker does today."""
    app = d.letter_ready()

    d.draft(app.id, "adversarial_embellishment")

    version_id = d.current_letter_id(app.id)
    assert d.texts(version_id) == [P1, EMBELLISHED, ATTRIBUTED]
    assert d.flag_triples(version_id) == [("needs_check", "12", ATTRIBUTED)]
    view = d.letters.view(app.id)
    assert {(b.kind, b.claim) for b in view.blockers} == {("needs_check", "12")}
    with pytest.raises(d.docs.ApprovalBlocked) as blocked:
        d.letters.approve(app.id, version_id)
    assert blocked.value.message == "1 paragraph needs your check."


def test_letter_added_details_surface_as_a_claim_to_check(d: LetterDesk):
    app = d.letter_ready()

    d.draft(app.id, "added_details")

    version_id = d.current_letter_id(app.id)
    detail = "the routes served hospitals"
    assert d.flag_triples(version_id) == [("needs_check", detail, P1)]
    assert d.unit_with(version_id, P1)["added_details"] == f'["{detail}"]'
    assert d.unit_with(version_id, P2)["flag_list"] == []
    view = d.letters.view(app.id)
    assert [(f.kind, f.claim) for unit in view.units for f in unit.flags] == [
        ("needs_check", detail)
    ]
    assert {(b.kind, b.claim) for b in view.blockers} == {("needs_check", detail)}
    with pytest.raises(d.docs.ApprovalBlocked):
        d.letters.approve(app.id, version_id)
    # Editing the paragraph (which clears nothing it keeps, but re-checks it) or removing it
    # resolves the flag; removing leaves the clean paragraph to approve.
    after = d.letters.remove_unit(app.id, version_id, d.unit_with(version_id, P1)["id"])
    d.letters.approve(app.id, after)
    assert d.version_row(after)["status"] == "approved"


def test_letter_a_title_with_more_names_supports_only_the_whole_title(d: LetterDesk):
    app = d.letter_ready()
    d.r.run.execute(
        "UPDATE jobs SET title=? WHERE id=?", ("Route Planner (Globex Pilot)", "letter-job")
    )

    d.draft(app.id, "company_and_title")

    version_id = d.current_letter_id(app.id)
    triples = d.flag_triples(version_id)
    # "Route Planner" alone is not the job's title any more, so it is a name to check; the
    # company, written whole, still is not. Nothing of the title's other names became allowed.
    assert [(kind, claim) for kind, claim, _ in triples] == [("needs_check", "Route Planner")]
    assert COMPANY not in {claim for _, claim, _ in triples}


def test_letter_a_stored_tone_or_length_outside_the_sets_falls_back_to_the_defaults(
    d: LetterDesk,
):
    app = d.letter_ready()
    d.script(DRAFT_COVER_LETTER, "ok")
    ref = d.r.run.app.state.task_service.enqueue(
        DRAFT_COVER_LETTER,
        app.id,
        input_ref=request_text(app.id, None, "", False, "breezy", "epic"),
    )

    assert d.r.run.wait_terminal(ref.id).status.value == "succeeded"

    (version,) = d.letter_versions(app.id)
    assert (version["tone"], version["length"]) == ("warm", "medium")
    sent = d.request_for_letter().user
    assert "Tone: warm" in sent and "Length: medium" in sent
    assert "breezy" not in sent and "epic" not in sent


def test_the_letter_request_itself_refuses_a_tone_or_length_outside_the_sets():
    job = TailoringJob("job-x", "A fictional posting.", ())
    for tone, length in (("breezy", "medium"), ("warm", "epic"), ("WARM", "medium")):
        with pytest.raises(ValueError):
            asyncio.run(
                run_draft_cover_letter(
                    None,  # type: ignore[arg-type]  # refused before the runner is used
                    job,
                    (),
                    None,
                    tone,
                    length,
                    "",
                    (),
                    None,  # type: ignore[arg-type]
                    meter=None,  # type: ignore[arg-type]
                )
            )
