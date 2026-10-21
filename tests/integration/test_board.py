"""M5.4 board screen: page and fragments, the status POST, follow-up dialogs, table view.

Assumed interface (the implementer matches it; markup and ids are listed in
``tests/board_pages.py``):

- ``GET /board`` is a token-free page (``PAGE_TAG``). Query values: ``view`` (``board`` or
  ``table``), ``sort`` (``company``, ``role``, ``status``, ``applied``, ``activity``), ``dir``
  (``asc`` or ``desc``), ``page`` (50 rows a page; clamped) and ``closed`` (``1`` shows the Closed
  cards). With ``HX-Request`` (and the token) it answers the ``#board`` block with
  ``HX-Push-Url`` set to the ``/board`` address with the validated values, never a fragment route.
  An unknown ``sort`` is a plain-text 400, never a 500. A GET writes nothing.
- ``GET /board/columns`` (token, fragment) answers ``#board-content`` for the same query values.
- ``POST /applications/{id}/status`` (token and origin): exactly the fields ``version``,
  ``status`` and optionally ``source`` (``menu`` or ``board``); any other field, a repeated one,
  a bad ``version`` or an unknown status is a plain-text 400; a body over 64 KiB a plain-text
  413; an unknown or unsafe id the fixed plain-text 404 (``ApplicationNotFound.message``); a
  stale ``version`` a 409 with the reload prompt. It is the only move endpoint, for the menu and
  for a drop. A 200 answers ``#board-content`` (the moved card with its new ``data-version``;
  the card's title carries ``data-autofocus`` when the card is shown) with an announcement
  "Moved to <label>." in an ``HX-Trigger`` header, and for Interviewing and Rejected the
  follow-up ``<dialog data-modal>`` too.
- ``POST /applications/{id}/interview-date`` (token and origin): ``date``, ``time``,
  ``time_zone`` and ``duration_minutes`` (all present, any may be empty). 422 answers the form
  with the error tied to its field: ``starts_at`` (the ``date`` or ``time`` input), ``time_zone``
  or ``duration_minutes``; the error element is ``#interview-<field>-error`` and the input has
  ``aria-describedby`` naming it and ``aria-invalid="true"``. 200 answers ``#board-content``.
- ``POST /applications/{id}/feedback`` (token and origin): ``text``. 422 for blank text (error
  ``#feedback-text-error``); 200 answers ``#board-content``.
- The dialog's zone defaults to ``request.app.state.system_time_zone`` (a name or None, set by
  ``create_app`` from ``system_time_zone_name()``; tests set it). When it is None the zone input
  is empty and ``required``.

Everyone and everything here is fictional.
"""

import re
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from app.domain.errors import ApplicationNotFound
from tests.application_desk import wall
from tests.board_pages import (
    Seeder,
    by_id,
    card_status,
    cards,
    cards_by_status,
    column_keys,
    dialog_title,
    dialogs,
    elements,
    fields_in,
    menu_of,
    normalise_ids,
    said_headers,
    stamp_after,
    table,
    table_ids,
    ticker,
)
from tests.integration.test_app_shell import find_all
from tests.integration.test_inbox import get, post, seeded
from tests.refine_harness import refining

pytestmark = pytest.mark.integration

STATUS_KEYS = (
    "preparing",
    "applied",
    "screening",
    "interviewing",
    "offer",
    "rejected",
    "withdrawn",
    "accepted",
    "declined",
)
LABELS = {key: key.capitalize() for key in STATUS_KEYS}
HOSTILE = "https://hostile.example.test"
INVALID = "This request isn't valid."


def seeder(r: Any) -> Seeder:
    app = r.run.app
    return Seeder.make(
        app.state.workspace.db,
        app.state.applications,
        app.state.application_statuses,
        ticker(r.run.clock),
        stamp_after(r.run.clock),
    )


def seed_board(r: Any) -> dict[str, Any]:
    return seeder(r).plan()


def version_of(r: Any, application_id: str) -> int:
    return int(
        r.run.rows("SELECT version FROM applications WHERE id=?", (application_id,))[0]["version"]
    )


def status_of(r: Any, application_id: str) -> str:
    return str(
        r.run.rows("SELECT status_key FROM applications WHERE id=?", (application_id,))[0][
            "status_key"
        ]
    )


def history(r: Any, application_id: str) -> list[tuple[Any, ...]]:
    rows = r.run.rows(
        "SELECT from_status, to_status, source FROM status_history WHERE application_id=? "
        "ORDER BY changed_at, id",
        (application_id,),
    )
    return [tuple(row.values()) for row in rows]


def snapshot(r: Any) -> dict[str, list[dict[str, Any]]]:
    """Every table of the database as it is now."""
    names = [
        row["name"]
        for row in r.run.rows(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        )
    ]
    return {name: r.run.rows(f"SELECT * FROM {name} ORDER BY 1") for name in names}  # noqa: S608


def written(r: Any) -> dict[str, list[dict[str, Any]]]:
    """The tables a move or a follow-up may touch."""
    return {
        name: r.run.rows(f"SELECT * FROM {name} ORDER BY 1")  # noqa: S608
        for name in ("applications", "status_history", "application_notes", "events", "jobs")
    }


def move(r: Any, application: Any, status: str, source: str = "menu", **extra: str) -> Any:
    return post(
        r,
        f"/applications/{application.id}/status",
        version=str(version_of(r, application.id)),
        status=status,
        source=source,
        **extra,
    )


def post_raw(r: Any, path: str, data: Any, **headers: str) -> Any:
    return r.client.post(
        path, data=data, headers=r.run.run.action(**{"HX-Request": "true", **headers})
    )


def query(address: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(address).query).items()}


# --- the page and its fragments -----------------------------------------------------------------


