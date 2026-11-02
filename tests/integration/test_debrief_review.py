"""Review fixes for the debrief (M8.3 part A): the token-free page holds no saved text, format
characters and tabs, an atomic status confirmation, the planned tier, the scope of Save and Not
needed, and the UX details of the form and its states. Fictional data; the desk, helpers and
markup readers are those of ``test_debrief.py`` and ``tests/debrief_pages.py``.
"""

import re

import pytest

from app.domain.settings import AI_PROVIDER, AIProviderSettings
from app.services.settings import SettingsService
from tests.board_pages import elements
from tests.debrief_pages import (
    DISMISSED,
    DRAFTS_STARTED,
    LABELS,
    PARAGRAPH_ONE,
    SAVED,
    control,
    draft_items,
    has,
    hidden_value,
    inside,
    one,
    says,
    texts_shown,
)
from tests.fakes.ai import load_ai_fixture
from tests.integration.test_board import snapshot
from tests.integration.test_debrief import (
    ASKED,
    EXPERIENCE,
    HOW,
    TASK,
    Desk,
    sightings_of,
)
from tests.refine_harness import refining
from tests.tailoring_pages import announced

pytestmark = pytest.mark.integration

FIXED_404 = "This interview wasn't found."


def htmx_get(r, path: str):
    return r.client.get(path, headers=r.run.run.htmx())


# --- item 1: the token-free page holds no saved text -------------------------------------------


def test_debrief_token_free_page_holds_no_saved_text_but_the_token_fragment_does(tmp_path):
    texts = {
        "questions": f"Describe {ASKED} work?",
        "how_it_went": f"It went {HOW}.",
        "experiences": PARAGRAPH_ONE + f" {EXPERIENCE}",
    }
    with refining(tmp_path) as r:
        d = Desk(r)
        d.save(**texts)
        d.drafted()
        draft_words = "Planned 12 routes."

        page = r.client.get(d.path())
        fragment = d.get()

        assert page.status_code == 200 and "<html" in page.text
        for text in (*texts.values(), draft_words, ASKED, HOW, EXPERIENCE):
            assert text not in page.text and text not in says(page.text), text
        for marker in (ASKED, HOW, EXPERIENCE, draft_words):
            assert marker in fragment.text, marker
        # The shell: heading, time, privacy line; no form, no read-only texts, no drafts list.
        assert one(page.text, "debrief-heading") is not None
        assert "Interview on" in says(page.text) and "stays on this computer" in says(page.text)
        assert texts_shown(page.text) == {} and not has(page.text, "debrief-drafts-made")
        # The placeholder loads the fragment through a token-protected request on load.
        holder = one(page.text, "debrief")
        assert holder is not None
        assert holder.attrs["hx-get"] == d.path()
        assert holder.attrs["hx-trigger"] == "load"
        assert holder.attrs["hx-swap"] == "outerHTML"
        assert r.client.get(d.path(), headers={"HX-Request": "true"}).status_code == 403
        loaded = htmx_get(r, str(holder.attrs["hx-get"]))
        assert loaded.status_code == 200 and EXPERIENCE in loaded.text


@pytest.mark.parametrize("state", ["open", "saved", "dismissed"])
def test_debrief_token_free_page_is_a_shell_in_every_state(tmp_path, state):
    with refining(tmp_path) as r:
        d = Desk(r)
        if state == "saved":
            d.save(questions="A fictional question?", how_it_went="calm")
        elif state == "dismissed":
            d.post("/dismiss", {"version": "0"})

        page = r.client.get(d.path()).text

        for name in ("debrief-form", "debrief-save", "debrief-drafts", "debrief-status"):
            assert not has(page, name), name
        assert not [
            e for e in elements(page) if e.tag in {"textarea", "form"} and "debrief" in str(e.attrs)
        ]
        holder = one(page, "debrief")
        assert holder is not None and holder.attrs["hx-trigger"] == "load"
        assert one(page, "debrief-heading") is not None
        assert "Back to the application" in says(page)


# --- item 2: format characters, "how it went" and tabs -----------------------------------------

FORMAT = [0x202E, 0x2028, 0x200B, 0x200D, 0xFEFF, 0x2060, 0xE0041]


