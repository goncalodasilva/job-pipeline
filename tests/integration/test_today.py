"""Today reads fictional data with a frozen clock; reading never acknowledges it."""

from pathlib import Path

import pytest

from app.onboarding.progress import SetupProgress
from app.services.facts import DraftInput, DraftOrigin, FactInput
from app.services.setup_state import SetupStateService
from tests.board_pages import by_id, elements
from tests.reminder_desk import reminding

pytestmark = pytest.mark.integration


def skip(run, step):
    run.execute("UPDATE setup_state SET status='pending' WHERE step_id=?", (step,))
    progress = SetupProgress(run.app.state.setup_registry, SetupStateService(run.db, run.clock))
    progress.skip(step)
    run.app.state.onboarding.refresh(progress)


def seed(run):
    data = run.seeder()
    application = data.application(
        "today-application", "Orchard Buyer", "Fictional Orchards", "applied"
    )
    data.job("today-new", "Tree Specialist", "Fictional Orchards")
    run.execute("UPDATE jobs SET fit_band='strong' WHERE id='today-new'")
    run.app.state.facts.create_drafts(
        DraftOrigin(source="cv_import"), [DraftInput(text="Maintained a fictional seed catalogue.")]
    )
    skip(run, "web_search")
    run.at("2026-10-20T09:00:00+00:00")
    run.event(
        "today-interview",
        application,
        "interview",
        "2026-10-21T09:00:00+00:00",
        zone="Europe/Lisbon",
    )
    return application


def test_ac_m5_6_blocks_in_priority_order(tmp_path: Path):
    with reminding(tmp_path, now="2026-10-01T09:00:00+00:00") as run:
        seed(run)
        response = run.get("/", token=False)
        assert response.status_code == 200
        sections = [
            e.attrs["id"]
            for e in elements(response.text)
            if e.tag == "section" and "today-block" in e.classes
        ]
        assert sections == [
            "today-interviews",
            "today-reminders",
            "today-jobs",
            "today-drafts",
            "today-setup",
        ]
        assert "1 Applied" in by_id(response.text, "today-summary").text


def test_ac_m5_6_empty_blocks_hidden(tmp_path: Path):
    with reminding(tmp_path) as run:
        response = run.get("/", token=False)
        assert not [e for e in elements(response.text) if "today-block" in e.classes]
        assert "Suggested session" not in response.text
        assert "prep state" not in response.text


def test_ac_m5_6_nothing_due_says_so_plainly(tmp_path: Path):
    with reminding(tmp_path) as run:
        assert "Nothing needs your attention today." in run.get("/", token=False).text


@pytest.mark.parametrize("step", ["cv_import", "web_search"])
@pytest.mark.parametrize("status", ["skipped", "pending"])
def test_ac_m5_6_today_lists_skipped_optional_steps_with_links(
    tmp_path: Path, step: str, status: str
):
    with reminding(tmp_path) as run:
        run.execute("UPDATE setup_state SET status=? WHERE step_id=?", (status, step))
        progress = SetupProgress(run.app.state.setup_registry, SetupStateService(run.db, run.clock))
        run.app.state.onboarding.refresh(progress)
        response = run.get("/", token=False)
        section = by_id(response.text, "today-setup")
        assert section is not None
        links = [e for e in elements(response.text) if e.tag == "a" and e.within(section)]
        assert [e.attrs["href"] for e in links] == [f"/setup/{step}"]


def test_ac_m5_5_due_reminder_on_today_with_frozen_clock(tmp_path: Path):
    with reminding(tmp_path, now="2026-10-01T09:00:00+00:00") as run:
        application = seed(run)
        response = run.get("/", token=False)
        section = by_id(response.text, "today-reminders")
        assert section is not None and "Follow-up due" in section.text
        forms = [e for e in elements(response.text) if e.tag == "form" and e.within(section)]
        assert {e.attrs["action"].rsplit("/", 1)[1] for e in forms} == {"done", "snooze", "dismiss"}
        assert all(e.attrs["hx-target"] == "#today-content" for e in forms)
        assert f"/applications/{application.id}" in response.text
        assert run.rows("SELECT * FROM reminder_states") == []