def test_board_page_full_and_fragments(tmp_path):
    with refining(tmp_path) as r:
        made = seed_board(r)
        full = r.client.get("/board")
        assert full.status_code == 200 and "<html" in full.text
        assert 'id="board"' in full.text and 'id="board-content"' in full.text
        assert [e.text for e in elements(full.text) if e.tag == "h1"] == ["Board"]
        # SortableJS and board.js belong to this page only, the library first.
        sortable = full.text.index("/static/vendor/Sortable.min.js")
        assert sortable < full.text.index("/static/js/board.js")
        for path in ("/", "/inbox", "/companies", "/facts", "/settings"):
            other = r.client.get(path)
            assert "board.js" not in other.text and "Sortable" not in other.text, path
        assert r.client.get("/static/js/board.js").status_code == 200

        fragment = get(r, "/board")
        assert fragment.status_code == 200 and "<html" not in fragment.text
        assert 'id="board"' in fragment.text and 'id="board-content"' in fragment.text
        assert urlsplit(fragment.headers["HX-Push-Url"]).path == "/board"

        # Columns: preparing and active statuses in position order, an empty one still listed.
        assert column_keys(full.text) == [
            "preparing",
            "applied",
            "screening",
            "interviewing",
            "offer",
        ]
        lists = find_all(
            elements(full.text), lambda e: e.tag == "ul" and "data-sortable" in e.attrs
        )
        assert [e.attrs["data-status"] for e in lists] == column_keys(full.text)
        # Most recently active first: job-3 moved to Applied after job-2.
        assert cards_by_status(full.text) == {
            "preparing": [made["board-job-1"].id],
            "applied": [made["board-job-3"].id, made["board-job-2"].id],
            "screening": [made["board-job-4"].id],
            "interviewing": [made["board-job-5"].id],
        }
        # Headings carry the label and the count.
        headings = {
            e.text
            for e in elements(full.text)
            if e.tag == "h2"
            and re.match(r"^(Preparing|Applied|Screening|Interviewing|Offer)\b", e.text)
        }
        assert any(re.fullmatch(r"Applied\W*2\W*", text) for text in headings), headings
        assert any(re.fullmatch(r"Offer\W*0\W*", text) for text in headings), headings

        # One card: company, role, the hooks the drop needs, and the keyboard menu.
        application = made["board-job-5"]
        menu = menu_of(full.text, application.id)
        card = menu["card"]
        assert "elm Fictional Yard" in card.text and "Pallet Driver" in card.text
        assert card.attrs["data-version"] == str(version_of(r, application.id))
        assert card_status(card) == "interviewing"
        [button] = [
            e
            for e in elements(full.text)
            if e.tag == "button" and e.within(card) and e.text.startswith("Move to")
        ]
        assert button.attrs.get("aria-haspopup") == "menu"
        assert button.attrs.get("aria-expanded") == "false"
        assert menu["action"] == f"/applications/{application.id}/status"
        assert menu["hidden"] == {"version": card.attrs["data-version"], "source": "menu"}
        # Every other status is offered, each by its label (the current one may be too).
        assert {k: v for k, v in menu["buttons"].items() if k != "interviewing"} == {
            k: v for k, v in LABELS.items() if k != "interviewing"
        }
        assert menu["buttons"].get("interviewing", "Interviewing") == "Interviewing"


def test_closed_column_collapsed_until_asked(tmp_path):
    with refining(tmp_path) as r:
        seed_board(r)
        collapsed = r.client.get("/board").text
        toggle = by_id(collapsed, "board-closed-toggle")
        assert toggle is not None and toggle.text == "Closed: 3"
        assert toggle.attrs.get("aria-expanded") == "false"
        assert by_id(collapsed, "board-closed-cards") is None
        for hidden in ("Boiler Fitter", "Frost Watcher", "Pruning Lead", "Closed: rejected"):
            assert hidden not in collapsed

        opened = r.client.get("/board?closed=1").text
        toggle = by_id(opened, "board-closed-toggle")
        assert toggle is not None and toggle.text == "Closed: 3"
        assert toggle.attrs.get("aria-expanded") == "true"
        assert by_id(opened, "board-closed-cards") is not None
        for shown in ("Boiler Fitter", "Frost Watcher", "Pruning Lead"):
            assert shown in opened
        assert opened.count("Closed: rejected") == 2 and opened.count("Closed: withdrawn") == 1
        # The same through the fragment route.
        assert "Closed: withdrawn" in get(r, "/board/columns?closed=1").text
        assert "Closed: withdrawn" not in get(r, "/board/columns").text


@pytest.mark.parametrize(
    ("path", "marker"),
    [
        ("/board", "board-content"),
        ("/board?view=table", "board-table"),
        ("/board?closed=1", "board-closed-cards"),
        ("/board/columns", "board-columns"),
        ("/board/columns?closed=1", "board-closed-cards"),
        ("/board/columns?view=table&sort=role", "board-table"),
    ],
)
def test_board_fragments_need_token(tmp_path, path, marker):
    with refining(tmp_path) as r:
        seed_board(r)
        assert r.client.get(path, headers={"HX-Request": "true"}).status_code == 403
        shown = get(r, path)
        assert shown.status_code == 200 and "<html" not in shown.text
        assert f'id="{marker}"' in shown.text
        if path.startswith("/board/columns"):
            assert "<h1" not in shown.text  # just the content, not the page heading
            if "HX-Push-Url" in shown.headers:  # a fragment route is never pushed
                assert urlsplit(shown.headers["HX-Push-Url"]).path == "/board"
    # The page itself needs no token; a fragment route without htmx is not a page.
    (tmp_path / "again").mkdir()
    with refining(tmp_path / "again") as r:
        assert r.client.get("/board").status_code == 200
        assert r.client.get("/board/columns").status_code == 403


