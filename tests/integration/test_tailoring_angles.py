"""M6.5 step 2 of the workspace, the angle, through the real task runner, the real prompt and the
fake provider (AC-M6.1 feedback flags; spec 3.1 item 5, 3.6; ADR-106 items 8 and 11).

Assumed interface: see ``tests/tailoring_pages.py``. In short: ``POST
/applications/{id}/tailoring/2/propose`` starts ``propose_angles`` (dedupe ``propose_angles:<
application id>``); the task writes one ``angle_proposals`` row with ``ctx.complete``; step 2
shows ``li[data-angle]`` per proposal with a prefilled form (``name``, ``description``, hidden
``version`` and ``reuse_angle_id``; submit "Use this angle") and ``form#angle-form`` ("Write your
own angle"); ``POST .../2/angle`` saves the angle on the workspace through
``tailoring_repo.update_workspace`` (``version`` checked: missing 400, stale 409, blank name 422,
unknown ``reuse_angle_id`` 422). A proposal saved unchanged is origin ``ai``; edited or written
is ``user``; an earlier angle saved unchanged is reused (no new row, that row untouched).

Earlier angles are those used by the workspaces of other applications, most recently updated
workspace first, at most ten. The fixtures are those of ``tests/fixtures/ai/propose_angles``;
``angle-earlier-1`` is the id they reuse. Nothing is real; no network.
"""

import json
from typing import Any

import pytest

from app.domain.errors import InvalidModelOutput, VersionConflict, message_for_kind
from app.modules.ai.providers.fake import FAKE_THOROUGH_MODEL
from tests.board_pages import elements
from tests.tailoring_ai_inputs import EARLIER_ANGLE_ID
from tests.tailoring_pages import (
    PROPOSE_ANGLES,
    Tailoring,
    angle_items,
    announced,
    by_id,
    form_values,
    forms_in,
    has_id,
    hidden_value,
    stepper,
    tailoring_app,
)

pytestmark = pytest.mark.integration

NOT_IN_FACT_BANK = "This isn't in your fact bank."
REUSED = "Used in another application"
EARLIER_NAME, EARLIER_DESCRIPTION = "Reliable planner", "Lead with the weekly route planning."


@pytest.fixture
def tw(tmp_path):
    with tailoring_app(tmp_path) as desk:
        yield desk


def seed_earlier(tw: Tailoring, *, many: bool = False) -> list[str]:
    """The earlier angle the fixtures reuse and, with ``many``, ten older ones: ``angle-x01`` is
    the oldest of eleven angles, ``angle-earlier-1`` the most recently used."""
    sent = [EARLIER_ANGLE_ID]
    if many:
        for n in range(1, 11):
            stamp = f"2026-10-{n:02d}T09:00:00+00:00"
            tw.earlier_angle(
                f"angle-x{n:02d}", f"job-x{n:02d}", name=f"Angle {n}", updated_at=stamp
            )
        sent += [f"angle-x{n:02d}" for n in range(2, 11)]
    tw.earlier_angle(
        EARLIER_ANGLE_ID,
        "job-earlier-1",
        name=EARLIER_NAME,
        description=EARLIER_DESCRIPTION,
        updated_at="2026-10-20T09:00:00+00:00",
    )
    return sent


def workspace(tw: Tailoring, aid: str) -> dict[str, Any] | None:
    found = tw.rows("SELECT * FROM workspaces WHERE application_id=?", (aid,))
    return found[0] if found else None


def angle_rows(tw: Tailoring) -> list[dict[str, Any]]:
    return tw.rows("SELECT * FROM angles ORDER BY id")


def save(tw: Tailoring, aid: str, form_page: str, container, **changes: str):
    """Post the form the page shows (as the browser would), with ``changes`` typed over it."""
    (form,) = forms_in(form_page, container) if container.tag != "form" else (container,)
    values = {**form_values(form_page, form), **changes}
    return tw.post_fields(str(form.attrs["action"]), values)


