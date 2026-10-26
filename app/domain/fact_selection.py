"""The one way fact context is built for an AI request (spec 3.1 item 1; AC-M3.4; ADR-010,
ADR-089 item 6). Pure.

``facts_for_ai`` keeps only the confirmed, current versions of facts that aren't marked
"never use in applications", each cited as ``F<number>@<version>``; a fact marked "generalize
names" is kept with a flag. ``prompt_fact_block`` is the code-owned text every task's facts
go through: each fact's label (with ``GENERALIZE_MARK`` when it is generalized) and what it
says, with the policy whether the request needs ``GENERALIZE_INSTRUCTION``. The prompt
renderer (``render_prompt``, ``app/modules/ai/prompts.py``) adds that instruction to the
system section itself, once; it lives only here, so no prompt file or user override can
drop it.

The fact being refined (``refine_target``) may also be a draft or a "generalize names"
fact; it is sent on its own, so its wording can be delimited as untrusted (a draft can come
from a CV), and ``prompt_fact_block(..., also_generalize=...)`` asks for the instruction
for it. Services never build a fact reference or a fact block any other way
(``tests/unit/services/test_fact_context_boundary.py``).
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final

from app.domain.facts import FactContent, FactRef, FactStatus, Privacy

GENERALIZE_MARK: Final = "(generalize names)"
GENERALIZE_INSTRUCTION: Final = (
    f"Facts marked {GENERALIZE_MARK} name an employer, client, school or person that the user "
    "doesn't want named in what is written. Use what these facts say, but write about them "
    "without those names: describe each one only by what the facts state about it (call it "
    '"a bank" only if a fact says it is a bank). Any descriptive word you add while '
    "generalizing, such as a size, a sector or a place, is a new claim: list it in "
    "added_details."
)
NO_FACTS: Final = "(none)"
_PARTS: Final = (
    ("text", "Text"),
    ("situation", "Situation"),
    ("action", "Action"),
    ("result", "Result"),
)
# Statuses a fact being refined may have: never a discarded or archived one.
_REFINABLE: Final = frozenset({FactStatus.DRAFT, FactStatus.CONFIRMED})


@dataclass(frozen=True)
class FactRecord:
    """One stored version of one fact, as a service reads it. ``role`` is a label such as
    "Operations Analyst at Example Widgets Ltd"; ``skills`` are skill names."""

    number: int
    version: int
    is_current: bool
    status: FactStatus
    privacy: Privacy
    content: FactContent
    role: str | None = None
    skills: tuple[str, ...] = ()


@dataclass(frozen=True)
class AIFact:
    """One fact as an AI request may hold it. ``generalize``: write about it without names."""

    ref: FactRef
    content: FactContent
    role: str | None
    skills: tuple[str, ...]
    generalize: bool


@dataclass(frozen=True)
class FactPromptBlock:
    """Immutable code-owned policy and text. Rendering the text is optional; the prompt
    renderer always enforces ``generalize`` in its system notice, even under overrides."""

    text: str
    generalize: bool

    def __str__(self) -> str:
        return self.text


def _as_ai_fact(record: FactRecord) -> AIFact:
    return AIFact(
        ref=FactRef(record.number, record.version),
        content=record.content,
        role=record.role,
        skills=tuple(record.skills),
        generalize=record.privacy == Privacy.GENERALIZE,
    )


def facts_for_ai(records: Iterable[FactRecord]) -> tuple[AIFact, ...]:
    """The records an AI request may hold, in the given order, each fact version once: only
    confirmed, current versions of facts that aren't "never use"."""
    chosen: dict[FactRef, AIFact] = {}
    for record in records:
        usable = (
            record.is_current
            and record.status == FactStatus.CONFIRMED
            and record.privacy != Privacy.NEVER_USE
        )
        if usable:
            fact = _as_ai_fact(record)
            chosen.setdefault(fact.ref, fact)
    return tuple(chosen.values())


def worded_then(
    number: int,
    version: int,
    content: FactContent,
    role: str | None,
    skills: tuple[str, ...],
    *,
    generalize: bool,
) -> AIFact:
    """One stored version of a fact as it was worded when a request held it (its role, skills
    and privacy are the fact's own, as now). For checking an edited line against the facts a
    document was generated from; it never builds a request."""
    return AIFact(FactRef(number, version), content, role, skills, generalize)


def refine_target(record: FactRecord) -> AIFact | None:
    """The fact to refine as a request may hold it: a current version of a draft or a
    confirmed fact that isn't "never use"; None for anything else (ADR-089 item 6)."""
    if not record.is_current or record.privacy == Privacy.NEVER_USE:
        return None
    if record.status not in _REFINABLE:
        return None
    return _as_ai_fact(record)


def fact_label(fact: AIFact) -> str:
    """How a request names one fact: its reference, and the mark when it is generalized."""
    return f"{fact.ref} {GENERALIZE_MARK}" if fact.generalize else str(fact.ref)


def fact_text(fact: AIFact) -> str:
    """What one fact says, without its reference: its role, its skills and each part of its
    wording on its own line. Every ``<`` is written ``&lt;``, so the text can't open a tag."""
    lines = []
    if fact.role:
        lines.append(f"Role: {fact.role}")
    if fact.skills:
        lines.append(f"Skills: {', '.join(fact.skills)}")
    for name, label in _PARTS:
        part = getattr(fact.content, name)
        if part:
            lines.append(f"{label}: {part}")
    return "\n".join(lines).replace("<", "&lt;")


def prompt_fact_block(facts: Sequence[AIFact], *, also_generalize: bool = False) -> FactPromptBlock:
    """The facts as a prompt shows them: each one's label, then what it says (without
    facts, ``NO_FACTS``), with the policy the prompt renderer enforces whatever its
    template: ``generalize`` when any fact here is generalized or when ``also_generalize``
    says a fact sent elsewhere in the same request is (the one being refined)."""
    entries = [f"[{fact_label(fact)}]\n{fact_text(fact)}" for fact in facts]
    body = "\n\n".join(entries) if entries else NO_FACTS
    return FactPromptBlock(body, also_generalize or any(fact.generalize for fact in facts))


__all__ = [
    "GENERALIZE_INSTRUCTION",
    "GENERALIZE_MARK",
    "NO_FACTS",
    "AIFact",
    "FactPromptBlock",
    "FactRecord",
    "fact_label",
    "fact_text",
    "facts_for_ai",
    "prompt_fact_block",
    "refine_target",
    "worded_then",
]
