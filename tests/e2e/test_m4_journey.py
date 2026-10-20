"""M4.11: the whole M4 journey in a browser with the real CLI, the fake AI provider and the fake
job page fetcher, by keyboard, at desktop and phone width.

Add one job by link (``*.example.test`` is served by the fake fetcher the app builds while
fakes are allowed) and one by pasted text; both are summarized by the fake provider and carry
its "Practice data" label. Then triage them by keyboard (J and K, snooze, remove with the
"other" reason and undo it, open the drawer, correct a field), follow the company link to the
company page, edit the company notes, save a filtered view, mark the job to apply, and check
each key screen with axe and for sideways scrolling. Nothing here uses a real provider, the
network or real data; every job and company is fictional.
"""

import pytest
from playwright.sync_api import Browser, expect

from app.modules.storage import Database
from tests.e2e.axe import assert_no_serious_violations
from tests.e2e.conftest import DESKTOP, PHONE
from tests.e2e.test_facts_page import RECORD_ANNOUNCEMENTS, press_by_keyboard, tab_until, wait_said
from tests.e2e.test_shell import has_no_horizontal_scroll, open_app
from tests.running_app import RunningApp
from tests.waiting import wait_until

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.parametrize("running_app", ["complete"], indirect=True),
]

LINK = "https://jobs.lumenboard.example.test/postings/widget-engineer-1001"
PASTED = (
    "Fictional canal gardener wanted at Willow Quay Fictional. Tend imaginary flowers, water "
    "the plants every morning and plan seasonal planting with a small friendly team."
)
SUMMARY = "Operate a fictional deployment pipeline."  # the fake provider's fixed answer
COMPANY = "Harbor Kite Fictional"  # the fake provider's fixed company
LABEL = "Practice data"
NEW_TITLE = "Fictional principal gardener"
REASON = "The commute is too long for a fictional week."
COMPANY_NOTE = "Ask about the fictional greenhouse rota."


def query(app: RunningApp, sql: str, params: tuple[object, ...] = ()) -> list[tuple[object, ...]]:
    with Database.open(app.data_dir / "app.db") as db:
        return [tuple(row) for row in db.connection.execute(sql, params).fetchall()]


def job_by_source_kind(app: RunningApp, kind: str) -> str:
    found = query(app, "SELECT job_id FROM job_sources WHERE kind=?", (kind,))
    assert len(found) == 1, (kind, found)
    return str(found[0][0])


def state_of(app: RunningApp, job_id: str) -> str:
    return str(query(app, "SELECT triage_state FROM jobs WHERE id=?", (job_id,))[0][0])


def summaries(app: RunningApp) -> int:
    return int(query(app, "SELECT count(*) FROM job_summaries")[0][0])


def shown_job(page) -> str:
    job_id = page.locator("#inbox-card").get_attribute("data-job-id")
    assert job_id
    return job_id


def clean_screen(page) -> None:
    assert_no_serious_violations(page)
    assert has_no_horizontal_scroll(page)[0]