def test_board_page_push_address_keeps_validated_values(tmp_path):
    with refining(tmp_path) as r:
        seed_board(r)
        shown = get(r, "/board?view=table&sort=company&dir=asc&page=1&push=1")
        pushed = shown.headers["HX-Push-Url"]
        assert urlsplit(pushed).path == "/board"
        assert query(pushed)["view"] == "table" and query(pushed)["sort"] == "company"
        assert query(pushed)["dir"] == "asc" and "push" not in query(pushed)


def test_board_get_has_no_side_effects(tmp_path):
    with refining(tmp_path) as r:
        seeded(r, 3)  # jobs nobody applied for
        seed_board(r)
        assert get(r, "/board").status_code == 200  # first read may set up anything it needs
        assert cards(r.client.get("/board").text), "the board lists the applications"
        before = snapshot(r)
        paths = (
            "/board",
            "/board?view=table",
            "/board?view=table&sort=company&dir=desc&page=2",
            "/board?view=table&sort=nope",
            "/board?closed=1",
            "/board/columns",
            "/board/columns?closed=1",
            "/board/columns?view=table&sort=activity",
        )
        for path in paths:
            r.client.get(path)
            get(r, path)
            r.client.head(path)
        assert snapshot(r) == before
        # Reading the board does not start work either.
        assert r.run.app.state.task_service.recent(50) == []


def empty_states(items):
    [main] = find_all(items, lambda e: e.tag == "main")
    return find_all(items, lambda e: "empty-state" in e.classes and e.within(main))


def test_empty_board_links_to_inbox(tmp_path):
    with refining(tmp_path) as r:
        for path in ("/board", "/board?view=table"):
            full = r.client.get(path)
            assert full.status_code == 200 and "<html" in full.text, path
            items = elements(full.text)
            [state] = empty_states(items)
            sentences = [e.text for e in items if e.tag == "p" and e.within(state)]
            assert any(len(text.split()) >= 4 for text in sentences), sentences
            links = [e for e in items if e.tag == "a" and e.within(state)]
            assert [e.attrs["href"] for e in links if e.attrs.get("href") == "/inbox"], path
            assert [e.text for e in items if e.tag == "h1"] == ["Board"]
            # No empty columns and no empty table under the empty state.
            assert by_id(full.text, "board-columns") is None, path
            assert by_id(full.text, "board-table") is None, path
        fragment = get(r, "/board")
        assert "empty-state" in fragment.text and 'href="/inbox"' in fragment.text


def test_board_card_words_for_days_and_next_event(tmp_path):
    with refining(tmp_path) as r:
        made = seed_board(r)
        quiet = made["board-job-4"].id  # Screening
        events = made["board-job-5"].id  # Interviewing
        text = lambda: " ".join(  # noqa: E731
            e.text
            for e in cards(r.client.get("/board").text)
            if e.attrs["data-application-id"] == quiet
        )
        assert "today" in text()
        r.run.clock.advance(timedelta(days=1))
        assert "yesterday" in text()
        r.run.clock.advance(timedelta(days=2))
        assert "3 days ago" in text()
        # An event shows as the next thing; a scheduled date never counts as activity.
        r.run.app.state.applications.add_interview_date(
            events,
            starts_at=wall("2026-11-03T14:00"),
            time_zone="America/New_York",
        )
        page = r.client.get("/board").text
        [card] = [e for e in cards(page) if e.attrs["data-application-id"] == events]
        assert "Interview" in card.text
        stamps = [
            e.attrs.get("datetime", "")
            for e in elements(page)
            if e.tag == "time" and e.within(card)
        ]
        assert any(stamp.startswith("2026-11-03") for stamp in stamps), stamps


# --- the status POST ----------------------------------------------------------------------------


def test_status_post_needs_token_origin_and_version(tmp_path):
    with refining(tmp_path) as r:
        made = seed_board(r)
        application = made["board-job-2"]  # Applied
        path = f"/applications/{application.id}/status"
        version = version_of(r, application.id)
        body = {"version": str(version), "status": "screening", "source": "menu"}
        before = written(r)

        assert r.client.post(path, data=body).status_code == 403
        assert r.client.post(path, data=body, headers=r.run.run.with_token()).status_code == 403
        hostile = r.run.run.with_token(origin=HOSTILE)
        assert r.client.post(path, data=body, headers=hostile).status_code == 403
        assert written(r) == before

        bad = [
            {**body, "extra": "1"},
            {k: v for k, v in body.items() if k != "version"},
            {k: v for k, v in body.items() if k != "status"},
            {**body, "version": "abc"},
            {**body, "version": "0"},
            {**body, "version": "-1"},
            {**body, "version": "1.5"},
            {**body, "version": ""},
            {**body, "version": "9" * 40},
            {**body, "status": ""},
            {**body, "status": "no-such-status"},
            {**body, "status": "../applied"},
            {**body, "source": "drawer"},
            {**body, "source": "junk"},
            {**body, "version": [str(version), str(version)]},
            {**body, "status": ["screening", "offer"]},
        ]
        for values in bad:
            refused = post_raw(r, path, values)
            assert refused.status_code == 400, values
            assert refused.text == INVALID, values
        assert written(r) == before

        # A huge body is cut off with the plain-text 413 before anything is read.
        huge = post_raw(r, path, {**body, "pad": "x" * 70_000})
        assert huge.status_code == 413 and huge.text == INVALID
        assert written(r) == before

        # Stale version: 409 with the reload prompt, nothing written.
        stale = post(r, path, **{**body, "version": str(version + 5)})
        assert stale.status_code == 409 and "Reload the page" in stale.text
        assert 'role="alert"' in stale.text
        assert written(r) == before

        # Unknown and unsafe ids: the fixed plain-text 404 that echoes nothing.
        for unknown in ("no-such-application", "a.b", "x" * 80):
            missing = post(r, f"/applications/{unknown}/status", **body)
            assert missing.status_code == 404, unknown
            assert missing.text == ApplicationNotFound.message, unknown
            assert missing.headers["content-type"].startswith("text/plain"), unknown
        assert written(r) == before

        # A good request moves the card and says so.
        moved = post(r, path, **body)
        assert moved.status_code == 200 and "<html" not in moved.text
        assert 'id="board-content"' in moved.text
        assert "Moved to Screening." in said_headers(moved.headers)
        assert (status_of(r, application.id), version_of(r, application.id)) == (
            "screening",
            version + 1,
        )
        assert history(r, application.id)[-1] == ("applied", "screening", "menu")
        # The card comes back with its new version and sits in its new list; focus goes to it.
        [card] = [e for e in cards(moved.text) if e.attrs["data-application-id"] == application.id]
        assert card.attrs["data-version"] == str(version + 1)
        assert card_status(card) == "screening"
        marked = [e for e in elements(moved.text) if "data-autofocus" in e.attrs]
        assert len(marked) == 1 and (marked[0] is card or marked[0].within(card))

        # The old version is stale now; a drop sends source=board and no source is allowed.
        again = post(r, path, **body)
        assert again.status_code == 409
        other = made["board-job-3"]
        dropped = post(
            r,
            f"/applications/{other.id}/status",
            version=str(version_of(r, other.id)),
            status="offer",
            source="board",
        )
        assert dropped.status_code == 200 and history(r, other.id)[-1][2] == "board"
        third = made["board-job-4"]
        bare = post(
            r,
            f"/applications/{third.id}/status",
            version=str(version_of(r, third.id)),
            status="offer",
        )
        assert bare.status_code == 200 and history(r, third.id)[-1][2] in ("menu", "board")


