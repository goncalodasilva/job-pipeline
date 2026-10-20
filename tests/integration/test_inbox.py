"""M4.7 page/fragment boundaries, read-only prefetch and versioned triage UI."""

import re

import pytest

from tests.job_fixtures import generate_jobs, seed_jobs
from tests.refine_harness import refining

pytestmark = pytest.mark.integration
FORM = {"HX-Request": "true", "Content-Type": "application/x-www-form-urlencoded"}


def seeded(r, count=3):
    seed_jobs(r.run.app.state.workspace.db, generate_jobs(count))


def get(r, path):
    return r.client.get(path, headers=r.run.run.htmx())


def post(r, path, **values):
    return r.client.post(path, data=values, headers=r.run.run.action(**{"HX-Request": "true"}))


def test_inbox_page_full_and_fragments(tmp_path):
    with refining(tmp_path) as r:
        seeded(r)
        full = r.client.get("/inbox")
        assert full.status_code == 200 and "<html" in full.text and 'id="inbox"' in full.text
        fragment = get(r, "/inbox")
        assert fragment.status_code == 200 and "<html" not in fragment.text
        assert 'id="inbox-card"' in fragment.text and 'id="inbox-queue"' in fragment.text
        assert "fixture0003" in fragment.text
        assert "/static/js/shortcuts.js" in full.text


@pytest.mark.parametrize(
    "path",
    [
        "/inbox?view=snoozed",
        "/inbox/jobs/job-0001/card",
        "/inbox/jobs/job-0001/detail",
        "/inbox/queue?view=queue",
        "/settings/shortcuts",
    ],
)
def test_inbox_fragments_need_token(tmp_path, path):
    with refining(tmp_path) as r:
        seeded(r)
        assert r.client.get(path, headers={"HX-Request": "true"}).status_code == 403
        assert get(r, path).status_code == 200


@pytest.mark.parametrize(
    "path", ["/inbox/jobs/job-0001/seen", "/inbox/jobs/job-0001/triage", "/settings/shortcuts"]
)
def test_triage_posts_need_token_and_origin(tmp_path, path):
    with refining(tmp_path) as r:
        seeded(r)
        assert r.client.post(path, data={"version": "1", "action": "remove"}).status_code == 403
        assert (
            r.client.post(
                path,
                data={"version": "1", "action": "remove"},
                headers={**r.run.run.htmx(), "Origin": "https://hostile.example.test"},
            ).status_code
            == 403
        )
        assert (
            r.run.rows("SELECT triage_state FROM jobs WHERE id='job-0001'")[0]["triage_state"]
            == "new"
        )


def test_prefetch_get_marks_nothing_seen(tmp_path):
    with refining(tmp_path) as r:
        seeded(r)
        before = r.run.rows("SELECT * FROM jobs")
        for path in (
            "/inbox",
            "/inbox/jobs/job-0003/card",
            "/inbox/jobs/job-0002/card",
            "/inbox/jobs/job-0003/detail",
        ):
            assert get(r, path).status_code == 200
        assert r.run.rows("SELECT * FROM jobs") == before
        shown = post(r, "/inbox/jobs/job-0003/seen", version="1")
        assert shown.status_code == 200 and shown.json() == {"version": 2, "state": "seen"}
        assert (
            r.run.rows("SELECT triage_state FROM jobs WHERE id='job-0002'")[0]["triage_state"]
            == "new"
        )
        assert post(r, "/inbox/jobs/job-0003/seen", version="2").json()["version"] == 2


def test_ac_m4_8_card_shows_industry_department_experience_salary(tmp_path):
    with refining(tmp_path) as r:
        seeded(r, 1)
        r.run.execute(
            "UPDATE jobs SET department='Fictional systems',experience_min=3,"
            "experience_max=5,salary_min=55000,salary_max=65000,salary_currency='EUR',"
            "salary_period='year',posted_date='2026-01-01' WHERE id='job-0001'"
        )
        shown = get(r, "/inbox/jobs/job-0001/card")
        for text in (
            "Fictional agriculture",
            "Fictional systems",
            "remote",
            "3\u20135 years",
            "55,000",
            "65,000",
            "EUR",
            "year",
            "Posted",
        ):
            assert text in shown.text
        assert "Strong" in shown.text and "Conflicts with your preferences" in shown.text


