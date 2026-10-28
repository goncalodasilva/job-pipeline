"""M6.10 the Submit step (step 5 of the Tailoring tab): the checklist, the exported files and their
downloads, the posting link and "I've submitted it - mark Applied" through the existing status move
(AC-M6.6, AC-M6.7; detailed design "Export and freeze" and "Application workspace" step 5;
ADR-106 item 10; spec 2.9 rules 2, 12 and 13).

Assumed interface: the docstring of ``tests/export_pages.py`` ("Step 5 (Submit)"). The service half
is in ``test_exports.py`` and ``test_freeze_on_applied.py``. Real SQLite in a temp folder, the fake
provider behind the spy, a frozen clock, no network, no fixed waits.
"""

import html

import pytest

from app.domain.errors import ApplicationNotFound
from tests.board_pages import elements
from tests.export_pages import (
    APPLIED_ANNOUNCE,
    BUTTON_NAMES,
    EXPORT_ANNOUNCE,
    MARK_APPLIED,
    POSTING_URL,
    Exporting,
    applied_url,
    checklist,
    done_flags,
    download_buttons,
    download_url,
    export_url,
    exporting_app,
    exports_module,
    file_items,
    step5_page_url,
    step5_url,
    submit_step,
)
from tests.integration.test_board import snapshot
from tests.tailoring_ai_inputs import FACT_3
from tests.tailoring_pages import (
    TITLES,
    announced,
    by_id,
    has_id,
    hidden_value,
    inside,
    open_step,
    page_url,
    said_text,
    stepper,
)

pytestmark = pytest.mark.integration

ROLE_LABEL = "Operations Analyst at Example Widgets Ltd"


@pytest.fixture
def d(tmp_path):
    with exporting_app(tmp_path) as desk:
        yield desk


def both(d: Exporting, aid: str) -> list[str]:
    """The fragment (token) and the token-free page: the step reads the same on both."""
    return [d.step5(aid).text, d.page5(aid).text, d.fragment(aid, 5).text]


def words(page: str, name: str) -> str:
    return said_text(checklist(page)[name].text)


def links(page: str, name: str) -> list[str]:
    box = checklist(page)[name]
    found = inside(page, box, lambda e: e.tag == "a")
    return [html.unescape(str(e.attrs["href"])) for e in found]


def autofocus(page: str) -> list[str]:
    return [str(e.attrs.get("id", e.tag)) for e in elements(page) if "data-autofocus" in e.attrs]


# --- the step is real now ------------------------------------------------------------------------


def test_submit_step_replaces_the_step_five_placeholder_and_holds_no_personal_detail(d: Exporting):
    app = d.export_ready()
    d.export_ok(app.id)
    before, requests = snapshot(d.r), len(d.spy.requests)

    for shown in both(d, app.id):
        assert open_step(shown) == 5
        text = said_text(shown)
        assert "later step" not in text.lower() and "Coming later" not in text
        assert f"Step 5: {TITLES[5]}" in text
        assert by_id(shown, "submit-heading").text == "Submit"
        assert by_id(shown, "submit-heading").attrs["tabindex"] == "-1"
        assert submit_step(shown).attrs["data-applied"] == "false"
        assert "Coming later" not in stepper(shown)[5].text
        # Rule 13: the token-free page shows no contact detail, and no document text.
        for private in ("Zara", "Quillfeather", "example.org", "+44 20 7946", "Quillhaven"):
            assert private not in shown
    # Reading, however often, writes nothing and starts nothing.
    assert snapshot(d.r) == before and len(d.spy.requests) == requests
    assert d.r.client.head(step5_page_url(app.id)).status_code == 200
    # Steps 3 and 4 have their own steps and no Submit parts.
    assert not has_id(d.step3(app.id).text, "submit-step")
    assert not has_id(d.step4(app.id).text, "submit-step")


# --- the checklist -------------------------------------------------------------------------------


