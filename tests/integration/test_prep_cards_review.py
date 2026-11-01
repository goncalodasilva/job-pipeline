"""Review fixes of the narrative cards (M8.2 part B, review round 1; AC-M8.4 honesty rule for
generated prep text; spec 3.1, 3.3; ADR-124 items 7 and 8). Fictional temporary data.

New tests only: ``tests/integration/test_prep_cards.py`` keeps the contract of the first part B
tests. What is added here:

- a reused card is re-checked against the TARGET application's title and company;
- paragraphs code left out are counted on the card (never their text);
- the service tells the route whether the card was approved (no extra read);
- the empty states, the salary card after a removal, card-level messages, the angle, the
  accessible names, the in-page index, the recovery of typed text and the "needs re-check" words.
"""

import re

import pytest

from app.domain.errors import VersionConflict
from app.services.documents import ApprovalBlocked
from tests.integration.test_board import snapshot
from tests.integration.test_inbox import get
from tests.prep_card_pages import (
    ANGLE_NAME,
    COMPANY,
    EMPTY_TEXT,
    KIND,
    KINDS,
    P1,
    P2,
    CardDesk,
    card_app,
)
from tests.tailoring_pages import announced, by_id, has_id, said_text

pytestmark = pytest.mark.integration

NAMING = "I planned 60 delivery routes a week with a team of 3, and I want to do it at " + COMPANY
OTHER_JOB = "other-card-job"


@pytest.fixture
def d(tmp_path):
    with card_app(tmp_path) as desk:
        yield desk


def naming_card() -> dict:
    return {"units": [{"text": NAMING + ".", "fact_ids": ["F1@1"], "added_details": []}]}


def same_company(d: CardDesk, job: str = OTHER_JOB) -> None:
    d.r.run.execute(
        "UPDATE jobs SET company_id=(SELECT company_id FROM jobs WHERE id='card-job') WHERE id=?",
        (job,),
    )


def tag(page: str, element_id: str) -> str:
    found = re.search(rf"<[a-z0-9]+ [^>]*id=\"{element_id}\"[^>]*>", page)
    assert found is not None, element_id
    return found.group(0)


# --- reuse: the copy is checked against the application it is copied to --------------------------


def test_a_reused_card_naming_another_company_is_flagged_and_cannot_be_approved(d: CardDesk):
    d.set_angle()
    d.draft(KIND, data=naming_card())
    source = d.approve(KIND)
    assert d.card(KIND).units[0].flags == (), "the company is the source application's own"
    other = d.another(OTHER_JOB)

    new_id = d.service.use_card(other, KIND, source)

    (row,) = d.units(new_id)
    assert [(f["kind"], f["claim"]) for f in row["flag_list"]] == [("needs_check", COMPANY)]
    card = d.card(KIND, other)
    assert card.state == "draft" and [b.claim for b in card.blockers] == [COMPANY]
    with pytest.raises(ApprovalBlocked) as blocked:
        d.service.approve(other, KIND, new_id)
    assert blocked.value.message == "1 paragraph needs your check."
    assert d.version_row(new_id)["status"] == "draft"
    page = get(d.r, d.path("", other)).text
    assert re.search(
        r'<button[^>]*id="card-tell_me_about_yourself-approve"[^>]*aria-disabled', page
    )
    # Editing the name away clears the flag and the card can then be approved.
    fixed = d.service.edit_unit(
        other, KIND, new_id, row["id"], "I planned 60 delivery routes a week with a team of 3."
    )
    assert d.flag_triples(fixed.version_id) == []
    d.service.approve(other, KIND, fixed.version_id)


def test_a_reused_card_naming_the_same_company_is_not_flagged(d: CardDesk):
    d.set_angle()
    d.draft(KIND, data=naming_card())
    source = d.approve(KIND)
    other = d.another(OTHER_JOB)
    same_company(d)

    new_id = d.service.use_card(other, KIND, source)

    assert d.flag_triples(new_id) == [] and d.card(KIND, other).blockers == ()
    d.service.approve(other, KIND, new_id)