def test_angles_reuse_earlier_and_pick_edit_or_write(tw: Tailoring):
    app = tw.ready()
    sent = seed_earlier(tw, many=True)
    tw.propose(app.id, "ok")

    # Up to ten angles chosen in other applications went out, most recently used first.
    (request,) = tw.requests(PROPOSE_ANGLES)
    assert request.model == FAKE_THOROUGH_MODEL and not request.allow_web_search
    assert request.user.count("<untrusted_document") == 3  # job, facts, the angles: one block
    assert all(angle_id in request.user for angle_id in sent)
    assert "angle-x01" not in request.user
    (stored,) = tw.rows("SELECT * FROM angle_proposals")
    proposed = json.loads(stored["result"])["angles"]
    assert [item["reuse_angle_id"] for item in proposed] == [EARLIER_ANGLE_ID, None]
    assert (stored["prompt_id"], stored["prompt_version"], stored["provider"]) == (
        "propose_angles",
        "2",
        "fake",
    )

    shown = tw.page(app.id, 2).text
    first, second = angle_items(shown)
    assert "Reliable planner" in first.text and "The role centres on route planning." in first.text
    assert REUSED in first.text and REUSED not in second.text
    assert "Needs your choice" in stepper(shown)[2].text
    assert workspace(tw, app.id) is None  # reading and proposing create no workspace
    earlier_before = tw.rows("SELECT * FROM angles WHERE id=?", (EARLIER_ANGLE_ID,))
    count = len(angle_rows(tw))

    # Pick the reused angle as it is: the workspace points at the existing row.
    answered = save(tw, app.id, shown, first)
    assert answered.status_code == 200 and "Angle saved." in announced(answered)
    assert workspace(tw, app.id)["angle_id"] == EARLIER_ANGLE_ID
    assert len(angle_rows(tw)) == count
    assert tw.rows("SELECT * FROM angles WHERE id=?", (EARLIER_ANGLE_ID,)) == earlier_before
    done = tw.page(app.id, 2).text
    assert by_id(done, "chosen-angle").text == f"Your angle: {EARLIER_NAME}"
    assert "Done" in stepper(done)[2].text

    # Pick the other proposal as it is: a new angle, written by the AI.
    shown = tw.page(app.id, 2).text
    second = angle_items(shown)[1]
    save(tw, app.id, shown, second)
    chosen = workspace(tw, app.id)["angle_id"]
    (row,) = tw.rows("SELECT * FROM angles WHERE id=?", (chosen,))
    assert (row["name"], row["origin"]) == ("Process improver", "ai")
    assert row["description"] == "Lead with the faster invoice processing."
    assert len(angle_rows(tw)) == count + 1

    # Edit a proposal: a new angle written by the user; the shared earlier row stays as it was.
    shown = tw.page(app.id, 2).text
    first = angle_items(shown)[0]
    save(tw, app.id, shown, first, description="Lead with route planning and 3 depots.")
    edited = workspace(tw, app.id)["angle_id"]
    (row,) = tw.rows("SELECT * FROM angles WHERE id=?", (edited,))
    assert edited not in (EARLIER_ANGLE_ID, chosen) and row["origin"] == "user"
    assert row["description"] == "Lead with route planning and 3 depots."
    assert tw.rows("SELECT * FROM angles WHERE id=?", (EARLIER_ANGLE_ID,)) == earlier_before

    # Write one: the same form works without any proposal.
    shown = tw.page(app.id, 2).text
    own = by_id(shown, "angle-form")
    before = workspace(tw, app.id)
    save(tw, app.id, shown, own, name="My own angle", description="Lead with calm delivery.")
    written = workspace(tw, app.id)
    (row,) = tw.rows("SELECT * FROM angles WHERE id=?", (written["angle_id"],))
    assert (row["name"], row["origin"]) == ("My own angle", "user")
    assert written["version"] == before["version"] + 1
    assert len(angle_rows(tw)) == count + 3
    assert by_id(tw.page(app.id, 2).text, "chosen-angle").text == "Your angle: My own angle"


