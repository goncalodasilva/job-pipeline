"""M5.4 the board in a browser: drag and the keyboard menu, follow-ups, Closed, table, phone.

Assumed markup and behaviour (see ``tests/board_pages.py`` and ``tests/integration/test_board.py``):

- Columns are ``#board-columns section.board-column[data-status]`` with a sortable
  ``ul[data-sortable][data-status]`` of ``li.board-card[data-application-id]``; an empty list is
  still tall enough to drop on. A card's "Move to…" button opens a ``role="menu"`` of
  ``role="menuitem"`` buttons whose text is the status label: Enter opens it and focuses the
  first item, ArrowDown/ArrowUp move, Enter moves the card, Esc closes it and gives the focus
  back to the button.
- A move (menu or drop) says "Moved to <label>." in the live region and puts the focus on the
  moved card (a card, its title, or anything inside it) when the card is shown.
- ``window.Sortable`` is the vendored library; ``Sortable.get(list)`` is the list's instance.
  Dragging is ``disabled`` at phone width (the menu stays) and ``animation`` is 0 with reduced
  motion. After every htmx swap each list has an instance again.
- A refused drop (409) puts the card back in its list and position and says why in the live
  region ("This item changed elsewhere…").
- The follow-up dialogs are ``dialog[data-modal]`` named "Add the interview date?" (labels
  Date, Time, Time zone, Duration…; buttons Save and Skip) and "Any feedback worth noting?"
  (label Feedback; buttons Save and Skip). Esc is Skip.
- Table view: ``#board-table`` with sort links in its header cells, ``aria-sort`` on the sorted
  one; the toggle is ``nav[aria-label="Board view"]`` with the links "Board" and "Table".
"""

import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import Any

import pytest
from playwright.sync_api import Locator, Page, expect

from app.domain.ids import is_safe_id
from app.modules.storage import Database
from app.services.applications import ApplicationService, ApplicationStatuses
from tests.board_pages import Seeder
from tests.e2e.axe import assert_no_serious_violations, wait_for_settled_styles
from tests.e2e.conftest import DESKTOP, PHONE
from tests.e2e.test_facts_page import RECORD_ANNOUNCEMENTS, press_by_keyboard, wait_said
from tests.e2e.test_shell import assert_target_size, has_no_horizontal_scroll, new_page, open_app
from tests.fakes.clock import FrozenClock
from tests.fakes.ids import SequentialIdFactory
from tests.waiting import wait_until

pytestmark = pytest.mark.e2e

SEED_START = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)  # long before the app's own clock
MOVE = re.compile(r"^Move to")
STALE_WORDS = "changed elsewhere"


@contextmanager
def desk(app: Any) -> Iterator[Seeder]:
    """Fictional jobs and applications written into the running app's database."""
    clock = FrozenClock(SEED_START)
    with Database.open(app.data_dir / "app.db") as db:
        yield Seeder.make(
            db,
            ApplicationService(lambda: db, clock, SequentialIdFactory("seed")),
            ApplicationStatuses(lambda: db, clock, SequentialIdFactory("seedstatus")),
            lambda: clock.advance(timedelta(minutes=1)),
            lambda: str(clock.now().isoformat()),
        )