def test_submit_checklist_follows_the_work(d: Exporting):
    app = d.cv_ready("checklist-job", template=False)
    d.put_template("PT")
    aid = app.id

    shown = d.step5(aid).text
    assert list(checklist(shown)) == ["details", "cv", "letter", "files", "pages"]
    assert done_flags(shown) == {
        "details": "false",
        "cv": "false",
        "letter": "true",  # no letter: skipped
        "files": "false",
        "pages": "false",
    }
    assert "Your details" in words(shown, "details")
    assert any(link.startswith("/settings") for link in links(shown, "details"))
    assert "CV not approved yet" in words(shown, "cv")
    assert links(shown, "cv") == [page_url(aid, 3)]
    assert "No cover letter" in words(shown, "letter")

    d.save_contact()
    assert done_flags(d.step5(aid).text)["details"] == "true"

    d.generate(aid, "ok")
    shown = d.step5(aid).text
    assert done_flags(shown)["cv"] == "false" and "not approved" in words(shown, "cv")
    d.service.approve(aid, d.current_version_id(aid))
    shown = d.step5(aid).text
    assert done_flags(shown)["cv"] == "true" and "CV approved" in words(shown, "cv")
    assert links(shown, "cv") == []

    d.draft(aid, "ok")  # a letter exists now, and it is only a draft
    shown = d.step5(aid).text
    assert done_flags(shown)["letter"] == "false"
    assert "Cover letter not approved yet" in words(shown, "letter")
    assert links(shown, "letter") == [page_url(aid, 4)]
    d.letters.approve(aid, d.current_letter_id(aid))
    shown = d.step5(aid).text
    assert done_flags(shown)["letter"] == "true"
    assert "Cover letter approved" in words(shown, "letter")
    assert "No cover letter" not in words(shown, "letter")
    assert done_flags(shown)["files"] == "false" and "Files not exported yet" in words(
        shown, "files"
    )

    d.export_ok(aid)
    shown = d.step5(aid).text
    assert done_flags(shown) == dict.fromkeys(("details", "cv", "letter", "files", "pages"), "true")
    assert "Files exported" in words(shown, "files")
    pages = d.export_of(aid, "cv", "pdf")["pages"]
    assert str(pages) in words(shown, "pages") and "2" in words(shown, "pages")
    assert done_flags(d.page5(aid).text) == done_flags(shown)

    # A change after the export means the files are not those of the current version any more.
    version = d.current_version_id(aid)
    unit = d.units(version, removed=False)[1]
    d.service.edit_unit(aid, version, unit["id"], "Planned 60 delivery routes, mostly at night.")
    shown = d.step5(aid).text
    flags = done_flags(shown)
    assert flags["cv"] == "false" and flags["files"] == "false"


def test_submit_pages_check_against_the_template(d: Exporting):
    # A template that allows one page and a CV that needs more.
    app = d.export_ready("long-cv-job", cv_approved=False, letter=False, max_pages=1)
    version = d.current_version_id(app.id)
    for number in range(70):
        if number % 2:
            added = d.service.add_unit(app.id, version, "languages", FACT_3, fact_ids=("F3@1",))
        else:
            added = d.service.add_unit(
                app.id,
                version,
                "experience",
                "Planned 60 delivery routes a week with a team of 3.",
                role=ROLE_LABEL,
                fact_ids=("F1@1",),
            )
        version = added.version_id
    d.service.approve(app.id, version)

    before_export = d.step5(app.id).text
    assert done_flags(before_export)["pages"] == "false"
    assert "export" in words(before_export, "pages").lower()  # known only after exporting

    d.export_ok(app.id)

    pages = d.export_of(app.id, "cv", "pdf")["pages"]
    assert pages >= 2, "the test CV must be longer than one page"
    shown = d.step5(app.id).text
    assert done_flags(shown)["pages"] == "false"
    assert str(pages) in words(shown, "pages") and "1" in words(shown, "pages")

    # A short CV for the same one-page template fits.
    roomy = d.export_ready("short-cv-job", letter=False)
    d.export_ok(roomy.id)
    shown = d.step5(roomy.id).text
    assert done_flags(shown)["pages"] == "true"


# --- files and downloads -------------------------------------------------------------------------


def test_submit_step_lists_files_with_download_buttons(d: Exporting):
    app = d.export_ready()
    shown = d.step5(app.id).text
    assert has_id(shown, "submit-no-files") and not has_id(shown, "submit-files")

    d.export_ok(app.id)

    shown = d.step5(app.id).text
    assert not has_id(shown, "submit-no-files")
    assert len(file_items(shown)) == 4
    found = download_buttons(shown)
    assert set(found) == set(BUTTON_NAMES)
    for (document, fmt), (text, path) in found.items():
        row = d.export_of(app.id, document, fmt)
        assert text == BUTTON_NAMES[(document, fmt)]
        assert path == download_url(app.id, row["id"])
        assert row["stored_name"] in said_text(
            next(
                i
                for i in file_items(shown)
                if i.attrs["data-format"] == fmt and i.attrs["data-document"] == document
            ).text
        )
        assert d.download(app.id, row["id"]).content == d.file_bytes(row)
    # The page (no token) lists the same buttons; the file itself still needs the token.
    assert download_buttons(d.page5(app.id).text) == found
    assert d.r.client.get(next(iter(found.values()))[1]).status_code == 403


