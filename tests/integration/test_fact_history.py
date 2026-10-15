"""A fact's history (M3.6; AC-M3.3: every edit creates a version and the history shows the diff).

``GET /facts/{id}/history`` answers the ``#fact-history`` fragment (launch token); the fact's
page (``GET /facts/{id}/edit``) holds the same section, so it is read without scripts. It lists
the fact's events and its versions, newest first. Each version has its number, its source, its
date and, for what the fake provider made, the "Practice data (fake provider)" badge; each one
after the first shows the word-level diff against the one before, as ``<ins>`` and ``<del>``
that start with the hidden words "added" and "removed", so the change is marked by text as well
as colour. A run of spaces alone is never read out as a change. The contract's names live in
``tests/facts_page.py``. Real SQLite in a temp folder, the frozen clock, the fake provider, no
network; every value is fictional.
"""

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.domain.diff import DiffOp, word_diff
from app.domain.facts import FactType, Privacy
from app.services.facts import DraftInput, FactInput
from tests.fact_bank import event_types, typed, version_rows
from tests.facts_page import (
    DIFF_ADDED,
    DIFF_END_ADDED,
    DIFF_END_REMOVED,
    DIFF_REMOVED,
    DIFF_SPACING_ONLY,
    EVENT_LABELS,
    HISTORY,
    HISTORY_EVENTS_ID,
    HISTORY_ID,
    HISTORY_VERSIONS_ID,
    PRACTICE_BADGE,
    VERSION_SOURCES,
    FactsApp,
    by_id,
    edit_url,
    facts_app,
    headings,
    history_url,
    inside,
    page_of,
    privacy_event,
    required,
    says,
    version_heading,
    version_id_attr,
)
from tests.integration.test_setup_redirect import ORIGIN, TOKEN_HEADER, Element, outline

pytestmark = pytest.mark.integration

ROUTES = "Planned 60 delivery routes a week with a team of 3."
MORE_ROUTES = "Planned 80 delivery routes a week with a team of 3."
EVERY_WEEK = "Planned 80 delivery routes every week with a team of 3 planners."
INVOICES = "Cut invoice processing time by 35% across 4 warehouses."
DEGREE = "BSc in Industrial Management, Example University, 2018"
SITUATION = "Invoices took nine days to process."
ACTION = "Mapped the steps and removed two approvals."
RESULT = "Processing fell to six days."
BETTER_RESULT = "Processing fell to four days."
HOSTILE = '"><script id="injected">window.fictional=1</script><b id="bold">Planned routes</b>'
IMPORT_A = "imp-a"


# --- helpers -----------------------------------------------------------------------------------


def history(facts: FactsApp, fact_id: str) -> list[Element]:
    """The fragment: ``#fact-history`` is its root."""
    elements = facts.fragment(history_url(fact_id))
    assert required(elements, HISTORY_ID) is elements[0], "the fragment is #fact-history"
    return elements


def both(facts: FactsApp, fact_id: str) -> tuple[list[Element], list[Element]]:
    """The history as the fragment gives it and as the fact's page holds it."""
    return history(facts, fact_id), page_of(facts, edit_url(fact_id))


def entries(elements: list[Element]) -> list[Element]:
    """The version entries in the order they are shown."""
    listing = required(elements, HISTORY_VERSIONS_ID)
    prefix = version_id_attr(0)[:-1]
    return [
        e
        for e in inside(elements, listing)
        if (e.attrs.get("id") or "").startswith(prefix)
        and (e.attrs.get("id") or "")[len(prefix) :].isdigit()
    ]


def event_lines(elements: list[Element]) -> list[str]:
    """The events in the order they are shown: the text of each list item."""
    listing = required(elements, HISTORY_EVENTS_ID)
    return [e.text for e in inside(elements, listing) if e.tag == "li"]


def marked(elements: list[Element], entry: Element, tag: str) -> list[str]:
    """The changed runs of words an entry marks with ``tag``, without the hidden word each
    starts with and the hidden words each ends with. Fails when one lacks either."""
    word = DIFF_ADDED if tag == "ins" else DIFF_REMOVED
    end = DIFF_END_ADDED if tag == "ins" else DIFF_END_REMOVED
    found = []
    for element in inside(elements, entry):
        if element.tag != tag:
            continue
        said = element.text.strip()
        assert said.lower().startswith(word), f"<{tag}> starts with the word {word!r}: {said!r}"
        assert said.endswith(end), f"<{tag}> ends with {end!r}: {said!r}"
        found.append(said[len(word) : -len(end)].lstrip(": ").strip())
    return found


