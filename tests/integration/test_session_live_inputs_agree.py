"""The live test's own judge of a question agrees with the app's (M7.10; AC-M7.2).

``open_question_problems`` and ``allowed_for`` in ``tests/session_live_inputs.py`` restate the
rule the app applies in ``SessionService`` (``_output_safe``) before it keeps a generated question.
If the restatement drifted, the live test would pass questions the app replaces, or fail ones it
keeps. Here both are run on the same questions and the same five backlog entries, and must give
the same outcome; and each ``allowed[i]`` of the built inputs must belong to the request ``i`` it is
used for (its note, topic or fact text is in that request, and not in the others).
"""

from collections.abc import Iterator
from pathlib import Path

import pytest

from app.services.session_backlog import SessionItem
from app.services.sessions import _output_safe
from tests.refine_harness import Refining, refining
from tests.session_live_inputs import (
    NOTES,
    QUESTION_COUNT,
    allowed_for,
    built_inputs,
    open_question_problems,
    seed_items,
)

pytestmark = pytest.mark.integration

CLEAN = "Tell me about an experience you would like to keep."
CLEAN_WHY = "Practice data: this is a fictional example question."
LONG = "Tell me about " + "an experience you would like to keep " * 12 + "."
# 'keep_on' says where the app keeps the question (everywhere, or only on an item whose label,
# note or topic holds the words); None for a question every item replaces.
CASES: tuple[tuple[str, str, str, str | None], ...] = (
    ("clean", CLEAN, CLEAN_WHY, "everywhere"),
    ("yes/no opener", "Did you plan the routes?", CLEAN_WHY, None),
    ("tag ending", "You planned the routes, right?", CLEAN_WHY, None),
    ("new name not in sources", "How did you work with Globex Corporation?", CLEAN_WHY, None),
    ("topic echoed", "What did you do with Terraform?", CLEAN_WHY, "terraform"),
    ("note echoed", "What happened at the Example Freight depot?", CLEAN_WHY, "Example Freight"),
    # Length is the schema's rule (the live test checks the schema), not the keep-or-replace rule.
    ("over 300 characters", LONG, CLEAN_WHY, "everywhere"),
    ("why with a new number", CLEAN, "Because you saved 90 percent last year.", None),
    ("why with a new name", CLEAN, "Because Globex Corporation asked for it.", None),
)


@pytest.fixture
def entries(tmp_path: Path) -> Iterator[tuple[Refining, tuple[SessionItem, ...], tuple[str, ...]]]:
    with refining(tmp_path) as desk:
        known = seed_items(desk)
        backlog = desk.run.app.state.sessions.backlog()
        assert len(backlog) == QUESTION_COUNT
        yield desk, backlog, known


def test_the_five_entries_cover_every_source_shape(entries: tuple[Refining, ...]) -> None:
    _, backlog, _ = entries
    assert sorted(e.item.kind for e in backlog) == [
        "capture_note",
        "capture_note",
        "cluster",
        "cluster",
        "quality_hint",
    ]


@pytest.mark.parametrize(("label", "question", "why", "keep_on"), CASES, ids=[c[0] for c in CASES])
def test_live_judge_and_the_app_keep_or_replace_alike(
    entries: tuple[Refining, tuple[SessionItem, ...], tuple[str, ...]],
    label: str,
    question: str,
    why: str,
    keep_on: str | None,
) -> None:
    _, backlog, known = entries
    kept_somewhere = False
    for entry in backlog:
        app_keeps = _output_safe(question, why, entry, known)
        live_keeps = open_question_problems(question, why, allowed_for(entry, known)) == []
        assert live_keeps == app_keeps, (label, entry.item.kind, entry.label)
        expected = keep_on == "everywhere" or (
            keep_on is not None and keep_on.lower() in (entry.payload + entry.label).lower()
        )
        assert app_keeps == expected, (label, entry.item.kind, entry.label)
        kept_somewhere = kept_somewhere or app_keeps
    assert kept_somewhere == (keep_on is not None), label


def test_each_allowed_belongs_to_the_request_it_is_used_for(tmp_path: Path) -> None:
    built = built_inputs(tmp_path)
    assert len(built.allowed) == len(built.questions) == QUESTION_COUNT
    for i, allowed in enumerate(built.allowed):
        user = built.questions[i].user.lower()
        for source in allowed.sources:
            # the facts block is JSON text, so the line breaks of a fact are escaped there
            parts = [part for line in source.split("\n") for part in line.split("\\n") if part]
            assert parts, i
            for part in parts:
                assert part.lower() in user, (i, built.kinds[i])
                for j, other in enumerate(built.questions):
                    if j != i:
                        assert part.lower() not in other.user.lower(), (i, j)
        assert allowed.known_skills == built.known_skills
    notes = [i for i, kind in enumerate(built.kinds) if kind == "capture_note"]
    assert {NOTES[0] in built.allowed[i].sources[0] for i in notes} == {True, False}
    assert any(not a.sources for a in built.allowed), "the plain requirement has no source text"


def test_a_question_over_300_characters_is_the_schemas_to_refuse(tmp_path: Path) -> None:
    """Length is not in the keep-or-replace rule, so both judges keep it; the schema (which the
    live test checks first) is what refuses it."""
    from jsonschema import Draft202012Validator

    built = built_inputs(tmp_path)
    assert len(LONG) > 300
    schema = Draft202012Validator(built.questions[0].output_schema)
    assert list(schema.iter_errors({"question": LONG, "why": CLEAN_WHY}))
    assert not list(schema.iter_errors({"question": CLEAN, "why": CLEAN_WHY}))