@pytest.mark.parametrize("code", FORMAT, ids=lambda c: f"U+{c:04X}")
@pytest.mark.parametrize("field", ["questions", "how_it_went", "experiences"])
def test_debrief_save_refuses_hidden_and_format_characters_in_every_text(tmp_path, field, code):
    with refining(tmp_path) as r:
        d = Desk(r)
        before = snapshot(r)

        refused = d.save(**{field: f"Fictional {chr(code)}text"})

        assert refused.status_code == 422, refused.text[:200]
        assert control(refused.text, LABELS[field]).attrs.get("aria-invalid") == "true"
        assert snapshot(r) == before and d.row() is None


def test_debrief_how_it_went_keeps_newlines_and_spacing(tmp_path):
    """A pin: newlines stay in the notes."""
    with refining(tmp_path) as r:
        d = Desk(r)

        saved = d.save(how_it_went="  Calm.\r\nFriendly.\n\nGood.  ")

        assert saved.status_code == 200
        assert d.row()["how_it_went"] == "  Calm.\nFriendly.\n\nGood.  "


def test_debrief_tabs_become_spaces_in_every_text(tmp_path):
    with refining(tmp_path) as r:
        d = Desk(r)

        saved = d.save(
            questions="Why\tthis role?\n\tWalk me through it?",
            how_it_went="Calm.\tFriendly.",
            experiences="I led\tthe team.\n\n\t\n\nSecond.",
        )

        assert saved.status_code == 200, saved.text[:300]
        row = d.row()
        assert row["questions_text"] == "Why this role?\nWalk me through it?"
        assert row["how_it_went"] == "Calm. Friendly."
        assert row["experiences_text"] == "I led the team.\n\nSecond."
        assert "\t" not in "".join(str(v) for v in row.values())
        assert {q["text"] for q in d.asked()} == {"Why this role?", "Walk me through it?"}


def test_debrief_tabs_typed_before_a_refusal_come_back_as_spaces(tmp_path):
    with refining(tmp_path) as r:
        d = Desk(r)

        refused = d.save(how_it_went="Calm.\tFriendly.", experiences="x" * 4001)

        assert refused.status_code == 422
        raw = re.search(r'<textarea[^>]*id="debrief-how"[^>]*>(.*?)</textarea>', refused.text, re.S)
        assert raw is not None and raw.group(1) == "Calm. Friendly."


# --- item 3: the status confirmation is atomic -------------------------------------------------


def test_application_service_move_in_joins_the_callers_transaction(tmp_path):
    with refining(tmp_path) as r:
        d = Desk(r)
        service = r.run.app.state.applications
        version = d.app_version()
        before = snapshot(r)

        with pytest.raises(RuntimeError, match="fictional"), d.db.transaction() as conn:
            service.move_in(conn, d.aid, "screening", expected_version=version, source="board")
            raise RuntimeError("fictional failure after the move")

        assert snapshot(r) == before, "the move was rolled back with the caller's transaction"
        with d.db.transaction() as conn:
            service.move_in(conn, d.aid, "screening", expected_version=version, source="board")
        status = r.run.rows("SELECT status_key,version FROM applications WHERE id=?", (d.aid,))
        assert status == [{"status_key": "screening", "version": version + 1}]


def test_debrief_status_confirm_is_one_transaction_when_the_write_fails(tmp_path):
    with refining(tmp_path) as r:
        d = Desk(r)
        d.save(next_step="next_round")
        before = snapshot(r)
        r.run.execute(
            "CREATE TRIGGER fictional_abort BEFORE UPDATE ON debriefs "
            "WHEN NEW.status_confirmed=1 BEGIN SELECT RAISE(ABORT,'fictional failure'); END"
        )

        failed = d.post("/status", {"version": str(d.app_version())})

        assert failed.status_code >= 400
        r.run.execute("DROP TRIGGER fictional_abort")
        assert snapshot(r) == before, "neither the move nor the confirmation was kept"
        assert d.row()["status_confirmed"] == 0
        # The control: without the fault the same confirmation works.
        ok = d.post("/status", {"version": str(d.app_version())})
        assert ok.status_code == 200 and d.row()["status_confirmed"] == 1