def test_card_omits_absent_fields(tmp_path):
    with refining(tmp_path) as r:
        seeded(r, 1)
        shown = get(r, "/inbox/jobs/job-0001/card")
        assert "Salary:" not in shown.text and "Experience:" not in shown.text
        assert ">None<" not in shown.text


def test_processing_card_refetches_on_task_events(tmp_path):
    with refining(tmp_path) as r:
        seeded(r, 1)
        r.run.hold.hold = True
        task = r.run.app.state.job_summary.start("job-0001")
        r.run.wait_entered(1)
        try:
            shown = get(r, "/inbox/jobs/job-0001/card")
            assert "Processing" in shown.text
            assert (
                "tasks:resync from:body" in shown.text and "tasks:updated from:body" in shown.text
            )
        finally:
            r.run.hold.open()
        assert r.run.wait_terminal(task.id).status.value == "succeeded"
        shown = get(r, "/inbox/jobs/job-0001/card")
        assert "Processing" not in shown.text
        assert "Practice data" in shown.text


def test_empty_inbox_offers_add_job(tmp_path):
    with refining(tmp_path) as r:
        shown = r.client.get("/inbox")
        assert "No jobs yet. Add one by link or by pasting the ad." in shown.text
        assert "+ Add job" in shown.text
        assert "job-text" in shown.text and "job-url" in shown.text


def test_triage_response_undo_and_removed_view_restore(tmp_path):
    with refining(tmp_path) as r:
        seeded(r)
        removed = post(
            r,
            "/inbox/jobs/job-0003/triage",
            version="1",
            action="remove",
            reason="salary",
            next_id="job-0002",
        )
        assert removed.status_code == 200 and "Removed. Undo available." in removed.text
        assert 'data-undo-seconds="10"' in removed.text
        assert "job-0002" in removed.text
        stale = post(r, "/inbox/jobs/job-0003/triage", version="1", action="undo")
        assert stale.status_code == 409 and "Reload the page" in stale.text
        undone = post(r, "/inbox/jobs/job-0003/triage", version="2", action="undo")
        assert undone.status_code == 200
        assert (
            r.run.rows("SELECT triage_state FROM jobs WHERE id='job-0003'")[0]["triage_state"]
            == "new"
        )
        post(r, "/inbox/jobs/job-0003/triage", version="3", action="remove")
        assert "fixture0003" in get(r, "/inbox?view=removed").text
        restored = post(
            r, "/inbox/jobs/job-0003/triage", version="4", action="restore", view="removed"
        )
        assert restored.status_code == 200
        assert (
            r.run.rows("SELECT triage_state FROM jobs WHERE id='job-0003'")[0]["triage_state"]
            == "seen"
        )


def test_invalid_reason_text_422_tied_to_field(tmp_path):
    with refining(tmp_path) as r:
        seeded(r, 1)
        shown = post(
            r,
            "/inbox/jobs/job-0001/triage",
            version="1",
            action="remove",
            reason="other",
            text="x" * 501,
        )
        assert shown.status_code == 422 and 'aria-invalid="true"' in shown.text
        assert "500" in shown.text
        assert r.run.rows("SELECT triage_state FROM jobs")[0]["triage_state"] == "new"


def test_shortcuts_setting_saved_with_version(tmp_path):
    with refining(tmp_path) as r:
        assert "Use single-key shortcuts" in r.client.get("/settings").text
        shown = post(r, "/settings/shortcuts", version="0")
        assert shown.status_code == 200 and "Saved." in shown.text
        assert 'data-shortcuts="false"' in r.client.get("/inbox").text
        stale = post(r, "/settings/shortcuts", version="0", enabled="1")
        assert stale.status_code == 409 and "Reload the page" in stale.text
        saved = post(r, "/settings/shortcuts", version="1", enabled="1")
        assert saved.status_code == 200 and 'data-shortcuts="true"' in r.client.get("/inbox").text


@pytest.mark.parametrize("id", ["missing", "bad!id", "x" * 80])
def test_unknown_or_unsafe_job_id_gets_the_fixed_404_on_every_route(tmp_path, id):
    with refining(tmp_path) as r:
        seeded(r)
        for suffix in ("card", "detail"):
            shown = get(r, f"/inbox/jobs/{id}/{suffix}")
            assert shown.status_code == 404 and id not in shown.text
        forms = (
            ("seen", {"version": "1"}),
            ("triage", {"version": "1", "action": "remove"}),
            ("triage", {"version": "1", "action": "to_apply"}),
            # A refused value never turns an unknown job into a field error.
            ("triage", {"version": "1", "action": "snooze", "choice": "date", "picked": "junk"}),
            (
                "triage",
                {"version": "1", "action": "snooze", "choice": "date", "picked": "2000-01-01"},
            ),
            ("triage", {"version": "1", "action": "remove", "reason": "nonsense"}),
        )
        fixed = get(r, "/inbox/jobs/missing/card").text
        for suffix, values in forms:
            shown = post(r, f"/inbox/jobs/{id}/{suffix}", **values)
            assert shown.status_code == 404 and shown.text == fixed and id not in shown.text, values