def test_ac_m5_1_board_moves_are_recorded_in_history(tmp_path):
    """The UI side of AC-M5.1: every move from the board adds a history row with where it
    came from, where it went and how (the service's own rules are M5.3's tests)."""
    with refining(tmp_path) as r:
        made = seed_board(r)
        application = made["board-job-2"]  # Applied
        before = history(r, application.id)
        assert move(r, application, "screening", "menu").status_code == 200
        r.run.clock.advance(timedelta(minutes=1))
        assert move(r, application, "offer", "board").status_code == 200
        r.run.clock.advance(timedelta(minutes=1))
        assert move(r, application, "applied", "menu").status_code == 200
        assert history(r, application.id) == [
            *before,
            ("applied", "screening", "menu"),
            ("screening", "offer", "board"),
            ("offer", "applied", "menu"),
        ]
        # A refused move (stale version, unknown status) leaves no row behind.
        count = len(history(r, application.id))
        stale = post(
            r, f"/applications/{application.id}/status", version="1", status="offer", source="menu"
        )
        assert stale.status_code == 409
        assert len(history(r, application.id)) == count


def test_move_into_and_out_of_closed(tmp_path):
    with refining(tmp_path) as r:
        made = seed_board(r)
        application = made["board-job-2"]
        shut = move(r, application, "rejected")
        assert shut.status_code == 200
        toggle = by_id(shut.text, "board-closed-toggle")
        assert toggle is not None and toggle.text == "Closed: 4"
        assert application.id not in cards_by_status(shut.text).get("applied", [])
        assert "Moved to Rejected." in said_headers(shut.headers)
        r.run.clock.advance(timedelta(minutes=1))  # two moves, two distinct times
        reopened = move(r, application, "applied")
        assert application.id in cards_by_status(reopened.text)["applied"]
        assert by_id(reopened.text, "board-closed-toggle").text == "Closed: 3"
        assert [row[:2] for row in history(r, application.id)][-2:] == [
            ("applied", "rejected"),
            ("rejected", "applied"),
        ]


def test_drop_and_menu_post_identical_bodies(tmp_path):
    results = {}
    for source in ("menu", "board"):
        folder = tmp_path / source
        folder.mkdir()
        with refining(folder) as r:
            made = seed_board(r)
            application = made["board-job-3"]
            page = r.client.get("/board").text
            menu = menu_of(page, application.id)
            card = menu["card"]
            # What each sender takes from the page: the menu form's own fields, or the card's
            # data attributes and the list it is dropped on (board.js).
            assert menu["hidden"]["version"] == card.attrs["data-version"]
            assert card.attrs["data-application-id"] == application.id
            assert card_status(card) == "applied"
            body = {
                "version": card.attrs["data-version"],
                "status": "interviewing",
                "source": source,
            }
            response = post(r, f"/applications/{application.id}/status", **body)
            # Every application id differs between the two apps (random ids): map them all.
            ids = {made_one.id: f"APP-{job}" for job, made_one in made.items()}
            row = r.run.rows("SELECT * FROM applications WHERE id=?", (application.id,))[0]
            events = r.run.rows(
                "SELECT from_status, to_status, changed_at, source FROM status_history "
                "WHERE application_id=? ORDER BY changed_at, id",
                (application.id,),
            )
            results[source] = {
                "status": response.status_code,
                "text": normalise_ids(response.text, ids),
                "said": said_headers(response.headers),
                "application": {k: v for k, v in row.items() if k != "id"},
                "history": [{k: v for k, v in e.items() if k != "source"} for e in events],
                "sources": [e["source"] for e in events],
                "followup": dialog_title(response.text),
            }
            if source == "board":
                script = r.client.get("/static/js/board.js").text
                # The drop builds the same three fields as the menu form.
                for word in ("version", "status", "source", "data-version", "/status"):
                    assert word in script, word
                assert "board" in script
    menu, board = results["menu"], results["board"]
    assert menu["sources"][-1] == "menu" and board["sources"][-1] == "board"
    assert menu["sources"][:-1] == board["sources"][:-1]
    for key in ("status", "text", "said", "application", "history", "followup"):
        assert menu[key] == board[key], key
    assert menu["status"] == 200 and menu["followup"] == "Add the interview date?"