def expected(old: str, new: str, op: DiffOp) -> list[str]:
    """The changed runs ``word_diff`` gives, without the ones that are only spacing."""
    return [" ".join(s.text.split()) for s in word_diff(old, new) if s.op is op and s.text.strip()]


def edit(facts: FactsApp, fact_id: str, **content: str) -> None:
    current = facts.bank.facts.get(fact_id)
    data = FactInput(
        type=current.type,
        role_id=current.role_id,
        skills=current.skills,
        tags=current.tags,
        start_date=current.start_date,
        end_date=current.end_date,
        **content,
    )
    facts.bank.facts.edit(fact_id, data, expected_version=current.version)


def next_day(facts: FactsApp) -> datetime:
    facts.run.clock.advance(timedelta(days=1))
    return facts.run.clock.now()


def instant(when: Element) -> datetime:
    """The moment a ``<time>`` names: an ISO 8601 ``datetime`` with its offset, so it is the
    same moment whatever zone it is shown in."""
    moment = datetime.fromisoformat(when.attrs.get("datetime") or "")
    assert moment.utcoffset() is not None, when.attrs
    return moment


# --- AC-M3.3 -----------------------------------------------------------------------------------


def test_ac_m3_3_history_lists_versions_and_shows_diff(tmp_path: Path) -> None:
    """Three versions of one fact: the history lists them newest first, each with its number,
    source and date, and each later one with what was added and removed, said in words."""
    with facts_app(tmp_path) as facts:
        days = [facts.run.clock.now()]
        own = typed(facts.bank, ROUTES)
        days.append(next_day(facts))
        edit(facts, own.id, text=MORE_ROUTES)
        days.append(next_day(facts))
        edit(facts, own.id, text=EVERY_WEEK)
        assert [row["version"] for row in version_rows(facts.db, own.id)] == [1, 2, 3]

        for elements in both(facts, own.id):
            section = required(elements, HISTORY_ID)
            assert HISTORY in [e.text for e in inside(elements, section) if e.tag == "h2"]
            shown = entries(elements)
            assert [e.attrs.get("id") for e in shown] == [
                version_id_attr(3),
                version_id_attr(2),
                version_id_attr(1),
            ], "newest first"
            for entry, number in zip(shown, (3, 2, 1), strict=True):
                titles = [e.text for e in inside(elements, entry) if e.tag in ("h3", "h4")]
                assert titles == [version_heading(number)]
                assert says(elements, VERSION_SOURCES["manual"], entry), "its source, in words"
                (when,) = [e for e in inside(elements, entry) if e.tag == "time"]
                assert instant(when) == days[number - 1]
                assert when.text, "the date is text too"
                assert not says(elements, PRACTICE_BADGE, entry), "the user wrote these"

            third, second, first = shown
            # The first version has nothing before it: its wording, with nothing marked.
            assert ROUTES in first.text
            assert not marked(elements, first, "ins") and not marked(elements, first, "del")
            # Later ones mark what changed against the one before, by text and by element.
            assert marked(elements, second, "ins") == expected(ROUTES, MORE_ROUTES, DiffOp.ADDED)
            assert marked(elements, second, "del") == expected(ROUTES, MORE_ROUTES, DiffOp.REMOVED)
            assert marked(elements, second, "ins") == ["80"]
            assert marked(elements, second, "del") == ["60"]
            assert marked(elements, third, "ins") == expected(MORE_ROUTES, EVERY_WEEK, DiffOp.ADDED)
            assert marked(elements, third, "del") == expected(
                MORE_ROUTES, EVERY_WEEK, DiffOp.REMOVED
            )
            assert "every" in marked(elements, third, "ins")
            # What stayed the same is there as well, so the version reads whole.
            for words in ("Planned", "delivery routes", "with a team of"):
                assert words in second.text and words in third.text

            # The events, newest first, each with its date.
            lines = event_lines(elements)
            assert len(lines) == 3
            assert EVENT_LABELS["edited"] in lines[0] and EVENT_LABELS["edited"] in lines[1]
            assert EVENT_LABELS["created"] in lines[2]
            listing = required(elements, HISTORY_EVENTS_ID)
            stamps = [instant(e) for e in inside(elements, listing) if e.tag == "time"]
            assert stamps == [days[2], days[1], days[0]]
        assert event_types(facts.db, own.id) == ["created", "edited", "edited"]


def test_history_of_a_single_version_shows_its_wording(tmp_path: Path) -> None:
    with facts_app(tmp_path) as facts:
        own = typed(facts.bank, ROUTES)
        elements = history(facts, own.id)
        (entry,) = entries(elements)
        assert entry.attrs.get("id") == version_id_attr(1) and ROUTES in entry.text
        assert not [e for e in elements if e.tag in ("ins", "del")]
        assert event_lines(elements) and EVENT_LABELS["created"] in event_lines(elements)[0]
        # The page of the fact carries the same section under the editor.
        page = page_of(facts, edit_url(own.id))
        assert HISTORY in headings(page, "h2")
        assert required(page, version_id_attr(1)).within(required(page, HISTORY_ID))


