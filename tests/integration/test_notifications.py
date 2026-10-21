"""Notifications are read-only until their shown keys are posted as seen."""

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from app.services.task_views import TopBarView
from tests.board_pages import by_id, elements
from tests.reminder_desk import reminding

pytestmark = pytest.mark.integration


def seed(run):
    data = run.seeder()
    first = data.application("orchard-one", "Seed Buyer", "Larkspur Orchards", "applied")
    run.at("2026-10-02T09:00:00+00:00")
    second = data.application("orchard-two", "Tree Keeper", "Larkspur Orchards", "applied")
    run.at("2026-10-20T09:00:00+00:00")
    return first, second


def test_ac_m5_5_due_reminder_in_notifications_with_frozen_clock(tmp_path: Path):
    with reminding(tmp_path, now="2026-10-01T09:00:00+00:00") as run:
        first, second = seed(run)
        response = run.get("/notifications/panel")
        assert response.status_code == 200
        links = [e for e in elements(response.text) if e.tag == "a"]
        assert [a.attrs["href"] for a in links] == [
            f"/applications/{second.id}",
            f"/applications/{first.id}",
        ]
        assert "Tree Keeper" in links[0].text
        assert {b.text.strip() for b in elements(response.text) if b.tag == "button"} >= {
            "Done",
            "Snooze",
            "Dismiss",
        }


def test_notifications_fragment_needs_token_and_get_marks_nothing_seen(tmp_path: Path):
    with reminding(tmp_path, now="2026-10-01T09:00:00+00:00") as run:
        seed(run)
        before = run.tables()
        assert run.get("/notifications/panel", token=False).status_code == 403
        assert run.get("/notifications/count", token=False).status_code == 403
        assert run.get("/notifications/panel").status_code == 200
        assert run.get("/notifications/count").status_code == 200
        assert run.tables() == before


def test_seen_post_needs_token_origin_and_marks_shown_only(tmp_path: Path):
    with reminding(tmp_path, now="2026-10-01T09:00:00+00:00") as run:
        seed(run)
        shown = run.due()[0]
        data = {"keys": json.dumps([shown.key])}
        assert run.client.post("/notifications/seen", data=data).status_code == 403
        assert (
            run.client.post(
                "/notifications/seen", data=data, headers=run.run.with_token()
            ).status_code
            == 403
        )
        assert run.post("/notifications/seen", **data).status_code == 204
        states = run.rows("SELECT occurrence_key, seen_at, version FROM reminder_states")
        assert len(states) == 1 and states[0]["occurrence_key"] == shown.key
        assert states[0]["seen_at"] == run.clock.now().isoformat()
        assert states[0]["version"] == 1
        assert run.post("/notifications/seen", **data).status_code == 204
        assert run.rows("SELECT version FROM reminder_states")[0]["version"] == 1
        count = by_id(run.get("/notifications/count").text, "notifications-count")
        assert count is not None and count.text == "Notifications: 1 new"
        assert run.post(f"/reminders/{shown.key}/done", version="1").status_code == 200


def test_unseen_count_in_words_and_sidebar_badge(tmp_path: Path):
    with reminding(tmp_path, now="2026-10-01T09:00:00+00:00") as run:
        seed(run)
        html = run.get("/", token=False).text
        assert by_id(html, "notifications-toggle").text.strip() == "Notifications: 2 new"
        assert by_id(html, "today-link").text.strip() == "Today, 2 due"


def test_board_card_due_marker_has_text(tmp_path: Path):
    with reminding(tmp_path, now="2026-10-01T09:00:00+00:00") as run:
        first, _ = seed(run)
        card = next(
            e
            for e in elements(run.get("/board").text)
            if e.attrs.get("data-application-id") == first.id
        )
        assert "Follow-up due" in card.text


def test_tasks_needing_attention_line_links_to_tasks(tmp_path: Path):
    with reminding(tmp_path) as run:
        run.app.state.task_views = Mock()
        run.app.state.task_views.top_bar.return_value = TopBarView(
            "No tasks running", ("task-one:1",)
        )
        link = next(
            e
            for e in elements(run.get("/notifications/panel").text)
            if "data-open-tasks" in e.attrs
        )
        assert link.text == "1 task needs attention"
        assert link.attrs["href"] == "#tasks-toggle"


def test_badge_count_is_one_indexed_query(tmp_path: Path):
    with reminding(tmp_path, now="2026-10-01T09:00:00+00:00") as run:
        seed(run)
        statements = []
        run.db.connection.set_trace_callback(statements.append)
        try:
            assert run.app.state.reminders.notifications().unseen == 2
        finally:
            run.db.connection.set_trace_callback(None)
        reads = [s for s in statements if s.lstrip().upper().startswith(("SELECT", "WITH"))]
        assert len(reads) == 1
        plan = run.rows("EXPLAIN QUERY PLAN " + reads[0])
        assert any("INDEX" in str(row) for row in plan)


@pytest.mark.parametrize("keys", ["not json", "{}", '["unknown"]', '["../unsafe"]'])
def test_seen_refuses_bad_or_non_due_keys(tmp_path: Path, keys: str):
    with reminding(tmp_path) as run:
        before = run.tables()
        assert run.post("/notifications/seen", keys=keys).status_code == 400
        assert run.tables() == before
