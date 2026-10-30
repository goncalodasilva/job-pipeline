"""Resolve session drafts against this item's own allowed sources (ADR-117)."""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.domain.fact_selection import AIFact, fact_text
from app.domain.facts import FactContent, FactType
from app.domain.honesty import new_claims
from app.domain.skills import normalize_skill

MISSING_PARTS = ("result", "number", "scale", "own_part", "context")


def _names_skill(text: str, skill: str) -> bool:
    # Same punctuation-aware boundary as coverage: C, C++ and C# are distinct.
    name = normalize_skill(skill)
    return bool(
        name and re.search(r"(?<![\w+#])" + re.escape(name) + r"(?![\w+#])", normalize_skill(text))
    )


@dataclass(frozen=True)
class AnswerFlag:
    kind: str
    text: str
    message: str = "This isn't in your answer"


@dataclass(frozen=True)
class StructuredAnswer:
    type: FactType
    content: FactContent
    role_key: str | None
    skills: tuple[str, ...]
    missing: tuple[str, ...]
    flags: tuple[AnswerFlag, ...]

    @property
    def needs_check(self) -> bool:
        return bool(self.flags)


def resolve_structured_answer(
    data: Mapping[str, Any],
    answers: Sequence[str],
    *,
    note: str | None = None,
    target: AIFact | None = None,
    roles: Mapping[str, str] | None = None,
    generalized_roles: frozenset[str] = frozenset(),
    known_skills: Sequence[str] = (),
) -> StructuredAnswer:
    roles = roles or {}
    role = data.get("role_key")
    flags = []
    if role is not None and role not in roles:
        flags.append(AnswerFlag("unknown_role", str(role)))
        role = None
    ordinary = [*answers, note]
    hidden: list[str | None] = []
    if target:
        (hidden if target.generalize else ordinary).append(fact_text(target))
    if role:
        (hidden if role in generalized_roles else ordinary).append(roles[role])
    kind = FactType(data["type"])
    content = (
        FactContent(text=data["text"])
        if kind == FactType.FACT
        else FactContent(situation=data["situation"], action=data["action"], result=data["result"])
    )
    returned_skills = tuple(dict.fromkeys(data["skills"]))
    wording = "\n".join(p for p in content.normalized() if p)
    for claim in new_claims(
        "\n".join((wording, *returned_skills)),
        ordinary,
        known_skills=tuple(dict.fromkeys((*known_skills, *returned_skills))),
        name_hidden_sources=hidden,
    ):
        flags.append(AnswerFlag(claim.kind, claim.text))
    flags.extend(AnswerFlag("added_detail", t) for t in data["added_details"])
    own_words = "\n".join(answers)
    skills = tuple(s for s in returned_skills if _names_skill(own_words, s))
    flags.extend(AnswerFlag("skill", s) for s in returned_skills if s not in skills)
    # Supplement the shared lexical checker without changing its existing semantics.
    # An output can name a skill in prose even when it returns no skill tags.
    allowed = "\n".join(p for p in (*ordinary, *hidden) if p)
    flags.extend(
        AnswerFlag("skill", s)
        for s in known_skills
        if _names_skill(wording, s) and not _names_skill(allowed, s)
    )
    missing = tuple(p for p in MISSING_PARTS if p in data["missing"])
    return StructuredAnswer(kind, content, role, skills, missing, tuple(dict.fromkeys(flags)))
