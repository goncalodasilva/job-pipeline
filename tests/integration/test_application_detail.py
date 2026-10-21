"""M5.5: application detail, versioned notes/events, and read-only calendar download."""

from datetime import UTC, datetime, timedelta

import pytest
from icalendar import Calendar

from app.domain.errors import ApplicationNotFound
from tests.board_pages import elements
from tests.integration.test_board import seeder, snapshot, version_of
from tests.integration.test_inbox import get, post
from tests.refine_harness import refining

pytestmark = pytest.mark.integration


def seed(r):
    return seeder(r).application("detail-job", "Seed Buyer", "Fictional Orchard", "applied")


def event_values(**overrides):
    return {
        "type": "interview",
        "date": "2026-11-03",
        "time": "14:00",
        "duration_minutes": "60",
        "time_zone": "America/New_York",
        "round": "Second",
        "format": "Video",
        "interviewers": "Fictional Panel",
        "location": "https://meet.example.test/room",
        "details": "Discuss the fictional orchard.",
        "thank_you_reminder": "1",
        **overrides,
    }


def add(r, aid, **overrides):
    return post(r, f"/applications/{aid}/events", **event_values(**overrides))


def event_row(r):
    return r.run.rows("SELECT * FROM events")[0]


def test_application_drawer_needs_token(tmp_path):
    with refining(tmp_path) as r:
        a = seed(r)
        path = f"/applications/{a.id}/detail"
        assert r.client.get(path).status_code == 403
        shown = get(r, path)
        assert shown.status_code == 200 and "<dialog" in shown.text
        assert "Open workspace" in shown.text and f'href="/applications/{a.id}"' in shown.text
        assert "Seed Buyer" in shown.text and "Fictional Orchard" in shown.text
        assert "Tailoring" not in shown.text and "Interview prep" not in shown.text


def test_application_page_by_safe_id_only_fixed_404(tmp_path):
    with refining(tmp_path) as r:
        a = seed(r)
        before = snapshot(r)
        full = r.client.get(f"/applications/{a.id}")
        assert full.status_code == 200 and "<html" in full.text
        assert r.client.head(f"/applications/{a.id}").status_code == 200
        assert (
            r.client.get(f"/applications/{a.id}", headers={"HX-Request": "true"}).status_code == 403
        )
        for aid in ("missing", "<script>", "bad.id"):
            answer = r.client.get(f"/applications/{aid}")
            assert answer.status_code == 404 and answer.text == ApplicationNotFound.message
        assert snapshot(r) == before


def test_event_saved_as_utc_with_zone_name(tmp_path):
    with refining(tmp_path) as r:
        a = seed(r)
        done = add(r, a.id)
        assert done.status_code == 200
        e = event_row(r)
        assert datetime.fromisoformat(e["starts_at"]) == datetime(2026, 11, 3, 19, tzinfo=UTC)
        assert e["time_zone"] == "America/New_York" and e["thank_you_reminder"] == 1
        assert e["round"] == "Second" and e["interviewers"] == "Fictional Panel"
        assert "board:changed" in done.headers["HX-Trigger-After-Settle"]


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"date": "2027-03-14", "time": "02:30"}, "time"),
        ({"time_zone": "Mars/Crater"}, "time_zone"),
        ({"date": "no"}, "date"),
        ({"duration_minutes": "0"}, "duration_minutes"),
        ({"type": "unknown"}, "type"),
        ({"all_day": "1"}, "all_day"),
    ],
)
def test_event_gap_time_and_unknown_zone_422_tied_to_fields(tmp_path, overrides, field):
    with refining(tmp_path) as r:
        a = seed(r)
        before = snapshot(r)
        answer = add(r, a.id, **overrides)
        assert answer.status_code == 422
        fields = [e for e in elements(answer.text) if e.attrs.get("name") == field]
        assert any(
            e.attrs.get("aria-invalid") == "true" and e.attrs.get("aria-describedby")
            for e in fields
        )
        assert snapshot(r) == before