def test_card_and_preview_escape_posting_text(tmp_path):
    with refining(tmp_path) as r:
        seeded(r, 1)
        r.run.execute(
            "UPDATE jobs SET title='<img src=x onerror=alert(1)>',"
            "snapshot_text='<script>fictional attack</script>' WHERE id='job-0001'"
        )
        for suffix in ("card", "detail"):
            shown = get(r, f"/inbox/jobs/job-0001/{suffix}")
            assert "<img src=x" not in shown.text and "<script>fictional attack" not in shown.text
            assert "&lt;img" in shown.text


def card_json_state(r, id):
    return r.run.rows("SELECT triage_state,version FROM jobs WHERE id=?", (id,))[0]


def test_card_source_host_and_other_sites_or_pasted_text(tmp_path):
    with refining(tmp_path) as r:
        seeded(r, 2)
        for n, (job, url, kind) in enumerate(
            (
                ("job-0001", "https://careers.example.test/a", "link"),
                ("job-0001", "https://board.example.test/a", "link"),
                ("job-0002", None, "pasted"),
            ),
            1,
        ):
            r.run.execute(
                "INSERT INTO job_sources (id,job_id,url,canonical_url,kind,found_at,created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (f"src-{n}", job, url, url, kind, f"2026-10-20T09:0{n}:00+00:00", "2026-10-20"),
            )
        linked = get(r, "/inbox/jobs/job-0001/card").text
        assert "Source:" in linked and "careers.example.test" in linked
        assert "also posted on 1 other site" in linked
        assert 'rel="noopener noreferrer"' in linked
        pasted = get(r, "/inbox/jobs/job-0002/card").text
        assert "Pasted text" in pasted and "also posted" not in pasted


def test_views_show_state_with_restore(tmp_path):
    with refining(tmp_path) as r:
        seeded(r)
        for id in ("job-0001", "job-0002", "job-0003"):
            post(r, f"/inbox/jobs/{id}/seen", version="1")
        assert (
            post(r, "/inbox/jobs/job-0003/triage", version="2", action="remove").status_code == 200
        )
        assert (
            post(r, "/inbox/jobs/job-0002/triage", version="2", action="to_apply").status_code
            == 200
        )
        snoozed = post(
            r, "/inbox/jobs/job-0001/triage", version="2", action="snooze", choice="tomorrow"
        )
        assert snoozed.status_code == 200 and "Snoozed until" in snoozed.text
        assert snoozed.headers["HX-Trigger-After-Settle"].startswith('{"announce": "Snoozed until ')
        for view, id, label, button in (
            ("removed", "job-0003", "Removed", ">Restore<"),
            ("to_apply", "job-0002", "To apply", ">Back to queue<"),
            ("snoozed", "job-0001", "Snoozed until", ">Back to queue<"),
        ):
            shown = get(r, f"/inbox/queue?view={view}")
            assert id in shown.text and label in shown.text and button in shown.text
            # Removed and To apply cards have left the queue; a snoozed one can still be
            # applied for, removed or snoozed again (ADR-098 M4.2 item 4).
            queue_actions = 'data-action="apply"' in shown.text
            assert queue_actions is (view == "snoozed"), view
            assert ('id="inbox-menu"' in shown.text) is (view == "snoozed"), view
        # Restore from To apply and Snoozed goes back to where the job was; Removed to Seen.
        for id, version, view in (("job-0002", 3, "to_apply"), ("job-0001", 3, "snoozed")):
            back = post(
                r, f"/inbox/jobs/{id}/triage", version=str(version), action="restore", view=view
            )
            assert back.status_code == 409  # Restore is only for Removed; those use Undo
        undone = post(r, "/inbox/jobs/job-0002/triage", version="3", action="undo", view="to_apply")
        assert undone.status_code == 200
        assert card_json_state(r, "job-0002")["triage_state"] == "seen"