def test_interviews_block_next_7_days_local_dates(tmp_path: Path):
    with reminding(tmp_path, now="2026-10-20T09:00:00+00:00", zone="Europe/Lisbon") as run:
        application = run.applied("interview-job")
        run.at("2026-10-20T09:00:00+00:00")
        for event, start, status in [
            ("past", "2026-10-20T08:59:59+00:00", "scheduled"),
            ("now", "2026-10-20T09:00:00+00:00", "scheduled"),
            ("tokyo", "2026-10-20T23:30:00+00:00", "scheduled"),
            ("edge", "2026-10-27T08:59:59+00:00", "scheduled"),
            ("beyond", "2026-10-27T09:00:00+00:00", "scheduled"),
            ("cancelled", "2026-10-22T09:00:00+00:00", "cancelled"),
        ]:
            run.event(event, application, "interview", start, zone="Asia/Tokyo", status=status)
        html = run.get("/", token=False).text
        rows = [e for e in elements(html) if "data-interview-id" in e.attrs]
        assert [e.attrs["data-interview-id"] for e in rows] == ["now", "tokyo", "edge"]
        assert "21 October 2026, 08:30" in rows[1].text
        assert "Asia/Tokyo" in rows[1].text


def test_new_jobs_block_counts_unseen_and_top_three_strong(tmp_path: Path):
    with reminding(tmp_path) as run:
        data = run.seeder()
        for i in range(6):
            data.job(f"new-{i}", f"Fictional role {i}", "Fictional Orchards")
        run.execute("UPDATE jobs SET fit_band='strong'")
        run.execute("UPDATE jobs SET triage_state='seen' WHERE id='new-4'")
        run.execute("UPDATE jobs SET triage_state='removed' WHERE id='new-5'")
        run.execute(
            "UPDATE jobs SET triage_state='snoozed', snooze_until='2026-10-19' WHERE id='new-3'"
        )
        html = run.get("/", token=False).text
        section = by_id(html, "today-jobs")
        assert section is not None and "4 unseen jobs" in section.text
        strong = [e for e in elements(html) if "data-strong-job" in e.attrs]
        assert len(strong) == 3
        assert {e.attrs["data-strong-job"] for e in strong} <= {"new-0", "new-1", "new-2", "new-3"}
        assert 'href="/inbox' in html


def test_drafts_block_counts_fact_drafts(tmp_path: Path):
    with reminding(tmp_path) as run:
        run.app.state.facts.create(FactInput(text="Kept a fictional orchard log."))
        run.app.state.facts.create_drafts(
            DraftOrigin(source="cv_import"),
            [DraftInput(text=f"Fictional draft {i}.") for i in range(2)],
        )
        html = run.get("/", token=False).text
        assert "2 fact drafts" in by_id(html, "today-drafts").text
        assert 'href="/facts?tab=drafts"' in html


def test_today_get_has_no_side_effects(tmp_path: Path):
    with reminding(tmp_path, now="2026-10-01T09:00:00+00:00") as run:
        seed(run)
        before = run.tables()
        for path in ("/", "/today/content"):
            assert run.get(path).status_code == 200
        assert run.tables() == before


def test_today_fragment_needs_token(tmp_path: Path):
    with reminding(tmp_path) as run:
        assert run.get("/today/content", token=False).status_code == 403
        assert run.client.get("/", headers={"HX-Request": "true"}).status_code == 403
        fragment = run.get("/today/content")
        assert fragment.status_code == 200 and 'id="today-content"' in fragment.text
        assert "<html" not in fragment.text


def test_today_and_notifications_conflicts_have_distinct_alert_ids(tmp_path: Path):
    with reminding(tmp_path, now="2026-10-01T09:00:00+00:00") as run:
        seed(run)
        reminder = run.due()[0]
        ids = []
        for target in ("today-content", "notifications-panel"):
            response = run.post_data(
                f"/reminders/{reminder.key}/done", {"version": "999"}, **{"HX-Target": target}
            )
            assert response.status_code == 409
            alert = next(e for e in elements(response.text) if e.attrs.get("role") == "alert")
            ids.append(alert.attrs["id"])
        assert len(set(ids)) == 2
