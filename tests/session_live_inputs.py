"""The requests the app builds for the two session tasks, from fictional data, and the check that
a generated question is open (M7.10; AC-M7.2). Shared by the opt-in live test
(``tests/live/test_m7_live.py``) and by the offline control that proves these inputs build and the
check works (``tests/integration/test_m7_cross_cutting.py``).

``built_inputs`` runs the app's own flow in a temporary folder against the fake provider: five
fictional items (two capture notes, two requirements asked by one saved job, one story with a
missing result). First an earlier session of one question about the stocktake note, which the
person is "not sure" about; 22 days later (its cooldown has passed) a session of five questions
in which the stocktake note comes back with its earlier wording (so ``earlier_wordings`` is not
empty in that request), is answered once, and the other four are skipped. Each request is the one
a service builds, history included: the history of a later request holds only the canned skipped
questions, because an answered item turns into a draft and leaves the history, so the answer
itself is never in a later question request (the offline control asserts this). Nothing here
touches the network, a real provider, a keychain or a real data folder.

``Allowed`` is what the app lets a question say (its ``_output_safe`` rule): the item's own
wording, the facts it may use, a skill topic, the person's earlier answers. ``question_problems``
checks the question against it, and the why line must carry no claim of its own either. An empty
result means the app would keep the question instead of replacing it with its fallback.
"""

import json
import re
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from app.domain.fact_selection import fact_text, facts_for_ai
from app.domain.facts import FactType
from app.domain.fit import Requirement
from app.domain.honesty import new_claims
from app.domain.questions import question_problems
from app.domain.structured_answer import resolve_structured_answer
from app.modules.ai import AIRequest
from app.services.facts import FactInput
from app.services.session_backlog import SessionFocus, SessionItem
from tests.integration.test_board import seeder
from tests.refine_harness import Refining, refining
from tests.tailoring_pages import seed_summary

ENRICHMENT_QUESTION = "enrichment_question"
STRUCTURE_ANSWER = "structure_answer"
QUESTION_COUNT = 5
DEADLINE = 20.0

NOTES = (
    "Ran the fictional spring stocktake at the Example Freight depot in two days.",
    "Trained two new fictional drivers on the pallet scanner.",
)
REQUIREMENTS = (
    Requirement("live-r1", "must_have", "Five years of route planning"),
    Requirement("live-r2", "key", "Experience with Terraform"),
)
STORY = (
    "The depot had no roster.",
    "Built a roster for four teams.",
)
# Related to the first note (the stocktake), so the draft made from it can be judged against it.
ANSWER = "I ran the spring stocktake at the depot with a partner and we finished it in two days."


@dataclass(frozen=True)
class Allowed:
    """What one question may say: the sources, the facts whose names are withheld, the skills."""

    sources: tuple[str, ...]
    hidden: tuple[str, ...]
    known_skills: tuple[str, ...]


@dataclass(frozen=True)
class BuiltInputs:
    questions: tuple[AIRequest, ...]
    allowed: tuple[Allowed, ...]
    structure: AIRequest
    kinds: tuple[str, ...] = ()  # the item kind each question asks about
    answer_note: str = ""  # the note the answer is about, as the app passes it when it resolves
    known_skills: tuple[str, ...] = ()


_PRESUMING = re.compile(r"^(how|what|when|where|why|which) (did|do|have|was) you\b", re.IGNORECASE)


def presumes(question: str) -> bool:
    """Whether a question about a *requirement* takes for granted that the person has done it
    ("How did you...", "You ..."). Live-only: the app does not replace such a question, but
    a requirement is something a job asks for and not something the person said, so it
    should be asked openly."""
    text = question.strip()
    return bool(_PRESUMING.match(text)) or text.lower().startswith("you ")


def open_question_problems(question: str, why: str, allowed: Allowed) -> list[str]:
    """Why the app would replace this question and why line (none: it keeps them)."""
    problems = list(
        question_problems(
            question,
            allowed.sources,
            allowed.known_skills,
            name_hidden_sources=allowed.hidden,
        )
    )
    claims = new_claims(
        why,
        allowed.sources,
        known_skills=allowed.known_skills,
        name_hidden_sources=allowed.hidden,
    )
    if claims:
        problems.append("why_new_claims")
    return problems


def allowed_for(entry: SessionItem, known_skills: tuple[str, ...]) -> Allowed:
    """The app's ``_output_safe`` sources for one backlog entry (a ``SessionItem``)."""
    facts = facts_for_ai(entry.facts)
    item = entry.item
    topic = (
        (item.target_key.removeprefix("skill:"),)
        if item.kind == "cluster" and item.target_key.startswith("skill:")
        else ()
    )
    note = (entry.payload,) if item.kind == "capture_note" else ()
    ordinary = tuple(fact_text(f) for f in facts if not f.generalize)
    return Allowed(
        (*ordinary, *topic, *note, *entry.personal_answers),
        tuple(fact_text(f) for f in facts if f.generalize),
        known_skills,
    )