def test_debrief_status_confirm_rolls_the_move_back_when_the_debrief_changed(tmp_path, monkeypatch):
    from app.services import debrief as debrief_service

    with refining(tmp_path) as r:
        d = Desk(r)
        d.save(next_step="offer")
        before = snapshot(r)
        monkeypatch.setattr(
            debrief_service.debrief_repo, "mark_status_confirmed", lambda *a, **k: False
        )

        refused = d.post("/status", {"version": str(d.app_version())})

        assert refused.status_code == 409 and "Reload" in refused.text
        assert snapshot(r) == before, "the move did not stay when the confirmation was refused"
        assert r.run.rows("SELECT status_key FROM applications WHERE id=?", (d.aid,)) == [
            {"status_key": "applied"}
        ]


# --- item 4: the planned tier is the tier of the request sent ----------------------------------


@pytest.mark.parametrize(
    ("overrides", "planned"),
    [
        ({}, "fast"),
        ({"structure_answer": "thorough"}, "thorough"),
        ({"structure_answer": "fast", "debrief_facts": "thorough"}, "fast"),
        ({"debrief_facts": "thorough"}, "fast"),
    ],
    ids=["no-override", "structure-answer-thorough", "debrief-facts-ignored", "only-debrief-facts"],
)
def test_debrief_plan_uses_the_tier_the_structure_answer_request_will_use(
    tmp_path, overrides, planned
):
    with refining(tmp_path) as r:
        d = Desk(r)
        d.save(experiences=PARAGRAPH_ONE)
        settings = SettingsService(d.db, r.run.clock)
        loaded = settings.load(AI_PROVIDER)
        changed: AIProviderSettings = loaded.value.model_copy(update={"task_tiers": overrides})
        settings.save(AI_PROVIDER, changed, expected_version=loaded.version)

        plan = r.run.app.state.debrief.plan_start(d.aid, d.event_id)
        d.drafted()

        assert plan.tier == planned
        used = r.run.rows(
            "SELECT tier FROM usage_records WHERE task_type=? ORDER BY rowid", (TASK,)
        )
        assert used and {row["tier"] for row in used} == {plan.tier}, "the request's own tier"


def test_debrief_an_explicit_fast_run_is_planned_and_used(tmp_path):
    """A pin: the budget dialog's one-off Fast wins over the task's own override."""
    with refining(tmp_path) as r:
        d = Desk(r)
        d.save(experiences=PARAGRAPH_ONE)
        settings = SettingsService(d.db, r.run.clock)
        loaded = settings.load(AI_PROVIDER)
        changed = loaded.value.model_copy(update={"task_tiers": {"structure_answer": "thorough"}})
        settings.save(AI_PROVIDER, changed, expected_version=loaded.version)

        plan = r.run.app.state.debrief.plan_start(d.aid, d.event_id, tier="fast")
        d.drafted(tier="fast")

        assert plan.tier == "fast"
        used = r.run.rows("SELECT tier FROM usage_records WHERE task_type=?", (TASK,))
        assert used and {row["tier"] for row in used} == {"fast"}


# --- item 5: scope of Save and Not needed ------------------------------------------------------


def test_debrief_cancelled_event_changes_nothing_when_saved_or_dismissed(tmp_path):
    with refining(tmp_path) as r:
        d = Desk(r)
        cancelled = d.interview(
            "cancelled-event",
            status="cancelled",
            start="2026-10-19T09:00:00+00:00",
            end="2026-10-19T10:00:00+00:00",
        )
        before = snapshot(r)
        action = r.run.run.action(**{"HX-Request": "true"})

        saved = r.client.post(
            d.path("/save", cancelled),
            data={"version": "0", "questions": "A fictional question?", "next_step": "offer"},
            headers=action,
        )
        dismissed = r.client.post(
            d.path("/dismiss", cancelled), data={"version": "0"}, headers=action
        )

        assert (saved.status_code, saved.text) == (404, FIXED_404)
        assert (dismissed.status_code, dismissed.text) == (404, FIXED_404)
        assert snapshot(r) == before