def test_story_history_diffs_each_part(tmp_path: Path) -> None:
    """A story's diff is made part by part, each under its label; a part that didn't change
    marks nothing."""
    with facts_app(tmp_path) as facts:
        story = facts.bank.facts.create(
            FactInput(type=FactType.STORY, situation=SITUATION, action=ACTION, result=RESULT)
        )
        edit(facts, story.id, situation=SITUATION, action=ACTION, result=BETTER_RESULT)
        elements = history(facts, story.id)
        second, first = entries(elements)
        for label in ("Situation", "Action", "Result"):
            assert says(elements, label, first) and says(elements, label, second), label
        assert SITUATION in first.text and ACTION in first.text and RESULT in first.text
        assert marked(elements, second, "ins") == ["four"]
        assert marked(elements, second, "del") == ["six"]
        assert SITUATION in second.text and ACTION in second.text, "unchanged parts read whole"


def test_history_spacing_only_change_is_not_announced(tmp_path: Path) -> None:
    """TD-023: ``word_diff`` yields runs that are only spacing. They are never marked as
    added or removed; a version that changed nothing else says so in words."""
    with facts_app(tmp_path) as facts:
        own = typed(facts.bank, "Planned 60 delivery routes a week.")
        spaced = "Planned 60  delivery routes\na week."
        edit(facts, own.id, text=spaced)
        edit(facts, own.id, text="Planned 60  delivery routes\nevery  week.")
        assert len(version_rows(facts.db, own.id)) == 3, "inner spacing is a new version"
        assert any(
            s.op is not DiffOp.EQUAL and not s.text.strip()
            for s in word_diff("Planned 60 delivery routes a week.", spaced)
        ), "the diff does hold spacing-only runs"

        for elements in both(facts, own.id):
            third, second, _first = entries(elements)
            for entry in (second, third):
                for tag in ("ins", "del"):
                    assert all(marked(elements, entry, tag)), "no change that is only spacing"
            assert not marked(elements, second, "ins") and not marked(elements, second, "del")
            assert says(elements, DIFF_SPACING_ONLY, second)
            assert not says(elements, DIFF_SPACING_ONLY, third)
            assert marked(elements, third, "ins") == ["every"]
            assert marked(elements, third, "del") == ["a"]


def test_history_marks_practice_data_and_sources(tmp_path: Path) -> None:
    """ADR-089 item 8: a version the fake provider made says "Practice data (fake provider)"
    in the history, also after the user confirmed and reworded the fact; the user's own
    version doesn't."""
    with facts_app(tmp_path) as facts:
        facts.add_import(IMPORT_A)
        (draft,) = facts.drafts(IMPORT_A, DraftInput(text=DEGREE, original_answer=DEGREE))
        facts.bank.facts.confirm(draft.id, expected_version=draft.version)
        edit(facts, draft.id, text="BSc in Industrial Management, Example University")

        for elements in both(facts, draft.id):
            second, first = entries(elements)
            assert says(elements, PRACTICE_BADGE, first)
            assert says(elements, VERSION_SOURCES["cv_import"], first)
            assert not says(elements, PRACTICE_BADGE, second)
            assert says(elements, VERSION_SOURCES["manual"], second)
            assert "2018" in " ".join(marked(elements, second, "del"))
            lines = event_lines(elements)
            assert [
                next(label for label in EVENT_LABELS.values() if label in line) for line in lines
            ] == [EVENT_LABELS["edited"], EVENT_LABELS["confirmed"], EVENT_LABELS["created"]]