def test_a_reused_card_naming_the_job_title_of_another_application_is_flagged(d: CardDesk):
    d.set_angle()
    d.draft(
        KIND,
        data={
            "units": [
                {
                    "text": "I planned 60 delivery routes a week as a Route Planner.",
                    "fact_ids": ["F1@1"],
                    "added_details": [],
                }
            ]
        },
    )
    source = d.approve(KIND)
    other = d.another(OTHER_JOB)

    new_id = d.service.use_card(other, KIND, source)

    assert [claim for _, claim, _ in d.flag_triples(new_id)] == ["Route Planner"]


# --- what code left out is told, without its text ------------------------------------------------


def long_card() -> dict:
    return {
        "units": [
            {"text": "w" * 700, "fact_ids": ["F1@1"], "added_details": []},
            {"text": "x" * 700, "fact_ids": ["F1@1"], "added_details": []},
            {"text": "y" * 100, "fact_ids": ["F1@1"], "added_details": []},
        ]
    }


def test_paragraphs_left_out_are_counted_on_the_card_and_their_text_is_kept_out_of_task_rows(
    d: CardDesk,
):
    d.draft(KIND, data=long_card())

    card = d.card(KIND)
    assert len(card.units) == 1
    assert "2 paragraphs were left out" in card.left_out and "1,200" in card.left_out
    page = get(d.r, d.path()).text
    assert "2 paragraphs were left out" in said_text(by_id(page, f"card-{KIND}-left-out").text)
    assert "xxxx" not in page and "yyyy" not in page
    for row in d.rows("SELECT * FROM tasks"):
        assert "xxxx" not in repr(row) and "yyyy" not in repr(row)

    # An edit starts another version: the note belonged to the draft the model wrote.
    version = d.version_id(KIND)
    d.service.edit_unit(d.aid, KIND, version, d.card(KIND).units[0].id, "I planned routes.")
    assert d.card(KIND).left_out == ""
    assert not has_id(get(d.r, d.path()).text, f"card-{KIND}-left-out")


def test_a_clean_draft_and_a_redraft_after_a_cut_one_say_nothing_was_left_out(d: CardDesk):
    d.draft(KIND, data=long_card())
    assert d.card(KIND).left_out

    d.draft(KIND, "ok")

    assert d.card(KIND).left_out == ""
    assert not has_id(get(d.r, d.path()).text, f"card-{KIND}-left-out")


# --- the service tells the route whether the card was approved ----------------------------------


def test_the_outcome_of_a_change_says_whether_the_card_was_approved(d: CardDesk):
    d.draft(KIND, "ok")
    draft = d.version_id(KIND)
    first = d.card(KIND).units[0].id

    edited = d.service.edit_unit(d.aid, KIND, draft, first, "I planned 60 routes a week.")
    assert edited.was_approved is False
    approved = d.approve(KIND)
    after = d.service.edit_unit(
        d.aid, KIND, approved, d.card(KIND).units[0].id, "I planned 60 routes weekly."
    )
    assert after.was_approved is True
    again = d.service.remove_unit_outcome(d.aid, KIND, after.version_id, d.card(KIND).units[1].id)
    assert again.was_approved is False and d.version_row(again.version_id)["status"] == "draft"


def test_an_action_reads_the_cards_once_not_twice(d: CardDesk, monkeypatch):
    d.draft(KIND, "ok")
    version = d.version_id(KIND)
    unit = d.card(KIND).units[0].id
    reads = []
    real = d.service.view
    monkeypatch.setattr(d.service, "view", lambda aid: reads.append(aid) or real(aid))

    answered = d.post(d.unit_path(KIND, unit, "edit"), {"version": version, "text": "I planned."})

    assert answered.status_code == 200 and len(reads) == 1


# --- the empty states -----------------------------------------------------------------------------


def test_a_card_whose_paragraphs_were_all_removed_does_not_blame_the_facts(d: CardDesk):
    d.draft(KIND, "ok")
    version = d.version_id(KIND)
    for _ in range(len(d.card(KIND).units)):
        version = d.service.remove_unit(d.aid, KIND, version, d.card(KIND).units[0].id)

    card = d.card(KIND)
    assert card.state == "empty" and card.removed_all is True
    page = get(d.r, d.path()).text
    assert "You removed every paragraph. Draft again to get a new one." in said_text(
        by_id(page, f"card-{KIND}-removed").text
    )
    assert not has_id(page, f"card-{KIND}-empty") and not has_id(page, f"card-{KIND}-sessions")
    assert EMPTY_TEXT not in said_text(by_id(page, f"card-{KIND}").text)


