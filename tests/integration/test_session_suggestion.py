"""M7.8 Today's "Suggested session" block (AC-M7.6: suggestions respect the configured
frequency; pause stops them). The assumed markup, route and rules are in
``tests/today_suggestion_pages.py``.

A real migrated SQLite database in a temp folder, the fake provider, the frozen clock, no
network; every value is fictional. The pure window rules are already proven in
``tests/unit`` (M7.2); these tests prove the page applies them: which setting, which stored
reset, which backlog and provider.
"""

import json
from datetime import datetime, timedelta
from typing import Any

import pytest

from app.domain.settings import AI_PROVIDER, SESSIONS, AIProviderSettings, SessionSettings
from app.modules.storage import sessions_repo
from app.services.settings import SettingsService
from tests.board_pages import elements, said_headers
from tests.cv_import_harness import Importing, importing
from tests.facts_page import form_values, posts_to
from tests.reminder_desk import all_tables
from tests.today_suggestion_pages import (
    BLOCK_ID,
    DISMISS_ID,
    DISMISS_PATH,
    DISMISSED,
    HEADING,
    NOTHING_DUE,
    START_ID,
    START_PATH,
    SUGGESTION_KEY,
    T0,
    TEXT,
    block,
    continue_link,
    form_in,
    inside,
    make_seeder,
    seed_backlog,
    start_values,
    stored_reset,
    suggested,
)

pytestmark = pytest.mark.integration

HTMX = {"HX-Request": "true"}
FREQUENCIES = ("never", "weekly", "twice_weekly", "after_application")
SECOND = timedelta(seconds=1)


@pytest.fixture
def desk(tmp_path):
    with importing(tmp_path) as run:
        seed_backlog(make_seeder(run.app, run.clock))
        assert run.app.state.sessions.backlog(), "the fixture has something to ask"
        run.clock.set(T0)
        yield run


# --- helpers ------------------------------------------------------------------------------


def today(run: Importing, path: str = "/") -> str:
    response = run.client.get(path)
    assert response.status_code == 200, (path, response.status_code, response.text[:300])
    return response.text


def settings(run: Importing) -> SettingsService:
    return SettingsService(run.app.state.workspace.db, run.clock)


def save_sessions(run: Importing, **changes: Any) -> None:
    """Store the Sessions setting through the existing versioned service."""
    service = settings(run)
    current = service.load(SESSIONS)
    value = SessionSettings(**{**current.value.model_dump(), **changes})
    service.save(SESSIONS, value, expected_version=current.version)


def reset_of(run: Importing) -> datetime | None:
    return stored_reset(lambda sql: run.rows(sql))


def store_reset(run: Importing, instant: datetime) -> None:
    run.execute(
        "INSERT INTO settings (key, value, created_at, updated_at, version) VALUES (?,?,?,?,1) "
        "ON CONFLICT (key) DO UPDATE SET value=excluded.value, version=version+1",
        (SUGGESTION_KEY, json.dumps({"last_reset": instant.isoformat()}), T0.isoformat(),
         T0.isoformat()),
    )  # fmt: skip


def applied_at(run: Importing, job_id: str, instant: datetime) -> None:
    """An application for ``job_id`` whose ``applied_at`` is exactly ``instant``. The column is
    written once, when the application first reaches Applied (a trigger keeps the first time),
    so the clock is set just before the seeder's one-minute tick."""
    run.clock.set(instant - timedelta(minutes=1))
    make_seeder(run.app, run.clock).application(job_id, "Depot Supervisor", None, "applied")
    run.clock.set(T0)
    [row] = run.rows("SELECT applied_at FROM applications WHERE job_id=?", (job_id,))
    assert datetime.fromisoformat(row["applied_at"]) == instant, "fixture: applied_at as asked"


def at(run: Importing, instant: datetime) -> str:
    run.clock.set(instant)
    return today(run)


def tables(run: Importing) -> dict[str, list[Any]]:
    return all_tables(lambda sql: run.rows(sql))