def test_history_shows_merge_and_privacy_events(tmp_path: Path) -> None:
    """Status, privacy and merge changes are events in the history; a merge and its undo are
    versions too, each with its source."""
    with facts_app(tmp_path) as facts:
        target = typed(facts.bank, ROUTES)
        facts.add_import(IMPORT_A)
        (draft,) = facts.drafts(IMPORT_A, DraftInput(text=MORE_ROUTES, original_answer=ROUTES))
        seen = facts.bank.facts.set_privacy(
            target.id, Privacy.NEVER_USE, expected_version=target.version
        )
        merged = facts.bank.facts.merge(
            draft.id,
            target.id,
            draft.content,
            expected_draft_version=draft.version,
            expected_target_version=seen.version,
        )

        for elements in both(facts, target.id):
            second, first = entries(elements)
            assert says(elements, VERSION_SOURCES["merge"], second)
            assert says(elements, PRACTICE_BADGE, second), (
                "ADR-091 item 5: taken as the fake made it"
            )
            assert not says(elements, PRACTICE_BADGE, first)
            assert marked(elements, second, "ins") == ["80"]
            lines = event_lines(elements)
            assert EVENT_LABELS["merged"] in lines[0]
            assert privacy_event("never_use") in lines[1]
            assert EVENT_LABELS["created"] in lines[2]
        # The draft's own history says where it went; its wording is not a new version.
        elements = history(facts, draft.id)
        assert EVENT_LABELS["merged_into"] in event_lines(elements)[0]
        assert len(entries(elements)) == 1

        closed = facts.bank.facts.get(draft.id)
        facts.bank.facts.undo_merge(
            draft.id, expected_draft_version=closed.version, expected_target_version=merged.version
        )
        elements = history(facts, target.id)
        third, _second, _first = entries(elements)
        assert says(elements, VERSION_SOURCES["merge_undo"], third)
        assert marked(elements, third, "ins") == ["60"] and marked(elements, third, "del") == ["80"]
        assert EVENT_LABELS["merge_undone"] in event_lines(elements)[0]

        # Archive, restore and a privacy change back are listed too, newest first.
        current = facts.bank.facts.get(target.id)
        current = facts.bank.facts.set_privacy(
            target.id, Privacy.GENERALIZE, expected_version=current.version
        )
        current = facts.bank.facts.archive(target.id, expected_version=current.version)
        facts.bank.facts.restore(target.id, expected_version=current.version)
        lines = event_lines(history(facts, target.id))
        assert EVENT_LABELS["restored"] in lines[0]
        assert EVENT_LABELS["archived"] in lines[1]
        assert privacy_event("generalize") in lines[2]
        assert len(lines) == len(event_types(facts.db, target.id)), "every event is listed"
        # A discarded draft's history can still be read.
        gone = facts.bank.facts.get(draft.id)
        facts.bank.facts.discard(draft.id, expected_version=gone.version)
        assert EVENT_LABELS["discarded"] in event_lines(history(facts, draft.id))[0]


def test_history_escapes_hostile_text(tmp_path: Path) -> None:
    """Wording is the user's or AI-made: escaped in the full text and inside the diff."""
    with facts_app(tmp_path) as facts:
        own = typed(facts.bank, HOSTILE)
        edit(facts, own.id, text=HOSTILE + ' <img src=x id="injected" onerror="fictional()">')
        edit(facts, own.id, text="Planned routes")
        for elements in both(facts, own.id):
            assert by_id(elements, "injected") is None and by_id(elements, "bold") is None
            assert not [
                e
                for e in elements
                if e.tag in ("script", "img") and e.within(required(elements, HISTORY_ID))
            ]
            third, second, first = entries(elements)
            assert "<script" in first.text, "shown as the text it is"
            assert any("onerror" in run for run in marked(elements, second, "ins"))
            assert marked(elements, third, "del")


# --- security ----------------------------------------------------------------------------------


def test_history_fragment_needs_token(tmp_path: Path) -> None:
    """The history is a fragment route, not a page: no token, no history, with or without
    ``HX-Request``; with the token it answers the fragment only and changes nothing."""
    with facts_app(tmp_path) as facts:
        own = typed(facts.bank, ROUTES)
        edit(facts, own.id, text=MORE_ROUTES)
        client = facts.run.client
        before = facts.snapshot()
        token = facts.token
        refused: tuple[dict[str, str], ...] = (
            {},
            {"HX-Request": "true"},
            {"origin": ORIGIN},
            {TOKEN_HEADER: ""},
            {TOKEN_HEADER: "x" * len(token)},
            {TOKEN_HEADER: token[:-1]},
        )
        for headers in refused:
            response = client.get(history_url(own.id), headers=headers)
            assert response.status_code == 403, (headers, response.status_code)
            assert "delivery routes" not in response.text and token not in response.text
        # The token is a header, never part of the address.
        assert client.get(history_url(own.id), params={"token": token}).status_code == 403

        for headers in (facts.run.run.with_token(), facts.run.run.htmx()):
            response = client.get(history_url(own.id), headers=headers)
            assert response.status_code == 200, response.status_code
            assert "<html" not in response.text and token not in response.text
            assert response.headers.get("cache-control") == "no-store"
            elements = outline(response.text)
            assert required(elements, HISTORY_ID) is elements[0]
            assert not headings(elements, "h1"), "a fragment has no page heading"
        assert client.post(history_url(own.id), headers=facts.run.run.action()).status_code == 405
        assert facts.snapshot() == before