def test_a_card_the_model_left_empty_keeps_the_facts_message_and_is_not_removed_all(d: CardDesk):
    d.draft(KIND, "empty")

    assert d.card(KIND).removed_all is False
    assert EMPTY_TEXT in said_text(get(d.r, d.path()).text)


# --- the salary card -----------------------------------------------------------------------------


def test_removing_a_salary_line_keeps_the_card_approved_by_code(d: CardDesk):
    d.set_minimum(60000)
    d.set_posting_pay(55000, 65000)
    v1 = d.service.build_salary(d.aid)
    first = d.card("salary").units[0].id

    answered = d.post(d.unit_path("salary", first, "remove"), {"version": v1})

    assert answered.status_code == 200
    assert announced(answered) == ["Line removed. The salary card stays approved."]
    v2 = d.version_id("salary")
    row = d.version_row(v2)
    assert (row["status"], row["origin"], row["parent_version_id"]) == ("approved", "code", v1)
    card = d.card("salary")
    assert card.state == "approved" and card.approved is True
    assert [u.text for u in card.units] == ["The posting states 55,000 to 65,000 EUR per year."]
    # Rebuilding brings the removed line back, as a new approved version.
    v3 = d.service.build_salary(d.aid, version=v2)
    assert d.version_row(v3)["status"] == "approved" and len(d.card("salary").units) == 2


def test_removing_every_salary_line_leaves_a_card_that_can_be_rebuilt(d: CardDesk):
    d.set_minimum(60000)
    v = d.service.build_salary(d.aid)
    for _ in range(len(d.card("salary").units)):
        v = d.service.remove_unit(d.aid, "salary", v, d.card("salary").units[0].id)

    page = get(d.r, d.path()).text
    assert by_id(page, "card-salary").attrs["data-state"] == "empty"
    assert "You removed every line. Rebuild the card to get it back." in said_text(page)
    assert has_id(page, "card-salary-build")
    rebuilt = d.service.build_salary(d.aid, version=v)
    assert d.card("salary").state == "approved" and d.version_row(rebuilt)["status"] == "approved"


def test_the_salary_card_says_it_is_approved_when_built_and_kept_locally_and_links_settings(
    d: CardDesk,
):
    page = get(d.r, d.path()).text

    hint = said_text(by_id(page, "card-salary-hint").text)
    assert "approved when it is built" in hint and "local database" in hint
    assert re.search(r'<a [^>]*href="/settings"[^>]*>', tag_block(page, "card-salary-hint"))
    intro = said_text(by_id(page, "cards-use").text)
    assert "approved when it is built" in intro and "local database" in intro


def tag_block(page: str, element_id: str) -> str:
    found = re.search(rf'<p[^>]*id="{element_id}"[^>]*>.*?</p>', page, re.S)
    assert found is not None, element_id
    return found.group(0)


def test_the_refusal_to_build_the_salary_card_links_to_settings(d: CardDesk):
    d.set_minimum(None)

    refused = d.post(d.path("/salary"), {"version": ""})

    assert refused.status_code == 409
    assert re.search(r'<a [^>]*id="card-salary-settings"[^>]*href="/settings"', refused.text)


# --- messages inside the card ---------------------------------------------------------------------


def test_a_refusal_is_shown_inside_its_card_as_an_alert_and_not_announced_twice(d: CardDesk):
    d.draft(KIND, "adversarial_uncited_paragraph")
    version = d.version_id(KIND)

    refused = d.post(d.path(f"/{KIND}/approve"), {"version": version})

    assert refused.status_code == 409
    message = by_id(refused.text, f"card-{KIND}-message")
    assert message.attrs.get("role") == "alert" and "1 paragraph needs your check." in message.text
    assert message.within(by_id(refused.text, f"card-{KIND}"))
    assert not has_id(refused.text, "cards-message"), "not also at the top of the page"
    assert announced(refused) == [], "the alert announces it"
    heading = refused.text.index(f'id="card-{KIND}-heading"')
    assert heading < refused.text.index(f'id="card-{KIND}-message"')