def post(run: Importing, path: str, *, htmx: bool = True, **values: str) -> Any:
    headers = run.run.action(**(HTMX if htmx else {}))
    return run.client.post(path, data=values, headers=headers)


def set_provider(run: Importing, value: AIProviderSettings) -> None:
    service = settings(run)
    service.save(AI_PROVIDER, value, expected_version=service.load(AI_PROVIDER).version)


def open_session(run: Importing, session_id: str = "open-session") -> str:
    with run.app.state.workspace.db.transaction() as conn:
        sessions_repo.insert_session(
            conn,
            id=session_id,
            focus_kind="most_useful",
            length=3,
            now=run.clock.now().isoformat(),
        )
    return session_id


# --- frequency ----------------------------------------------------------------------------


def _never(run: Importing) -> None:
    assert not suggested(today(run)), "never: not even with nothing stored"
    store_reset(run, T0 - timedelta(days=60))
    applied_at(run, "never-app", T0 + timedelta(hours=1))
    assert not suggested(at(run, T0 + timedelta(days=90)))
    # Nothing else on the page changes meaning: the block is simply absent.
    assert block(today(run)) is None
    # Control: the same stored state with another frequency does suggest, so "never" is what
    # hid it (and not a missing backlog, provider or window).
    save_sessions(run, frequency="weekly")
    assert suggested(today(run))
    save_sessions(run, frequency="never")
    assert not suggested(today(run))


def _weekly(run: Importing) -> None:
    html = today(run)  # no reset yet: due at once (ADR-119)
    assert suggested(html)
    section = block(html)
    assert section is not None
    assert HEADING in section.text and TEXT[3] in section.text
    # The block is optional: Today's "nothing due" logic does not count it.
    assert NOTHING_DUE in html
    store_reset(run, T0)
    assert not suggested(at(run, T0))
    assert not suggested(at(run, T0 + timedelta(days=4)))  # the twice-a-week window passed
    assert not suggested(at(run, T0 + timedelta(days=7) - SECOND))
    assert suggested(at(run, T0 + timedelta(days=7)))
    assert suggested(at(run, T0 + timedelta(days=30)))


def _twice_weekly(run: Importing) -> None:
    assert suggested(today(run)), "no reset yet: due at once"
    store_reset(run, T0)
    assert not suggested(at(run, T0 + timedelta(hours=1)))
    assert not suggested(at(run, T0 + timedelta(hours=84) - SECOND))
    assert suggested(at(run, T0 + timedelta(hours=84)))


def _after_application(run: Importing) -> None:
    assert not suggested(today(run)), "no application at all: nothing to follow up"
    store_reset(run, T0)
    assert not suggested(at(run, T0 + timedelta(days=30))), "no application since the reset"
    applied_at(run, "earlier-app", T0 - timedelta(hours=1))
    assert not suggested(at(run, T0 + timedelta(days=1))), "applied before the reset"
    applied_at(run, "later-app", T0 + timedelta(hours=1))
    assert not suggested(at(run, T0 + timedelta(minutes=59))), "applied_at is still ahead"
    assert suggested(at(run, T0 + timedelta(hours=1)))
    # An application older than the next reset no longer counts.
    store_reset(run, T0 + timedelta(hours=2))
    assert not suggested(at(run, T0 + timedelta(hours=3)))
    applied_at(run, "newest-app", T0 + timedelta(hours=4))
    assert suggested(at(run, T0 + timedelta(hours=5)))


@pytest.mark.parametrize("frequency", FREQUENCIES)
def test_ac_m7_6_today_suggestion_respects_frequency(desk, frequency):
    save_sessions(desk, frequency=frequency)
    {
        "never": _never,
        "weekly": _weekly,
        "twice_weekly": _twice_weekly,
        "after_application": _after_application,
    }[frequency](desk)


def test_ac_m7_6_default_setting_is_weekly(desk):
    # Nothing saved: the default frequency (weekly) and the default length (3) apply.
    assert desk.rows("SELECT * FROM settings WHERE key='sessions'") == []
    html = today(desk)
    assert suggested(html) and TEXT[3] in html
    assert start_values(html) == {"focus": "most_useful", "length": "3"}