def test_view_change_announces_and_empty_views_explain(tmp_path):
    with refining(tmp_path) as r:
        seeded(r, 1)
        shown = get(r, "/inbox/queue?view=removed&focus=1")
        assert shown.status_code == 200 and "No removed jobs." in shown.text
        assert shown.headers["HX-Trigger-After-Settle"] == '{"announce": "Removed: 0 jobs."}'
        assert "<html" not in shown.text
        quiet = get(r, "/inbox/queue?view=queue")
        assert "HX-Trigger-After-Settle" not in quiet.headers
        assert "data-autofocus" not in quiet.text
        junk = get(r, "/inbox/queue?view=bogus&page=-4&selected=%3Cx%3E")
        assert junk.status_code == 200 and "fixture0001" in junk.text


def test_card_route_announces_and_focuses_only_when_asked(tmp_path):
    with refining(tmp_path) as r:
        seeded(r)
        asked = get(
            r,
            "/inbox/jobs/job-0002/card?focus=1&position=2&total=3&previous=job-0003&next=job-0001",
        )
        assert 'data-announce="Job 2 of 3: ' in asked.text and "data-autofocus" in asked.text
        assert 'data-nav="next"' in asked.text and 'data-nav="previous"' in asked.text
        plain = get(r, "/inbox/jobs/job-0002/card")
        assert "data-announce" not in plain.text and "data-autofocus" not in plain.text
        hostile = get(r, "/inbox/jobs/job-0002/card?previous=%22%3E%3Cscript%3E&next=a%20b&view=x")
        assert hostile.status_code == 200 and "<script>" not in hostile.text


def test_snooze_by_date_and_refusals_tied_to_the_date_field(tmp_path):
    with refining(tmp_path) as r:
        seeded(r, 1)
        post(r, "/inbox/jobs/job-0001/seen", version="1")
        for picked in ("2020-01-01", "not-a-date", ""):
            refused = post(
                r,
                "/inbox/jobs/job-0001/triage",
                version="2",
                action="snooze",
                choice="date",
                picked=picked,
            )
            assert refused.status_code == 422
            assert 'id="snooze-date"' in refused.text and 'aria-invalid="true"' in refused.text
            assert '<details class="card-menu" open>' in refused.text
        assert card_json_state(r, "job-0001") == {"triage_state": "seen", "version": 2}
        ok = post(
            r,
            "/inbox/jobs/job-0001/triage",
            version="2",
            action="snooze",
            choice="date",
            picked="2099-01-05",
        )
        assert ok.status_code == 200 and "Snoozed until Mon 5 Jan." in ok.text
        # A picked date the user typed is not read for the quick choices.
        post(r, "/inbox/jobs/job-0001/triage", version="3", action="undo")
        quick = post(
            r,
            "/inbox/jobs/job-0001/triage",
            version="4",
            action="snooze",
            choice="next_week",
            picked="junk",
        )
        assert quick.status_code == 200


def test_triage_and_seen_posts_refuse_bad_forms(tmp_path):
    with refining(tmp_path) as r:
        seeded(r, 1)
        path = "/inbox/jobs/job-0001/triage"
        for values in (
            {"version": "1", "action": "delete"},
            {"version": "1"},
            {"version": "x", "action": "remove"},
            {"version": "0", "action": "remove"},
            {"version": "1", "action": "remove", "surprise": "1"},
        ):
            assert post(r, path, **values).status_code == 400, values
        assert post(r, "/inbox/jobs/job-0001/seen", version="1", extra="1").status_code == 400
        assert post(r, "/inbox/jobs/job-0001/seen", version="nope").status_code == 400
        assert post(r, "/inbox/jobs/missing/seen", version="1").status_code == 404
        assert (
            post(r, "/inbox/jobs/missing/triage", version="1", action="remove").status_code == 404
        )
        assert post(r, "/inbox/jobs/job-0001/seen", version="7").status_code == 409
        assert card_json_state(r, "job-0001") == {"triage_state": "new", "version": 1}
        # A field given twice is refused too (a form that posted two reasons).
        twice = r.client.post(
            path,
            content="version=1&action=remove&reason=salary&reason=other",
            headers=r.run.run.action(**FORM),
        )
        assert twice.status_code == 400
        assert card_json_state(r, "job-0001") == {"triage_state": "new", "version": 1}


