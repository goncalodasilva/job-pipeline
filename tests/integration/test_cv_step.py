"""M6.8 the CV step (step 3 of the Tailoring tab) as the user reads it: the diff against the master
CV, flags inline with their actions, the approval button and its reason, the contact block and role
headers, the practice label, versions viewed read-only, escaping, and the token, origin and version
rules of its routes (AC-M6.1, AC-M6.3, AC-M6.4 in the UI; detailed design "Diff conventions" and
"Editing rules"; ADR-106 items 5, 8, 9 and 11; ADR-113 items 5, 8 and 10; spec 2.9 rules 2, 12, 13).

Assumed interface: see the docstring of ``tests/cv_step_pages.py`` (routes under
``/applications/{id}/tailoring/3/...``, the ``#cv-step`` markup with ``data-line``, ``data-mark``
and ``data-flag`` hooks, the announcement strings). The service half (``DocumentService``) is
M6.7's and is used as it is. Real SQLite in a temp folder, the fake provider behind the spy, no
network, no fixed waits. The mutating routes are exercised in ``test_cv_step_actions.py``.
"""

import json

import pytest

from app.domain.diff import DiffOp, word_diff
from app.domain.errors import ApplicationNotFound
from app.domain.facts import Privacy
from tests.board_pages import elements
from tests.cv_step_pages import (
    CHECK_NUMBER,
    CONFIRM_BUTTON,
    HOSTILE,
    MARKS,
    NOT_IN_FACT_BANK,
    PRACTICE,
    STALE_NOTE,
    VERSION_NOT_FOUND,
    CvStep,
    approve_button,
    assert_escaped,
    autofocus_elements,
    buttons_in,
    cv_step_app,
    cv_url,
    fact_links,
    flags_of,
    has_line_form,
    line_by_unit,
    line_form,
    line_text,
    lines,
    mark_text,
    one_part,
    page_url3,
    parts,
    removed_lines,
    state,
    step,
    tailored_lines,
    unit_url,
    version_id,
)
from tests.document_pages import LANGUAGE_UNIT, NOTE, ROLE_A, ROLE_B, SUMMARY_UNIT, template_json
from tests.integration.test_app_shell import Element
from tests.integration.test_board import snapshot
from tests.integration.test_documents_generation import (
    GLOBEX,
    INFLATED,
    INVENTED_ROLE,
    OK_TEXTS,
    UNCITED,
)
from tests.settings_board_pages import accessible_name
from tests.tailoring_ai_inputs import (
    DETAILS,
    FACT_1,
    FACT_2,
    FACT_3,
    FACT_7,
    FACT_8,
    personal_values,
)
from tests.tailoring_pages import (
    announced,
    by_id,
    form_values,
    has_id,
    hidden_value,
    inside,
    open_step,
    said_text,
    stepper,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def cv(tmp_path):
    with cv_step_app(tmp_path) as desk:
        yield desk


def both(cv: CvStep, aid: str) -> list[str]:
    """The fragment (token) and the token-free page: the step reads the same on both."""
    return [cv.step3(aid).text, cv.page3(aid).text]


def line_of(cv: CvStep, shown: str, aid: str, text: str):
    return line_by_unit(shown, cv.unit_id(aid, text))


def header_args(page: str) -> tuple[str, Element, str]:
    return page, step(page), "data-role-header"


def states(page: str) -> dict[int, str]:
    return {step_number: item.text for step_number, item in stepper(page).items()}


# --- Diff conventions: symbol AND word, never colour only ---------------------------------------


def test_cv_step_diff_symbols_and_text_labels(cv: CvStep):
    app = cv.cv_ready()
    cv.generate(app.id, "reordered")
    expected = {
        SUMMARY_UNIT: "added",  # it cites several facts
        FACT_7: "moved",  # same words, out of the master CV's order
        FACT_1: "unchanged",
        FACT_2: "unchanged",
        LANGUAGE_UNIT: "reworded",  # F3's words, changed
    }

    for shown in both(cv, app.id):
        tailored = tailored_lines(shown)
        assert len(tailored) == len(expected)
        for text, mark in expected.items():
            line = line_of(cv, shown, app.id, text)
            symbol, word = MARKS[mark]
            assert line.attrs["data-mark"] == mark
            # The symbol and the word are both text of the mark, not a colour or an icon alone.
            assert mark_text(shown, line) == f"{symbol} {word}"
            assert "data-moved" not in line.attrs
        # Added, Removed, Reworded and Moved each have their own symbol and word.
        assert len({MARKS[m][0] for m in ("added", "removed", "reworded", "moved")}) == 4
        legend = said_text(by_id(shown, "cv-diff-legend").text)
        for mark in ("added", "removed", "reworded", "moved"):
            assert MARKS[mark][1] in legend and MARKS[mark][0] in legend

        # Reworded: the word diff against the master line, removed words and added words apart.
        reworded = line_of(cv, shown, app.id, LANGUAGE_UNIT)
        segments = word_diff(FACT_3, LANGUAGE_UNIT)
        removed = [s.text.strip() for s in segments if s.op is DiffOp.REMOVED and s.text.strip()]
        added = [s.text.strip() for s in segments if s.op is DiffOp.ADDED and s.text.strip()]
        assert removed and added
        assert [e.text for e in inside(shown, reworded, lambda e: e.tag == "del")] == removed
        assert [e.text for e in inside(shown, reworded, lambda e: e.tag == "ins")] == added
        # Lines that are not reworded show their words plainly.
        for text in (SUMMARY_UNIT, FACT_1, FACT_7):
            plain = line_of(cv, shown, app.id, text)
            assert line_text(shown, plain) == text
            assert not inside(shown, plain, lambda e: e.tag in ("ins", "del"))

        # Removed: a usable master line the CV leaves out is listed, with its symbol and word,
        # and has nothing to edit.
        (gone,) = removed_lines(shown)
        assert FACT_8 in gone.text
        assert mark_text(shown, gone) == f"{MARKS['removed'][0]} Removed"
        assert "data-unit-id" not in gone.attrs
        assert not buttons_in(shown, gone)
        assert not inside(shown, gone, lambda e: e.tag in ("form", "textarea", "input"))

    # A reworded line that also moved says both.
    edited = cv.service.edit_unit(
        app.id,
        cv.current_version_id(app.id),
        cv.unit_id(app.id, FACT_7),
        "Scheduled 12 vans for every night shift.",
    )
    shown = cv.step3(app.id).text
    line = line_by_unit(shown, edited.unit_id)
    assert line.attrs["data-mark"] == "reworded" and line.attrs.get("data-moved") == "true"
    assert mark_text(shown, line) == "~ Reworded"
    assert one_part(shown, line, "data-moved-mark").text == "↕ Moved"


def test_cv_step_unchanged_lines_and_user_lines_have_labels_too(cv: CvStep):
    app = cv.cv_ready()
    cv.generate(app.id, "ok")
    added = cv.service.add_unit(
        app.id, cv.current_version_id(app.id), "languages", "Fluent in Portuguese."
    )

    shown = cv.step3(app.id).text

    for line in tailored_lines(shown):
        assert mark_text(shown, line)  # every line says what it is, in words
    mine = line_by_unit(shown, added.unit_id)
    assert mine.attrs["data-mark"] == "added" and mine.attrs["data-origin"] == "user"
    assert "Edited by you" in mine.text
    assert not parts(shown, line_of(cv, shown, app.id, FACT_1), "data-origin-label")


# --- Facts: hover and disclosure, links to the fact editor --------------------------------------


def test_cv_step_each_line_links_its_facts_to_the_fact_editor(cv: CvStep):
    app = cv.cv_ready()
    cv.generate(app.id, "ok")

    for shown in both(cv, app.id):
        summary = line_of(cv, shown, app.id, SUMMARY_UNIT)
        assert fact_links(shown, summary) == [
            (f"/facts/{cv.fact_id(1)}/edit", FACT_1),
            (f"/facts/{cv.fact_id(2)}/edit", FACT_2),
        ]
        single = line_of(cv, shown, app.id, FACT_7)
        assert fact_links(shown, single) == [(f"/facts/{cv.fact_id(7)}/edit", FACT_7)]
        # The facts sit in a native disclosure, so the keyboard reaches them without hovering.
        (disclosure,) = parts(shown, single, "data-line-facts")
        assert disclosure.tag == "details"
        assert disclosure.text.startswith("Facts used (1)")
        names = [e for e in inside(shown, disclosure, lambda e: e.tag == "summary")]
        assert len(names) == 1 and names[0].text == "Facts used (1)"

    # The wording shown is the user's own wording of the version the line cites, even after the
    # fact was reworded (the link still opens the fact, which has moved on).
    from app.services.facts import FactInput

    one = cv.r.facts.get(cv.fact_id(1))
    cv.r.facts.edit(
        one.id,
        FactInput(text="Planned 61 delivery routes a week.", role_id=one.role_id),
        expected_version=one.version,
    )
    shown = cv.step3(app.id).text
    links = fact_links(shown, line_of(cv, shown, app.id, FACT_1))
    assert links == [(f"/facts/{cv.fact_id(1)}/edit", FACT_1)]

    # A line that cites nothing says so, and has no empty disclosure.
    added = cv.service.add_unit(
        app.id, cv.current_version_id(app.id), "languages", "Fluent in Portuguese."
    )
    shown = cv.step3(app.id).text
    bare = line_by_unit(shown, added.unit_id)
    assert not parts(shown, bare, "data-line-facts")
    assert "No facts cited" in one_part(shown, bare, "data-no-facts").text


# --- Flags inline with their claims, and the actions ---------------------------------------------


def test_cv_step_flags_inline_with_actions(cv: CvStep):
    first = cv.cv_ready()
    second = cv.cv_ready("uncited-job")
    cv.generate(first.id, "adversarial_inflated_number")
    cv.generate(second.id, "adversarial_uncited_paragraph")
    mine = first.id

    shown = cv.step3(mine).text
    flagged = line_of(cv, shown, mine, INFLATED)
    # The flags are inside the line they are about, with their claims, in words.
    assert flagged.attrs["data-flagged"] == "true"
    assert flags_of(shown, flagged) == [
        ("needs_check", CHECK_NUMBER, "85%"),
        ("needs_check", CHECK_NUMBER, "14"),
    ]
    assert INFLATED in line_text(shown, flagged)
    # The actions: Edit, Remove, and "This is true — save it as a fact".
    assert {"Edit", "Remove", CONFIRM_BUTTON} <= set(buttons_in(shown, flagged))
    unit_id = str(flagged.attrs["data-unit-id"])
    version = version_id(shown)
    edit = line_form(shown, flagged, "edit")
    assert (
        edit.attrs["action"] == unit_url(mine, unit_id, "edit") and edit.attrs["method"] == "post"
    )
    assert hidden_value(shown, edit, "version") == version
    assert form_values(shown, edit)["text"] == INFLATED  # the editor starts from the line
    (box,) = inside(shown, edit, lambda e: e.tag == "textarea")
    assert accessible_name(shown, box) == "Edit this line"
    assert box.attrs["name"] == "text" and box.attrs["data-draft-key"] == f"edit:{unit_id}"
    remove = line_form(shown, flagged, "remove")
    assert remove.attrs["action"] == unit_url(mine, unit_id, "remove")
    assert hidden_value(shown, remove, "version") == version
    confirm = line_form(shown, flagged, "confirm")
    assert confirm.attrs["action"] == unit_url(mine, unit_id, "confirm")
    assert hidden_value(shown, confirm, "version") == version
    # Every action names the line it acts on.
    for button in inside(shown, flagged, lambda e: e.tag == "button"):
        target = str(button.attrs.get("aria-describedby"))
        assert target == f"line-text-{unit_id}" and has_id(shown, target)

    # A line with nothing to check has Edit and Remove, but not the fact action.
    clean = line_of(cv, shown, mine, SUMMARY_UNIT)
    assert "data-flagged" not in clean.attrs and flags_of(shown, clean) == []
    assert not has_line_form(shown, clean, "confirm")
    assert CONFIRM_BUTTON not in buttons_in(shown, clean)
    assert {"Edit", "Remove"} <= set(buttons_in(shown, clean))

    # A line that cites nothing is not in the fact bank.
    other = cv.step3(second.id).text
    uncited = line_of(cv, other, second.id, UNCITED)
    assert flags_of(other, uncited) == [("unsupported", NOT_IN_FACT_BANK, UNCITED)]
    assert CONFIRM_BUTTON in buttons_in(other, uncited)


def test_cv_step_flag_covers_blockers_not_stored_on_the_unit(cv: CvStep):
    app = cv.cv_ready()
    cv.generate(app.id, "ok")
    version = cv.current_version_id(app.id)
    assert cv.flag_triples(version) == []  # nothing was flagged when it was made
    one = cv.r.facts.get(cv.fact_id(1))
    cv.r.facts.set_privacy(one.id, Privacy.NEVER_USE, expected_version=one.version)

    shown = cv.step3(app.id).text

    # F1 is "never use" now: the lines that cite it show it, though no flag is stored on them.
    for text in (SUMMARY_UNIT, FACT_1):
        line = line_of(cv, shown, app.id, text)
        assert line.attrs["data-flagged"] == "true"
        assert flags_of(shown, line) == [("unsupported", NOT_IN_FACT_BANK, "F1@1")]
        assert CONFIRM_BUTTON in buttons_in(shown, line)
    assert "data-flagged" not in line_of(cv, shown, app.id, FACT_2).attrs


# --- Approval: disabled, with the reason, until nothing blocks -----------------------------------


def test_cv_step_approve_disabled_with_reason(cv: CvStep):
    app = cv.cv_ready()
    cv.generate(app.id, "adversarial_invented_employer")  # two lines need a check
    version = cv.current_version_id(app.id)

    for shown in both(cv, app.id):
        button = approve_button(shown)
        assert button is not None and button.tag == "button" and button.text == "Approve CV"
        # Blocked, not disabled: the keyboard reaches it and hears why (ADR-086).
        assert button.attrs["aria-disabled"] == "true" and "disabled" not in button.attrs
        assert button.attrs["aria-describedby"] == "cv-approve-reason"
        assert by_id(shown, "cv-approve-reason").text == "2 lines need your check."
        assert state(shown) == "draft" and "Needs your check" in states(shown)[3]
        assert said_text(by_id(shown, "cv-status").text) == "Draft, version 1. Not approved yet."
    # The server refuses the same press, and nothing changes.
    before = snapshot(cv.r)
    refused = cv.post_cv(app.id, "approve", {"version": version})
    assert refused.status_code == 409
    assert by_id(refused.text, "cv-error").text == "2 lines need your check."
    assert announced(refused) == ["2 lines need your check."]
    assert snapshot(cv.r) == before and cv.version_row(version)["status"] == "draft"

    # Resolve one: the reason counts the lines still blocking, in the same words.
    first = cv.unit_id(app.id, GLOBEX)
    assert cv.remove(app.id, first).status_code == 200
    shown = cv.step3(app.id).text
    assert by_id(shown, "cv-approve-reason").text == "1 line needs your check."
    assert approve_button(shown).attrs["aria-disabled"] == "true"
    assert INVENTED_ROLE not in said_text(by_id(shown, "cv-approve-reason").text)

    # Resolve the other: nothing blocks, the button is live and the reason is gone.
    second = cv.unit_id(app.id, FACT_1)
    assert cv.confirm(app.id, second).status_code == 200
    shown = cv.step3(app.id).text
    button = approve_button(shown)
    assert button is not None and "aria-disabled" not in button.attrs
    assert not has_id(shown, "cv-approve-reason")
    assert "Ready to approve" in states(shown)[3]

    approved = cv.approve_now(app.id)
    assert approved.status_code == 200 and announced(approved) == ["CV approved."]
    shown = approved.text
    assert state(shown) == "approved" and "Done" in states(shown)[3]
    assert approve_button(shown) is None
    assert said_text(by_id(shown, "cv-approved").text).startswith("Approved.")
    assert said_text(by_id(shown, "cv-status").text) == "Approved, version 3."
    assert cv.version_row(cv.current_version_id(app.id))["status"] == "approved"


def test_cv_step_approve_reason_counts_blockers_that_are_not_unit_flags(cv: CvStep):
    app = cv.cv_ready()
    cv.generate(app.id, "ok")
    shown = cv.step3(app.id).text
    assert "aria-disabled" not in approve_button(shown).attrs  # a clean CV can be approved
    one = cv.r.facts.get(cv.fact_id(1))
    cv.r.facts.set_privacy(one.id, Privacy.NEVER_USE, expected_version=one.version)

    shown = cv.step3(app.id).text

    # The stored flags are empty; the blockers (the facts as they are now) disable the button.
    assert cv.flag_triples(cv.current_version_id(app.id)) == []
    assert approve_button(shown).attrs["aria-disabled"] == "true"
    assert by_id(shown, "cv-approve-reason").text == "2 lines need your check."
    refused = cv.post_cv(app.id, "approve", {"version": version_id(shown)})
    assert refused.status_code == 409
    assert said_text(by_id(refused.text, "cv-error").text) == "2 lines need your check."


def test_cv_step_nothing_to_approve_has_its_own_reason(cv: CvStep):
    app = cv.cv_ready()
    cv.generate(app.id, "ok")
    for text in OK_TEXTS:
        assert cv.remove(app.id, cv.unit_id(app.id, text)).status_code == 200

    shown = cv.step3(app.id).text

    assert tailored_lines(shown) == []
    assert approve_button(shown).attrs["aria-disabled"] == "true"
    assert by_id(shown, "cv-approve-reason").text == "There is nothing to approve yet."
    refused = cv.post_cv(app.id, "approve", {"version": version_id(shown)})
    assert refused.status_code == 409
    assert by_id(refused.text, "cv-error").text == "There is nothing to approve yet."
    assert cv.version_row(cv.current_version_id(app.id))["status"] == "draft"


# --- Contact block and role headers: built by code, not editable ---------------------------------


def test_cv_step_contact_block_and_role_headers_are_code_built_not_editable(cv: CvStep):
    app = cv.cv_ready()
    cv.save_details()
    cv.generate(app.id, "ok")

    shown = cv.step3(app.id).text
    contact = by_id(shown, "cv-contact")
    assert one_part(shown, contact, "data-contact-name").text == DETAILS.name
    assert [e.text for e in parts(shown, contact, "data-contact-line")] == [
        DETAILS.email,
        DETAILS.phone,
        DETAILS.location,
        *DETAILS.links,
    ]
    settings_link = inside(shown, contact, lambda e: e.tag == "a")
    assert [(e.attrs["href"], e.text) for e in settings_link] == [
        ("/settings", "Change in Settings")
    ]
    assert not inside(shown, contact, lambda e: e.tag in ("form", "input", "textarea", "button"))

    # Rule 13: the token-free page holds no personal detail, only a lazy placeholder.
    page = cv.page3(app.id).text
    placeholder = by_id(page, "cv-contact")
    assert placeholder.attrs["hx-get"] == cv_url(app.id, "contact")
    assert placeholder.attrs["hx-trigger"] == "load"
    for value in personal_values():
        assert value not in page, value
    assert cv.r.client.get(cv_url(app.id, "contact")).status_code == 403  # the token
    assert by_id(cv.contact(app.id).text, "cv-contact").text == contact.text

    # Role headers come from the stored role and the snapshot's date format, with no control.
    headers = parts(shown, step(shown), "data-role-header")
    for header, (role, dates) in zip(
        headers[:2],
        ((ROLE_A, ("Mar 2019", "Jun 2022")), (ROLE_B, ("Jan 2017", "Feb 2019"))),
        strict=True,
    ):
        for part in (role[0], role[1], *dates):
            assert part in header.text, (part, header.text)
    for header in headers:
        assert not inside(shown, header, lambda e: e.tag in ("form", "input", "textarea", "button"))
        assert not any(a.tag == "li" and "data-line" in a.attrs for a in header.ancestors())
    # Section headings are the snapshot's own and are never lines or editable.
    headings = [e.text for e in parts(shown, step(shown), "data-section-heading")]
    assert headings == ["Summary", "Work history", "Languages"]
    assert all(text not in " ".join(line.text for line in lines(shown)) for text in headings[1:2])
    sections = inside(
        shown, step(shown), lambda e: e.tag == "section" and "data-section" in e.attrs
    )
    assert len(sections) == 3


def test_cv_step_date_format_follows_the_versions_snapshot(cv: CvStep):
    app = cv.cv_ready()
    cv.generate(app.id, "ok")
    first = cv.current_version_id(app.id)
    cv.r.run.execute(
        "UPDATE cv_templates SET spec=? WHERE country='PT'", (template_json(date_format="iso"),)
    )
    cv.generate(app.id, "ok")

    latest = [h.text for h in parts(*header_args(cv.step3(app.id).text))]
    older = [h.text for h in parts(*header_args(cv.step3(app.id, first).text))]

    # A later template edit changes new versions only: each version keeps its own snapshot.
    assert "2019-03" in latest[0] and "2022-06" in latest[0]
    assert "Mar 2019" in older[0] and "Jun 2022" in older[0]


# --- Practice data label --------------------------------------------------------------------------


def test_cv_step_fake_output_is_labelled_practice_data(cv: CvStep):
    app = cv.cv_ready()
    cv.generate(app.id, None)  # the app's own fake answers, as in the demo

    for shown in both(cv, app.id):
        label = by_id(shown, "cv-practice")
        assert PRACTICE in label.text
        assert all(line_text(shown, line).startswith(PRACTICE) for line in tailored_lines(shown))
    # The user's own edits keep the label: it comes from the nearest version a provider wrote.
    unit = cv.units(cv.current_version_id(app.id))[0]
    cv.service.edit_unit(app.id, cv.current_version_id(app.id), unit["id"], unit["text"] + " Done.")
    assert PRACTICE in by_id(cv.step3(app.id).text, "cv-practice").text


# --- The step replaces the placeholder ----------------------------------------


def test_cv_step_replaces_the_step_three_placeholder(cv: CvStep):
    app = cv.cv_ready()

    for shown in [*both(cv, app.id), cv.step3(app.id).text]:
        assert "later step" not in step(shown).text.lower()
        assert "Coming later" not in states(shown)[3]
        assert "Not started" in states(shown)[3]
        assert state(shown) == "none"
        order = [e.attrs.get("id") for e in elements(shown) if e.attrs.get("id")]
        # The CV's controls come before the template unit's many fields.
        assert order.index("cv-step") < order.index("cv-template")
        assert by_id(shown, "cv-heading").text == "Tailored CV"
    page = cv.page3(app.id).text
    assert open_step(page) == 3
    # The other steps hold no CV step.
    for number in (1, 2, 4, 5):
        assert not has_id(cv.fragment(app.id, number).text, "cv-step"), number

    # The state words follow the work.
    cv.generate(app.id, "adversarial_uncited_paragraph")
    assert "Needs your check" in states(cv.step3(app.id).text)[3]
    cv.remove(app.id, cv.unit_id(app.id, UNCITED))
    assert "Ready to approve" in states(cv.step3(app.id).text)[3]
    cv.approve_now(app.id)
    assert "Done" in states(cv.step3(app.id).text)[3]
    cv.generate(app.id, "malformed", status="failed")
    failed = cv.step3(app.id).text
    assert "Needs attention" in states(failed)[3] and state(failed) == "failed"


# --- Versions: a list, and read-only viewing of an older one ------------------------------


def test_cv_step_version_list_and_read_only_older_version(cv: CvStep):
    app = cv.cv_ready()
    cv.generate(app.id, "ok")
    v1 = cv.current_version_id(app.id)
    edited = cv.service.edit_unit(
        app.id, v1, cv.unit_id(app.id, FACT_1), "Planned 60 routes weekly."
    )
    v2 = edited.version_id
    cv.generate(app.id, "ok", note=NOTE)
    v3 = cv.current_version_id(app.id)
    before = snapshot(cv.r)

    for shown in both(cv, app.id):
        items = inside(shown, by_id(shown, "cv-versions"), lambda e: e.tag == "li")
        assert [i.attrs["data-version-id"] for i in items] == [v3, v2, v1]  # newest first
        assert [i.attrs["data-version-status"] for i in items] == ["draft"] * 3
        assert "Version 3" in items[0].text and "Latest" in items[0].text
        assert "Latest" not in items[1].text and "Latest" not in items[2].text
        assert "Written by the AI" in items[2].text and "Edited by you" in items[1].text
        assert NOTE in items[0].text  # the note it was regenerated with
        links = [inside(shown, i, lambda e: e.tag == "a")[0] for i in items]
        assert [(a.attrs["href"], a.text) for a in links] == [
            (page_url3(app.id, v3), "View version 3"),
            (page_url3(app.id, v2), "View version 2"),
            (page_url3(app.id, v1), "View version 1"),
        ]
        assert links[2].attrs["hx-get"] == cv_url(app.id, f"version/{v1}")
        assert links[2].attrs["hx-target"] == "#tailoring"
        assert links[2].attrs["hx-push-url"] == page_url3(app.id, v1)

    # Viewing an older version: its own lines, labelled, with nothing to edit.
    for old in (cv.step3(app.id, v1).text, cv.page3(app.id, v1).text):
        assert step(old).attrs["data-version-id"] == v1
        assert step(old).attrs["data-readonly"] == "true"
        assert said_text(by_id(old, "cv-status").text) == "Version 1 (read-only)."
        assert "read-only" in by_id(old, "cv-readonly-note").text
        back = by_id(old, "cv-latest")
        assert back.tag == "a" and back.text == "Back to the latest version"
        texts = [line_text(old, line) for line in tailored_lines(old)]
        assert FACT_1 in texts and "Planned 60 routes weekly." not in texts
        controls = inside(
            old,
            step(old),
            lambda e: (
                e.tag in ("form", "textarea", "input", "select", "button")
                or (e.tag == "summary" and e.text in ("Edit", "Add a line"))
            ),
        )
        assert controls == [], [(e.tag, e.text) for e in controls]
        assert not has_id(old, "cv-approve") and not has_id(old, "cv-generate-form")
        assert not has_id(old, "cv-add-form")
        viewing = [
            link.text
            for link in inside(old, by_id(old, "cv-versions"), lambda e: e.tag == "a")
            if link.attrs.get("aria-current") == "true"
        ]
        assert viewing == ["View version 1"]
    # The latest version is the default view, and it is editable.
    assert step(cv.step3(app.id).text).attrs["data-readonly"] == "false"
    # Looking changed nothing; a post from the old view conflicts.
    assert snapshot(cv.r) == before
    stale = cv.post_unit(app.id, cv.unit_id(app.id, FACT_2), "remove", {"version": v1})
    assert stale.status_code == 409

    # An unknown version, or one of another application, is not found; the page ignores it.
    other = cv.cv_ready("other-cv-job")
    cv.generate(other.id, "ok")
    foreign = cv.current_version_id(other.id)
    for bad in ("no-such-version", foreign, "bad.id"):
        answer = cv.step3(app.id, bad)
        assert answer.status_code == 404 and answer.text == VERSION_NOT_FOUND
        page = cv.page3(app.id, bad)
        assert page.status_code == 200 and version_id(page.text) == v3  # ignored, not echoed
        assert bad not in page.text


# --- Escaping -------------------------------------------------------------------------------------


def test_cv_step_text_escaped(cv: CvStep):
    app = cv.cv_ready()
    cv.generate(app.id, "ok")
    unit_text, wording, note, claim, role = HOSTILE
    version = cv.current_version_id(app.id)
    cv.service.edit_unit(app.id, version, cv.unit_id(app.id, FACT_1), unit_text)
    cv.generate(app.id, "ok", note=note)  # a hostile note, listed with its version
    kept = cv.unit_id(app.id, unit_text)
    # Stored data the app's own paths would not make: the stored-row guards are lifted for the
    # test only, so every place the screen shows stored text can be given hostile text.
    cv.tamper(
        "UPDATE document_units SET flags=? WHERE id=?",
        (json.dumps([{"kind": "needs_check", "claim": claim}]), kept),
        triggers=("document_units_never_updated",),
    )
    cv.tamper(
        "UPDATE fact_versions SET text=? WHERE fact_id=? AND version=1",
        (wording, cv.fact_id(1)),
        triggers=("fact_versions_never_updated",),
    )
    cv.r.run.execute("UPDATE roles SET title=? WHERE title=?", (role, ROLE_A[0]))

    for shown in both(cv, app.id):
        for hostile in (unit_text, wording, note, claim, role):
            assert_escaped(shown, hostile)
        line = line_by_unit(shown, kept)
        assert line_text(shown, line) == unit_text  # shown as the text it is
        assert flags_of(shown, line) == [("needs_check", "Check this claim", claim)]
        edit = line_form(shown, line, "edit")
        assert form_values(shown, edit)["text"] == unit_text  # a textarea cannot be broken out of
        hostile_tags = inside(
            shown, step(shown), lambda e: e.tag in ("script", "img", "svg", "b", "u")
        )
        assert hostile_tags == []

    # A refused edit shows what was typed as text, and the message never echoes it.
    refused = cv.edit(app.id, kept, HOSTILE[0] + "\x07")
    assert refused.status_code == 422
    assert_escaped(refused.text, HOSTILE[0])
    for message in (e.text for e in elements(refused.text) if "field-error" in e.classes):
        assert HOSTILE[0] not in message
    # So does a conflict: the kept text is data.
    stale = cv.edit(app.id, kept, HOSTILE[2], version="stale-version-id")
    assert stale.status_code == 409
    assert_escaped(stale.text, HOSTILE[2])
    assert by_id(stale.text, "cv-kept-text").text == HOSTILE[2]


# --- Boundaries: token, origin, version ----------------------------------------------------


def test_cv_step_fragments_need_token_posts_need_origin_and_version(cv: CvStep):
    app = cv.cv_ready()
    cv.generate(app.id, "adversarial_invented_employer")
    version = cv.current_version_id(app.id)
    uid = cv.unit_id(app.id, GLOBEX)
    anonymous = cv.r.client
    htmx = cv.r.run.run.htmx()
    run = cv.r.run.run

    # Reads: the fragments and the contact need the token; the page does not and writes nothing.
    reads = (
        f"/applications/{app.id}/tailoring/3",
        cv_url(app.id, f"version/{version}"),
        cv_url(app.id, "contact"),
    )
    before, requests = snapshot(cv.r), len(cv.spy.requests)
    for path in reads:
        assert anonymous.get(path).status_code == 403
        assert anonymous.get(path, headers=htmx).status_code == 200
    page = anonymous.get(page_url3(app.id, version))
    assert page.status_code == 200 and "<html" in page.text
    assert anonymous.get(page_url3(app.id), headers={"HX-Request": "true"}).status_code == 403
    assert anonymous.head(page_url3(app.id)).status_code == 200
    for aid in ("missing", "bad.id", "<script>"):
        for path in (
            f"/applications/{aid}/tailoring/3",
            cv_url(aid, f"version/{version}"),
            cv_url(aid, "contact"),
        ):
            answer = anonymous.get(path, headers=htmx)
            assert answer.status_code == 404 and answer.text == ApplicationNotFound.message
        answer = anonymous.get(page_url3(aid))
        assert answer.status_code == 404 and answer.text == ApplicationNotFound.message
    assert snapshot(cv.r) == before and len(cv.spy.requests) == requests

    # Writes: the token and the origin are both needed; nothing changes without them.
    posts = (
        (cv_url(app.id, "generate"), {"note": "shorter"}),
        (unit_url(app.id, uid, "edit"), {"version": version, "text": "Edited."}),
        (unit_url(app.id, uid, "remove"), {"version": version}),
        (unit_url(app.id, uid, "confirm"), {"version": version}),
        (cv_url(app.id, "unit/add"), {"version": version, "section": "languages", "text": "A."}),
        (cv_url(app.id, "approve"), {"version": version}),
    )
    before = snapshot(cv.r)
    for path, values in posts:
        assert anonymous.post(path, data=values).status_code == 403
        assert anonymous.post(path, data=values, headers=run.with_token()).status_code == 403
        wrong = {**htmx, "Origin": "https://attacker.example.test"}
        assert anonymous.post(path, data=values, headers=wrong).status_code == 403
        no_token = {"Origin": run.action()["origin"]}
        assert anonymous.post(path, data=values, headers=no_token).status_code == 403
    assert snapshot(cv.r) == before and len(cv.spy.requests) == requests

    # With both, a missing or malformed version is a 400, an unknown or repeated or extra field
    # is a 400, and a stale version is a 409; none of them writes.
    for path, values in posts[1:]:
        without = {k: v for k, v in values.items() if k != "version"}
        assert cv.post_fields(path, without).status_code == 400, path
        assert cv.post_fields(path, {**values, "version": "bad.id"}).status_code == 400, path
        assert cv.post_fields(path, {**values, "extra": "1"}).status_code == 400, path
        stale = cv.post_fields(path, {**values, "version": "no-such-version"})
        assert stale.status_code == 409, path
        assert "reload" in said_text(stale.text).lower()
    # The citations are not the form's to change.
    cited = {"version": version, "text": "Edited.", "fact_ids": "F2@1"}
    assert cv.post_fields(unit_url(app.id, uid, "edit"), cited).status_code == 400
    assert snapshot(cv.r) == before and cv.versions(app.id)[-1]["id"] == version
    # Unsafe ids in the path are not found and echo nothing.
    for bad in ("bad.id", "<script>", "no-such-unit"):
        answer = cv.post_unit(app.id, bad, "remove", {"version": version})
        assert answer.status_code == 404 and bad not in answer.text
    answer = cv.post_cv("missing", "approve", {"version": version})
    assert answer.status_code == 404 and answer.text == ApplicationNotFound.message


def test_cv_step_stale_note_is_in_the_step_and_hidden(cv: CvStep):
    app = cv.cv_ready()
    cv.generate(app.id, "ok")

    shown = cv.step3(app.id).text

    note = by_id(shown, "cv-stale-note")
    assert "hidden" in note.attrs and "data-stale-note" in note.attrs
    assert STALE_NOTE in note.text
    assert "data-keep-edits" in by_id(shown, "tailoring").attrs
    assert not autofocus_elements(shown)  # a plain read takes no focus
    for other in (1, 2, 5):
        assert "data-keep-edits" not in by_id(cv.fragment(app.id, other).text, "tailoring").attrs
    assert open_step(shown) == 3


def test_cv_step_old_version_refresh_keeps_that_version(cv: CvStep):
    app = cv.cv_ready()
    cv.generate(app.id, "ok")
    first = cv.current_version_id(app.id)
    cv.service.edit_unit(app.id, first, cv.unit_id(app.id, FACT_1), "Planned 60 routes weekly.")
    old = cv.step3(app.id, first).text
    assert by_id(old, "tailoring").attrs["hx-get"] == cv_url(app.id, f"version/{first}")


def test_cv_step_stepper_on_other_steps_shows_current_cv_state(cv: CvStep):
    app = cv.cv_ready()
    cv.generate(app.id, "ok")
    for number in (1, 2, 4, 5):
        assert "Ready to approve" in states(cv.fragment(app.id, number).text)[3]
    cv.service.approve(app.id, cv.current_version_id(app.id))
    assert "Done" in states(cv.fragment(app.id, 2).text)[3]


def test_cv_corrections_remain_available_without_generation_prerequisites(cv: CvStep):
    app = cv.cv_ready()
    cv.generate(app.id, "ok")
    for number in (1, 2, 3, 7, 8):
        fact = cv.r.facts.get(cv.fact_id(number))
        cv.r.facts.set_privacy(fact.id, Privacy.NEVER_USE, expected_version=fact.version)
    shown = cv.step3(app.id).text
    assert has_id(shown, "cv-not-ready")
    line = line_by_unit(shown, cv.unit_id(app.id, FACT_1))
    assert has_line_form(shown, line, "edit")
    assert has_line_form(shown, line, "remove")
    assert has_line_form(shown, line, "confirm")
    assert has_id(shown, "cv-approve")
    assert not has_id(shown, "cv-generate-form")


def test_cv_action_dialog_keeps_its_exact_committed_version(cv: CvStep, monkeypatch):
    app = cv.cv_ready()
    cv.generate(app.id, "ok")
    original = cv.service.edit_unit
    committed = []

    def edit_then_other_write(*args, **kwargs):
        outcome = original(*args, **kwargs)
        committed.append(outcome)
        latest = cv.service.view(app.id)
        other = next(u for u in latest.units if u.unit_id != outcome.unit_id)
        cv.service.remove_unit(app.id, outcome.version_id, other.unit_id)
        return outcome

    monkeypatch.setattr(cv.service, "edit_unit", edit_then_other_write)
    answer = cv.edit(app.id, cv.unit_id(app.id, FACT_1), "Led 9 people and saved $2m.")
    assert answer.status_code == 200
    dialog = by_id(answer.text, "cv-save-fact")
    form = inside(answer.text, dialog, lambda e: e.tag == "form")[0]
    values = form_values(answer.text, form)
    assert values["version"] == committed[0].version_id
    assert cv.post_fields(str(form.attrs["action"]), values).status_code == 409


def test_cv_old_page_contact_load_uses_its_template_snapshot(cv: CvStep):
    app = cv.cv_ready()
    cv.save_details()
    cv.generate(app.id, "ok")
    old = cv.current_version_id(app.id)
    cv.r.run.execute(
        "UPDATE cv_templates SET spec=? WHERE country='PT'", (template_json(details_shown=[]),)
    )
    cv.generate(app.id, "ok")
    shown = cv.page3(app.id, old).text
    url = str(by_id(shown, "cv-contact").attrs["hx-get"])
    loaded = cv.r.client.get(url, headers=cv.r.run.run.htmx())
    assert DETAILS.email in loaded.text
    assert DETAILS.email not in cv.contact(app.id).text
    unknown = cv.r.client.get(
        cv_url(app.id, "contact") + "?version=missing", headers=cv.r.run.run.htmx()
    )
    assert unknown.status_code == 404 and unknown.text == VERSION_NOT_FOUND


def test_cv_completion_refresh_suppresses_already_shown_attempt(cv: CvStep):
    from tests.tailoring_pages import announced

    app = cv.cv_ready()
    cv.generate(app.id, "ok")
    key = cv.service.view(app.id).task.key
    assert announced(cv.step3(app.id, **{"HX-Trigger": "tailoring", "X-JP-CV-Seen": key})) == []


def test_cv_latest_page_contact_pins_version_before_lazy_load(cv: CvStep):
    app = cv.cv_ready()
    cv.save_details()
    cv.generate(app.id, "ok")
    version = cv.current_version_id(app.id)
    placeholder = by_id(cv.page3(app.id).text, "cv-contact")
    values = json.loads(str(placeholder.attrs.get("hx-vals", "{}")))
    assert values == {"version": version}
    cv.r.run.execute(
        "UPDATE cv_templates SET spec=? WHERE country='PT'", (template_json(details_shown=[]),)
    )
    cv.generate(app.id, "ok")
    contact = cv.r.client.get(
        str(placeholder.attrs["hx-get"]), params=values, headers=cv.r.run.run.htmx()
    )
    assert DETAILS.email in contact.text
