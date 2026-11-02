"""Deterministic interview-question reuse and source checks (M8.1, ADR-124)."""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from app.domain.coverage import CoverageRequirement, CoverageSkill, cluster_requirements
from app.domain.fit import Requirement
from app.domain.honesty import new_claims
from app.domain.similarity import similarity, words
from app.domain.web_sources import _safe_url, _source_date, _text

QUESTION_TYPES: Final = ("behavioral", "technical", "cv", "ask_them")
_WEIGHTS: Final = {"must_have": 3, "key": 2, "nice": 1}
_SENIORITY: Final = frozenset(
    {
        "senior",
        "junior",
        "lead",
        "principal",
        "staff",
        "mid",
        "level",
        "entry",
        "graduate",
        "ii",
        "iii",
        "iv",
        "sr",
        "jr",
    }
)


@dataclass(frozen=True)
class BankQuestion:
    id: str
    text: str
    type: str
    requirements: tuple[str, ...] = ()
    company_id: str | None = None
    role_family: str = ""
    seniority: str = ""
    asked_count: int = 0
    origin: str = "generated"
    source_url: str | None = None
    found_at: str | None = None
    source_date: str | None = None
    provider: str | None = None
    model: str | None = None
    prompt_id: str | None = None
    prompt_version: str | None = None
    version: int = 1


@dataclass(frozen=True)
class RankedQuestion:
    question: BankQuestion
    score: int
    requirement_ids: tuple[str, ...]


@dataclass(frozen=True)
class BankSelection:
    ranked: tuple[RankedQuestion, ...]
    already_prepared: tuple[RankedQuestion, ...]
    uncovered: tuple[Requirement, ...]


def role_family(title: str) -> str:
    return " ".join(
        word for word, count in words(title).items() for _ in range(count) if word not in _SENIORITY
    )


def rank_questions(
    questions: Iterable[BankQuestion],
    requirements: Sequence[Requirement],
    *,
    skills: Iterable[CoverageSkill] = (),
    company_id: str | None = None,
    title: str = "",
    ready_ids: Iterable[str] = (),
) -> BankSelection:
    eligible = sorted(
        (q for q in questions if q.company_id is None or q.company_id == company_id),
        key=lambda q: q.id,
    )
    ready = frozenset(ready_ids)
    source = [CoverageRequirement(r.id, "job", r.kind, r.text, "preparing") for r in requirements]
    tag_owners = {}
    for q in eligible:
        for index, text in enumerate(q.requirements):
            key = f"bank:{q.id}:{index}"
            tag_owners[key] = q.id
            source.append(CoverageRequirement(key, "bank", "key", text, "saved"))
    matches: dict[str, set[str]] = {q.id: set() for q in eligible}
    for cluster in cluster_requirements(source, skills):
        target_ids = {r.id for r in cluster.requirements if r.job_id == "job"}
        for member in cluster.requirements:
            if member.id in tag_owners:
                matches[tag_owners[member.id]].update(target_ids)
    by_id = {r.id: r for r in requirements}
    ranked = []
    for q in eligible:
        overlap = matches[q.id]
        if not (
            any(by_id[r].kind in {"must_have", "key"} for r in overlap)
            or (q.type == "behavioral" and not q.requirements)
            or q.id in ready
        ):
            continue
        family_match = bool(
            title
            and q.role_family
            and similarity(role_family(title), role_family(q.role_family)) >= 0.6
        )
        score = (
            sum(_WEIGHTS.get(by_id[r].kind, 0) for r in overlap) + q.asked_count + int(family_match)
        )
        ranked.append(RankedQuestion(q, score, tuple(sorted(overlap))))
    ranked.sort(key=lambda item: (-item.score, item.question.id))
    covered = {rid for item in ranked for rid in item.requirement_ids}
    return BankSelection(
        tuple(r for r in ranked if r.question.id not in ready),
        tuple(r for r in ranked if r.question.id in ready),
        tuple(r for r in requirements if r.kind in {"must_have", "key"} and r.id not in covered),
    )


def duplicate_question(
    candidate: BankQuestion, questions: Iterable[BankQuestion]
) -> BankQuestion | None:
    return next(
        (
            q
            for q in sorted(questions, key=lambda q: q.id)
            if q.type == candidate.type
            and q.company_id == candidate.company_id
            and similarity(q.text, candidate.text) >= 0.9
        ),
        None,
    )


@dataclass(frozen=True)
class QuestionDraft:
    text: str
    type: str
    requirement_ids: tuple[str, ...] = ()
    requirements: tuple[str, ...] = ()
    company_specific: bool = False
    source_url: str | None = None
    source_date: str | None = None


def resolve_interview_questions(
    data: Mapping[str, Any],
    requirements: Sequence[Requirement],
    *,
    job_text: str,
    cv_text: str,
    known_skills: Iterable[str] = (),
    count: int = 10,
    ask_count: int = 3,
) -> tuple[QuestionDraft, ...]:
    by_id = {r.id: r for r in requirements}
    result: list[QuestionDraft] = []
    for kind, limit in (
        ("questions", max(0, min(count, 10))),
        ("ask_them", max(0, min(ask_count, 3))),
    ):
        kept = 0
        items = data.get(kind, [])
        if not isinstance(items, list):
            continue
        for item in items[:50]:
            if kept >= limit or not isinstance(item, dict):
                continue
            text = _text(item.get("text"))
            qtype = "ask_them" if kind == "ask_them" else item.get("type")
            refs = item.get("requirement_ids", [])
            if (
                not text
                or qtype not in QUESTION_TYPES
                or (qtype == "cv" and not cv_text)
                or not isinstance(refs, list)
                or any(not isinstance(r, str) or r not in by_id for r in refs)
                or (kind == "questions" and not refs)
                or new_claims(text, (job_text, cv_text), known_skills=known_skills)
            ):
                continue
            if any(similarity(text, q.text) >= 0.9 and qtype == q.type for q in result):
                continue
            ids = tuple(dict.fromkeys(refs))
            result.append(
                QuestionDraft(
                    text,
                    qtype,
                    ids,
                    tuple(by_id[r].text for r in ids),
                    item.get("company_specific") is True,
                )
            )
            kept += 1
    return tuple(result)


def resolve_web_items(
    data: Mapping[str, Any], searched_urls: Iterable[str]
) -> tuple[QuestionDraft, ...]:
    allowed = frozenset(searched_urls)
    result: list[QuestionDraft] = []
    items = data.get("questions", [])
    if not isinstance(items, list):
        return ()
    for item in items[:50]:
        if not isinstance(item, dict):
            continue
        text, url, qtype = _text(item.get("text")), item.get("source_url"), item.get("type")
        if (
            not text
            or qtype not in QUESTION_TYPES
            or not isinstance(url, str)
            or url not in allowed
            or not _safe_url(url)
        ):
            continue
        result.append(
            QuestionDraft(
                text,
                qtype,
                company_specific=True,
                source_url=url,
                source_date=_source_date(item.get("source_date")),
            )
        )
        if len(result) >= 10:
            break
    return tuple(result)