# --- the posting link ----------------------------------------------------------------------------


def test_submit_step_shows_the_posting_link_or_says_there_is_none(d: Exporting):
    app = d.export_ready()
    for shown in both(d, app.id):
        assert has_id(shown, "submit-no-posting") and not has_id(shown, "submit-posting-link")

    d.add_posting_link("export-job", POSTING_URL + "?ref=<script>alert(1)</script>&a=b")

    for shown in both(d, app.id):
        link = by_id(shown, "submit-posting-link")
        assert html.unescape(str(link.attrs["href"])).startswith(POSTING_URL)
        assert "noopener" in str(link.attrs.get("rel", ""))
        assert "<script>alert(1)" not in shown  # escaped, never markup
        assert not has_id(shown, "submit-no-posting")


# --- marking Applied -----------------------------------------------------------------------------


def test_submit_mark_applied_uses_the_status_move(d: Exporting):
    app = d.export_ready()
    d.export_ok(app.id)
    shown = d.step5(app.id).text
    form = by_id(shown, "submit-applied-form")
    assert html.unescape(str(form.attrs["action"])) == applied_url(app.id)
    controls = inside(shown, form, lambda e: e.tag in ("input", "select", "textarea"))
    assert {str(e.attrs.get("name")) for e in controls} == {"version"}
    version = hidden_value(shown, form, "version")
    assert version == str(d.application_row(app.id)["version"])
    button = by_id(shown, "submit-mark-applied")
    assert html.unescape(button.text.strip()) == MARK_APPLIED
    assert button.attrs.get("aria-disabled") != "true" and "disabled" not in button.attrs

    answer = d.post_fields(applied_url(app.id), {"version": version})

    assert answer.status_code == 200
    assert APPLIED_ANNOUNCE in announced(answer)
    assert autofocus(answer.text) == ["submit-heading"]
    row = d.application_row(app.id)
    assert row["status_key"] == "applied" and row["applied_at"] is not None
    history = d.rows(
        "SELECT from_status, to_status, source FROM status_history WHERE application_id=? "
        "ORDER BY changed_at, id",
        (app.id,),
    )
    assert [(h["from_status"], h["to_status"]) for h in history] == [
        (None, "preparing"),
        ("preparing", "applied"),
    ]
    assert history[-1]["source"] == "drawer"  # an existing source: the move is the board's own
    for after in (answer.text, d.step5(app.id).text, d.page5(app.id).text):
        assert submit_step(after).attrs["data-applied"] == "true"
        assert has_id(after, "submit-applied") and "Applied" in said_text(
            by_id(after, "submit-applied").text
        )
        assert not has_id(after, "submit-applied-form") and not has_id(after, "submit-mark-applied")
        assert "Done" in stepper(after)[5].text
        assert len(download_buttons(after)) == 4  # the files stay downloadable

    # A second press is refused and writes nothing.
    before = snapshot(d.r)
    again = d.post_fields(applied_url(app.id), {"version": str(row["version"])})
    assert again.status_code == 409 and has_id(again.text, "submit-error")
    assert snapshot(d.r) == before


def test_submit_mark_applied_stale_missing_or_extra_version_writes_nothing(d: Exporting):
    app = d.export_ready()
    version = str(d.application_row(app.id)["version"])
    before = snapshot(d.r)

    stale = d.post_fields(applied_url(app.id), {"version": str(int(version) + 7)})
    assert stale.status_code == 409 and has_id(stale.text, "submit-conflict")
    assert "reload" in said_text(stale.text).lower()
    for bad in (
        {},
        {"version": "bad.id"},
        {"version": ""},
        {"version": version, "status": "offer"},
    ):
        assert d.post_fields(applied_url(app.id), bad).status_code == 400, bad
    assert d.post_fields(applied_url("missing-id"), {"version": "1"}).status_code == 404
    assert d.post_fields(applied_url("bad.id"), {"version": "1"}).status_code == 404
    assert snapshot(d.r) == before and d.submissions() == []