# --- follow-up dialogs --------------------------------------------------------------------------


def test_follow_up_dialog_for_interviewing_and_rejected_only(tmp_path):
    with refining(tmp_path) as r:
        r.run.app.state.system_time_zone = "Europe/Lisbon"
        made = seed_board(r)
        statuses = r.run.app.state.application_statuses
        waiting = statuses.add("Waiting", "active")
        lost = statuses.add("Rejected by phone", "closed")  # a label alone is not a follow-up

        # Interviewing offers the date.
        shown = move(r, made["board-job-2"], "interviewing")
        assert shown.status_code == 200
        assert dialog_title(shown.text) == "Add the interview date?"
        [dialog] = dialogs(shown.text)
        assert "data-modal" in dialog.attrs
        form = [e for e in elements(shown.text) if e.tag == "form" and e.within(dialog)]
        assert [e.attrs["action"] for e in form] == [
            f"/applications/{made['board-job-2'].id}/interview-date"
        ]
        fields = fields_in(shown.text, dialog)
        assert set(fields) == {"date", "time", "time_zone", "duration_minutes"}
        assert (
            fields["date"].attrs.get("type") == "date"
            and fields["time"].attrs.get("type") == "time"
        )
        assert fields["time_zone"].attrs.get("value") == "Europe/Lisbon"
        assert fields["duration_minutes"].attrs.get("value") == "60"
        labels = {e.text for e in elements(shown.text) if e.tag == "label" and e.within(dialog)}
        assert {"Date", "Time", "Time zone"} <= labels
        buttons = {
            e.text: e for e in elements(shown.text) if e.tag == "button" and e.within(dialog)
        }
        assert "Skip" in buttons and "Save" in buttons
        assert "data-modal-close" in buttons["Skip"].attrs
        # The zone is asked for when the computer's is unknown.
        r.run.app.state.system_time_zone = None
        unknown = move(r, made["board-job-3"], "interviewing")
        zone = fields_in(unknown.text, dialogs(unknown.text)[0])["time_zone"]
        assert not zone.attrs.get("value") and "required" in zone.attrs

        # Rejected offers feedback.
        shown = move(r, made["board-job-4"], "rejected")
        assert dialog_title(shown.text) == "Any feedback worth noting?"
        [dialog] = dialogs(shown.text)
        assert "data-modal" in dialog.attrs
        form = [e for e in elements(shown.text) if e.tag == "form" and e.within(dialog)]
        assert [e.attrs["action"] for e in form] == [
            f"/applications/{made['board-job-4'].id}/feedback"
        ]
        assert set(fields_in(shown.text, dialog)) == {"text"}
        names = {e.text for e in elements(shown.text) if e.tag == "button" and e.within(dialog)}
        assert {"Skip", "Save"} <= names

        # Nothing else asks: not the other built-in statuses, not custom ones.
        application = made["board-job-5"]
        for status in (
            "preparing",
            "applied",
            "screening",
            "offer",
            "withdrawn",
            "accepted",
            "declined",
            waiting.key,
            lost.key,
        ):
            answer = move(r, application, status)
            assert answer.status_code == 200, status
            assert dialogs(answer.text) == [], status
        # Moving to a status the dialog already offered for still records the move.
        assert history(r, made["board-job-2"].id)[-1][:2] == ("applied", "interviewing")


@pytest.mark.parametrize(
    ("values", "field"),
    [
        pytest.param(
            {"date": "2027-03-28", "time": "01:30", "time_zone": "Europe/Lisbon"},
            "starts_at",
            id="spring_forward_gap",
        ),
        pytest.param(
            {"date": "", "time": "10:00", "time_zone": "Europe/Lisbon"}, "starts_at", id="no_date"
        ),
        pytest.param(
            {"date": "2026-11-03", "time": "", "time_zone": "Europe/Lisbon"},
            "starts_at",
            id="no_time",
        ),
        pytest.param(
            {"date": "2026-02-30", "time": "10:00", "time_zone": "Europe/Lisbon"},
            "starts_at",
            id="no_such_day",
        ),
        pytest.param(
            {"date": "2026-11-03", "time": "25:99", "time_zone": "Europe/Lisbon"},
            "starts_at",
            id="no_such_time",
        ),
        pytest.param(
            {"date": "2026-11-03", "time": "10:00", "time_zone": "Mars/Base"},
            "time_zone",
            id="unknown_zone",
        ),
        pytest.param(
            {"date": "2026-11-03", "time": "10:00", "time_zone": ""}, "time_zone", id="no_zone"
        ),
        pytest.param(
            {"date": "2026-11-03", "time": "10:00", "time_zone": "../../etc/localtime"},
            "time_zone",
            id="zone_path",
        ),
        pytest.param(
            {
                "date": "2026-11-03",
                "time": "10:00",
                "time_zone": "Europe/Lisbon",
                "duration_minutes": "0",
            },
            "duration_minutes",
            id="zero_minutes",
        ),
        pytest.param(
            {
                "date": "2026-11-03",
                "time": "10:00",
                "time_zone": "Europe/Lisbon",
                "duration_minutes": "1441",
            },
            "duration_minutes",
            id="over_a_day",
        ),
        pytest.param(
            {
                "date": "2026-11-03",
                "time": "10:00",
                "time_zone": "Europe/Lisbon",
                "duration_minutes": "abc",
            },
            "duration_minutes",
            id="words_for_minutes",
        ),
        pytest.param(
            {
                "date": "2026-11-03",
                "time": "10:00",
                "time_zone": "Europe/Lisbon",
                "duration_minutes": "",
            },
            "duration_minutes",
            id="no_minutes",
        ),
    ],
)
def test_interview_date_gap_time_422_tied_to_field(tmp_path, values, field):
    with refining(tmp_path) as r:
        made = seed_board(r)
        application = made["board-job-5"]
        before = written(r)
        sent = {"duration_minutes": "60", **values}
        answer = post(r, f"/applications/{application.id}/interview-date", **sent)
        assert answer.status_code == 422, answer.text
        assert "<html" not in answer.text
        error = by_id(answer.text, f"interview-{field}-error")
        assert error is not None and error.text, "an error message tied to the field"
        tied = [
            e
            for e in elements(answer.text)
            if e.tag in ("input", "select")
            and f"interview-{field}-error" in (e.attrs.get("aria-describedby") or "").split()
        ]
        assert tied and all(e.attrs.get("aria-invalid") == "true" for e in tied)
        expected = {"date", "time"} if field == "starts_at" else {field}
        assert {e.attrs.get("name") for e in tied} <= expected
        # What the user typed stays in the form, and nothing was written.
        kept = {
            e.attrs.get("name"): e.attrs.get("value")
            for e in elements(answer.text)
            if e.tag == "input"
        }
        for name, value in sent.items():
            if value and name in kept and field != name:
                assert kept[name] == value, name
        assert written(r) == before