def test_debrief_save_and_dismiss_still_work_before_the_interview_ends(tmp_path):
    """The page stays reachable and usable for an interview that was shorter than planned."""
    with refining(tmp_path) as r:
        d = Desk(r)
        coming = d.interview(
            "coming-event", start="2026-10-20T11:30:00+00:00", end="2026-10-20T13:00:00+00:00"
        )

        assert d.get(coming).status_code == 200
        assert d.save(coming, questions="A fictional question?").status_code == 200
        assert d.row(coming)["state"] == "done"


# --- item 6: the form -------------------------------------------------------------------------


def fragment_of(d: Desk) -> str:
    return d.get().text


def test_debrief_form_says_it_is_optional_and_offers_not_needed_beside_save(tmp_path):
    with refining(tmp_path) as r:
        d = Desk(r)

        page = fragment_of(d)

        assert "Optional: add only what you want to remember." in says(page)
        save = one(page, "debrief-save")
        assert save is not None and save.text == "Save debrief"
        form = one(page, "debrief-form")
        assert form is not None
        forms = [
            e
            for e in elements(page)
            if e.tag == "form" and e.attrs.get("action", "").endswith("/dismiss")
        ]
        [dismiss] = forms
        assert not dismiss.within(form) and dismiss.attrs["hx-post"] == d.path("/dismiss")
        assert dismiss.attrs["hx-target"] == "#debrief"
        values = {
            str(e.attrs["name"]): e.attrs.get("value")
            for e in inside(page, dismiss, lambda e: e.tag == "input")
        }
        assert values == {"version": "0"}, "no return=today from the page"
        [button] = inside(page, dismiss, lambda e: e.tag == "button")
        assert button.text == "Not needed"
        # The Save button still submits the main form even though it sits beside the other form.
        assert save.attrs.get("form") == "debrief-form" or save.within(form)


def test_debrief_not_needed_from_the_page_marks_it_dismissed(tmp_path):
    with refining(tmp_path) as r:
        d = Desk(r)
        page = fragment_of(d)
        [dismiss] = [
            e
            for e in elements(page)
            if e.tag == "form" and e.attrs.get("action", "").endswith("/dismiss")
        ]

        done = d.post("/dismiss", {"version": hidden_value(page, dismiss, "version")})

        assert done.status_code == 200 and announced(done) == [DISMISSED]
        assert d.row()["state"] == "dismissed" and has(done.text, "debrief-dismissed")


def test_debrief_next_round_fields_are_one_fieldset_with_a_legend(tmp_path):
    with refining(tmp_path) as r:
        d = Desk(r)

        page = fragment_of(d)

        [fieldset] = [e for e in elements(page) if e.tag == "fieldset"]
        [legend] = inside(page, fieldset, lambda e: e.tag == "legend")
        assert legend.text == "Next round (only if there is one)"
        for name in ("next_date", "next_time", "next_zone"):
            assert control(page, LABELS[name]).within(fieldset), name
        assert not control(page, LABELS["next_step"]).within(fieldset)


def test_debrief_form_hints_and_descriptions(tmp_path):
    with refining(tmp_path) as r:
        d = Desk(r)

        page = fragment_of(d)

        questions = control(page, LABELS["questions"])
        assert "debrief-privacy" in str(questions.attrs["aria-describedby"]).split()
        for name in ("questions", "how_it_went", "experiences"):
            assert "maxlength" not in control(page, LABELS[name]).attrs, name
        hint = one(page, "debrief-experiences-hint")
        assert hint is not None and "true" in hint.text


def test_debrief_time_zone_defaults_then_echoes_what_was_typed(tmp_path):
    with refining(tmp_path) as r:
        d = Desk(r)
        state = r.run.app.state

        state.system_time_zone = "Asia/Tokyo"
        assert control(fragment_of(d), LABELS["next_zone"]).attrs["value"] == "Asia/Tokyo"

        state.system_time_zone = None
        zone = control(fragment_of(d), LABELS["next_zone"])
        assert zone.attrs["value"] == "Europe/Lisbon", "the interview's own zone"

        typed = d.save(
            next_step="next_round",
            next_date="2026-11-03",
            next_time="14:00",
            next_zone="Mars/Olympus",
        )
        assert typed.status_code == 422
        assert control(typed.text, LABELS["next_zone"]).attrs["value"] == "Mars/Olympus"
        assert control(typed.text, LABELS["next_date"]).attrs["value"] == "2026-11-03"

        state.system_time_zone = "Asia/Tokyo"
        cleared = d.save(
            next_step="next_round", next_date="2026-11-03", next_time="14:00", next_zone=""
        )
        assert cleared.status_code == 422
        assert control(cleared.text, LABELS["next_zone"]).attrs.get("value", "") == ""