def test_remove_reason_text_only_for_other_and_kept_when_refused(tmp_path):
    with refining(tmp_path) as r:
        seeded(r, 1)
        refused = post(
            r,
            "/inbox/jobs/job-0001/triage",
            version="1",
            action="remove",
            reason="salary",
            text="Too low",
        )
        assert (
            refused.status_code == 422
            and "Add text only for Not interested or Other." in refused.text
        )
        assert "data-reopen" in refused.text and "Too low" in refused.text
        bad = post(
            r, "/inbox/jobs/job-0001/triage", version="1", action="remove", reason="nonsense"
        )
        assert bad.status_code == 422 and "Choose one of the listed reasons" in bad.text
        ok = post(
            r,
            "/inbox/jobs/job-0001/triage",
            version="1",
            action="remove",
            reason="other",
            text="A fictional reason",
        )
        assert ok.status_code == 200
        row = r.run.rows("SELECT removal_reason,removal_text FROM jobs WHERE id='job-0001'")[0]
        assert row == {"removal_reason": "other", "removal_text": "A fictional reason"}


def test_shortcuts_form_is_validated_and_stale_save_keeps_no_form(tmp_path):
    with refining(tmp_path) as r:
        assert post(r, "/settings/shortcuts", version="0", enabled="yes").status_code == 400
        assert post(r, "/settings/shortcuts", version="-1").status_code == 400
        assert post(r, "/settings/shortcuts", version="0", extra="1").status_code == 400
        twice = r.client.post(
            "/settings/shortcuts",
            content="version=0&enabled=1&enabled=",
            headers=r.run.run.action(**FORM),
        )
        assert twice.status_code == 400
        assert post(r, "/settings/shortcuts", version="0").status_code == 200
        stale = post(r, "/settings/shortcuts", version="0", enabled="1")
        assert stale.status_code == 409 and "<form" not in stale.text  # no Save over a newer value
        assert "Use single-key shortcuts" in get(r, "/settings/shortcuts").text


def test_help_lists_shortcuts_or_says_they_are_off(tmp_path):
    with refining(tmp_path) as r:
        on = r.client.get("/inbox").text
        assert "<kbd>J</kbd>" in on and 'data-shortcuts="true"' in on
        post(r, "/settings/shortcuts", version="0")
        off = r.client.get("/inbox").text
        assert "Single-key shortcuts are off." in off and "<kbd>J</kbd>" not in off


def test_snooze_date_has_its_own_form_so_enter_cannot_pick_tomorrow(tmp_path):
    with refining(tmp_path) as r:
        seeded(r, 1)
        post(r, "/inbox/jobs/job-0001/seen", version="1")
        html = get(r, "/inbox/jobs/job-0001/card").text
        forms = [part.split("</form>")[0] for part in html.split("<form")[1:]]
        with_date = [form for form in forms if 'name="picked"' in form]
        assert len(with_date) == 1
        assert 'value="tomorrow"' not in with_date[0] and 'value="date"' in with_date[0]
        assert with_date[0].count('type="submit"') == 1


def test_remove_dialog_has_a_real_other_button_and_no_second_reason_field(tmp_path):
    with refining(tmp_path) as r:
        seeded(r, 1)
        html = get(r, "/inbox/jobs/job-0001/card").text
        dialog = html.split('id="remove-dialog"')[1].split("</dialog>")[0]
        assert 'type="hidden" name="reason"' not in dialog
        assert dialog.count('name="reason"') == 8  # seven chips and Other, each one value
        start = dialog.index('value="other"')
        other = dialog[dialog.rindex("<button", 0, start) : dialog.index(">", start)]
        assert 'type="submit"' in other and "data-other-reason" in other
        assert 'aria-expanded="false"' in other
        assert 'role="dialog"' not in html and "aria-modal" not in html


def test_letters_and_key_hints_only_while_shortcuts_are_on(tmp_path):
    with refining(tmp_path) as r:
        seeded(r, 1)
        post(r, "/inbox/jobs/job-0001/seen", version="1")
        on = get(r, "/inbox/jobs/job-0001/card").text
        assert (
            'class="key"' in on and 'aria-keyshortcuts="A"' in on and 'aria-keyshortcuts="R"' in on
        )
        removed = post(r, "/inbox/jobs/job-0001/triage", version="2", action="remove")
        assert 'aria-keyshortcuts="Z"' in removed.text
        page = r.client.get("/inbox").text
        assert "<kbd>Z</kbd>" in page
        post(r, "/settings/shortcuts", version="0")
        off = get(r, "/inbox/jobs/job-0001/card").text
        assert 'class="key"' not in off and "aria-keyshortcuts" not in off
        assert (
            "aria-keyshortcuts"
            not in post(r, "/inbox/jobs/job-0001/triage", version="3", action="undo").text
        )
        assert "<kbd>" not in r.client.get("/inbox").text