def test_suggestion_text_and_start_form_follow_the_default_length(desk):
    for length in (3, 5):
        save_sessions(desk, length=length)
        html = today(desk)
        section = block(html)
        assert section is not None and TEXT[length] in section.text
        assert start_values(html) == {"focus": "most_useful", "length": str(length)}
    assert "about 5 minutes" not in block(today(desk)).text  # length 5 now


# --- pause --------------------------------------------------------------------------------


def test_ac_m7_6_pause_stops_suggestions(desk):
    assert suggested(today(desk))
    # Through the real route, as the Settings form posts it.
    paused = post(
        desk, "/settings/sessions", version="0", frequency="weekly", length="3",
        ask_later_default="week", paused="1",
    )  # fmt: skip
    assert paused.status_code == 200, paused.text[:300]
    assert not suggested(today(desk)) and block(today(desk)) is None
    resumed = post(
        desk, "/settings/sessions", version="1", frequency="weekly", length="3",
        ask_later_default="week",
    )  # fmt: skip
    assert resumed.status_code == 200
    assert suggested(today(desk))

    # Paused stops every frequency, whatever the window and the applications say.
    applied_at(desk, "paused-app", T0 + timedelta(hours=1))
    for frequency in FREQUENCIES:
        save_sessions(desk, frequency=frequency, paused=True)
        assert not suggested(at(desk, T0 + timedelta(days=400))), frequency
    assert block(today(desk)) is None


# --- dismiss ------------------------------------------------------------------------------


def test_suggestion_dismiss_hides_until_next_window(desk):
    page = today(desk)
    assert suggested(page)
    form = form_in(page, DISMISS_ID)
    assert form is not None and posts_to(form) == DISMISS_PATH
    assert dict(form_values(elements(page), form)) == {}, "Dismiss sends no fields"
    buttons = [e for e in inside(page, form) if e.tag == "button"]
    assert len(buttons) == 1 and buttons[0].text == "Dismiss"
    name = buttons[0].attrs.get("aria-label") or buttons[0].text
    assert name.startswith("Dismiss") and "session" in name.lower()

    before = tables(desk)
    reply = post(desk, DISMISS_PATH)
    assert reply.status_code == 200, reply.text[:300]
    assert "<html" not in reply.text
    answer = elements(reply.text)
    assert [e.attrs["id"] for e in answer if e.attrs.get("id") == "today-content"] == [
        "today-content"
    ]
    assert block(reply.text) is None, "the answer no longer shows the block"
    focus = [e for e in answer if "data-autofocus" in e.attrs]
    assert [e.attrs.get("id") for e in focus] == ["page-heading"]
    assert DISMISSED in said_headers(reply.headers)

    # Only the reset was written: the window starts now.
    after = tables(desk)
    assert [name for name in after if after[name] != before[name]] == ["settings"]
    assert reset_of(desk) == T0

    assert not suggested(today(desk))
    assert not suggested(at(desk, T0 + timedelta(days=7) - SECOND))
    assert suggested(at(desk, T0 + timedelta(days=7)))

    # A second dismissal moves the window; the setting is updated, not duplicated.
    later = T0 + timedelta(days=8)
    desk.clock.set(later)
    assert post(desk, DISMISS_PATH).status_code == 200
    assert reset_of(desk) == later
    assert len(desk.rows(f"SELECT * FROM settings WHERE key='{SUGGESTION_KEY}'")) == 1  # noqa: S608
    assert not suggested(at(desk, later + timedelta(days=6)))
    assert suggested(at(desk, later + timedelta(days=7)))

    # Without scripts the form goes back to Today.
    plain = post(desk, DISMISS_PATH, htmx=False)
    assert plain.status_code == 303 and plain.headers["location"] == "/"