def test_a_success_is_shown_inside_its_card_and_announced_once(d: CardDesk):
    d.draft(KIND, "ok")

    done = d.post(d.path(f"/{KIND}/approve"), {"version": d.version_id(KIND)})

    message = by_id(done.text, f"card-{KIND}-message")
    assert "role" not in message.attrs and "Card approved." in message.text
    assert announced(done) == ["Card approved."]
    assert not has_id(done.text, "cards-message")


def test_a_page_level_refusal_stays_at_the_top(d: CardDesk):
    stale = d.post(d.path(f"/{KIND}/generate"), {"version": "no-such-version"})

    assert stale.status_code == 409
    assert "Reload" in stale.text and has_id(stale.text, "cards-conflict")


# --- the page -------------------------------------------------------------------------------------


def test_the_refresh_button_asks_for_the_page_without_the_silent_flag(d: CardDesk):
    page = get(d.r, d.path()).text

    note = re.search(r"<p data-stale-note.*?</p>", page, re.S)
    assert note is not None
    assert f'hx-get="{d.path()}"' in note.group(0) and "refresh=1" not in note.group(0)


def test_the_chosen_angle_is_named_or_the_page_links_to_choose_one(d: CardDesk):
    none = get(d.r, d.path()).text
    assert "No angle chosen yet" in said_text(by_id(none, "cards-angle").text)
    assert re.search(
        rf'<a [^>]*id="cards-angle-link"[^>]*href="/applications/{d.aid}'
        r'\?tab=tailoring&amp;step=2"',
        none,
    )

    d.set_angle()
    chosen = get(d.r, d.path()).text
    assert f"Angle: {ANGLE_NAME}" in said_text(by_id(chosen, "cards-angle").text)
    assert not has_id(chosen, "cards-angle-link")


def test_the_reuse_hint_names_the_angle_and_is_linked_to_its_button(d: CardDesk):
    d.set_angle()
    d.approved(KIND)
    other = d.another(OTHER_JOB)

    page = get(d.r, d.path("", other)).text

    hint = by_id(page, f"card-{KIND}-use-hint")
    assert f"same angle ({ANGLE_NAME})" in hint.text
    assert f"card-{KIND}-use-hint" in tag(page, f"card-{KIND}-use")


def test_every_card_action_names_its_card_heading_as_a_description(d: CardDesk):
    d.draft(KIND, "ok")
    d.set_minimum(60000)
    d.service.build_salary(d.aid)
    page = get(d.r, d.path()).text

    for button, heading in (
        (f"card-{KIND}-generate", f"card-{KIND}-heading"),
        (f"card-{KIND}-approve", f"card-{KIND}-heading"),
        ("card-salary-build", "card-salary-heading"),
        ("card-why_leaving-generate", "card-why_leaving-heading"),
    ):
        described = re.search(r'aria-describedby="([^"]*)"', tag(page, button))
        assert described is not None and heading in described.group(1).split(), button


def test_generate_has_its_own_short_hint_instead_of_the_long_intro(d: CardDesk):
    page = get(d.r, d.path()).text

    for kind in KINDS:
        described = re.search(r'aria-describedby="([^"]*)"', tag(page, f"card-{kind}-generate"))
        assert described is not None
        ids = described.group(1).split()
        assert f"card-{kind}-generate-hint" in ids and "cards-use" not in ids
        assert has_id(page, f"card-{kind}-generate-hint")


def test_the_page_has_an_index_of_links_to_the_seven_cards(d: CardDesk):
    page = get(d.r, d.path()).text

    nav = re.search(r'<nav[^>]*id="cards-index"[^>]*>(.*?)</nav>', page, re.S)
    assert nav is not None
    assert re.findall(r'href="#(card-[a-z_]+)"', nav.group(1)) == [
        *(f"card-{k}" for k in KINDS),
        "card-salary",
    ]
    assert page.index('id="cards-index"') < page.index('id="card-tell_me_about_yourself"')