def test_interview_date_saved_with_zone_and_utc(tmp_path):
    with refining(tmp_path) as r:
        made = seed_board(r)
        application = made["board-job-5"]
        path = f"/applications/{application.id}/interview-date"
        done = post(
            r,
            path,
            date="2026-11-03",
            time="14:00",
            time_zone="America/New_York",
            duration_minutes="90",
        )
        assert done.status_code == 200 and 'id="board-content"' in done.text
        assert "announce" in said_headers(done.headers)
        rows = r.run.rows(
            "SELECT type, starts_at, ends_at, time_zone FROM events WHERE application_id=?",
            (application.id,),
        )
        assert [tuple(row.values()) for row in rows] == [
            (
                "interview",
                "2026-11-03T19:00:00+00:00",
                "2026-11-03T20:30:00+00:00",
                "America/New_York",
            )
        ]
        # The repeated autumn hour takes its first occurrence.
        other = made["board-job-4"]
        repeated = post(
            r,
            f"/applications/{other.id}/interview-date",
            date="2026-10-25",
            time="01:30",
            time_zone="Europe/Lisbon",
            duration_minutes="60",
        )
        assert repeated.status_code == 200
        [row] = r.run.rows("SELECT starts_at FROM events WHERE application_id=?", (other.id,))
        assert row["starts_at"] == "2026-10-25T00:30:00+00:00"
        # Skipping is not a request at all: nothing else was written for the other jobs.
        assert r.run.rows("SELECT COUNT(*) AS n FROM events")[0]["n"] == 2


def test_feedback_saved_blank_refused_and_capped(tmp_path):
    with refining(tmp_path) as r:
        made = seed_board(r)
        application = made["board-job-6"]  # Rejected
        path = f"/applications/{application.id}/feedback"
        notes = lambda: r.run.rows(  # noqa: E731
            "SELECT kind, text FROM application_notes WHERE application_id=?", (application.id,)
        )
        before = written(r)

        assert r.client.post(path, data={"text": "x"}).status_code == 403
        assert (
            r.client.post(path, data={"text": "x"}, headers=r.run.run.with_token()).status_code
            == 403
        )
        assert (
            r.client.post(
                path, data={"text": "x"}, headers=r.run.run.with_token(origin=HOSTILE)
            ).status_code
            == 403
        )
        assert post_raw(r, path, {"text": "x", "extra": "1"}).status_code == 400
        assert post_raw(r, path, {}).status_code == 400
        assert post_raw(r, path, {"text": ["a", "b"]}).status_code == 400
        huge = post_raw(r, path, {"text": "x" * 70_000})
        assert huge.status_code == 413 and huge.text == INVALID
        assert post(r, "/applications/no-such-application/feedback", text="x").status_code == 404
        assert written(r) == before

        for blank in ("", "   \n "):
            refused = post(r, path, text=blank)
            assert refused.status_code == 422, repr(blank)
            error = by_id(refused.text, "feedback-text-error")
            assert error is not None and error.text
            field = [e for e in elements(refused.text) if e.tag == "textarea"]
            assert field and "feedback-text-error" in field[0].attrs.get("aria-describedby", "")
            assert field[0].attrs.get("aria-invalid") == "true"
        assert written(r) == before

        text = "<b>Fictional</b> note: they wanted more irrigation depth."
        saved = post(r, path, text=text)
        assert saved.status_code == 200 and 'id="board-content"' in saved.text
        assert "<b>Fictional</b>" not in saved.text  # never markup
        assert [tuple(row.values()) for row in notes()] == [("feedback", text)]


def test_follow_up_posts_unknown_ids_get_the_fixed_404(tmp_path):
    with refining(tmp_path) as r:
        seed_board(r)
        for route, values in (
            (
                "interview-date",
                {
                    "date": "2026-11-03",
                    "time": "10:00",
                    "time_zone": "Europe/Lisbon",
                    "duration_minutes": "60",
                },
            ),
            ("feedback", {"text": "x"}),
        ):
            for unknown in ("no-such-application", "a.b"):
                missing = post(r, f"/applications/{unknown}/{route}", **values)
                assert missing.status_code == 404, (route, unknown)
                assert missing.text == ApplicationNotFound.message


# --- table view ---------------------------------------------------------------------------------

COLUMNS = ["Company", "Role", "Status", "Applied", "Last activity"]
SORT_KEYS = ["company", "role", "status", "applied", "activity"]