def draft_problems(built: BuiltInputs, data: Any) -> list[str]:
    """Codes only: why the app's own resolution of the real structuring answer is not clean."""
    try:
        resolved = resolve_structured_answer(
            data,
            (ANSWER,),
            note=built.answer_note,
            known_skills=built.known_skills,
        )
    except Exception as error:  # a shape the schema let through; the type is enough
        return [f"unusable_{type(error).__name__}"]
    problems = [f"flag_{flag.kind}" for flag in resolved.flags]
    if resolved.skills:
        problems.append("skills_kept")
    if resolved.role_key is not None:
        problems.append("role_key")
    wording = "\n".join(part for part in resolved.content.normalized() if part)
    claims = new_claims(wording, [ANSWER, built.answer_note], known_skills=built.known_skills)
    problems.extend(f"new_claims_{claim.kind}" for claim in claims)
    return sorted(set(problems))


def seed_items(desk: Refining) -> tuple[str, ...]:
    """Five fictional items: two capture notes, two requirements one saved job asks for (one of
    them a skill the app knows) and one story with a missing result. Returns the known skills."""
    run = desk.run
    run.app.state.skills.add("Terraform")
    for n, text in enumerate(NOTES):
        run.execute(
            "INSERT INTO capture_notes(id,text,created_at,updated_at) VALUES (?,?,?,?)",
            (f"live-note-{n}", text, run.clock.now().isoformat(), run.clock.now().isoformat()),
        )
    seed = seeder(desk)
    seed.job("live-job", "Route Planner", "Harbor Kite Fictional")
    seed_summary(seed.db, "live-job", REQUIREMENTS, summary_id="summary-live-job")
    with seed.db.transaction() as conn:
        conn.execute("UPDATE jobs SET triage_state='seen' WHERE id='live-job'")
    situation, action = STORY
    desk.facts.create(FactInput(type=FactType.STORY, situation=situation, action=action))
    return tuple(skill.name for skill in run.app.state.skills.list())


def built_inputs(folder: Path) -> BuiltInputs:
    """Run the earlier session and the five-question session on fictional data and keep what the
    app asked the provider."""
    with refining(folder) as desk:
        run = desk.run
        sessions = run.app.state.sessions
        known = seed_items(desk)
        assert len(sessions.backlog()) >= QUESTION_COUNT, "five fictional items to ask about"

        # An earlier session: the person is not sure about the stocktake note. Once its cooldown
        # has passed the note is asked again, with the earlier wording sent along.
        earlier = sessions.start(SessionFocus("capture_note", "live-note-0"), 3)
        assert earlier.task is not None and earlier.session_id is not None
        run.wait_terminal(earlier.task.id)
        view = sessions.view(earlier.session_id)
        last = view.answers[-1]
        sessions.outcome(
            earlier.session_id,
            last.id,
            "not_sure",
            expected_version=last.version,
            session_version=view.session.version,
        )
        sessions.end(
            earlier.session_id, expected_version=sessions.view(earlier.session_id).session.version
        )
        run.clock.advance(timedelta(days=22))
        asked_before = [r for r in run.spy.requests if r.task == ENRICHMENT_QUESTION]
        assert len(asked_before) == 1, "the earlier session asked one question"
        assert len(sessions.backlog()) >= QUESTION_COUNT, "the five items are all due again"

        allowed: list[Allowed] = []
        kinds: list[str] = []
        entry = sessions.backlog()[0]
        allowed.append(allowed_for(entry, known))
        kinds.append(entry.item.kind)
        started = sessions.start(SessionFocus(), QUESTION_COUNT)
        assert started.task is not None and started.session_id is not None
        sid = started.session_id
        run.wait_terminal(started.task.id)
        answered_note = ""
        for position in range(1, QUESTION_COUNT + 1):
            view = sessions.view(sid)
            last = view.answers[-1]
            assert last.position == position, "one question per position"
            if NOTES[0] in entry.payload and not answered_note:  # the one answer: second schema
                answered_note = json.loads(entry.payload)["note"]
                ref = sessions.answer(
                    sid,
                    last.id,
                    ANSWER,
                    expected_version=last.version,
                    session_version=view.session.version,
                )
                assert ref.task is not None
                run.wait_terminal(ref.task.id)
            else:
                sessions.outcome(
                    sid,
                    last.id,
                    "skipped",
                    expected_version=last.version,
                    session_version=view.session.version,
                )
            if position == QUESTION_COUNT:
                break
            entry = sessions.backlog()[0]
            allowed.append(allowed_for(entry, known))
            kinds.append(entry.item.kind)
            nxt = sessions.next(sid, expected_version=sessions.view(sid).session.version)
            assert nxt.task is not None
            run.wait_terminal(nxt.task.id)
            assert len(sessions.view(sid).answers) == position + 1, "the next question was asked"
        assert answered_note, "the stocktake note was one of the five items"
        questions = tuple(r for r in run.spy.requests if r.task == ENRICHMENT_QUESTION)[1:]
        structure = [r for r in run.spy.requests if r.task == STRUCTURE_ANSWER]
        assert len(questions) == QUESTION_COUNT, len(questions)
        assert len(structure) == 1
        return BuiltInputs(
            questions, tuple(allowed), structure[0], tuple(kinds), answered_note, known
        )
