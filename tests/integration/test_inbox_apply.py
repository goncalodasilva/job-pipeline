"""M5.3: the inbox A key, card, drawer, Undo and Back to queue go through the application service.

The routes and their announcements are the ones M4.7 built (``/inbox/jobs/{id}/triage`` for the
card and the A key, ``/inbox/jobs/{id}/detail-triage`` for the drawer); the answers keep their
copy. A refused Undo answers 409 and shows the service's message. Everyone and everything here
is fictional.
"""

import pytest

from tests.application_desk import UNDO_REFUSED
from tests.integration.test_inbox import post, seeded
from tests.refine_harness import refining

pytestmark = pytest.mark.integration
CARD = "/inbox/jobs/{}/triage"
DRAWER = "/inbox/jobs/{}/detail-triage"


def tuples(rows):
    return [tuple(row.values()) for row in rows]


def applications(r):
    return tuples(
        r.run.rows("SELECT id, job_id, status_key, applied_at, version FROM applications")
    )


def history(r):
    return tuples(
        r.run.rows(
            "SELECT from_status, to_status, source FROM status_history ORDER BY changed_at, id"
        )
    )


def job(r, job_id="job-0001"):
    [row] = r.run.rows("SELECT triage_state, version FROM jobs WHERE id=?", (job_id,))
    return tuple(row.values())


def spy_on(monkeypatch, service, name):
    calls = []
    original = getattr(service, name)

    def spy(*args, **kwargs):
        calls.append(args[:1])
        return original(*args, **kwargs)

    monkeypatch.setattr(service, name, spy)
    return calls


def test_apply_in_inbox_creates_one_preparing_application(tmp_path, monkeypatch):
    with refining(tmp_path) as r:
        seeded(r, 3)
        calls = spy_on(monkeypatch, r.run.app.state.applications, "apply_to_job")
        card = post(r, CARD.format("job-0001"), version="1", action="to_apply")
        assert card.status_code == 200 and "Marked to apply." in card.text
        [only] = applications(r)
        assert only[1:] == ("job-0001", "preparing", None, 1)
        assert history(r) == [(None, "preparing", "created")]
        assert job(r) == ("to_apply", 2)

        drawer = post(r, DRAWER.format("job-0002"), version="1", action="to_apply")
        assert drawer.status_code == 200 and "Marked to apply." in drawer.text
        assert {row[1] for row in applications(r)} == {"job-0001", "job-0002"}
        assert {row[2] for row in applications(r)} == {"preparing"}
        assert calls == [("job-0001",), ("job-0002",)]  # both entry points use the service

        # A second Apply and a stale page both get the reload prompt and add nothing.
        assert post(r, CARD.format("job-0001"), version="2", action="to_apply").status_code == 409
        assert post(r, CARD.format("job-0003"), version="9", action="to_apply").status_code == 409
        assert len(applications(r)) == 2 and len(history(r)) == 2
        assert job(r, "job-0003") == ("new", 1)


def test_inbox_undo_and_back_to_queue_remove_an_untouched_application(tmp_path, monkeypatch):
    with refining(tmp_path) as r:
        seeded(r, 3)
        calls = spy_on(monkeypatch, r.run.app.state.applications, "undo_apply")
        post(r, CARD.format("job-0001"), version="1", action="to_apply")
        undone = post(r, CARD.format("job-0001"), version="2", action="undo")
        assert undone.status_code == 200
        assert "Back in the queue." in undone.headers["HX-Trigger-After-Settle"]
        assert applications(r) == [] and history(r) == []
        assert job(r) == ("new", 3)

        # Back to queue from the To apply view, and from the drawer, do the same.
        post(r, CARD.format("job-0002"), version="1", action="to_apply")
        back = post(r, CARD.format("job-0002"), version="2", action="undo", view="to_apply")
        assert back.status_code == 200 and applications(r) == []
        post(r, DRAWER.format("job-0003"), version="1", action="to_apply")
        drawer = post(r, DRAWER.format("job-0003"), version="2", action="undo")
        assert drawer.status_code == 200 and "Back in the queue." in drawer.text
        assert applications(r) == [] and history(r) == []
        assert job(r, "job-0003") == ("new", 3)
        assert calls == [("job-0001",), ("job-0002",), ("job-0003",)]


def test_inbox_undo_of_a_changed_application_is_refused_and_keeps_both(tmp_path):
    with refining(tmp_path) as r:
        seeded(r, 2)
        service = r.run.app.state.applications
        for job_id, route in (("job-0001", CARD), ("job-0002", DRAWER)):
            post(r, CARD.format(job_id), version="1", action="to_apply")
            [application] = [row for row in applications(r) if row[1] == job_id]
            service.move(application[0], "applied", expected_version=1, source="board")
            before = (applications(r), history(r))
            refused = post(r, route.format(job_id), version="2", action="undo")
            assert refused.status_code == 409, route
            assert UNDO_REFUSED in refused.text, route
            assert job(r, job_id) == ("to_apply", 2)
            assert (applications(r), history(r)) == before
            assert [row[2] for row in applications(r) if row[1] == job_id] == ["applied"]


def test_inbox_removing_and_undoing_a_job_never_creates_an_application(tmp_path):
    with refining(tmp_path) as r:
        seeded(r, 1)
        assert post(r, CARD.format("job-0001"), version="1", action="remove").status_code == 200
        assert post(r, CARD.format("job-0001"), version="2", action="undo").status_code == 200
        assert applications(r) == [] and history(r) == []
        assert job(r) == ("new", 3)


def test_app_state_exposes_the_application_services(tmp_path):
    from app.services.applications import ApplicationService, ApplicationStatuses
    from app.services.board import BoardQuery

    with refining(tmp_path) as r:
        state = r.run.app.state
        assert isinstance(state.applications, ApplicationService)
        assert isinstance(state.application_statuses, ApplicationStatuses)
        assert isinstance(state.board, BoardQuery)
        seeded(r, 1)
        state.applications.apply_to_job("job-0001", expected_job_version=1)
        view = state.board.board()
        assert [card.job_id for column in view.columns for card in column.cards] == ["job-0001"]