def test_board_table_view_has_caption_columns_and_every_application(tmp_path):
    with refining(tmp_path) as r:
        made = seed_board(r)
        shown = r.client.get("/board?view=table&sort=company&dir=asc")
        parsed = table(shown.text)
        assert "".join(parsed.caption).strip(), "a table needs a caption"
        [header] = parsed.headers
        assert all(name in cell for name, cell in zip(COLUMNS, header["cells"], strict=True))
        assert len(header["cells"]) == 5
        assert sorted(table_ids(shown.text)) == sorted(a.id for a in made.values())  # closed too
        rows = {row["id"]: row["cells"] for row in parsed.rows}
        assert rows[made["board-job-6"].id][:3] == [
            "Fern Fictional Labs",
            "Boiler Fitter",
            "Closed: rejected",
        ]
        assert rows[made["board-job-2"].id][2] == "Applied"
        # Companies sort without regard to case: alder, Birch, Birch, cedar, Dune, elm, Fern, ...
        companies = [row["cells"][0] for row in parsed.rows]
        assert companies == sorted(companies, key=str.lower)
        # The Board/Table toggle links both ways and marks the current one.
        links = {
            e.text: e
            for e in elements(shown.text)
            if e.tag == "a"
            and e.text in ("Board", "Table")
            and e.attrs.get("href", "").startswith("/board")
        }
        assert set(links) == {"Board", "Table"}
        assert links["Table"].attrs.get("aria-current") == "true"
        assert links["Board"].attrs.get("aria-current") != "true"
        assert query(links["Table"].attrs["href"]).get("view") == "table"


@pytest.mark.parametrize("sort", SORT_KEYS)
def test_board_table_sorts_each_column_both_ways(tmp_path, sort):
    with refining(tmp_path) as r:
        seed_board(r)
        column = SORT_KEYS.index(sort)
        shown = {}
        for direction in ("asc", "desc"):
            page = r.client.get(f"/board?view=table&sort={sort}&dir={direction}")
            assert page.status_code == 200
            parsed = table(page.text)
            [header] = parsed.headers
            wanted = {"asc": "ascending", "desc": "descending"}[direction]
            assert [v for v in header["aria_sort"] if v and v != "none"] == [wanted]
            assert header["aria_sort"][column] == wanted
            shown[direction] = [row["cells"][column] for row in parsed.rows]
        if sort in ("company", "role"):
            assert shown["asc"] == sorted(shown["asc"], key=str.lower)
            assert shown["desc"] == sorted(shown["desc"], key=str.lower, reverse=True)
        # Every header holds a sort link, and the sorted one offers the other direction.
        header = table(page.text).headers[0]
        assert len(header["hrefs"]) == 5
        for key, href in zip(SORT_KEYS, header["hrefs"], strict=True):
            assert query(href)["sort"] == key
        assert query(header["hrefs"][column])["dir"] == "asc"  # it is sorted desc now


def test_board_table_defaults_to_latest_activity_first(tmp_path):
    with refining(tmp_path) as r:
        made = seed_board(r)
        shown = r.client.get("/board?view=table")
        header = table(shown.text).headers[0]
        assert header["aria_sort"][SORT_KEYS.index("activity")] == "descending"
        # The last application touched comes first.
        assert table_ids(shown.text)[0] == made["board-job-8"].id


def test_board_query_values_are_validated_and_pages_clamp(tmp_path):
    with refining(tmp_path) as r:
        seeder(r).plan()
        # An unknown sort is dropped for the default order (the list-page convention), never a
        # 500, and the value is not echoed.
        default = table_ids(r.client.get("/board", params={"view": "table"}).text)
        assert default
        for sort in ("nope", "COMPANY ", "company;drop", "%27", "x" * 500):
            page = r.client.get("/board", params={"view": "table", "sort": sort})
            assert page.status_code == 200, sort
            assert table_ids(page.text) == default, sort
            assert sort.strip() not in page.text
            fragment = r.client.get(
                "/board/columns",
                params={"view": "table", "sort": sort},
                headers=r.run.run.htmx(),
            )
            assert fragment.status_code == 200, sort
            assert table_ids(fragment.text) == default, sort
        # Other odd values are dropped for their defaults or refused, but never a server error.
        for odd in ({"view": "sideways"}, {"dir": "sideways"}, {"closed": "perhaps"}):
            assert r.client.get("/board", params=odd).status_code < 500, odd
            assert r.client.get("/board", params={"view": "table", **odd}).status_code < 500, odd


def test_board_table_is_50_per_page_and_clamps_the_page(tmp_path):
    with refining(tmp_path) as r:
        desk = seeder(r)
        for n in range(1, 56):
            desk.application(
                f"bulk-job-{n:03d}",
                f"Fictional Role {n:03d}",
                f"Fictional Company {n % 7}",
                "applied",
            )
        first = r.client.get("/board?view=table&sort=role&dir=asc")
        assert len(table_ids(first.text)) == 50
        assert "page=2" in first.text  # a way to the next page without a mouse
        second = r.client.get("/board?view=table&sort=role&dir=asc&page=2")
        assert len(table_ids(second.text)) == 5
        assert not set(table_ids(first.text)) & set(table_ids(second.text))
        for odd in ("0", "-4", "abc", "", "1.5"):
            page = r.client.get("/board", params={"view": "table", "sort": "role", "page": odd})
            assert page.status_code == 200 and len(table_ids(page.text)) == 50, odd
        for far in ("99", "99999999999999999999"):
            page = r.client.get("/board", params={"view": "table", "sort": "role", "page": far})
            assert page.status_code == 200 and len(table_ids(page.text)) == 5, far
        assert len(table_ids(get(r, "/board/columns?view=table&sort=role&page=2").text)) == 5


def test_board_board_view_ignores_table_values(tmp_path):
    with refining(tmp_path) as r:
        made = seed_board(r)
        page = r.client.get("/board?sort=company&dir=desc&page=3")
        assert page.status_code == 200
        assert sum(len(v) for v in cards_by_status(page.text).values()) == 5
        assert made  # the open applications are all on the board