def rows(app: Any, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    with Database.open(app.data_dir / "app.db") as db:
        return db.connection.execute(sql, params).fetchall()


def status_of(app: Any, application_id: str) -> str:
    return str(rows(app, "SELECT status_key FROM applications WHERE id=?", (application_id,))[0][0])


def history(app: Any, application_id: str) -> list[tuple[Any, ...]]:
    return rows(
        app,
        "SELECT from_status, to_status, source FROM status_history WHERE application_id=? "
        "ORDER BY changed_at, id",
        (application_id,),
    )


def card(page: Page, application_id: str) -> Locator:
    assert is_safe_id(application_id)
    return page.locator(f'li.board-card[data-application-id="{application_id}"]')


def column_list(page: Page, key: str) -> Locator:
    return page.locator(f'#board-columns ul[data-sortable][data-status="{key}"]')


def focus_is_in(page: Page, locator: Locator) -> None:
    """The focus is on the element or inside it (waits for a swap's deliberate focus)."""
    handle = locator.element_handle()
    assert handle is not None
    page.wait_for_function("(el) => el.contains(document.activeElement)", arg=handle)


def wait_said_containing(page: Page, text: str) -> None:
    page.wait_for_function(
        "(text) => (window.__said || []).some((said) => said.includes(text))", arg=text
    )


def open_board(page: Page, app: Any, path: str = "/board") -> None:
    page.add_init_script(RECORD_ANNOUNCEMENTS)
    open_app(page, app, path)
    page.wait_for_function("() => typeof window.Sortable !== 'undefined'")


def focused_text(page: Page) -> str:
    text: str = page.evaluate("() => (document.activeElement?.textContent || '').trim()")
    return text


def open_menu(page: Page, one: Locator) -> None:
    button = one.get_by_role("button", name=MOVE)
    press_by_keyboard(page, button)
    expect(button).to_have_attribute("aria-expanded", "true")
    menu = one.get_by_role("menu")
    expect(menu).to_be_visible()
    expect(one.get_by_role("menuitem").first).to_be_focused()


def choose_by_keyboard(page: Page, one: Locator, label: str) -> None:
    """Enter opens the menu, the arrows choose and Enter moves (no mouse)."""
    open_menu(page, one)
    for _ in range(14):
        if focused_text(page) == label:
            page.keyboard.press("Enter")
            return
        page.keyboard.press("ArrowDown")
    raise AssertionError(f"no menu item {label!r} was reached with the arrow keys")


def drag(page: Page, application_id: str, target: Locator) -> None:
    """Drag a card onto a list with real mouse movement; wait for it to arrive, then drop."""
    one = card(page, application_id)
    one.scroll_into_view_if_needed()
    target.scroll_into_view_if_needed()
    start = one.bounding_box()
    end = target.bounding_box()
    assert start and end
    page.mouse.move(start["x"] + 24, start["y"] + 18)
    page.mouse.down()
    page.mouse.move(start["x"] + 30, start["y"] + 24, steps=4)
    page.mouse.move(end["x"] + end["width"] / 2, end["y"] + min(end["height"] / 2, 70), steps=30)
    # Wait for the drag to put the card in the target list, not for a fixed time.
    expect(target.locator(f'li[data-application-id="{application_id}"]')).to_have_count(1)
    page.mouse.up()


def normal_markup(one: Locator, application_id: str) -> str:
    """The card's markup without ids and client-only state (focus, drag, inline styles)."""
    html: str = one.evaluate("(el) => el.outerHTML")
    html = html.replace(application_id, "APP")
    html = re.sub(r'\s(?:style|tabindex|data-autofocus|draggable)(?:="[^"]*")?', "", html)
    html = re.sub(r"\s*\bsortable-[\w-]+", "", html)
    return re.sub(r"\s+", " ", html)


def every_list_has_an_instance(page: Page) -> None:
    page.wait_for_function(
        "() => { const lists = [...document.querySelectorAll('[data-sortable]')];"
        " return lists.length > 0 && lists.every((el) => !!window.Sortable.get(el)); }"
    )


# --- AC-M5.2: drag and menu give identical results ----------------------------------------------


def test_ac_m5_2_drag_and_status_menu_produce_identical_results(running_app, browser):
    with desk(running_app) as seed:
        by_menu = seed.application("twin-job-a", "Seed Sorter", "Fern Fictional Labs", "applied")
        by_drag = seed.application("twin-job-b", "Seed Sorter", "Fern Fictional Labs", "applied")
        seed.application("other-job", "Frost Watcher", "alder Fictional Mills", "interviewing")
    with new_page(browser, DESKTOP) as page:
        open_board(page, running_app)
        every_list_has_an_instance(page)

        choose_by_keyboard(page, card(page, by_menu.id), "Screening")
        wait_said(page, "Moved to Screening.")
        expect(
            column_list(page, "screening").locator(f'[data-application-id="{by_menu.id}"]')
        ).to_have_count(1)
        focus_is_in(page, card(page, by_menu.id))
        every_list_has_an_instance(page)  # set up again after the swap

        drag(page, by_drag.id, column_list(page, "screening"))
        wait_until(
            lambda: status_of(running_app, by_drag.id) == "screening",
            timeout=10,
            message="the dropped card's status was saved",
        )
        wait_said(page, "Moved to Screening.", times=2)
        expect(
            column_list(page, "screening").locator(f'[data-application-id="{by_drag.id}"]')
        ).to_have_count(1)
        expect(column_list(page, "applied").locator("li.board-card")).to_have_count(0)

        # The same status and version, and the same history apart from ids, times and how the
        # move was made (the source is the one thing that says menu or board).
        def stored(application_id: str) -> tuple[Any, ...]:
            return rows(
                running_app,
                "SELECT status_key, version FROM applications WHERE id=?",
                (application_id,),
            )[0]

        assert stored(by_menu.id) == stored(by_drag.id) == ("screening", 3)
        menu_history, drag_history = (
            history(running_app, by_menu.id),
            history(running_app, by_drag.id),
        )
        assert [h[:2] for h in menu_history] == [h[:2] for h in drag_history]
        assert [h[:2] for h in menu_history][-1] == ("applied", "screening")
        assert (menu_history[-1][2], drag_history[-1][2]) == ("menu", "board")
        assert [h[2] for h in menu_history][:-1] == [h[2] for h in drag_history][:-1]

        # And the same card, in the same place in the page.
        assert normal_markup(card(page, by_menu.id), by_menu.id) == normal_markup(
            card(page, by_drag.id), by_drag.id
        )
        assert_no_serious_violations(page)


# --- AC-M5.3: the follow-ups, offered and skippable ---------------------------------------------


@pytest.mark.parametrize("viewport", [DESKTOP, PHONE])
def test_ac_m5_3_interviewing_date_and_rejected_feedback_offered_and_skippable(
    running_app, browser, viewport
):
    with desk(running_app) as seed:
        dated = seed.application("follow-job-1", "Crop Analyst", "Dune Fictional Ltd", "applied")
        skipped = seed.application("follow-job-2", "Seed Buyer", "cedar Fictional Works", "applied")
        written = seed.application(
            "follow-job-3", "Pallet Driver", "elm Fictional Yard", "screening"
        )
        escaped = seed.application(
            "follow-job-4", "Boiler Fitter", "Fern Fictional Labs", "screening"
        )
        plain = seed.application(
            "follow-job-5", "Frost Watcher", "alder Fictional Mills", "applied"
        )
        seed.statuses.add("Waiting", "active")
    with new_page(browser, viewport) as page:
        open_board(page, running_app)

        # Interviewing offers the date, with the computer's time zone or a required field.
        choose_by_keyboard(page, card(page, dated.id), "Interviewing")
        dialog = page.get_by_role("dialog", name="Add the interview date?")
        expect(dialog).to_be_visible()
        focus_is_in(page, dialog)
        zone = dialog.get_by_label("Time zone")
        if zone.input_value() == "":
            expect(zone).to_have_attribute("required", "")
        expect(dialog.get_by_label(re.compile("^Duration"))).to_have_value("60")
        dialog.get_by_label("Date", exact=True).fill("2026-11-03")
        dialog.get_by_label("Time", exact=True).fill("14:00")
        zone.fill("America/New_York")
        press_by_keyboard(page, dialog.get_by_role("button", name="Save", exact=True))
        expect(dialog).not_to_be_visible()
        focus_is_in(page, card(page, dated.id))
        assert rows(
            running_app,
            "SELECT type, starts_at, ends_at, time_zone FROM events WHERE application_id=?",
            (dated.id,),
        ) == [
            (
                "interview",
                "2026-11-03T19:00:00+00:00",
                "2026-11-03T20:00:00+00:00",
                "America/New_York",
            )
        ]
        expect(card(page, dated.id)).to_contain_text("Interview")

        # Skip writes nothing; the move itself stays.
        choose_by_keyboard(page, card(page, skipped.id), "Interviewing")
        dialog = page.get_by_role("dialog", name="Add the interview date?")
        expect(dialog).to_be_visible()
        press_by_keyboard(page, dialog.get_by_role("button", name="Skip", exact=True))
        expect(dialog).not_to_be_visible()
        focus_is_in(page, card(page, skipped.id))
        assert rows(
            running_app, "SELECT COUNT(*) FROM events WHERE application_id=?", (skipped.id,)
        ) == [(0,)]
        assert status_of(running_app, skipped.id) == "interviewing"

        # Rejected offers feedback; it is kept as a note. The card moves into Closed.
        choose_by_keyboard(page, card(page, written.id), "Rejected")
        dialog = page.get_by_role("dialog", name="Any feedback worth noting?")
        expect(dialog).to_be_visible()
        field = dialog.get_by_label("Feedback")
        field.fill("They wanted more depth on irrigation. Fictional feedback.")
        press_by_keyboard(page, dialog.get_by_role("button", name="Save", exact=True))
        expect(dialog).not_to_be_visible()
        assert rows(
            running_app,
            "SELECT kind, text FROM application_notes WHERE application_id=?",
            (written.id,),
        ) == [("feedback", "They wanted more depth on irrigation. Fictional feedback.")]
        expect(page.get_by_role("button", name=re.compile(r"^Closed: 1$"))).to_be_visible()

        # Esc is Skip too.
        choose_by_keyboard(page, card(page, escaped.id), "Rejected")
        dialog = page.get_by_role("dialog", name="Any feedback worth noting?")
        expect(dialog).to_be_visible()
        page.keyboard.press("Escape")
        expect(dialog).not_to_be_visible()
        assert rows(
            running_app,
            "SELECT COUNT(*) FROM application_notes WHERE application_id=?",
            (escaped.id,),
        ) == [(0,)]
        assert status_of(running_app, escaped.id) == "rejected"
        expect(page.get_by_role("button", name=re.compile(r"^Closed: 2$"))).to_be_visible()

        # Other statuses, built in or custom, ask nothing.
        for label in ("Waiting", "Screening"):
            choose_by_keyboard(page, card(page, plain.id), label)
            wait_said(page, f"Moved to {label}.")
            expect(page.get_by_role("dialog")).to_have_count(0)
        assert_no_serious_violations(page)


# --- the keyboard menu --------------------------------------------------------------------------


@pytest.mark.parametrize("viewport", [DESKTOP, PHONE])
def test_board_keyboard_only_move_announces_and_keeps_focus(running_app, browser, viewport):
    with desk(running_app) as seed:
        mover = seed.application("key-job-1", "Seed Buyer", "cedar Fictional Works", "applied")
        seed.application("key-job-2", "Crop Analyst", "Dune Fictional Ltd", "applied")
    with new_page(browser, viewport) as page:
        open_board(page, running_app)
        one = card(page, mover.id)
        button = one.get_by_role("button", name=MOVE)
        expect(button).to_have_attribute("aria-haspopup", "menu")
        expect(button).to_have_attribute("aria-expanded", "false")

        # Enter opens the menu on its first item; the arrows move; Esc closes and gives the
        # focus back to the button without moving anything.
        open_menu(page, one)
        first = focused_text(page)
        page.keyboard.press("ArrowDown")
        assert focused_text(page) != first
        page.keyboard.press("ArrowUp")
        assert focused_text(page) == first
        page.keyboard.press("Escape")
        expect(one.get_by_role("menu")).not_to_be_visible()
        expect(button).to_be_focused()
        expect(button).to_have_attribute("aria-expanded", "false")
        assert status_of(running_app, mover.id) == "applied"

        # Choose a status: moved, announced, and the focus is on the moved card.
        choose_by_keyboard(page, one, "Screening")
        wait_said(page, "Moved to Screening.")
        moved = card(page, mover.id)
        expect(
            column_list(page, "screening").locator(f'[data-application-id="{mover.id}"]')
        ).to_have_count(1)
        focus_is_in(page, moved)
        assert status_of(running_app, mover.id) == "screening"
        assert history(running_app, mover.id)[-1] == ("applied", "screening", "menu")

        # One Tab from there reaches the same card's own menu button: the focus kept its place.
        page.keyboard.press("Tab")
        focus_is_in(page, moved)
        expect(moved.get_by_role("button", name=MOVE)).to_be_focused()
        # And the card can be moved again from where it is.
        page.keyboard.press("Enter")
        expect(moved.get_by_role("menu")).to_be_visible()
        page.keyboard.press("Escape")
        assert_no_serious_violations(page)


def test_stale_menu_move_shows_reload_prompt_and_keeps_the_board(running_app, browser):
    with desk(running_app) as seed:
        stale = seed.application("stale-job-1", "Seed Buyer", "cedar Fictional Works", "applied")
        seed.application("stale-job-2", "Crop Analyst", "Dune Fictional Ltd", "screening")
        with new_page(browser, DESKTOP) as page:
            open_board(page, running_app)
            # Another window moves the card first.
            seed.applications.move(
                stale.id, "applied", expected_version=stale.version, source="menu"
            )
            before = rows(running_app, "SELECT version FROM applications WHERE id=?", (stale.id,))
            choose_by_keyboard(page, card(page, stale.id), "Offer")
            alert = page.get_by_role("alert").filter(has_text="Reload the page")
            expect(alert).to_be_visible()
            # The rest of the board is still there and the card did not move.
            expect(page.locator("li.board-card")).to_have_count(2)
            expect(
                column_list(page, "applied").locator(f'[data-application-id="{stale.id}"]')
            ).to_have_count(1)
            assert (
                rows(running_app, "SELECT version FROM applications WHERE id=?", (stale.id,))
                == before
            )
            assert status_of(running_app, stale.id) == "applied"


# --- drag --------------------------------------------------------------------------------------


def test_refused_drop_returns_card_and_announces(running_app, browser):
    with desk(running_app) as seed:
        seed.application("refuse-job-1", "Seed Buyer", "cedar Fictional Works", "applied")
        seed.application("refuse-job-2", "Crop Analyst", "Dune Fictional Ltd", "applied")
        with new_page(browser, DESKTOP) as page:
            open_board(page, running_app)
            applied = column_list(page, "applied")
            expect(applied.locator("li.board-card")).to_have_count(2)
            top = applied.locator("li.board-card").first
            moving = str(top.get_attribute("data-application-id"))
            # The page's version of the card goes stale: someone else changed it.
            with_version = rows(
                running_app, "SELECT version FROM applications WHERE id=?", (moving,)
            )[0][0]
            seed.applications.move(moving, "applied", expected_version=with_version, source="menu")
            changed = history(running_app, moving)

            drag(page, moving, column_list(page, "screening"))

            wait_said_containing(page, STALE_WORDS)
            # Back where it was: first in Applied, nothing in Screening, nothing saved.
            expect(column_list(page, "screening").locator("li.board-card")).to_have_count(0)
            expect(applied.locator("li.board-card")).to_have_count(2)
            expect(applied.locator("li.board-card").first).to_have_attribute(
                "data-application-id", moving
            )
            assert status_of(running_app, moving) == "applied"
            assert history(running_app, moving) == changed


def test_dragging_is_off_at_phone_width_and_animation_off_with_reduced_motion(running_app, browser):
    with desk(running_app) as seed:
        seed.application("motion-job-1", "Seed Buyer", "cedar Fictional Works", "applied")

    def option(page: Page, name: str) -> Any:
        return page.evaluate(
            "(name) => [...document.querySelectorAll('[data-sortable]')]"
            ".map((el) => window.Sortable.get(el).option(name))",
            name,
        )

    with new_page(browser, PHONE) as page:
        open_board(page, running_app)
        every_list_has_an_instance(page)
        assert all(option(page, "disabled")), "no dragging at phone width"
    with new_page(browser, DESKTOP) as page:
        open_board(page, running_app)
        every_list_has_an_instance(page)
        assert not any(option(page, "disabled"))
        assert all(value > 0 for value in option(page, "animation"))
    with new_page(browser, DESKTOP, reduced_motion="reduce") as page:
        open_board(page, running_app)
        every_list_has_an_instance(page)
        assert option(page, "animation") == [0] * len(option(page, "animation"))


# --- Closed -------------------------------------------------------------------------------------


def test_closed_column_collapsed_and_expands(running_app, browser):
    with desk(running_app) as seed:
        seed.plan()
    with new_page(browser, DESKTOP) as page:
        open_board(page, running_app)
        toggle = page.get_by_role("button", name="Closed: 3", exact=True)
        expect(toggle).to_be_visible()
        expect(toggle).to_have_attribute("aria-expanded", "false")
        for hidden in ("Boiler Fitter", "Frost Watcher", "Pruning Lead"):
            expect(page.get_by_text(hidden)).to_have_count(0)

        press_by_keyboard(page, toggle)
        toggle = page.get_by_role("button", name="Closed: 3", exact=True)
        expect(toggle).to_have_attribute("aria-expanded", "true")
        expect(toggle).to_be_focused()
        closed = page.locator("#board-closed-cards")
        expect(closed.locator("li.board-card")).to_have_count(3)
        expect(closed.get_by_text("Closed: rejected")).to_have_count(2)
        expect(closed.get_by_text("Closed: withdrawn")).to_have_count(1)
        for shown in ("Boiler Fitter", "Frost Watcher", "Pruning Lead"):
            expect(closed.get_by_text(shown)).to_have_count(1)
        assert_no_serious_violations(page)

        # A closed application can be reopened from its own menu.
        reopened = closed.locator("li.board-card").filter(has_text="Boiler Fitter")
        choose_by_keyboard(page, reopened, "Applied")
        wait_said(page, "Moved to Applied.")
        expect(page.get_by_role("button", name=re.compile(r"^Closed: 2$"))).to_be_visible()
        expect(column_list(page, "applied").get_by_text("Boiler Fitter")).to_have_count(1)

        # It collapses again with the same button, and the address can ask for it open.
        # Moving a card out of the expanded Closed column leaves it expanded (the page address
        # said so), so the user does not have to open it again.
        toggle = page.get_by_role("button", name=re.compile(r"^Closed: 2$"))
        expect(toggle).to_have_attribute("aria-expanded", "true")
        expect(page.locator("#board-closed-cards li.board-card")).to_have_count(2)
        press_by_keyboard(page, toggle)
        expect(toggle).to_have_attribute("aria-expanded", "false")
        expect(page.locator("#board-closed-cards")).to_have_count(0)
        open_app(page, running_app, "/board?closed=1")
        expect(page.locator("#board-closed-cards li.board-card")).to_have_count(2)


# --- table view ---------------------------------------------------------------------------------


def test_board_table_view_sorts_by_keyboard(running_app, browser):
    with desk(running_app) as seed:
        seed.plan()
    with new_page(browser, DESKTOP) as page:
        open_board(page, running_app)
        toggle_nav = page.get_by_role("navigation", name="Board view")
        press_by_keyboard(page, toggle_nav.get_by_role("link", name="Table", exact=True))
        table = page.locator("#board-table")
        expect(table).to_be_visible()
        expect(page).to_have_url(re.compile(r"/board\?.*view=table"))
        expect(table.locator("caption")).not_to_be_empty()
        expect(toggle_nav.get_by_role("link", name="Table", exact=True)).to_have_attribute(
            "aria-current", "true"
        )
        expect(table.locator("tbody tr")).to_have_count(8)  # closed ones are listed too
        expect(table.get_by_text("Closed: rejected")).to_have_count(2)

        def link(column: str) -> Locator:
            return table.locator("th").filter(has_text=column).get_by_role("link")

        def first_column() -> list[str]:
            return [
                text.strip() for text in table.locator("tbody tr > *:first-child").all_inner_texts()
            ]

        press_by_keyboard(page, link("Company"))
        expect(table.locator("th").filter(has_text="Company")).to_have_attribute(
            "aria-sort", "ascending"
        )
        expect(link("Company")).to_be_focused()
        ascending = first_column()
        assert ascending == sorted(ascending, key=str.lower)
        assert ascending[0] == "alder Fictional Mills"  # case does not decide the order

        page.keyboard.press("Enter")
        expect(table.locator("th").filter(has_text="Company")).to_have_attribute(
            "aria-sort", "descending"
        )
        assert first_column() == sorted(ascending, key=str.lower, reverse=True)

        press_by_keyboard(page, link("Role"))
        expect(table.locator("th").filter(has_text="Role")).to_have_attribute(
            "aria-sort", "ascending"
        )
        expect(table.locator("th").filter(has_text="Company")).not_to_have_attribute(
            "aria-sort", "descending"
        )
        roles = [
            text.strip() for text in table.locator("tbody tr > *:nth-child(2)").all_inner_texts()
        ]
        assert roles == sorted(roles, key=str.lower)

        # The address keeps the view and the sort, so a reload shows the same table.
        expect(page).to_have_url(re.compile(r"sort=role"))
        page.reload()
        expect(page.locator("#board-table th").filter(has_text="Role")).to_have_attribute(
            "aria-sort", "ascending"
        )
        assert_no_serious_violations(page)

        # And back to the columns, still by keyboard.
        press_by_keyboard(page, toggle_nav.get_by_role("link", name="Board", exact=True))
        expect(page.locator("#board-columns")).to_be_visible()
        expect(page.locator("#board-table")).to_have_count(0)


# --- phone and axe ------------------------------------------------------------------------------


def test_board_phone_390x844_stacked_no_sideways_scroll(running_app, browser):
    with desk(running_app) as seed:
        seed.plan()
    with new_page(browser, PHONE) as page:
        open_board(page, running_app)
        columns = page.locator("#board-columns section.board-column")
        expect(columns.first).to_be_visible()
        count = columns.count()
        assert count >= 6  # five statuses and Closed
        boxes = [columns.nth(n).bounding_box() for n in range(count)]
        assert all(box is not None for box in boxes)
        for above, below in pairwise(boxes):
            assert above and below
            assert abs(above["x"] - below["x"]) <= 2, "columns share one left edge (stacked)"
            assert below["y"] >= above["y"] + above["height"] - 2, "each column is under the last"
            assert above["x"] + above["width"] <= PHONE["width"] + 1
        ok, scroll, inner = has_no_horizontal_scroll(page)
        assert ok, f"sideways scroll: page {scroll}px wide in a {inner}px window"
        every_list_has_an_instance(page)
        assert page.evaluate(
            "() => [...document.querySelectorAll('[data-sortable]')]"
            ".every((el) => window.Sortable.get(el).option('disabled') === true)"
        )

        # The menu is the way to move a card: reachable, big enough and inside the screen.
        one = page.locator("li.board-card").first
        button = one.get_by_role("button", name=MOVE)
        assert_target_size(button, "Move to…")
        open_menu(page, one)
        menu = one.get_by_role("menu").bounding_box()
        assert menu and menu["x"] >= 0 and menu["x"] + menu["width"] <= PHONE["width"] + 1
        page.keyboard.press("Escape")

        # The table scrolls inside its own region, not the page.
        press_by_keyboard(
            page,
            page.get_by_role("navigation", name="Board view").get_by_role(
                "link", name="Table", exact=True
            ),
        )
        expect(page.locator("#board-table")).to_be_visible()
        ok, scroll, inner = has_no_horizontal_scroll(page)
        assert ok, f"sideways scroll in the table view: page {scroll}px in a {inner}px window"
        region = page.evaluate(
            """() => {
                let node = document.getElementById('board-table').parentElement;
                while (node && node !== document.body) {
                    const overflow = getComputedStyle(node).overflowX;
                    if (overflow === 'auto' || overflow === 'scroll') {
                        return {tabindex: node.getAttribute('tabindex'),
                                role: node.getAttribute('role'),
                                scrolls: node.scrollWidth > node.clientWidth};
                    }
                    node = node.parentElement;
                }
                return null;
            }"""
        )
        assert region is not None, "the table sits in a scroll region of its own"
        if region["scrolls"]:
            assert region["tabindex"] == "0" and region["role"] == "region"


@pytest.mark.parametrize("viewport", [DESKTOP, PHONE])
@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_board_desktop_and_phone_axe(running_app, browser, viewport, scheme):
    with desk(running_app) as seed:
        seed.plan()
    with new_page(browser, viewport, color_scheme=scheme) as page:
        open_board(page, running_app)
        wait_for_settled_styles(page)
        assert_no_serious_violations(page)
        ok, scroll, inner = has_no_horizontal_scroll(page)
        assert ok, f"sideways scroll: page {scroll}px wide in a {inner}px window"

        one = page.locator("li.board-card").first
        open_menu(page, one)  # the open menu
        assert_no_serious_violations(page)
        page.keyboard.press("Escape")

        press_by_keyboard(page, page.get_by_role("button", name="Closed: 3", exact=True))  # Closed
        expect(page.locator("#board-closed-cards li.board-card")).to_have_count(3)
        assert_no_serious_violations(page)

        interviewing = page.locator("li.board-card").filter(has_text="Crop Analyst")
        choose_by_keyboard(page, interviewing, "Interviewing")  # the follow-up dialog
        expect(page.get_by_role("dialog", name="Add the interview date?")).to_be_visible()
        wait_for_settled_styles(page)
        assert_no_serious_violations(page)
        page.keyboard.press("Escape")
        expect(page.get_by_role("dialog")).to_have_count(0)

        open_app(page, running_app, "/board?view=table&sort=company")  # the table
        expect(page.locator("#board-table")).to_be_visible()
        assert_no_serious_violations(page)
        ok, scroll, inner = has_no_horizontal_scroll(page)
        assert ok, f"sideways scroll in the table view: page {scroll}px in a {inner}px window"


def test_empty_board_in_a_browser_links_to_the_inbox(running_app, browser):
    with new_page(browser, PHONE) as page:
        open_app(page, running_app, "/board")
        expect(page.get_by_role("heading", level=1, name="Board")).to_be_visible()
        link = page.locator("main").get_by_role("link", name=re.compile("inbox", re.IGNORECASE))
        expect(link).to_have_attribute("href", "/inbox")
        press_by_keyboard(page, link)
        expect(page).to_have_url(running_app.base_url + "/inbox")


# --- review additions ---------------------------------------------------------------------------


def test_sortable_instances_do_not_pile_up_across_moves(running_app, browser):
    with desk(running_app) as seed:
        first = seed.application("pile-job-1", "Seed Buyer", "cedar Fictional Works", "applied")
        seed.application("pile-job-2", "Crop Analyst", "Dune Fictional Ltd", "applied")
    with new_page(browser, DESKTOP) as page:
        open_board(page, running_app)
        every_list_has_an_instance(page)
        lists = page.locator("[data-sortable]").count()
        # Count what the page makes and gives up from here on (the first set was made already).
        page.evaluate(
            """() => {
                window.__sortable = { created: 0, destroyed: 0 };
                const create = window.Sortable.create;
                window.Sortable.create = function (...args) {
                    window.__sortable.created += 1;
                    return create.apply(this, args);
                };
                const destroy = window.Sortable.prototype.destroy;
                window.Sortable.prototype.destroy = function (...args) {
                    window.__sortable.destroyed += 1;
                    return destroy.apply(this, args);
                };
            }"""
        )
        for step, label in enumerate(("Screening", "Offer", "Applied"), start=1):
            choose_by_keyboard(page, card(page, first.id), label)
            wait_said(page, f"Moved to {label}.")
            page.wait_for_function(
                "([n]) => window.__sortable.created === n && window.__sortable.destroyed === n",
                arg=[lists * step],
            )
            every_list_has_an_instance(page)


def test_menu_item_chosen_with_the_mouse(running_app, browser):
    with desk(running_app) as seed:
        mover = seed.application("mouse-job-1", "Seed Buyer", "cedar Fictional Works", "applied")
    with new_page(browser, DESKTOP) as page:
        open_board(page, running_app)
        one = card(page, mover.id)
        one.get_by_role("button", name=MOVE).click()
        expect(one.get_by_role("menu")).to_be_visible()
        one.get_by_role("menuitem", name="Screening", exact=True).click()
        wait_said(page, "Moved to Screening.")
        assert status_of(running_app, mover.id) == "screening"
        assert history(running_app, mover.id)[-1] == ("applied", "screening", "menu")
        # A press elsewhere closes an open menu without moving anything.
        other = card(page, mover.id)
        other.get_by_role("button", name=MOVE).click()
        expect(other.get_by_role("menu")).to_be_visible()
        page.locator("#board-heading").click()
        expect(other.get_by_role("menu")).not_to_be_visible()
        assert status_of(running_app, mover.id) == "screening"


@pytest.mark.parametrize("viewport", [DESKTOP, {"width": 820, "height": 900}])
def test_closed_row_is_in_sight_below_the_columns(running_app, browser, viewport):
    with desk(running_app) as seed:
        seed.plan()
    with new_page(browser, viewport) as page:
        open_board(page, running_app)
        toggle = page.get_by_role("button", name="Closed: 3", exact=True)
        expect(toggle).to_be_visible()
        box = toggle.bounding_box()
        columns = page.locator("#board-columns .board-scroll").bounding_box()
        assert box and columns
        assert box["x"] >= 0 and box["x"] + box["width"] <= viewport["width"] + 1
        assert box["y"] >= columns["y"] + columns["height"] - 2, "Closed is a row under the columns"
        ok, scroll, inner = has_no_horizontal_scroll(page)
        assert ok, f"sideways scroll: page {scroll}px wide in a {inner}px window"
        # The columns scroll in a region of their own that the keyboard can reach.
        scroller = page.get_by_role("region", name="Board columns")
        expect(scroller).to_have_attribute("tabindex", "0")

        toggle.click()
        shown = page.locator("#board-closed-cards li.board-card")
        expect(shown).to_have_count(3)
        for n in range(3):
            one = shown.nth(n).bounding_box()
            assert one and one["x"] >= 0 and one["x"] + one["width"] <= viewport["width"] + 1
            assert one["width"] >= 180, f"a closed card is {one['width']:.0f}px wide"
        assert_no_serious_violations(page)


def test_follow_up_dialog_says_where_the_card_moved(running_app, browser):
    with desk(running_app) as seed:
        mover = seed.application("words-job-1", "Seed Buyer", "cedar Fictional Works", "applied")
    with new_page(browser, DESKTOP) as page:
        open_board(page, running_app)
        choose_by_keyboard(page, card(page, mover.id), "Interviewing")
        dialog = page.get_by_role("dialog", name="Add the interview date?")
        expect(dialog).to_be_visible()
        expect(dialog).to_contain_text("Moved to Interviewing.")
        page.keyboard.press("Escape")