@pytest.mark.parametrize("viewport", [DESKTOP, PHONE], ids=["desktop", "phone"])
def test_m4_journey_add_triage_detail_company(
    running_app: RunningApp, browser: Browser, viewport: dict[str, int]
) -> None:
    app = running_app
    with browser.new_context(viewport=viewport) as context:
        page = context.new_page()
        page.add_init_script(RECORD_ANNOUNCEMENTS)
        open_app(page, app, "/inbox")
        clean_screen(page)  # the empty inbox

        # --- add a job by link (fake fetcher) and see it summarized by the fake provider ---
        press_by_keyboard(page, page.get_by_role("button", name="+ Add job", exact=True))
        tab_until(page, page.locator("#job-url"))
        page.keyboard.type(LINK)
        press_by_keyboard(page, page.get_by_role("button", name="Add job", exact=True))
        intake = page.locator("#job-intake")
        expect(intake).to_contain_text("Job saved.")
        wait_said(page, "Job saved.")
        wait_until(lambda: summaries(app) == 1, timeout=20, message="the link was summarized")
        expect(intake.locator('[id^="job-summary-"]').first).to_contain_text(
            "Practice data (fake provider)"
        )
        fetched = job_by_source_kind(app, "fetched")
        assert query(app, "SELECT kind FROM job_sources WHERE job_id=?", (fetched,)) == [
            ("fetched",)
        ]

        # --- add a job by pasted text ---
        tab_until(page, page.locator("#job-text"))
        page.keyboard.type(PASTED)
        press_by_keyboard(page, page.get_by_role("button", name="Add job", exact=True))
        expect(intake).to_contain_text("Job saved.")
        wait_until(lambda: summaries(app) == 2, timeout=20, message="the paste was summarized")
        pasted = job_by_source_kind(app, "pasted")
        assert pasted != fetched
        clean_screen(page)
        page.keyboard.press("Escape")
        # The duplicate is found when the second summary lands, after the queue was last drawn
        # (nothing redraws it then), so read the inbox again to see the settled queue.
        open_app(page, app, "/inbox")

        # --- summarized and labelled as practice data. The fake answers the same title and
        # --- company for every posting, so the second shows as another source of the first.
        links = page.locator("#inbox-queue").locator("a[data-job-id]")
        expect(links).to_have_count(1)
        card = page.locator("#inbox-card")
        expect(card).to_contain_text(SUMMARY)
        expect(card).to_contain_text(LABEL)
        expect(card.get_by_role("link", name=COMPANY, exact=True)).to_be_visible()
        clean_screen(page)

        # --- the user says it is not a duplicate, from the drawer's sources ---
        tab_until(page, page.locator("#inbox-card-heading"))
        page.keyboard.press("Enter")
        drawer = page.get_by_role("dialog", name="Job details", exact=True)
        expect(drawer).to_be_visible()
        press_by_keyboard(page, drawer.get_by_role("button", name="Not a duplicate", exact=True))
        expect(drawer.locator("#job-sources")).to_contain_text("Job restored as its own card.")
        page.keyboard.press("Escape")
        expect(drawer).not_to_be_visible()
        expect(links).to_have_count(2)
        assert {fetched, pasted} == {link.get_attribute("data-job-id") for link in links.all()}
        clean_screen(page)

        # --- a filtered view, saved and opened again by keyboard ---
        open_app(page, app, "/inbox?work_mode=remote&group_by=company")
        saved = page.locator("#saved-views")
        press_by_keyboard(page, saved.locator("summary", has_text="Saved views"))
        name = page.get_by_label("Name this view")
        tab_until(page, name)
        page.keyboard.type("Remote by company")
        press_by_keyboard(page, page.get_by_role("button", name="Save view", exact=True))
        expect(saved).to_contain_text("View saved.")
        clean_screen(page)
        open_app(page, app, "/inbox")
        press_by_keyboard(page, saved.locator("summary", has_text="Saved views"))
        press_by_keyboard(page, saved.get_by_role("link", name="Remote by company"))
        page.wait_for_url("**work_mode=remote**")
        expect(page.locator(".queue-group").first).to_be_visible()
        open_app(page, app, "/inbox")

        # --- triage by keyboard: J and K, snooze, remove with a reason and undo ---
        expect(page.locator("#inbox-card")).to_be_visible()
        first = shown_job(page)
        wait_until(lambda: state_of(app, first) == "seen", timeout=10, message="shown card seen")
        tab_until(page, page.locator("#inbox-card-heading"))
        page.keyboard.press("j")
        expect(page.locator("#inbox-card")).not_to_have_attribute("data-job-id", first)
        second = shown_job(page)
        wait_until(lambda: state_of(app, second) == "seen", timeout=10, message="second seen")
        page.keyboard.press("k")
        expect(page.locator("#inbox-card")).to_have_attribute("data-job-id", first)

        menu = page.locator("#inbox-card").locator("summary", has_text="Card menu")
        press_by_keyboard(page, menu)
        press_by_keyboard(
            page,
            page.locator("#inbox-card").get_by_role("button", name="Tomorrow", exact=True),
        )
        expect(page.locator("#inbox-undo")).to_contain_text("Snoozed until")
        assert state_of(app, first) == "snoozed"
        expect(page.locator("#inbox-card")).to_have_attribute("data-job-id", second)

        tab_until(page, page.locator("#inbox-card-heading"))
        page.keyboard.press("r")
        dialog = page.get_by_role("dialog", name="Remove job", exact=True)
        expect(dialog).to_be_visible()
        press_by_keyboard(page, dialog.get_by_role("button", name="Other", exact=True))
        reason = dialog.get_by_label("Reason (optional)")
        expect(reason).to_be_focused()
        page.keyboard.type(REASON)
        press_by_keyboard(page, dialog.get_by_role("button", name="Remove job", exact=True))
        expect(page.locator("#inbox-undo")).to_contain_text("Removed. Undo available.")
        assert state_of(app, second) == "removed"
        press_by_keyboard(
            page, page.locator("#inbox-undo").get_by_role("button", name="Undo", exact=True)
        )
        expect(page.locator("#inbox-card")).to_have_attribute("data-job-id", second)
        assert state_of(app, second) == "seen"
        clean_screen(page)

        # --- the drawer: correct a field, then on to the company ---
        tab_until(page, page.locator("#inbox-card-heading"))
        page.keyboard.press("Enter")
        drawer = page.get_by_role("dialog", name="Job details", exact=True)
        expect(drawer).to_be_visible()
        expect(drawer.get_by_role("button", name="Close", exact=True)).to_be_focused()
        clean_screen(page)
        title = drawer.get_by_label("Title", exact=True)
        tab_until(page, title)
        page.keyboard.press("ControlOrMeta+A")
        page.keyboard.type(NEW_TITLE)
        press_by_keyboard(page, drawer.get_by_role("button", name="Save fields and notes"))
        expect(drawer.locator("#job-edit")).to_contain_text("Corrected by you")
        assert query(app, "SELECT title FROM jobs WHERE id=?", (second,)) == [(NEW_TITLE,)]
        clean_screen(page)
        company = drawer.get_by_role("link", name=COMPANY, exact=True)
        press_by_keyboard(page, company)
        page.wait_for_url("**/companies/**")
        expect(page.get_by_role("heading", level=1, name=COMPANY)).to_be_visible()
        roles = page.locator("#company-roles")
        expect(roles).to_contain_text("Seen")
        expect(roles).to_contain_text("Snoozed")
        clean_screen(page)

        # --- company notes ---
        notes = page.get_by_label("Notes", exact=True)
        tab_until(page, notes)
        page.keyboard.type(COMPANY_NOTE)
        press_by_keyboard(page, page.get_by_role("button", name="Save company", exact=True))
        expect(page.locator("#company-edit")).to_contain_text("Saved.")
        assert query(app, "SELECT notes FROM companies") == [(COMPANY_NOTE,)]
        clean_screen(page)
        page.reload()
        expect(page.get_by_label("Notes", exact=True)).to_have_value(COMPANY_NOTE)

        # --- back in the inbox: the corrected job, Apply by keyboard ---
        open_app(page, app, f"/inbox?selected={second}")
        expect(page.locator("#inbox-card-heading")).to_have_text(NEW_TITLE)
        tab_until(page, page.locator("#inbox-card-heading"))
        page.keyboard.press("a")
        expect(page.locator("#inbox-undo")).to_contain_text("Marked to apply.")
        assert state_of(app, second) == "to_apply"
        assert state_of(app, first) == "snoozed"
        clean_screen(page)