@pytest.mark.parametrize(
    "fields",
    [
        {"next_step": "", "next_date": "2026-11-03", "next_time": "14:00"},
        {"next_step": "offer", "next_date": "2026-11-03"},
        {"next_step": "rejected", "next_time": "14:00", "next_zone": "Asia/Tokyo"},
        {"next_step": "other", "next_date": "2026-11-03", "next_time": "14:00"},
    ],
    ids=["no-step", "offer-date", "rejected-time", "other-both"],
)
def test_debrief_date_without_a_next_round_step_is_a_field_error(tmp_path, fields):
    with refining(tmp_path) as r:
        d = Desk(r)
        before = snapshot(r)

        refused = d.save(**fields)

        assert refused.status_code == 422
        assert control(refused.text, LABELS["next_step"]).attrs.get("aria-invalid") == "true"
        assert one(refused.text, "debrief-next-step-error") is not None
        assert snapshot(r) == before and d.row() is None
        # What was typed comes back.
        for name in ("next_date", "next_time"):
            if name in fields:
                assert control(refused.text, LABELS[name]).attrs["value"] == fields[name]


def test_debrief_a_zone_alone_is_not_an_error_without_a_next_round(tmp_path):
    """A pin: the form always posts the zone, so it must not count as a next round."""
    with refining(tmp_path) as r:
        d = Desk(r)

        saved = d.save(next_step="offer", next_zone="Asia/Tokyo")

        assert saved.status_code == 200 and d.row()["next_event_id"] is None


def test_debrief_422_links_the_summary_to_the_fields_and_focuses_the_first_invalid_one(tmp_path):
    with refining(tmp_path) as r:
        d = Desk(r)

        refused = d.save(
            questions="A fine question?", experiences="x" * 4001, how_it_went="y" * 4001
        )

        assert refused.status_code == 422
        summary = one(refused.text, "debrief-errors")
        assert summary is not None and summary.attrs.get("role") == "alert"
        links = {
            str(e.attrs["href"]): e.text
            for e in inside(refused.text, summary, lambda e: e.tag == "a")
        }
        assert set(links) == {"#debrief-how", "#debrief-experiences"}
        for href in links:
            assert has(refused.text, href[1:]), href
        focused = [e for e in elements(refused.text) if "data-autofocus" in e.attrs]
        assert [e.attrs.get("id") for e in focused] == ["debrief-how"], "the first invalid field"
        assert "Nothing was saved." in summary.text


def test_debrief_working_text_is_plain_and_the_refresh_never_swaps_a_running_region(tmp_path):
    with refining(tmp_path) as r:
        d = Desk(r)
        d.save(experiences=PARAGRAPH_ONE)
        r.run.hold.hold = True
        try:
            started = d.drafts()
            assert started.status_code == 200
            page = fragment_of(d)
            while_running = htmx_get(r, d.path() + "?active=1")
        finally:
            r.run.hold.open()
        r.run.wait_terminal(d.tasks()[-1]["id"])
        after = htmx_get(r, d.path() + "?active=1")

        holder = one(page, "debrief")
        assert holder is not None
        trigger = str(holder.attrs.get("hx-trigger"))
        assert "every 2s" not in trigger, "the region is no longer swapped every two seconds"
        assert "tasks:updated from:body" in trigger and "tasks:resync from:body" in trigger
        assert holder.attrs["hx-get"] == d.path() + "?active=1"
        working = one(page, "debrief-working")
        assert working is not None and working.tag == "p"
        assert "role" not in working.attrs and "aria-live" not in working.attrs
        # While the task runs the refresh answers nothing to swap; afterwards the region.
        assert while_running.status_code == 204 and while_running.text == ""
        assert after.status_code == 200 and len(draft_items(after.text)) == 1
        # A settled region does not refresh itself.
        assert "hx-get" not in one(fragment_of(d), "debrief").attrs