def test_card_actions_swap_only_their_own_card_so_an_open_edit_elsewhere_survives(d: CardDesk):
    d.draft(KIND, "ok")
    d.draft("strengths", "ok_strengths")
    d.set_minimum(60000)
    page = get(d.r, d.path()).text
    unit = d.card(KIND).units[0].id
    own = {
        f'action="{d.unit_path(KIND, unit, "edit")}"': KIND,
        f'action="{d.path(f"/{KIND}/approve")}"': KIND,
        'action="' + d.path("/salary") + '"': "salary",
    }

    for needle, kind in own.items():
        form = re.search(rf"<form [^>]*{re.escape(needle)}[^>]*>", page)
        assert form is not None, needle
        assert f'hx-target="#card-{kind}"' in form.group(0), needle
        assert f'hx-select="#card-{kind}"' in form.group(0), needle
        assert 'hx-select-oob="#cards-status"' in form.group(0), needle
    generate = tag(page, f"card-{KIND}-generate-form")
    assert 'hx-target="#prep"' in generate, "a budget question needs the whole region"
    assert has_id(page, "cards-status")


def test_a_stale_edit_puts_what_was_typed_in_a_read_only_box(d: CardDesk):
    d.draft(KIND, "ok")
    unit = d.card(KIND).units[0].id

    stale = d.post(
        d.unit_path(KIND, unit, "edit"), {"version": "no-such-version", "text": "My typed words."}
    )

    assert stale.status_code == 409
    box = by_id(stale.text, f"card-{KIND}-recovered")
    assert "readonly" in box.attrs and "My typed words." in box.text
    assert box.within(by_id(stale.text, f"card-{KIND}"))
    assert "Your change was not saved" in said_text(stale.text)


def test_a_stale_action_without_text_has_no_recovery_box(d: CardDesk):
    d.draft(KIND, "ok")

    stale = d.post(d.path(f"/{KIND}/approve"), {"version": "no-such-version"})

    assert stale.status_code == 409 and not has_id(stale.text, f"card-{KIND}-recovered")


def test_a_nul_in_the_posted_text_is_not_echoed(d: CardDesk):
    d.draft(KIND, "ok")
    version = d.version_id(KIND)
    unit = d.card(KIND).units[0].id

    refused = d.post(d.unit_path(KIND, unit, "edit"), {"version": version, "text": "bad \x00 text"})

    assert refused.status_code == 422
    assert "\x00" not in refused.text and "bad  text" in refused.text.replace("\x00", "")


def test_the_needs_recheck_words_describe_the_real_path(d: CardDesk):
    d.draft(KIND, "ok")
    d.approve(KIND)
    d.withdraw("never_use", 1)

    page = get(d.r, d.path()).text

    words = said_text(by_id(page, f"card-{KIND}-recheck").text)
    assert "new draft" in words and "approve it again" in words
    assert "automatically" not in words
    assert not has_id(page, f"card-{KIND}-approve"), "the approved version itself can't be approved"


def test_the_tasks_indicator_announces_a_finished_card_so_the_card_adds_no_second_message(
    d: CardDesk,
):
    d.draft(KIND, "ok")

    indicator = d.r.client.get("/tasks/indicator", headers=d.r.run.run.htmx())

    assert indicator.status_code == 200
    assert "Draft narrative card finished." in indicator.headers["HX-Trigger"]


# --- reading writes nothing -----------------------------------------------------------------------


def test_other_application_and_unknown_gets_start_no_request_and_write_nothing(d: CardDesk):
    d.draft(KIND, "ok")
    other = d.another(OTHER_JOB)
    before, requests = snapshot(d.r), len(d.r.run.spy.requests)

    assert get(d.r, d.path("", other)).status_code == 200
    for aid in ("no-such-application", "bad%20id"):
        assert get(d.r, f"/applications/{aid}/prep/cards").status_code == 404

    assert snapshot(d.r) == before and len(d.r.run.spy.requests) == requests


def test_a_copy_for_a_stale_version_still_says_so(d: CardDesk):
    d.set_angle()
    source = d.approved(KIND)
    other = d.another(OTHER_JOB)
    first = d.service.use_card(other, KIND, source)

    with pytest.raises(VersionConflict):
        d.service.use_card(other, KIND, source)
    assert d.version_id(KIND, other) == first and P1 and P2
