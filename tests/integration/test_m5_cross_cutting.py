"""M5 privacy boundaries and complete-response performance, with fictional data only."""

from statistics import median
from time import perf_counter

import pytest

from tests.board_pages import Seeder, stamp_after, ticker
from tests.contact_pages import contact_values, interaction_values
from tests.integration.test_m4_cross_cutting import (
    assert_stored_only_in,
    final_states,
    open_events,
    read_until_final,
    watched,
)
from tests.reminder_desk import reminding
from tests.running_app import start_app

pytestmark = pytest.mark.integration

MARKERS = {
    "name": "CanaryContactFictionalQ71",
    "email": "canary-q71@example.test",
    "phone": "+00 555 017171",
    "notes": "CanaryContactNoteQ71",
    "interaction": "CanaryInteractionQ71",
    "application": "CanaryApplicationNoteQ71",
    "feedback": "CanaryRejectionQ71",
    "details": "CanaryEventDetailsQ71",
    "interviewers": "CanaryInterviewerQ71",
    "location": "CanaryEventLocationQ71",
}


def test_contacts_notes_feedback_and_event_details_never_in_logs_task_rows_or_events(
    tmp_path, monkeypatch, capsys
):
    with watched(tmp_path, monkeypatch, capsys) as watch:
        run = watch.run
        seed = Seeder.make(
            run.app.state.workspace.db,
            run.app.state.applications,
            run.app.state.application_statuses,
            ticker(run.clock),
            stamp_after(run.clock),
        )
        application = seed.application("privacy-job", "Fictional Gardener", "Fictional Quay")
        base = f"/applications/{application.id}"
        portal, opened = open_events(watch)
        with opened as stream:
            watch.surfaces.add("initial event frame", portal.call(stream.next_chunk).decode())
            posts = (
                (
                    base + "/contacts/new",
                    contact_values(**{k: MARKERS[k] for k in ("name", "email", "phone", "notes")}),
                ),
                (base + "/interactions", interaction_values(note=MARKERS["interaction"])),
                (base + "/notes", {"text": MARKERS["application"]}),
                (base + "/feedback", {"text": MARKERS["feedback"]}),
                (
                    base + "/events",
                    {
                        "type": "interview",
                        "date": "2027-01-12",
                        "time": "14:00",
                        "time_zone": "America/New_York",
                        "duration_minutes": "60",
                        **{k: MARKERS[k] for k in ("details", "interviewers", "location")},
                    },
                ),
            )
            for path, values in posts:
                response = watch.post(path, **values)
                assert response.status_code == 200, path
                watch.surfaces.add_headers(path, response)
            # These screens intentionally show private content; diagnostic surfaces must not.
            detail = watch.get(base + "/detail")
            assert detail.status_code == 200
            # Contact notes/phone are editable on the company page, not all on the drawer.
            company_id = run.rows("SELECT company_id FROM jobs WHERE id='privacy-job'")[0][
                "company_id"
            ]
            company = watch.get(f"/companies/{company_id}")
            assert company.status_code == 200
            shown = detail.text + company.text
            assert not [marker for marker in MARKERS.values() if marker not in shown]
            for key, marker in MARKERS.items():
                table = (
                    "contacts"
                    if key in {"name", "email", "phone", "notes"}
                    else "interactions"
                    if key == "interaction"
                    else "application_notes"
                    if key in {"application", "feedback"}
                    else "events"
                )
                assert_stored_only_in(run, marker, frozenset({table}))
            before = run.rows("SELECT * FROM tasks")
            # Exercise a real task event after saving the private fields, rather than only
            # scanning an empty stream. The fake provider never receives these fields.
            response = watch.post(
                "/inbox/intake",
                text=(
                    "Fictional greenhouse coordinator wanted to plan imaginary planting, "
                    "water fictional flowers and maintain pretend tools at Example Gardens."
                ),
            )
            assert response.status_code == 200
            tasks = watch.settle("summarize_job", 1)
            assert tasks[0]["status"] == "succeeded", tasks[0]["error"]
            frames = read_until_final(portal, stream, final_states(run))
            assert b"task" in frames
            watch.surfaces.add("task event frames", frames.decode())
            assert len(run.rows("SELECT * FROM tasks")) > len(before)
            assert run.rows("SELECT count(*) AS n FROM usage_records")[0]["n"] > 0
            for path in ("/tasks/panel", "/tasks/indicator", "/settings/usage"):
                response = watch.get(path)
                assert response.status_code == 200
                watch.surfaces.add(path, response.text)
                watch.surfaces.add_headers(path, response)

            def broken(*args, **kwargs):
                raise RuntimeError(" ".join(MARKERS.values()))

            monkeypatch.setattr(run.app.state.application_detail, "view", broken)
            response = watch.get(base + "/detail")
            assert response.status_code == 500
            watch.surfaces.add("unexpected error body", response.text)
            watch.surfaces.add_headers("unexpected error", response)
    watch.surfaces.assert_clean(tuple(MARKERS.values()))


def test_board_and_today_query_strings_not_in_access_log(tmp_path):
    marker = "CanaryBoardTodayQueryQ71"
    app = start_app(tmp_path)
    try:
        with app.client() as client:
            for path in ("/board", "/"):
                response = client.get(path, params={"search": marker})
                assert response.status_code == 200
    finally:
        app.stop()
    output = app.stdout_path.read_text() + app.stderr_path.read_text()
    output += "".join(p.read_text() for p in app.log_dir.glob("*.log*"))
    assert "GET /board HTTP/1.1" in output
    assert "GET / HTTP/1.1" in output
    assert marker not in output
    assert "?search=" not in output


@pytest.mark.slow
def test_ac_m5_fragments_under_150ms_with_500_applications(tmp_path, record_property):
    with reminding(tmp_path) as run:
        seed = run.seeder()
        statuses = ("preparing", "applied", "screening", "interviewing", "rejected")
        applications = [
            seed.application(
                f"scale-{n}",
                f"Fictional Role {n}",
                f"Fictional Company {n % 40}",
                statuses[n % len(statuses)],
            )
            for n in range(500)
        ]
        assert run.rows("SELECT count(*) AS n FROM applications")[0]["n"] == 500
        run.at("2026-12-01T09:00:00+00:00")  # include due rule reminders
        paths = (
            "/board/columns",
            "/board?view=table",
            f"/applications/{applications[0].id}/detail",
            "/today/content",
            "/notifications/count",
        )
        for path in paths:
            times = []
            for _ in range(5):
                start = perf_counter()
                response = run.get(path, htmx=True)
                times.append(perf_counter() - start)
                assert response.status_code == 200, path
                assert response.content
            elapsed = median(times)
            record_property(path, round(elapsed * 1000, 2))
            assert elapsed < 0.150, (path, times)