def test_details_button_and_navigation_in_every_view(tmp_path):
    with refining(tmp_path) as r:
        seeded(r)
        for id in ("job-0001", "job-0002", "job-0003"):
            post(r, f"/inbox/jobs/{id}/seen", version="1")
            post(r, f"/inbox/jobs/{id}/triage", version="2", action="remove")
        shown = get(r, "/inbox/jobs/job-0002/card?view=removed&previous=job-0003&next=job-0001")
        assert 'aria-haspopup="dialog"' in shown.text
        assert 'id="inbox-details-button"' in shown.text
        assert 'data-nav="next"' in shown.text and 'data-nav="previous"' in shown.text


def card_id(html):
    """The job the focused card shows."""
    return re.search(r'<article id="inbox-card"[^>]*data-job-id="([^"]+)"', html).group(1)


def many(r, count):
    seed_jobs(r.run.app.state.workspace.db, generate_jobs(count))


def test_page_two_survives_removal_refusal_and_undo(tmp_path):
    """51 jobs: the oldest is alone on page 2. Taking it away must not show the empty
    inbox while 50 jobs remain; a refused answer and Undo keep the page they came from."""
    with refining(tmp_path) as r:
        many(r, 51)
        second = get(r, "/inbox/queue?page=2")
        assert 'data-job-id="job-0001"' in second.text and "Earlier jobs" in second.text
        # A refusal keeps page 2 and the card the user was on.
        refused = post(
            r,
            "/inbox/jobs/job-0001/triage",
            version="1",
            action="remove",
            reason="other",
            text="x" * 501,
            page="2",
        )
        assert refused.status_code == 422
        assert 'name="page" value="2"' in refused.text
        assert card_id(refused.text) == "job-0001"
        # Removing the only card of the last page falls back to the page before.
        removed = post(r, "/inbox/jobs/job-0001/triage", version="1", action="remove", page="2")
        assert removed.status_code == 200
        assert "No jobs yet" not in removed.text and "last one" not in removed.text
        assert card_id(removed.text) == "job-0051"
        assert 'name="page" value="1"' in removed.text
        # Undo carries the page it was made on and shows the restored job again.
        undo = removed.text.split('id="inbox-undo"')[1].split("</form>")[0]
        assert 'name="page" value="2"' in undo
        undone = post(r, "/inbox/jobs/job-0001/triage", version="2", action="undo", page="2")
        assert undone.status_code == 200
        assert card_id(undone.text) == "job-0001"
        assert 'name="page" value="2"' in undone.text


def test_last_card_gone_gives_a_warm_focusable_empty_state(tmp_path):
    with refining(tmp_path) as r:
        first_run = r.client.get("/inbox").text
        assert "No jobs yet. Add one by link or by pasting the ad." in first_run
        assert "That was the last one" not in first_run
        assert 'id="inbox-empty-heading"' in first_run
        seeded(r, 1)
        post(r, "/inbox/jobs/job-0001/seen", version="1")
        done = post(r, "/inbox/jobs/job-0001/triage", version="2", action="to_apply")
        assert "That was the last one. Add another by link or by pasting the ad." in done.text
        heading = done.text.split('id="inbox-empty-heading"')[1].split(">")[0]
        assert 'tabindex="-1"' in heading and "data-autofocus" in heading
        # Asking for a view without focus (a refresh) takes nothing.
        quiet = get(r, "/inbox/queue?view=queue")
        assert "data-autofocus" not in quiet.text
        focused = get(r, "/inbox/queue?view=removed&focus=1")
        assert "data-autofocus" in focused.text.split('id="inbox-empty-heading"')[1].split(">")[0]


def test_refusal_answer_echoes_only_text_and_picked(tmp_path):
    with refining(tmp_path) as r:
        seeded(r, 1)
        shown = post(
            r,
            "/inbox/jobs/job-0001/triage",
            version="1",
            action="remove",
            reason="other",
            text="x" * 501,
            next_id="job-9999",
        )
        assert shown.status_code == 422
        assert "x" * 501 in shown.text
        assert 'value="job-9999"' not in shown.text
