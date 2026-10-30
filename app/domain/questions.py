"""English open-question guard (M7.4). Sources are data, never instructions."""

import re
from collections.abc import Iterable
from typing import Final

from app.domain.honesty import new_claims

_YES_NO: Final = re.compile(r"^(did|were|was|have|has|is|are|do|does|can|could|would)\b", re.I)
_TAG: Final = re.compile(r"\b(right|correct|didn['\u2019]t you|isn['\u2019]t it)\s*\?\s*$", re.I)
_INVITATION: Final = re.compile(r"^(tell me about|walk me through)\b", re.I)


def question_problems(
    question: str,
    sources: Iterable[str | None],
    known_skills: Iterable[str] = (),
    *,
    name_hidden_sources: Iterable[str | None] = (),
) -> tuple[str, ...]:
    wording = question.strip()
    problems = []
    if new_claims(
        wording, sources, known_skills=known_skills, name_hidden_sources=name_hidden_sources
    ):
        problems.append("new_claims")
    if _YES_NO.search(wording):
        problems.append("yes_no")
    if _TAG.search(wording):
        problems.append("tag")
    if not wording or (not wording.endswith("?") and not _INVITATION.search(wording)):
        problems.append("form")
    return tuple(problems)


def fallback_question(kind: str, label: str) -> tuple[str, str]:
    if kind == "capture_note":
        return (
            "Tell me about the experience in your note.",
            "Your note may hold an experience you want to keep.",
        )
    if kind == "quality_hint":
        return (
            "Tell me about your own part in this experience.",
            "A little more detail may strengthen this fact.",
        )
    return (
        "Tell me about an experience related to this requirement.",
        "Saved jobs ask for this experience.",
    )
