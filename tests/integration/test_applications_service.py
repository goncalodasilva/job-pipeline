"""M5.3 applications service: Apply, Undo, moves, follow-ups and statuses (AC-M5.1, AC-M5.3).

Choices the tests fix where the task text leaves room (the implementer follows them):

- Applying to a job that is already To apply raises ``TriageNotAllowed`` (the triage rule) and
  writes nothing, so a job never gets a second application. A stale version raises
  ``VersionConflict`` first.
- ``undo_apply`` returns the job to the state it had before Apply and deletes the application
  only while the migration's guard allows it; a changed application raises ``StatusNotAllowed``
  with the fixed message and leaves the job and every application row as they were.
- A status without a phase, with a blank or taken label, or an unknown status key raises
  ``StatusNotAllowed`` (never a bare ``ValueError``), and so does deleting a built-in or
  in-use status; a status that history still names is in use.
- Reordering swaps a status with its neighbour inside the same phase.

Everyone and everything here is fictional.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.domain.errors import VersionConflict
from app.domain.ids import is_safe_id
from tests.application_desk import START, UNDO_REFUSED, Desk, error, wall

pytestmark = pytest.mark.integration


@pytest.fixture
def desk(tmp_path):
    working = Desk(tmp_path)
    working.add_job("job-0001", "Greenhouse Technician")
    working.add_job("job-0002", "Irrigation Planner")
    yield working
    working.close()


def stamp(instant: datetime) -> str:
    return instant.astimezone(UTC).isoformat()


# --- Apply ------------------------------------------------------------------------------------


def test_apply_to_job_sets_to_apply_and_creates_one_preparing_application(desk):
    result = desk.applications.apply_to_job("job-0001", expected_job_version=1)
    assert result.job.triage_state == "to_apply" and result.job.previous_state == "new"
    assert result.job.version == 2
    application = result.application
    assert is_safe_id(application.id) and application.job_id == "job-0001"
    assert (application.status_key, application.applied_at, application.version) == (
        "preparing",
        None,
        1,
    )
    assert desk.rows("SELECT id, job_id, status_key FROM applications") == [
        (application.id, "job-0001", "preparing")
    ]
    assert desk.history(application.id) == [(None, "preparing", stamp(START), "created")]
    assert desk.job("job-0001") == result.job


def test_apply_uses_first_preparing_status_by_position(desk):
    researching = desk.statuses.add("Researching", "preparing")
    assert desk.order("preparing") == ["preparing", researching.key]
    desk.statuses.reorder(researching.key, "up", expected_version=researching.version)
    assert desk.order("preparing") == [researching.key, "preparing"]
    result = desk.apply("job-0001")
    assert result.application.status_key == researching.key
    assert desk.history(result.application.id) == [(None, researching.key, stamp(START), "created")]


def test_apply_twice_is_refused_and_keeps_the_single_application(desk):
    first = desk.apply("job-0001")
    before = desk.snapshot()
    with pytest.raises(error("TriageNotAllowed")):
        desk.applications.apply_to_job("job-0001", expected_job_version=first.job.version)
    assert desk.snapshot() == before
    assert desk.rows("SELECT count(*) FROM applications") == [(1,)]


def test_apply_with_stale_version_or_unknown_job_writes_nothing(desk):
    before = desk.snapshot()
    with pytest.raises(VersionConflict):
        desk.applications.apply_to_job("job-0001", expected_job_version=7)
    for job_id in ("job-missing", "bad id!", ""):
        with pytest.raises(error("JobNotFound")):
            desk.applications.apply_to_job(job_id, expected_job_version=1)
    assert desk.snapshot() == before


def fail_after_the_first_writes(monkeypatch, module, name):
    """Make a repository write raise once earlier writes of the same change have been made."""

    def refuse(*args, **kwargs):
        raise RuntimeError(f"{name} failed")

    monkeypatch.setattr(module, name, refuse)


def test_apply_is_one_transaction(desk, monkeypatch):
    from app.modules.storage import applications_repo

    # The job's move and the application are written before the history row fails.
    fail_after_the_first_writes(monkeypatch, applications_repo, "insert_history")
    before = desk.snapshot()
    with pytest.raises(RuntimeError):
        desk.applications.apply_to_job("job-0001", expected_job_version=1)
    assert desk.snapshot() == before
    assert desk.job("job-0001").triage_state == "new"


def test_apply_without_a_preparing_status_writes_nothing(desk):
    with desk.db.transaction() as conn:
        conn.execute("UPDATE application_statuses SET phase='active' WHERE key='preparing'")
    before = desk.snapshot()
    with pytest.raises(error("StatusNotAllowed")):
        desk.applications.apply_to_job("job-0001", expected_job_version=1)
    assert desk.snapshot() == before


# --- Undo and Back to queue -------------------------------------------------------------------


def test_undo_apply_removes_only_an_untouched_application(desk):
    with desk.db.transaction() as conn:  # job-0002 was shown before it was applied for
        conn.execute("UPDATE jobs SET triage_state='seen', version=2 WHERE id='job-0002'")
    for job_id, before_state in (("job-0001", "new"), ("job-0002", "seen")):
        applied = desk.apply(job_id)
        back = desk.applications.undo_apply(job_id, expected_job_version=applied.job.version)
        assert back.triage_state == before_state and back.previous_state is None
        assert back.version == applied.job.version + 1
        assert desk.job(job_id) == back
    assert desk.rows("SELECT count(*) FROM applications") == [(0,)]
    assert desk.rows("SELECT count(*) FROM status_history") == [(0,)]
    # The job can be applied for again; the second application is a fresh one.
    again = desk.apply("job-0001")
    assert desk.rows("SELECT id FROM applications") == [(again.application.id,)]
    assert desk.history(again.application.id)[0][3] == "created"


def test_undo_apply_with_stale_version_keeps_everything(desk):
    applied = desk.apply("job-0001")
    before = desk.snapshot()
    with pytest.raises(VersionConflict):
        desk.applications.undo_apply("job-0001", expected_job_version=applied.job.version + 5)
    assert desk.snapshot() == before


def test_undo_apply_after_changes_refused_and_job_kept(desk):
    def moved(application):
        desk.move(application, "applied")

    def moved_to_the_same_status(application):
        desk.move(application, "preparing", source="menu")

    def feedback(application):
        desk.applications.add_feedback(application.id, "Fictional note from the recruiter.")

    def interview(application):
        desk.applications.add_interview_date(
            application.id, starts_at=wall("2026-11-03T14:00"), time_zone="America/New_York"
        )

    for index, change in enumerate((moved, moved_to_the_same_status, feedback, interview)):
        job_id = desk.add_job(f"job-change-{index}", f"Fictional Role {index}")
        applied = desk.apply(job_id)
        desk.tick(minutes=1)
        change(applied.application)
        before = desk.snapshot()
        job = desk.job(job_id)
        with pytest.raises(error("StatusNotAllowed")) as refused:
            desk.applications.undo_apply(job_id, expected_job_version=job.version)
        assert refused.value.user_message == UNDO_REFUSED, change.__name__
        assert desk.snapshot() == before, change.__name__
        assert desk.job(job_id) == job and job.triage_state == "to_apply"


# --- moves and history (AC-M5.1) --------------------------------------------------------------


def test_ac_m5_1_status_change_recorded_in_history(desk):
    application = desk.apply("job-0001").application
    steps = (
        ("applied", "board"),
        ("interviewing", "menu"),
        ("interviewing", "drawer"),  # to the same status: still recorded
        ("preparing", "board"),  # any status may move to any other
    )
    expected = [(None, "preparing", stamp(START), "created")]
    first_active = None
    current = "preparing"
    for status, source in steps:
        desk.tick(hours=2)
        moved = desk.move(application, status, source)
        now = stamp(desk.clock.now())
        expected.append((current, status, now, source))
        current = status
        first_active = first_active or now
        assert moved.application.status_key == status
        assert moved.application.applied_at == first_active  # set once, never cleared or moved
    assert desk.history(application.id) == expected
    row = desk.rows("SELECT status_key, applied_at, version FROM applications")[0]
    assert row == ("preparing", first_active, 1 + len(steps))


def test_ac_m5_1_status_without_phase_refused(desk):
    before = desk.snapshot()
    for phase in (None, "", "other", "ACTIVE "):
        with pytest.raises(error("StatusNotAllowed")):
            desk.statuses.add("Waiting", phase)
    for label in ("", "   ", "Ready\nnow", "applied", "APPLIED "):
        with pytest.raises(error("StatusNotAllowed")):
            desk.statuses.add(label, "active")
    assert desk.snapshot() == before


def test_stale_move_raises_conflict_and_writes_nothing(desk):
    application = desk.apply("job-0001").application
    desk.tick(minutes=5)
    before = desk.snapshot()
    with pytest.raises(VersionConflict):
        desk.applications.move(application.id, "applied", expected_version=9, source="board")
    assert desk.snapshot() == before
    # A caller holding an old version loses after someone else moved it.
    desk.applications.move(application.id, "applied", expected_version=1, source="board")
    after_first = desk.snapshot()
    with pytest.raises(VersionConflict):
        desk.applications.move(application.id, "screening", expected_version=1, source="menu")
    assert desk.snapshot() == after_first
    assert len(desk.history(application.id)) == 2


def test_move_with_unknown_status_or_application_is_refused_and_writes_nothing(desk):
    application = desk.apply("job-0001").application
    before = desk.snapshot()
    with pytest.raises(error("StatusNotAllowed")):
        desk.applications.move(application.id, "no-such-status", expected_version=1, source="menu")
    for application_id in ("missing", "bad id!", ""):
        with pytest.raises(error("ApplicationNotFound")):
            desk.applications.move(application_id, "applied", expected_version=1, source="menu")
    assert desk.snapshot() == before


def test_move_is_one_transaction(desk, monkeypatch):
    from app.modules.storage import applications_repo

    application = desk.apply("job-0001").application
    # The status update is written before the history row fails.
    fail_after_the_first_writes(monkeypatch, applications_repo, "insert_history")
    before = desk.snapshot()
    with pytest.raises(RuntimeError):
        desk.applications.move(application.id, "applied", expected_version=1, source="board")
    assert desk.snapshot() == before


def test_undo_apply_is_one_transaction(desk, monkeypatch):
    from app.modules.storage import jobs_repo

    applied = desk.apply("job-0001")
    # The application is deleted before the job's move fails.
    fail_after_the_first_writes(monkeypatch, jobs_repo, "update_job_triage")
    before = desk.snapshot()
    with pytest.raises(RuntimeError):
        desk.applications.undo_apply("job-0001", expected_job_version=applied.job.version)
    assert desk.snapshot() == before
    assert desk.rows("SELECT count(*) FROM applications") == [(1,)]


def test_move_accepts_only_user_sources_and_safe_status_keys(desk):
    application = desk.apply("job-0001").application
    before = desk.snapshot()
    for source in ("created", "backfill", "", "other"):
        with pytest.raises(error("StatusNotAllowed")):
            desk.applications.move(application.id, "applied", expected_version=1, source=source)
    for key in ("", "bad id!", "x" * 65):
        with pytest.raises(error("StatusNotAllowed")):
            desk.applications.move(application.id, key, expected_version=1, source="menu")
    assert desk.snapshot() == before
    for source in ("board", "menu", "drawer"):
        version = desk.rows("SELECT version FROM applications")[0][0]
        desk.applications.move(application.id, "applied", expected_version=version, source=source)


# --- follow-ups (AC-M5.3) ---------------------------------------------------------------------


@pytest.mark.parametrize("choice", ["given", "skipped"])
def test_ac_m5_3_interviewing_offers_optional_date(desk, choice):
    application = desk.apply("job-0001").application
    moved = desk.move(application, "interviewing")
    assert moved.follow_up == "interview_date"
    assert moved.application.status_key == "interviewing"
    if choice == "skipped":
        # Skipping the offer is simply not calling the service again: nothing is written.
        assert desk.rows("SELECT count(*) FROM events") == [(0,)]
        assert desk.rows("SELECT count(*) FROM application_notes") == [(0,)]
        return
    desk.applications.add_interview_date(
        application.id, starts_at=wall("2026-11-03T14:00"), time_zone="America/New_York"
    )
    [event] = desk.rows(
        "SELECT application_id, type, starts_at, ends_at, time_zone, all_day, status FROM events"
    )
    assert (event[0], event[1], event[4], event[5], event[6]) == (
        application.id,
        "interview",
        "America/New_York",
        0,
        "scheduled",
    )
    starts = datetime.fromisoformat(event[2])
    assert starts == datetime(2026, 11, 3, 19, 0, tzinfo=UTC)  # 14:00 EST is 19:00 UTC
    assert starts.utcoffset() == timedelta(0)
    assert datetime.fromisoformat(event[3]) >= starts
    assert desk.rows("SELECT count(*) FROM application_notes") == [(0,)]


@pytest.mark.parametrize("choice", ["given", "skipped"])
def test_ac_m5_3_rejected_offers_optional_feedback(desk, choice):
    application = desk.apply("job-0001").application
    desk.move(application, "applied")
    moved = desk.move(application, "rejected")
    assert moved.follow_up == "feedback"
    assert moved.application.status_key == "rejected"
    if choice == "skipped":
        assert desk.rows("SELECT count(*) FROM application_notes") == [(0,)]
        assert desk.rows("SELECT count(*) FROM events") == [(0,)]
        return
    desk.applications.add_feedback(application.id, "They wanted more warehouse experience.")
    [note] = desk.rows("SELECT application_id, kind, text FROM application_notes")
    assert note == (application.id, "feedback", "They wanted more warehouse experience.")
    assert desk.rows("SELECT status_key FROM applications") == [("rejected",)]


def test_custom_status_has_no_follow_up(desk):
    waiting = desk.statuses.add("Waiting", "active")
    application = desk.apply("job-0001").application
    moved = desk.move(application, waiting.key)
    assert moved.follow_up is None
    # Built-ins other than Interviewing and Rejected offer nothing either, and a renamed
    # built-in keeps its follow-up: it is the key that counts, not the label.
    assert desk.move(application, "offer").follow_up is None
    renamed = desk.statuses.rename("rejected", "Not this time", expected_version=1)
    assert renamed.label == "Not this time"
    assert desk.move(application, "rejected").follow_up == "feedback"
    # A custom status labelled like a built-in follow-up status still offers nothing.
    closed = desk.statuses.add("Interviewing later", "active")
    assert desk.move(application, closed.key).follow_up is None


def test_interview_date_in_a_spring_forward_gap_is_refused_with_its_field(desk):
    application = desk.apply("job-0001").application
    before = desk.snapshot()
    with pytest.raises(error("EventTimeInvalid")) as refused:
        desk.applications.add_interview_date(
            application.id, starts_at=wall("2026-03-08T02:30"), time_zone="America/New_York"
        )
    assert refused.value.field == "starts_at"
    assert "does not exist" in refused.value.user_message
    assert desk.snapshot() == before


def test_interview_date_with_unknown_zone_is_refused_with_its_field(desk):
    application = desk.apply("job-0001").application
    before = desk.snapshot()
    for zone in ("Mars/Olympus_Mons", "", "../../etc/passwd", "UTC+3 please"):
        with pytest.raises(error("EventTimeInvalid")) as refused:
            desk.applications.add_interview_date(
                application.id, starts_at=wall("2026-11-03T14:00"), time_zone=zone
            )
        assert refused.value.field == "time_zone", zone
        assert refused.value.user_message
    assert desk.snapshot() == before


def test_interview_date_zone_must_be_a_canonical_name_from_the_list(desk):
    application = desk.apply("job-0001").application
    before = desk.snapshot()
    for zone in ("europe/paris", "EUROPE/PARIS", "posixrules", "localtime", "Europe", "Etc/../UTC"):
        with pytest.raises(error("EventTimeInvalid")) as refused:
            desk.applications.add_interview_date(
                application.id, starts_at=wall("2026-11-03T14:00"), time_zone=zone
            )
        assert refused.value.field == "time_zone", zone
        assert refused.value.user_message == "Choose a time zone from the list.", zone
    assert desk.snapshot() == before
    desk.applications.add_interview_date(
        application.id, starts_at=wall("2026-11-03T14:00"), time_zone="Europe/Paris"
    )
    assert desk.rows("SELECT time_zone FROM events") == [("Europe/Paris",)]


def test_interview_date_at_the_edges_of_the_calendar_is_refused_with_its_field(desk):
    application = desk.apply("job-0001").application
    before = desk.snapshot()
    for text, zone in (
        ("0001-01-01T00:30", "Asia/Tokyo"),  # before year 1 in UTC
        ("9999-12-31T23:30", "UTC"),  # the hour after it runs past year 9999
        ("9999-12-31T23:30", "America/New_York"),
    ):
        with pytest.raises(error("EventTimeInvalid")) as refused:
            desk.applications.add_interview_date(
                application.id, starts_at=wall(text), time_zone=zone
            )
        assert refused.value.field == "starts_at", (text, zone)
        assert refused.value.user_message
    assert desk.snapshot() == before


def test_interview_date_in_a_repeated_hour_takes_the_first_occurrence(desk):
    application = desk.apply("job-0001").application
    desk.applications.add_interview_date(
        application.id, starts_at=wall("2026-11-01T01:30"), time_zone="America/New_York"
    )
    [(starts,)] = desk.rows("SELECT starts_at FROM events")
    assert datetime.fromisoformat(starts) == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)  # EDT


def test_follow_ups_for_an_unknown_application_are_refused(desk):
    before = desk.snapshot()
    for application_id in ("missing", "bad id!"):
        with pytest.raises(error("ApplicationNotFound")):
            desk.applications.add_feedback(application_id, "Fictional feedback.")
        with pytest.raises(error("ApplicationNotFound")):
            desk.applications.add_interview_date(
                application_id, starts_at=wall("2026-11-03T14:00"), time_zone="UTC"
            )
    assert desk.snapshot() == before


# --- statuses ---------------------------------------------------------------------------------


def test_statuses_add_rename_reorder_delete_unused_versioned(desk):
    # add: label and phase, a safe key, placed last in the list
    waiting = desk.statuses.add("  Waiting  ", "active")
    assert (waiting.label, waiting.phase, waiting.built_in, waiting.version) == (
        "Waiting",
        "active",
        False,
        1,
    )
    assert is_safe_id(waiting.key)
    assert desk.order("active") == ["applied", "screening", "interviewing", "offer", waiting.key]
    with pytest.raises(error("StatusNotAllowed")):
        desk.statuses.add("waiting", "closed")  # labels are unique without regard to case

    # rename: a built-in can be renamed (its key is kept); stale versions and taken labels lose
    renamed = desk.statuses.rename("applied", "Sent", expected_version=1)
    assert (renamed.key, renamed.label, renamed.version, renamed.built_in) == (
        "applied",
        "Sent",
        2,
        True,
    )
    with pytest.raises(VersionConflict):
        desk.statuses.rename("applied", "Posted", expected_version=1)
    with pytest.raises(error("StatusNotAllowed")):
        desk.statuses.rename("applied", "Waiting", expected_version=2)
    assert desk.rows("SELECT label FROM application_statuses WHERE key='applied'") == [("Sent",)]

    # reorder: one step inside the phase; a stale version moves nothing
    before = desk.order("active")
    with pytest.raises(VersionConflict):
        desk.statuses.reorder(waiting.key, "up", expected_version=4)
    assert desk.order("active") == before
    up = desk.statuses.reorder(waiting.key, "up", expected_version=waiting.version)
    assert desk.order("active") == ["applied", "screening", "interviewing", waiting.key, "offer"]
    assert up.version == waiting.version + 1
    down = desk.statuses.reorder(waiting.key, "down", expected_version=up.version)
    assert desk.order("active") == before
    assert down.version == up.version + 1
    assert desk.order("preparing") == ["preparing"] and desk.order("closed")[0] == "rejected"

    # delete: built-ins never, in-use statuses never, stale never; an unused custom one once
    with pytest.raises(error("StatusNotAllowed")):
        desk.statuses.delete("withdrawn", expected_version=1)
    application = desk.apply("job-0001").application
    desk.move(application, waiting.key)
    with pytest.raises(error("StatusNotAllowed")):
        desk.statuses.delete(waiting.key, expected_version=down.version)
    desk.move(application, "applied")  # history still names the status, so it is still in use
    with pytest.raises(error("StatusNotAllowed")):
        desk.statuses.delete(waiting.key, expected_version=down.version)
    spare = desk.statuses.add("Spare", "closed")
    with pytest.raises(VersionConflict):
        desk.statuses.delete(spare.key, expected_version=spare.version + 3)
    assert desk.rows("SELECT count(*) FROM application_statuses WHERE key=?", (spare.key,)) == [
        (1,)
    ]
    assert desk.statuses.delete(spare.key, expected_version=spare.version) is None
    assert desk.rows("SELECT count(*) FROM application_statuses WHERE key=?", (spare.key,)) == [
        (0,)
    ]
    assert waiting.key in desk.order("active") and "offer" in desk.order("active")


def test_unknown_status_key_is_refused_for_rename_reorder_and_delete(desk):
    before = desk.snapshot()
    with pytest.raises(error("StatusNotAllowed")):
        desk.statuses.rename("missing", "Whatever", expected_version=1)
    with pytest.raises(error("StatusNotAllowed")):
        desk.statuses.reorder("missing", "up", expected_version=1)
    with pytest.raises(error("StatusNotAllowed")):
        desk.statuses.delete("missing", expected_version=1)
    assert desk.snapshot() == before