def test_angle_save_is_version_checked_and_refuses_bad_input(tw: Tailoring):
    app = tw.ready()
    seed_earlier(tw)
    shown = tw.page(app.id, 2).text
    own = by_id(shown, "angle-form")
    first = {**form_values(shown, own), "name": "First angle", "description": "One."}
    path = str(own.attrs["action"])
    assert path == f"/applications/{app.id}/tailoring/2/angle"
    assert tw.post_fields(path, first).status_code == 200
    saved_workspace, saved_angles = workspace(tw, app.id), angle_rows(tw)

    stale = tw.post_fields(path, {**first, "name": "Second angle"})  # the version it was shown
    assert stale.status_code == 409 and VersionConflict.message in stale.text.replace("&#39;", "'")
    without = {key: value for key, value in first.items() if key != "version"}
    assert tw.post_fields(path, {**without, "name": "Third angle"}).status_code == 400
    current = tw.angle_version(app.id)
    blank = tw.post_fields(path, {**first, "version": current, "name": "   "})
    assert blank.status_code == 422
    unknown = tw.post_fields(
        path, {**first, "version": current, "name": "Other", "reuse_angle_id": "angle-missing"}
    )
    assert unknown.status_code == 422
    assert workspace(tw, app.id) == saved_workspace and angle_rows(tw) == saved_angles


@pytest.mark.parametrize("fixture", ["adversarial_rationale_claim", "adversarial_uncited_angle"])
def test_angle_rationale_claim_flagged(tw: Tailoring, fixture: str):
    app = tw.ready()
    seed_earlier(tw)
    tw.propose(app.id, fixture)

    shown = tw.page(app.id, 2).text
    faithful, flagged = angle_items(shown)
    assert NOT_IN_FACT_BANK not in faithful.text
    assert NOT_IN_FACT_BANK in flagged.text
    if fixture == "adversarial_rationale_claim":
        assert "Globex Corporation" in flagged.text  # shown to the user, never hidden
    # Flagged angles are shown, never chosen for the user.
    assert workspace(tw, app.id) is None and len(angle_rows(tw)) == 1
    stored = json.loads(tw.rows("SELECT result FROM angle_proposals")[0]["result"])
    assert stored["angles"][1]["claims"] and not stored["angles"][0]["claims"]


def test_angles_four_or_unknown_reuse_refused_or_ignored(tw: Tailoring):
    app = tw.ready()

    # Four angles are refused (the schema and the checker), after the one retry; nothing stored.
    task = tw.propose(app.id, "adversarial_four_angles", status="failed")
    assert task.error == InvalidModelOutput.kind
    assert len(tw.requests(PROPOSE_ANGLES)) == 2
    assert tw.rows("SELECT * FROM angle_proposals") == []

    # No earlier angle exists, so both reused ids ("angle-earlier-1", "angle-not-sent") are
    # ignored: the proposals are kept without a reuse mark.
    tw.propose(app.id, "ok")
    assert EARLIER_ANGLE_ID not in tw.requests(PROPOSE_ANGLES)[-1].user
    (stored,) = tw.rows("SELECT result FROM angle_proposals")
    assert [a["reuse_angle_id"] for a in json.loads(stored["result"])["angles"]] == [None, None]
    shown = tw.page(app.id, 2).text
    assert len(angle_items(shown)) == 2 and REUSED not in shown
    assert tw.rows("SELECT * FROM angles") == []


@pytest.mark.parametrize("fixture", ["malformed", "schema_invalid"])
def test_angles_malformed_output_retry_then_invalid(tw: Tailoring, fixture: str):
    app = tw.ready()

    task = tw.propose(app.id, fixture, status="failed")

    assert task.error == InvalidModelOutput.kind
    assert len(tw.requests(PROPOSE_ANGLES)) == 2
    assert tw.rows("SELECT * FROM angle_proposals") == []
    shown = tw.page(app.id, 2).text
    alerts = [e for e in elements(shown) if e.attrs.get("role") == "alert"]
    assert [alert.text for alert in alerts] == [message_for_kind(task.error)]
    assert by_id(shown, "angle-propose").text == "Propose angles"
    assert "Needs attention" in stepper(shown)[2].text
    assert angle_items(shown) == [] and has_id(shown, "angle-form")  # writing one still works


def test_proposals_are_kept_as_history_and_propose_again_asks_again(tw: Tailoring):
    app = tw.ready()
    tw.propose(app.id, "ok")
    assert by_id(tw.page(app.id, 2).text, "angle-propose").text == "Propose again"

    tw.propose(app.id, "ok")

    assert len(tw.rows("SELECT * FROM angle_proposals")) == 2
    assert len(tw.requests(PROPOSE_ANGLES)) == 2
    shown = tw.page(app.id, 2).text
    assert hidden_value(shown, by_id(shown, "angle-form"), "version").isdigit()