def test_suggestion_dismiss_follows_the_chosen_frequency(desk):
    save_sessions(desk, frequency="twice_weekly")
    assert post(desk, DISMISS_PATH).status_code == 200
    assert not suggested(at(desk, T0 + timedelta(hours=84) - SECOND))
    assert suggested(at(desk, T0 + timedelta(hours=84)))

    # After each application: only an application after the dismissal brings it back.
    save_sessions(desk, frequency="after_application")
    applied_at(desk, "dismiss-app-1", T0 + timedelta(hours=100))
    assert suggested(at(desk, T0 + timedelta(hours=101)))
    assert post(desk, DISMISS_PATH).status_code == 200
    assert not suggested(at(desk, T0 + timedelta(hours=102)))
    applied_at(desk, "dismiss-app-2", T0 + timedelta(hours=103))
    assert suggested(at(desk, T0 + timedelta(hours=104)))


# --- the other conditions -----------------------------------------------------------------


def test_suggestion_hidden_without_backlog_or_provider(tmp_path):
    with importing(tmp_path) as run:
        run.clock.set(T0)
        # No jobs: nothing to ask, so nothing to suggest (a fresh weekly setting is due).
        assert run.app.state.sessions.backlog() == ()
        page = today(run)
        assert block(page) is None and HEADING not in page
        assert NOTHING_DUE in page

        seed_backlog(make_seeder(run.app, run.clock))
        run.clock.set(T0)
        assert suggested(today(run)), "control: jobs ask for things the facts do not cover"

        # The AI provider is not saved: no way to run a session.
        saved = settings(run).load(AI_PROVIDER).value
        assert saved.kind, "the harness saves the fake provider"
        set_provider(run, AIProviderSettings())
        page = today(run)
        assert block(page) is None and HEADING not in page
        set_provider(run, saved)
        assert suggested(today(run))

        # The only job is removed: the backlog is empty again.
        run.execute("UPDATE jobs SET triage_state='removed' WHERE id='sugg-job'")
        assert run.app.state.sessions.backlog() == ()
        assert block(today(run)) is None


# --- an open session ----------------------------------------------------------------------


def test_open_session_shows_continue(desk):
    save_sessions(desk, length=5)
    page = today(desk)
    assert start_values(page) == {"focus": "most_useful", "length": "5"}
    assert desk.rows("SELECT * FROM sessions") == []

    # Start posts to the existing route with the form's own fields; it also resets.
    started = post(desk, START_PATH, **{k: str(v) for k, v in start_values(page).items()})
    assert started.status_code == 200, started.text[:300]
    target = started.headers["HX-Redirect"]
    assert target.startswith("/sessions/")
    [stored] = desk.rows("SELECT id, focus_kind, length, state FROM sessions")
    assert target == f"/sessions/{stored['id']}"
    assert (stored["focus_kind"], stored["length"], stored["state"]) == ("most_useful", 5, "open")
    assert reset_of(desk) == T0, "starting a session resets the suggestion window"

    # The background question is not what is tested here; let it finish before more writes.
    for row in desk.rows("SELECT id FROM tasks"):
        desk.wait_terminal(row["id"])

    page = today(desk)
    link = continue_link(page)
    assert link is not None and link.attrs["href"] == target
    section = block(page)
    assert section is not None
    assert HEADING not in section.text and "questions, about" not in section.text
    assert form_in(page, START_ID) is None and form_in(page, DISMISS_ID) is None
    assert not [e for e in inside(page, section) if e.tag in ("form", "button")]

    # A running session is not a suggestion: neither the frequency nor the pause hides it.
    for changes in ({"frequency": "never"}, {"frequency": "weekly", "paused": True}):
        save_sessions(desk, **changes)
        assert continue_link(today(desk)) is not None, changes
    assert continue_link(at(desk, T0 + timedelta(days=60))) is not None

    # Ended: Today goes back to the normal rules (weekly, reset at T0: not due yet).
    save_sessions(desk, frequency="weekly", paused=False)
    desk.clock.set(T0 + timedelta(days=1))
    view = desk.app.state.sessions.view(stored["id"])
    desk.app.state.sessions.end(stored["id"], expected_version=view.session.version)
    page = today(desk)
    assert continue_link(page) is None and not suggested(page) and block(page) is None
    assert suggested(at(desk, T0 + timedelta(days=7)))