def test_submit_mark_applied_is_not_blocked_by_an_incomplete_checklist(d: Exporting):
    app = d.cv_ready("nothing-done-job", template=False)  # no details, no CV, no files
    shown = d.step5(app.id).text
    assert done_flags(shown)["cv"] == "false" and has_id(shown, "submit-mark-applied")

    answer = d.post_fields(
        applied_url(app.id), {"version": str(d.application_row(app.id)["version"])}
    )

    assert answer.status_code == 200
    assert d.application_row(app.id)["status_key"] == "applied"
    (submission,) = d.submissions()
    assert (submission["cv_version_id"], submission["letter_version_id"]) == (None, None)


# --- the export button ---------------------------------------------------------------------------


def test_submit_export_button_is_blocked_with_the_reason_and_the_server_agrees(d: Exporting):
    exports = exports_module()
    nameless = d.export_ready("nameless-job", details=False)
    shown = d.step5(nameless.id).text
    button = by_id(shown, "submit-export")
    assert button.attrs["aria-disabled"] == "true"
    assert button.attrs["aria-describedby"] == "submit-export-reason"
    assert said_text(by_id(shown, "submit-export-reason").text) == exports.NO_NAME
    assert d.export(nameless.id).status_code == 409

    d.save_contact()
    draft = d.export_ready("draft-job", cv_approved=False, letter=False)
    shown = d.step5(draft.id).text
    assert by_id(shown, "submit-export").attrs["aria-disabled"] == "true"
    assert said_text(by_id(shown, "submit-export-reason").text) == exports.NOT_APPROVED

    ready = d.export_ready("ready-job")
    shown = d.step5(ready.id).text
    assert "aria-disabled" not in by_id(shown, "submit-export").attrs
    assert by_id(shown, "submit-export").text.strip() == "Export files"
    form = by_id(shown, "submit-export-form")
    assert html.unescape(str(form.attrs["action"])) == export_url(ready.id)


def test_submit_export_answer_is_announced_focused_and_asks_no_provider(d: Exporting):
    app = d.export_ready()
    requests = len(d.spy.requests)

    answer = d.export(app.id)

    assert answer.status_code == 200
    assert announced(answer) == [EXPORT_ANNOUNCE]
    assert autofocus(answer.text) == ["submit-heading"]
    assert len(download_buttons(answer.text)) == 4
    assert open_step(answer.text) == 5 and has_id(answer.text, "tailoring")
    assert len(d.spy.requests) == requests


# --- tokens, origin, ids -------------------------------------------------------------------------


def test_submit_step_fragments_need_token_posts_need_origin_and_ids_are_checked(d: Exporting):
    app = d.export_ready()
    d.export_ok(app.id)
    row = d.exports(app.id)[0]
    anonymous = d.r.client
    htmx = d.r.run.run.htmx()
    run = d.r.run.run
    before = snapshot(d.r)

    # Reads: the fragment and the download need the token; the page does not and writes nothing.
    assert anonymous.get(step5_url(app.id)).status_code == 403
    assert anonymous.get(step5_url(app.id), headers=htmx).status_code == 200
    assert anonymous.get(download_url(app.id, row["id"])).status_code == 403
    page = anonymous.get(step5_page_url(app.id))
    assert page.status_code == 200 and "<html" in page.text
    assert anonymous.get(step5_page_url(app.id), headers={"HX-Request": "true"}).status_code == 403
    assert anonymous.head(step5_page_url(app.id)).status_code == 200
    for aid in ("missing", "bad.id", "<script>"):
        for path in (step5_url(aid),):
            answer = anonymous.get(path, headers=htmx)
            assert answer.status_code == 404 and answer.text == ApplicationNotFound.message
        answer = anonymous.get(step5_page_url(aid))
        assert answer.status_code == 404 and answer.text == ApplicationNotFound.message
    assert snapshot(d.r) == before

    # Writes: the token and the origin are both needed; nothing changes without them.
    version = str(d.application_row(app.id)["version"])
    posts = ((export_url(app.id), {}), (applied_url(app.id), {"version": version}))
    for path, values in posts:
        assert anonymous.post(path, data=values).status_code == 403
        assert anonymous.post(path, data=values, headers=run.with_token()).status_code == 403
        wrong = {**htmx, "Origin": "https://attacker.example.test"}
        assert anonymous.post(path, data=values, headers=wrong).status_code == 403
        no_token = {"Origin": run.action()["origin"]}
        assert anonymous.post(path, data=values, headers=no_token).status_code == 403
    assert snapshot(d.r) == before and d.submissions() == []
    assert d.application_row(app.id)["applied_at"] is None