# --- review additions ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("current", "expanded"),
    [
        pytest.param("http://testserver/board?closed=1", True, id="closed_open"),
        pytest.param("http://testserver/board?closed=1&closed=1", True, id="closed_twice"),
        pytest.param("http://testserver/board?view=table&closed=1", True, id="closed_with_more"),
        pytest.param("http://testserver/board?closed=0", False, id="closed_zero"),
        pytest.param("http://testserver/board", False, id="plain_board"),
        pytest.param("http://testserver/inbox?closed=1", False, id="other_page"),
        pytest.param("not a url", False, id="garbage"),
        pytest.param("http://[::1", False, id="unparsable"),
        pytest.param(None, False, id="no_header"),
    ],
)
def test_move_answer_shows_closed_as_the_page_address_says(tmp_path, current, expanded):
    with refining(tmp_path) as r:
        made = seed_board(r)
        application = made["board-job-2"]
        headers = {} if current is None else {"HX-Current-URL": current}
        moved = post_raw(
            r,
            f"/applications/{application.id}/status",
            {"version": str(version_of(r, application.id)), "status": "screening"},
            **headers,
        )
        assert moved.status_code == 200
        toggle = by_id(moved.text, "board-closed-toggle")
        assert toggle is not None and toggle.text == "Closed: 3"
        assert toggle.attrs.get("aria-expanded") == ("true" if expanded else "false")
        assert (by_id(moved.text, "board-closed-cards") is not None) is expanded
        if expanded:
            assert moved.text.count("Closed: rejected") == 2
        # The follow-ups answer with the board in the same state.
        other = made["board-job-5"]  # Interviewing
        saved = post_raw(
            r,
            f"/applications/{other.id}/interview-date",
            {
                "date": "2026-11-03",
                "time": "10:00",
                "time_zone": "Europe/Lisbon",
                "duration_minutes": "60",
            },
            **headers,
        )
        assert saved.status_code == 200
        assert (by_id(saved.text, "board-closed-cards") is not None) is expanded


def test_a_move_without_source_is_recorded_as_the_menu(tmp_path):
    with refining(tmp_path) as r:
        made = seed_board(r)
        application = made["board-job-2"]
        bare = post(
            r,
            f"/applications/{application.id}/status",
            version=str(version_of(r, application.id)),
            status="screening",
        )
        assert bare.status_code == 200
        assert history(r, application.id)[-1] == ("applied", "screening", "menu")


def test_board_script_sends_the_drop_as_the_same_move(tmp_path):
    with refining(tmp_path) as r:
        script = r.client.get("/static/js/board.js").text
        assert 'source: "board"' in script
        assert "version: version, status: status" in script
        assert '"/applications/" + encodeURIComponent(id) + "/status"' in script
        assert "htmx.ajax" in script and '"POST"' in script
        assert 'getAttribute("data-version")' in script


@pytest.mark.parametrize(
    ("length", "status"), [(5000, 200), (5001, 422)], ids=["at_the_cap", "over_the_cap"]
)
def test_feedback_length_cap(tmp_path, length, status):
    with refining(tmp_path) as r:
        made = seed_board(r)
        application = made["board-job-6"]
        before = r.run.rows(
            "SELECT COUNT(*) AS n FROM application_notes WHERE application_id=?", (application.id,)
        )
        answer = post(r, f"/applications/{application.id}/feedback", text="x" * length)
        assert answer.status_code == status
        count = r.run.rows(
            "SELECT COUNT(*) AS n FROM application_notes WHERE application_id=?", (application.id,)
        )[0]["n"]
        if status == 422:
            error = by_id(answer.text, "feedback-text-error")
            assert error is not None and error.text
            [field] = [e for e in elements(answer.text) if e.tag == "textarea"]
            assert "feedback-text-error" in field.attrs.get("aria-describedby", "")
            assert field.attrs.get("aria-invalid") == "true"
            assert count == before[0]["n"]
        else:
            assert count == before[0]["n"] + 1


def test_feedback_line_breaks_are_stored_as_lf_and_nul_is_dropped(tmp_path):
    with refining(tmp_path) as r:
        made = seed_board(r)
        application = made["board-job-6"]
        path = f"/applications/{application.id}/feedback"
        assert post(r, path, text="First line.\r\nSecond line.\rThird line.").status_code == 200
        assert post(r, path, text="Before\x00after").status_code == 200
        rows = r.run.rows(
            "SELECT text FROM application_notes WHERE application_id=? AND kind='feedback' "
            "ORDER BY created_at, id",
            (application.id,),
        )
        assert sorted(row["text"] for row in rows) == sorted(
            ["First line.\nSecond line.\nThird line.", "Beforeafter"]
        )


def test_board_table_row_order_follows_the_seeded_times(tmp_path):
    """Each application was made a minute after the last, in plan order, so the order of every
    sort is known: activity and status run in plan order; applied has the four never-applied
    ones first (no date) and then job 2 to 5 as they were first applied."""
    with refining(tmp_path) as r:
        made = seed_board(r)
        number = {a.id: int(job.rsplit("-", 1)[1]) for job, a in made.items()}

        def order(sort: str, direction: str) -> list[int]:
            page = r.client.get(f"/board?view=table&sort={sort}&dir={direction}")
            return [number[i] for i in table_ids(page.text)]

        plan = list(range(1, 9))
        assert order("activity", "asc") == plan
        assert order("activity", "desc") == plan[::-1]
        assert order("status", "asc") == plan
        assert order("status", "desc") == plan[::-1]
        applied = order("applied", "asc")
        assert applied[-4:] == [2, 3, 4, 5] and set(applied[:4]) == {1, 6, 7, 8}
        applied = order("applied", "desc")
        assert applied[:4] == [5, 4, 3, 2] and set(applied[4:]) == {1, 6, 7, 8}