def test_event_edit_versioned_and_cancel_keeps_timeline(tmp_path):
    with refining(tmp_path) as r:
        a = seed(r)
        assert add(r, a.id).status_code == 200
        e = event_row(r)
        path = f"/applications/{a.id}/events/{e['id']}"
        assert post(r, path, version="1", **event_values(time="15:00")).status_code == 200
        assert event_row(r)["version"] == 2
        before = snapshot(r)
        stale = post(r, path, version="1", **event_values(time="16:00"))
        assert stale.status_code == 409 and "Reload" in stale.text
        assert snapshot(r) == before
        assert post(r, path + "/cancel", version="2").status_code == 200
        assert event_row(r)["status"] == "cancelled" and event_row(r)["version"] == 3
        assert "Cancelled" in get(r, f"/applications/{a.id}/detail").text
        export = get(r, path + "/ics")
        vevent = Calendar.from_ical(export.content).walk("VEVENT")[0]
        assert str(vevent["STATUS"]) == "CANCELLED" and int(vevent["SEQUENCE"]) == 3


def test_ac_m5_4_ics_download_parses_and_keeps_time_zone(tmp_path):
    with refining(tmp_path) as r:
        a = seed(r)
        assert add(r, a.id).status_code == 200
        e = event_row(r)
        answer = get(r, f"/applications/{a.id}/events/{e['id']}/ics")
        assert answer.status_code == 200
        vevent = Calendar.from_ical(answer.content).walk("VEVENT")[0]
        assert str(vevent["DTSTART"].params["TZID"]) == "America/New_York"
        assert vevent.decoded("DTSTART").astimezone(UTC).hour == 19
        assert str(vevent["UID"]) == f"{e['id']}@jobpipeline.invalid"
        assert "Seed Buyer" in str(vevent["SUMMARY"])


def test_ics_download_needs_token_has_fixed_filename_and_no_side_effects(tmp_path):
    with refining(tmp_path) as r:
        a = seed(r)
        assert add(r, a.id).status_code == 200
        e = event_row(r)
        path = f"/applications/{a.id}/events/{e['id']}/ics"
        before = snapshot(r)
        assert r.client.get(path).status_code == 403
        answer = get(r, path)
        assert answer.status_code == 200
        assert answer.headers["Content-Type"].startswith("text/calendar")
        assert (
            answer.headers["Content-Disposition"] == 'attachment; filename="jobpipeline-event.ics"'
        )
        assert answer.headers["Cache-Control"] == "no-store"
        assert snapshot(r) == before
        other = seeder(r).application("other-job", "Picker", None)
        assert get(r, f"/applications/{other.id}/events/{e['id']}/ics").status_code == 404


@pytest.mark.parametrize(
    "location",
    [
        "javascript:alert(1)",
        "data:text/html,bad",
        "ftp://example.test/a",
        "https://meet.example.test/room",
        "Room 3",
    ],
)
def test_event_link_only_http_https(tmp_path, location):
    with refining(tmp_path) as r:
        a = seed(r)
        assert add(r, a.id, location=location).status_code == 200
        page = get(r, f"/applications/{a.id}/detail").text
        hrefs = [e.attrs.get("href") for e in elements(page) if e.tag == "a"]
        assert (location in hrefs) == location.startswith("https://")


def test_notes_saved_versioned_and_escaped(tmp_path):
    with refining(tmp_path) as r:
        a = seed(r)
        path = f"/applications/{a.id}/notes"
        assert post(r, path, text="<script>fictional()</script>").status_code == 200
        n = r.run.rows("SELECT * FROM application_notes")[0]
        text = get(r, f"/applications/{a.id}/detail").text
        assert "<script>fictional()" not in text and "&lt;script&gt;" in text
        edit = f"{path}/{n['id']}"
        assert post(r, edit, version="1", text="Changed note").status_code == 200
        before = snapshot(r)
        assert post(r, edit, version="1", text="Lost note").status_code == 409
        assert snapshot(r) == before
        assert post(r, path, text="   ").status_code == 422


