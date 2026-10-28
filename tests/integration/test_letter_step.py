"""M6.9 the cover letter step (step 4 of the Tailoring tab) as the user reads and drives it: facts
used per paragraph, the code-owned frame (greeting, opening line, closing, name), the optional
letter, flags and approval, tone and length, announcements and focus, and the token, origin and
version rules of its routes (AC-M6.1 to AC-M6.4 in the UI; detailed design "Application workspace"
step 4 and "Editing rules"; spec 2.9 rules 2, 12, 13).

Assumed interface: see the docstring of ``tests/letter_pages.py`` (routes under
``/applications/{id}/tailoring/4/...``, the ``#letter-step`` markup, the announcement strings). The
service half is in ``test_letter_generation.py``. Real SQLite in a temp folder, the fake provider
behind the spy, no network, no fixed waits.
"""

import html

import pytest

from app.domain.errors import ApplicationNotFound
from app.services.tailoring import NO_SUMMARY
from tests.board_pages import elements
from tests.cv_step_pages import (
    CHECK_NUMBER,
    HOSTILE,
    NOT_IN_FACT_BANK,
    PRACTICE,
    assert_escaped,
    buttons_in,
    described_by,
    fact_links,
    flags_of,
    has_line_form,
    one_part,
    parts,
)
from tests.integration.test_board import snapshot
from tests.letter_pages import (
    BAD_LENGTH,
    BAD_TONE,
    COMPANY,
    GLOBEX_P,
    INFLATED_P,
    KEPT_PARAGRAPH,
    LETTER_ANNOUNCE,
    NEW_CLAIM_P,
    OK_FACT_TEXTS,
    OK_PARAGRAPHS,
    P1,
    P3,
    TITLE,
    UNCITED_P,
    LetterDesk,
    frame_parts,
    letter_app,
    letter_autofocus,
    letter_page_url,
    letter_state,
    letter_step,
    letter_unit_url,
    letter_url,
    letter_version_id,
    options_of,
    paragraph_by_unit,
    paragraphs,
    selected_value,
)
from tests.tailoring_ai_inputs import DETAILS, personal_values
from tests.tailoring_pages import (
    announced,
    by_id,
    has_id,
    inside,
    open_step,
    page_url,
    said_text,
    stepper,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def d(tmp_path):
    with letter_app(tmp_path) as desk:
        yield desk


def both(d: LetterDesk, aid: str) -> list[str]:
    """The fragment (token) and the token-free page: the step reads the same on both."""
    return [d.step4(aid).text, d.page4(aid).text]


def states(page: str) -> dict[int, str]:
    return {number: item.text for number, item in stepper(page).items()}


# --- the step replaces the placeholder; the letter is optional -----------------------------------


def test_letter_step_replaces_the_step_four_placeholder_and_is_optional(d: LetterDesk):
    app = d.letter_ready()  # no CV, no template: the letter needs neither
    before, requests = snapshot(d.r), len(d.spy.requests)

    for shown in [*both(d, app.id), d.fragment(app.id, 4).text]:
        assert "later step" not in said_text(shown).lower()
        assert "Coming later" not in states(shown)[4] and "Not started" in states(shown)[4]
        assert letter_state(shown) == "none"
        assert by_id(shown, "letter-heading").text == "Cover letter"
        assert "optional" in by_id(shown, "letter-optional").text.lower()
        link = by_id(shown, "letter-next")
        assert link.text == "Continue without a letter"
        assert html.unescape(str(link.attrs["href"])) == page_url(app.id, 5)
        assert not has_id(shown, "letter-approve") and not paragraphs(shown)
        assert has_id(shown, "letter-generate-start")
        assert not has_id(shown, "cv-step")  # the CV's own step is on step 3
    assert open_step(d.page4(app.id).text) == 4
    assert "data-keep-edits" in by_id(d.step4(app.id).text, "tailoring").attrs
    # Reading, however often, writes nothing and starts nothing.
    assert snapshot(d.r) == before and len(d.spy.requests) == requests
    assert d.tasks_of("draft_cover_letter") == []
    # Step 5 and the CV step are unaffected: the letter's id is not on step 3.
    assert not has_id(d.step3(app.id).text, "letter-step")

    # Once the letter is approved the link says where to go next.
    d.draft_by_route(app.id, "ok")
    assert d.approve_letter_now(app.id).status_code == 200
    shown = d.step4(app.id).text
    assert by_id(shown, "letter-next").text == "Continue to Submit"
    assert "Done" in states(shown)[4] and letter_state(shown) == "approved"


def test_letter_step_not_ready_says_what_to_do_first(d: LetterDesk):
    app = d.letter_ready(summary=False)

    shown = d.step4(app.id).text

    assert by_id(shown, "letter-not-ready").text == NO_SUMMARY
    assert by_id(shown, "letter-not-ready-link").text == "Open job"
    assert not has_id(shown, "letter-generate-form")
    refused = d.post_letter(app.id, "generate", {"tone": "warm", "length": "medium"})
    assert refused.status_code == 409
    assert has_id(refused.text, "letter-error") and d.tasks_of("draft_cover_letter") == []
    # The step stays readable and still says it is optional.
    assert "optional" in by_id(shown, "letter-optional").text.lower()


# --- facts used per paragraph ---------------------------------------------------------------------


def test_letter_step_lists_the_facts_used_per_paragraph(d: LetterDesk):
    app = d.letter_ready()
    d.draft_by_route(app.id, "ok")
    fact_ids = [d.fact_id(number) for number in (1, 2, 3, 7)]
    expected = [
        [(f"/facts/{fact_ids[0]}/edit", OK_FACT_TEXTS[0][0])],
        [(f"/facts/{fact_ids[1]}/edit", OK_FACT_TEXTS[1][0])],
        [
            (f"/facts/{fact_ids[2]}/edit", OK_FACT_TEXTS[2][0]),
            (f"/facts/{fact_ids[3]}/edit", OK_FACT_TEXTS[2][1]),
        ],
    ]

    for shown in both(d, app.id):
        found = paragraphs(shown)
        assert [one_part(shown, p, "data-line-text").text for p in found] == OK_PARAGRAPHS
        assert [fact_links(shown, p) for p in found] == expected
        for paragraph, links in zip(found, expected, strict=True):
            # A native disclosure, so the keyboard reaches the facts without hovering.
            (disclosure,) = parts(shown, paragraph, "data-line-facts")
            assert disclosure.tag == "details"
            (name,) = inside(shown, disclosure, lambda e: e.tag == "summary")
            assert name.text == f"Facts used ({len(links)})"
        # A letter has no master version to compare with: no diff marks anywhere.
        assert not [e for e in elements(shown) if "data-diff-mark" in e.attrs]

    # A paragraph that cites nothing says so, and has no empty disclosure.
    assert d.add_paragraph(app.id, "I enjoy working with people.").status_code == 200
    shown = d.step4(app.id).text
    bare = paragraph_by_unit(shown, d.paragraph_id(app.id, "I enjoy working with people."))
    assert not parts(shown, bare, "data-line-facts")
    assert "No facts cited" in one_part(shown, bare, "data-no-facts").text


# --- greeting, opening line, closing and name are code's -----------------------------------------


def test_letter_frame_is_code_built_lazy_and_around_the_paragraphs(d: LetterDesk):
    app = d.letter_ready()
    nameless = d.letter_ready("no-name-job")
    bare = d.frame(nameless.id).text  # read before any name is saved
    d.save_details()
    d.draft_by_route(app.id, "ok")
    frame = d.letters.view(app.id).frame

    # Rule 13: the token-free page holds none of the personal details; it loads them lazily.
    page = d.page4(app.id).text
    placeholder = by_id(page, "letter-frame")
    assert placeholder.attrs["hx-get"] == letter_url(app.id, "frame")
    assert placeholder.attrs["hx-trigger"] == "load"
    for value in personal_values():
        assert value not in page, value
    assert frame_parts(page).get("name") is None

    for shown in (d.step4(app.id).text, d.frame(app.id).text):
        parts_by_name = frame_parts(shown)
        assert parts_by_name == {
            "greeting": frame.greeting,
            "opening": frame.opening,
            "closing": frame.closing,
            "name": DETAILS.name,
        }
        assert TITLE in parts_by_name["opening"] and COMPANY in parts_by_name["opening"]
        box = by_id(shown, "letter-frame")
        # Code-built, never editable: nothing to type in or press inside the frame.
        assert not inside(shown, box, lambda e: e.tag in ("form", "input", "textarea", "button"))
        links = inside(shown, box, lambda e: e.tag == "a")
        assert [e.attrs["href"] for e in links] == ["/settings"]

    # Without a saved name the sign-off says what to do instead of printing nothing.
    assert "name" not in frame_parts(bare) and has_id(bare, "letter-name-missing")
    assert frame_parts(bare)["opening"]

    # The frame is a protected read.
    assert d.r.client.get(letter_url(app.id, "frame")).status_code == 403
    for aid in ("missing", "bad.id"):
        answer = d.r.client.get(letter_url(aid, "frame"), headers=d.r.run.run.htmx())
        assert answer.status_code == 404 and answer.text == ApplicationNotFound.message


# --- tone and length ----------------------------------------------------------------------------


def test_letter_step_tone_and_length_selects_and_the_note(d: LetterDesk):
    app = d.letter_ready()

    shown = d.step4(app.id).text

    assert options_of(shown, "letter-tone") == [
        ("warm", "Warm"),
        ("formal", "Formal"),
        ("direct", "Direct"),
    ]
    assert options_of(shown, "letter-length") == [
        ("short", "Short"),
        ("medium", "Medium"),
        ("long", "Long"),
    ]
    assert selected_value(shown, "letter-tone") in ("warm", None)
    assert selected_value(shown, "letter-length") in ("medium", None)
    assert by_id(shown, "letter-tone").attrs["name"] == "tone"
    assert by_id(shown, "letter-length").attrs["name"] == "length"
    assert by_id(shown, "letter-note").attrs["name"] == "note"
    assert by_id(shown, "letter-generate-start").text == "Draft the letter"
    assert not has_id(shown, "letter-replace-all")  # nothing to replace yet

    d.draft_by_route(app.id, "ok", tone="formal", length="short", note="Keep it plain.")

    shown = d.step4(app.id).text
    assert selected_value(shown, "letter-tone") == "formal"
    assert selected_value(shown, "letter-length") == "short"
    assert by_id(shown, "letter-generate-start").text == "Regenerate"
    assert by_id(shown, "letter-replace-all").attrs["name"] == "replace_all"
    assert by_id(shown, "letter-confirm-replace").attrs["name"] == "confirm_replace"
    (version,) = d.letter_versions(app.id)
    assert (version["tone"], version["length"], version["note"]) == (
        "formal",
        "short",
        "Keep it plain.",
    )


def test_letter_step_refuses_a_bad_tone_or_length_and_queues_nothing(d: LetterDesk):
    app = d.letter_ready()
    for values, message, field in (
        ({"tone": "breezy", "length": "medium"}, BAD_TONE, "letter-tone"),
        ({"tone": "warm", "length": "epic"}, BAD_LENGTH, "letter-length"),
    ):
        refused = d.post_letter(app.id, "generate", values)
        assert refused.status_code == 422
        assert announced(refused) == [message]
        control = by_id(refused.text, field)
        assert control.attrs["aria-invalid"] == "true"
        assert described_by(refused.text, control) == message
        assert len(letter_autofocus(refused.text)) == 1
        assert "data-autofocus" in control.attrs
    assert d.tasks_of("draft_cover_letter") == [] and d.spy.requests == []


# --- flags inline, approval and its reason --------------------------------------------------------


def test_letter_step_flags_inline_and_approve_disabled_with_reason(d: LetterDesk):
    inflated = d.letter_ready()
    uncited = d.letter_ready("uncited-job")
    d.draft(inflated.id, "adversarial_inflated_number")
    d.draft(uncited.id, "adversarial_uncited_paragraph")

    shown = d.step4(inflated.id).text
    flagged = paragraph_by_unit(shown, d.paragraph_id(inflated.id, INFLATED_P))
    assert flagged.attrs["data-flagged"] == "true"
    assert flags_of(shown, flagged) == [
        ("needs_check", CHECK_NUMBER, "85%"),
        ("needs_check", CHECK_NUMBER, "14"),
    ]
    # The ordinary paragraph is untouched; the button is disabled and says why.
    clean = paragraph_by_unit(shown, d.paragraph_id(inflated.id, P1))
    assert "data-flagged" not in clean.attrs and flags_of(shown, clean) == []
    button = by_id(shown, "letter-approve")
    assert button.attrs["aria-disabled"] == "true"
    assert button.attrs["aria-describedby"] == "letter-approve-reason"
    assert by_id(shown, "letter-approve-reason").text == "1 paragraph needs your check."
    assert "Needs your check" in states(shown)[4] and letter_state(shown) == "draft"

    other = d.step4(uncited.id).text
    line = paragraph_by_unit(other, d.paragraph_id(uncited.id, UNCITED_P))
    assert flags_of(other, line) == [("unsupported", NOT_IN_FACT_BANK, UNCITED_P)]
    # Edit, remove and "this is true" are all offered on a flagged paragraph.
    assert {"Edit", "Remove", "This is true — save it as a fact"} <= set(buttons_in(other, line))
    assert has_line_form(other, line, "confirm")
    assert not has_line_form(
        other, paragraph_by_unit(other, d.paragraph_id(uncited.id, P1)), "confirm"
    )

    # Refused by the route too, with the service's own words.
    refused = d.approve_letter_now(inflated.id)
    assert refused.status_code == 409
    assert said_text(by_id(refused.text, "letter-error").text) == "1 paragraph needs your check."
    assert announced(refused) == ["1 paragraph needs your check."]
    assert d.version_row(d.current_letter_id(inflated.id))["status"] == "draft"

    # A clean letter approves, and the page says so.
    clean_app = d.letter_ready("clean-job")
    d.draft(clean_app.id, "ok")
    shown = d.step4(clean_app.id).text
    assert "aria-disabled" not in by_id(shown, "letter-approve").attrs
    assert not has_id(shown, "letter-approve-reason") and "Ready to approve" in states(shown)[4]


def test_letter_step_practice_label_and_versions(d: LetterDesk):
    app = d.letter_ready()
    d.draft_by_route(app.id, None)  # the app's own fake
    first = d.current_letter_id(app.id)
    shown = d.step4(app.id).text
    assert PRACTICE in by_id(shown, "letter-practice").text
    assert by_id(shown, "letter-status").text == "Draft, version 1. Not approved yet."
    assert (
        d.edit_paragraph(
            app.id, d.paragraph_id(app.id, d.units(first)[0]["text"]), KEPT_PARAGRAPH
        ).status_code
        == 200
    )
    assert PRACTICE in by_id(d.step4(app.id).text, "letter-practice").text  # survives edits

    # An older version is read-only: its words, no controls, a way back to the latest.
    old_fragment = d.step4(app.id, first).text
    assert letter_state(old_fragment) == "draft"
    assert by_id(old_fragment, "letter-step").attrs["data-readonly"] == "true"
    assert by_id(old_fragment, "letter-status").text == "Version 1 (read-only)."
    assert by_id(old_fragment, "letter-latest").text == "Back to the latest version"
    assert not [
        e for e in elements(old_fragment) if e.tag == "form" and e.within(letter_step(old_fragment))
    ]
    unknown = d.step4(app.id, "no-such-version")
    assert unknown.status_code == 404 and unknown.text == "That version wasn't found."
    # The page ignores an unknown version and shows the current one.
    page = d.page4(app.id, "no-such-version")
    assert page.status_code == 200 and "no-such-version" not in page.text
    assert letter_version_id(page.text) == d.current_letter_id(app.id)


# --- announcements, focus and the actions ---------------------------------------------------------


def test_letter_step_actions_announce_and_focus(d: LetterDesk):
    app = d.letter_ready()

    started = d.draft_by_route(app.id, "ok", tone="direct", length="long", note="Short and plain.")
    assert announced(started) == [LETTER_ANNOUNCE["started"]]
    assert [e.attrs.get("id") for e in letter_autofocus(started.text)] == ["letter-heading"]
    assert d.letter_versions(app.id)[0]["tone"] == "direct"
    v1 = d.current_letter_id(app.id)

    # Edit: saved, announced, focus on the changed paragraph.
    edited = d.edit_paragraph(app.id, d.paragraph_id(app.id, P1), KEPT_PARAGRAPH)
    assert edited.status_code == 200 and announced(edited) == [LETTER_ANNOUNCE["edit"]]
    v2 = d.current_letter_id(app.id)
    assert v2 != v1 and d.version_row(v2)["parent_version_id"] == v1
    assert (d.version_row(v2)["origin"], d.version_row(v2)["status"]) == ("user", "draft")
    (focus,) = letter_autofocus(edited.text)
    assert focus.tag == "li" and focus.attrs["data-unit-id"] == d.paragraph_id(
        app.id, KEPT_PARAGRAPH
    )
    assert focus.attrs["tabindex"] == "-1"
    assert letter_state(edited.text) == "draft" and letter_version_id(edited.text) == v2
    assert not has_id(edited.text, "letter-save-fact")  # no new claim: no question
    mine = paragraph_by_unit(edited.text, d.paragraph_id(app.id, KEPT_PARAGRAPH))
    assert mine.attrs["data-origin"] == "user" and "Edited by you" in mine.text

    # A new claim: the question, announced, with no autofocus but the dialog's own "Not now".
    claim = d.edit_paragraph(app.id, d.paragraph_id(app.id, KEPT_PARAGRAPH), NEW_CLAIM_P)
    assert claim.status_code == 200 and announced(claim) == [LETTER_ANNOUNCE["offer"]]
    dialog = by_id(claim.text, "letter-save-fact")
    assert dialog.attrs["role"] == "dialog" and dialog.attrs["aria-modal"] == "true"
    claims = inside(
        claim.text, by_id(claim.text, "letter-save-fact-claims"), lambda e: e.tag == "li"
    )
    assert [e.text for e in claims] == ["9", "$2m"]
    assert letter_autofocus(claim.text) == []
    (not_now,) = [
        e for e in inside(claim.text, dialog, lambda e: e.tag == "button") if e.text == "Not now"
    ]
    assert "autofocus" in not_now.attrs
    uid = d.paragraph_id(app.id, NEW_CLAIM_P)

    # "This is true" saves a fact and the paragraph cites it.
    facts = d.table_counts()["facts"]
    confirmed = d.confirm_paragraph(app.id, uid)
    assert confirmed.status_code == 200 and announced(confirmed) == [LETTER_ANNOUNCE["confirm"]]
    assert d.table_counts()["facts"] == facts + 1 and d.fact_row(9)["status"] == "confirmed"
    done = paragraph_by_unit(confirmed.text, d.paragraph_id(app.id, NEW_CLAIM_P))
    assert "data-flagged" not in done.attrs
    assert fact_links(confirmed.text, done) == [(f"/facts/{d.fact_id(9)}/edit", NEW_CLAIM_P)]
    assert [e.attrs["data-unit-id"] for e in letter_autofocus(confirmed.text)] == [
        done.attrs["data-unit-id"]
    ]

    # Add: the form has only the text; the paragraph is flagged until confirmed.
    shown = d.step4(app.id).text
    form = by_id(shown, "letter-add-form")
    names = {
        e.attrs.get("name")
        for e in inside(shown, form, lambda e: e.tag in ("input", "textarea", "select"))
    }
    assert names == {"version", "text"}
    added = d.add_paragraph(app.id, "I like tidy depots.")
    assert added.status_code == 200 and announced(added) == [LETTER_ANNOUNCE["add"]]
    line = paragraph_by_unit(added.text, d.paragraph_id(app.id, "I like tidy depots."))
    assert flags_of(added.text, line) == [("unsupported", NOT_IN_FACT_BANK, "I like tidy depots.")]

    # Remove: the paragraph leaves the page, stays as a tombstone, the heading takes focus.
    removed = d.remove_paragraph(app.id, d.paragraph_id(app.id, "I like tidy depots."))
    assert removed.status_code == 200 and announced(removed) == [LETTER_ANNOUNCE["remove"]]
    assert "I like tidy depots." not in said_text(by_id(removed.text, "letter-paragraphs").text)
    assert [e.attrs.get("id") for e in letter_autofocus(removed.text)] == ["letter-heading"]
    assert [
        r["removed"]
        for r in d.units(d.current_letter_id(app.id))
        if r["text"] == "I like tidy depots."
    ] == [1]

    # Approve, then an edit of the approved letter starts a new draft and says so.
    approved = d.approve_letter_now(app.id)
    assert approved.status_code == 200 and announced(approved) == [LETTER_ANNOUNCE["approve"]]
    assert letter_state(approved.text) == "approved" and has_id(approved.text, "letter-approved")
    assert [e.attrs.get("id") for e in letter_autofocus(approved.text)] == ["letter-heading"]
    approved_version = d.current_letter_id(app.id)
    again = d.edit_paragraph(
        app.id, d.paragraph_id(app.id, P3), "I speak English at work every day."
    )
    assert again.status_code == 200 and announced(again) == [LETTER_ANNOUNCE["new_draft"]]
    assert d.version_row(approved_version)["status"] == "approved"
    assert d.version_row(d.current_letter_id(app.id))["status"] == "draft"


def test_letter_step_regenerate_keeps_edits_and_replace_needs_the_confirmation(d: LetterDesk):
    app = d.letter_ready()
    d.draft_by_route(app.id, "ok")
    assert d.edit_paragraph(app.id, d.paragraph_id(app.id, P1), KEPT_PARAGRAPH).status_code == 200

    d.draft_by_route(app.id, "ok", note="Again.")
    latest = d.current_letter_id(app.id)
    assert KEPT_PARAGRAPH in d.texts(latest) and P1 not in d.texts(latest)

    tasks, requests = len(d.tasks_of("draft_cover_letter")), len(d.spy.requests)
    refused = d.post_letter(app.id, "generate", {"note": "again", "replace_all": "1"})
    assert refused.status_code == 422
    box = by_id(refused.text, "letter-confirm-replace")
    assert box.attrs["aria-invalid"] == "true" and "data-autofocus" in box.attrs
    assert by_id(refused.text, "letter-note").text == "again"  # the typing stays
    assert len(letter_autofocus(refused.text)) == 1
    assert len(d.tasks_of("draft_cover_letter")) == tasks and len(d.spy.requests) == requests

    d.draft_by_route(app.id, "ok", replace_all="1", confirm_replace="1")
    assert d.texts(d.current_letter_id(app.id)) == OK_PARAGRAPHS


# --- tokens, origin, version, ids and escaping ----------------------------------------------------


def test_letter_step_fragments_need_token_posts_need_origin_and_version(d: LetterDesk):
    app = d.letter_ready()
    d.draft(app.id, "adversarial_invented_employer")
    version = d.current_letter_id(app.id)
    uid = d.paragraph_id(app.id, GLOBEX_P)
    anonymous = d.r.client
    htmx = d.r.run.run.htmx()
    run = d.r.run.run

    # Reads: the fragments and the frame need the token; the page does not and writes nothing.
    reads = (
        f"/applications/{app.id}/tailoring/4",
        letter_url(app.id, f"version/{version}"),
        letter_url(app.id, "frame"),
    )
    before, requests = snapshot(d.r), len(d.spy.requests)
    for path in reads:
        assert anonymous.get(path).status_code == 403
        assert anonymous.get(path, headers=htmx).status_code == 200
    page = anonymous.get(letter_page_url(app.id, version))
    assert page.status_code == 200 and "<html" in page.text
    assert anonymous.get(letter_page_url(app.id), headers={"HX-Request": "true"}).status_code == 403
    assert anonymous.head(letter_page_url(app.id)).status_code == 200
    for aid in ("missing", "bad.id", "<script>"):
        for path in (
            f"/applications/{aid}/tailoring/4",
            letter_url(aid, f"version/{version}"),
            letter_url(aid, "frame"),
        ):
            answer = anonymous.get(path, headers=htmx)
            assert answer.status_code == 404 and answer.text == ApplicationNotFound.message
        answer = anonymous.get(letter_page_url(aid))
        assert answer.status_code == 404 and answer.text == ApplicationNotFound.message
    assert snapshot(d.r) == before and len(d.spy.requests) == requests

    # Writes: the token and the origin are both needed; nothing changes without them.
    posts = (
        (letter_url(app.id, "generate"), {"tone": "warm", "length": "short", "note": "shorter"}),
        (letter_unit_url(app.id, uid, "edit"), {"version": version, "text": "Edited."}),
        (letter_unit_url(app.id, uid, "remove"), {"version": version}),
        (letter_unit_url(app.id, uid, "confirm"), {"version": version}),
        (letter_url(app.id, "unit/add"), {"version": version, "text": "A."}),
        (letter_url(app.id, "approve"), {"version": version}),
    )
    for path, values in posts:
        assert anonymous.post(path, data=values).status_code == 403
        assert anonymous.post(path, data=values, headers=run.with_token()).status_code == 403
        wrong = {**htmx, "Origin": "https://attacker.example.test"}
        assert anonymous.post(path, data=values, headers=wrong).status_code == 403
        no_token = {"Origin": run.action()["origin"]}
        assert anonymous.post(path, data=values, headers=no_token).status_code == 403
    assert snapshot(d.r) == before and len(d.spy.requests) == requests

    # With both, a missing or malformed version is a 400, an unknown, repeated or extra field is a
    # 400, and a stale version is a 409; none of them writes.
    for path, values in posts[1:]:
        without = {k: v for k, v in values.items() if k != "version"}
        assert d.post_fields(path, without).status_code == 400, path
        assert d.post_fields(path, {**values, "version": "bad.id"}).status_code == 400, path
        assert d.post_fields(path, {**values, "extra": "1"}).status_code == 400, path
        stale = d.post_fields(path, {**values, "version": "no-such-version"})
        assert stale.status_code == 409, path
        assert "reload" in said_text(stale.text).lower()
    # The citations are not the form's to change, and a letter paragraph has no section or role.
    for extra in ({"fact_ids": "F2@1"}, {"section": "summary"}, {"role": "Analyst"}):
        cited = {"version": version, "text": "Edited.", **extra}
        assert d.post_fields(letter_unit_url(app.id, uid, "edit"), cited).status_code == 400
    added_with_section = {"version": version, "text": "A.", "section": "summary"}
    assert d.post_fields(letter_url(app.id, "unit/add"), added_with_section).status_code == 400
    assert snapshot(d.r) == before and d.letter_versions(app.id)[-1]["id"] == version
    # Unsafe ids in the path are not found and echo nothing.
    for bad in ("bad.id", "<script>", "no-such-unit"):
        answer = d.post_paragraph(app.id, bad, "remove", {"version": version})
        assert answer.status_code == 404 and bad not in answer.text
    answer = d.post_letter("missing", "approve", {"version": version})
    assert answer.status_code == 404 and answer.text == ApplicationNotFound.message
    # A line of the CV document is not reachable through the letter's routes.
    d.put_template("PT")
    d.generate(app.id, "ok")
    cv_line = d.unit_id(app.id, d.texts(d.current_version_id(app.id))[0])
    letter_version = d.current_letter_id(app.id)
    answer = d.post_paragraph(app.id, cv_line, "remove", {"version": letter_version})
    assert answer.status_code == 404 and cv_line not in answer.text
    assert d.current_letter_id(app.id) == letter_version


def test_letter_step_text_is_escaped(d: LetterDesk):
    app = d.letter_ready()
    d.draft(app.id, "ok")
    hostile_version = ""
    for hostile in HOSTILE:
        edited = d.edit_paragraph(
            app.id, d.paragraph_id(app.id, d.texts(d.current_letter_id(app.id))[0]), hostile
        )
        assert edited.status_code == 200, hostile
        hostile_version = hostile_version or d.current_letter_id(app.id)
        for shown in (d.step4(app.id).text, d.page4(app.id).text):
            assert_escaped(shown, hostile)
        assert_escaped(edited.text, hostile)
    # The note is shown back escaped when a start is refused.
    refused = d.post_letter(
        app.id, "generate", {"tone": "breezy", "length": "medium", "note": HOSTILE[0]}
    )
    assert refused.status_code == 422
    assert_escaped(refused.text, HOSTILE[0])
    # An older version's words are escaped too.
    assert_escaped(d.step4(app.id, hostile_version).text, HOSTILE[0])