def test_debrief_shows_a_visible_confirmation_line_that_is_not_a_live_region(tmp_path):
    with refining(tmp_path) as r:
        d = Desk(r)

        saved = d.save(experiences=PARAGRAPH_ONE, next_step="next_round")

        line = one(saved.text, "debrief-message")
        assert line is not None and line.tag == "p" and line.text == SAVED
        assert not {"role", "aria-live"} & set(line.attrs)
        assert not has(fragment_of(d), "debrief-message"), "only the answer to an action"
        started = d.drafts()
        assert announced(started) == [DRAFTS_STARTED]
        assert one(started.text, "debrief-message").text == DRAFTS_STARTED  # type: ignore[union-attr]
        r.run.wait_terminal(d.tasks()[-1]["id"])
        moved = d.post("/status", {"version": str(d.app_version())})
        assert has(moved.text, "debrief-moved")
        assert "role" not in one(moved.text, "debrief-moved").attrs  # type: ignore[union-attr]
        assert says(moved.text).count("Moved to Interviewing.") == 1


def test_debrief_failed_task_offers_try_again(tmp_path):
    with refining(tmp_path) as r:
        d = Desk(r)
        d.save(experiences=PARAGRAPH_ONE)
        r.run.spy.script(TASK, load_ai_fixture(TASK, "malformed"))
        d.drafted(status="failed")

        page = fragment_of(d)

        start = one(page, "debrief-drafts-start")
        assert start is not None and start.text == "Try again"
        assert has(page, "debrief-task-error")


def test_debrief_save_conflict_says_the_typed_text_was_not_saved(tmp_path):
    with refining(tmp_path) as r:
        d = Desk(r)
        stale = {"version": "0"}
        d.save(questions="A fictional question?", how_it_went="first")

        refused = r.client.post(
            d.path("/save"),
            data={
                **stale,
                "questions": "Another fictional question?",
                "how_it_went": f"Second {HOW}",
            },
            headers=r.run.run.action(**{"HX-Request": "true"}),
        )

        assert refused.status_code == 409 and "Reload" in refused.text
        prompt = one(refused.text, "debrief-conflict")
        assert prompt is not None and "not saved" in prompt.text
        kept = one(refused.text, "debrief-unsaved")
        assert kept is not None and HOW in kept.text and "Another fictional question?" in kept.text
        assert d.row()["how_it_went"] == "first"
        assert f"Second {HOW}" not in says(r.client.get(d.path()).text)


def test_debrief_conflict_without_typed_text_still_says_nothing_changed(tmp_path):
    with refining(tmp_path) as r:
        d = Desk(r)

        refused = d.post("/drafts", {"version": "999"})

        prompt = one(refused.text, "debrief-conflict")
        assert refused.status_code == 409 and prompt is not None
        assert "Nothing was changed." in prompt.text and not has(refused.text, "debrief-unsaved")


# --- item 6d: today and the timeline ----------------------------------------------------------


def test_debrief_timeline_link_is_described_by_its_event_text(tmp_path):
    with refining(tmp_path) as r:
        d = Desk(r)

        page = r.client.get(f"/applications/{d.aid}").text

        [link] = [e for e in elements(page) if "data-debrief-link" in e.attrs]
        assert link.text == "Debrief" and "aria-label" not in link.attrs
        described = str(link.attrs["aria-describedby"])
        assert described == f"event-text-{d.event_id}" and has(page, described)


# --- item 8: pins -----------------------------------------------------------------------------


def test_debrief_near_duplicate_lines_in_one_save_are_pinned(tmp_path):
    """A pin of today's behaviour: exact repeats (after trimming) are one line; a line that
    differs only in case or punctuation is its own line, merges into the same bank question and
    counts again (asked_count 2 and two sightings for two distinct lines)."""
    with refining(tmp_path) as r:
        d = Desk(r)
        lines = (
            "Why do you want this role?\nwhy do you want this role\nWhy do you want this role?  "
        )

        saved = d.save(questions=lines)

        assert saved.status_code == 200
        assert [(q["text"], q["asked_count"]) for q in d.asked()] == [
            ("Why do you want this role?", 2)
        ]
        assert len(sightings_of(r, "Why do you want this role?")) == 2