def test_timeline_orders_and_labels_entries(tmp_path):
    with refining(tmp_path) as r:
        a = seed(r)
        r.run.clock.advance(timedelta(seconds=1))
        assert (
            post(r, f"/applications/{a.id}/notes", text="Remember the orchard.").status_code == 200
        )
        r.run.clock.advance(timedelta(seconds=1))
        assert add(r, a.id).status_code == 200
        page = get(r, f"/applications/{a.id}/detail").text
        entries = [e for e in elements(page) if e.tag == "li" and "data-timeline-kind" in e.attrs]
        assert entries and entries[0].attrs["data-timeline-kind"] == "event"
        assert {e.attrs["data-timeline-kind"] for e in entries} == {"event", "note", "status"}
        assert "Applied" in page and "Remember the orchard." in page


def test_detail_posts_need_token_origin_and_reject_unknown_fields(tmp_path):
    with refining(tmp_path) as r:
        a = seed(r)
        for suffix, data in [("events", event_values()), ("notes", {"text": "A note"})]:
            path = f"/applications/{a.id}/{suffix}"
            before = snapshot(r)
            assert r.client.post(path, data=data).status_code == 403
            assert r.client.post(path, data=data, headers=r.run.run.with_token()).status_code == 403
            assert post(r, path, **data, extra="bad").status_code == 400
            assert snapshot(r) == before


def test_ac_m5_1_and_m5_3_status_from_detail_uses_shared_move_and_followups(tmp_path):
    with refining(tmp_path) as r:
        a = seed(r)
        answer = r.client.post(
            f"/applications/{a.id}/status",
            data={
                "version": str(version_of(r, a.id)),
                "status": "interviewing",
                "source": "menu",
            },
            headers=r.run.run.action(**{"HX-Request": "true", "HX-Target": "application-detail"}),
        )
        assert answer.status_code == 200 and 'id="application-detail"' in answer.text
        assert "Add the interview date?" in answer.text
        assert (
            r.run.rows("SELECT source FROM status_history ORDER BY changed_at DESC, id DESC")[0][
                "source"
            ]
            == "drawer"
        )


def test_all_day_deadline_and_repeated_time_notice(tmp_path):
    with refining(tmp_path) as r:
        a = seed(r)
        repeated = add(r, a.id, date="2026-11-01", time="01:30")
        assert repeated.status_code == 200 and "first occurrence" in repeated.text
        answer = add(
            r, a.id, type="deadline", date="2026-11-03", all_day="1", time="", duration_minutes=""
        )
        assert answer.status_code == 200
        e = r.run.rows("SELECT * FROM events WHERE all_day=1")[0]
        answer = get(r, f"/applications/{a.id}/events/{e['id']}/ics")
        vevent = Calendar.from_ical(answer.content).walk("VEVENT")[0]
        assert str(vevent["DTSTART"].params["VALUE"]) == "DATE"
        assert str(vevent.decoded("DTSTART")) == "2026-11-03"


def test_applied_date_uses_the_board_display_zone(tmp_path):
    from zoneinfo import ZoneInfo

    from app.services.application_detail import ApplicationDetail
    from tests.fakes.ids import SequentialIdFactory

    with refining(tmp_path) as r:
        r.run.clock.set(datetime(2026, 10, 21, 0, 30, tzinfo=UTC))
        a = seed(r)
        db = r.run.app.state.workspace.db
        r.run.app.state.application_detail = ApplicationDetail(
            lambda: db,
            r.run.clock,
            SequentialIdFactory("display"),
            r.run.app.state.wiring.calendar_provider(),
            zone=ZoneInfo("America/New_York"),
        )
        assert "Applied: 2026-10-20" in r.client.get(f"/applications/{a.id}").text