def test_open_session_continue_without_backlog_or_window(desk):
    # Inserted directly: no task runs, nothing resets the window.
    session_id = open_session(desk)
    store_reset(desk, T0)
    page = today(desk)
    link = continue_link(page)
    assert link is not None and link.attrs["href"] == f"/sessions/{session_id}"
    assert not suggested(page)
    run_ended = desk.rows("SELECT state FROM sessions")
    assert run_ended == [{"state": "open"}]


def test_open_session_hides_the_empty_line_but_a_suggestion_keeps_it(desk):
    # A mere suggestion does not count as something needing attention (control).
    page = today(desk)
    assert suggested(page) and NOTHING_DUE in page
    # With a session open, "Session in progress" is the content: the empty line is not said
    # directly above it. Whatever is due is still computed as before.
    open_session(desk)
    page = today(desk)
    assert continue_link(page) is not None
    assert NOTHING_DUE not in page
    # Ended again: the line is back.
    view = desk.app.state.sessions.view("open-session")
    desk.app.state.sessions.end("open-session", expected_version=view.session.version)
    page = today(desk)
    assert continue_link(page) is None and NOTHING_DUE in page


def test_after_application_compares_whole_second_and_microsecond_instants(desk):
    """``latest_applied_at`` compares ISO text; instants with and without microseconds order
    correctly around the reset (every writer stores ``astimezone(UTC).isoformat()``)."""
    save_sessions(desk, frequency="after_application")
    whole = T0 + timedelta(hours=1)
    micro = whole + timedelta(microseconds=500_000)
    applied_at(desk, "whole-app", whole)
    applied_at(desk, "micro-app", micro)
    store_reset(desk, whole)  # an application at exactly the reset is not after it
    assert not suggested(at(desk, whole + timedelta(microseconds=250_000)))
    assert not suggested(at(desk, whole))
    assert suggested(at(desk, micro)), "the half-second application is after the reset"
    store_reset(desk, whole + timedelta(microseconds=250_000))
    assert suggested(at(desk, micro))
    store_reset(desk, micro)
    assert not suggested(at(desk, micro + timedelta(days=1)))
    store_reset(desk, whole - timedelta(microseconds=1))
    assert suggested(at(desk, whole)), "whole-second instant is after a reset a microsecond before"


# --- reads write nothing ------------------------------------------------------------------


def test_today_get_writes_nothing(desk):
    token_headers = desk.run.htmx()

    def read_everything() -> None:
        for method in ("GET", "HEAD"):
            assert desk.client.request(method, "/").status_code == 200
        assert desk.client.get("/today/content", headers=token_headers).status_code == 200
        assert desk.client.get("/settings").status_code == 200
        assert desk.client.get("/sessions/new").status_code == 200

    # A suggestion is due: showing it does not store a reset, a task or a session.
    before = tables(desk)
    read_everything()
    assert tables(desk) == before
    assert reset_of(desk) is None
    assert suggested(today(desk))
    assert tables(desk) == before

    # With a window that has passed, and again with an open session.
    store_reset(desk, T0 - timedelta(days=30))
    before = tables(desk)
    read_everything()
    assert tables(desk) == before

    open_session(desk)
    before = tables(desk)
    read_everything()
    assert continue_link(today(desk)) is not None
    assert tables(desk) == before
    fragment = desk.client.get("/today/content", headers=token_headers)
    assert BLOCK_ID in fragment.text and "<html" not in fragment.text
    assert tables(desk) == before


def test_suggestion_block_follows_drafts_and_precedes_setup_steps(desk):
    # Block 5 comes after the drafts block (design); the other blocks keep their order.
    from app.services.facts import DraftInput, DraftOrigin

    desk.app.state.facts.create_drafts(
        DraftOrigin(source="cv_import"), [DraftInput(text="Maintained a fictional catalogue.")]
    )
    run_ids = [
        e.attrs["id"]
        for e in elements(today(desk))
        if e.tag == "section" and "today-block" in (e.attrs.get("class") or "").split()
    ]
    assert run_ids.index("today-drafts") < run_ids.index(BLOCK_ID)
