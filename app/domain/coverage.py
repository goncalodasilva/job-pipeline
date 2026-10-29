"""Deterministic requirement clusters and confirmed evidence strength (M7.1, ADR-118).

Pure, English content-word similarity. Representatives are fixed in signature order; a
bridge phrase cannot transitively join two otherwise unrelated groups. No global cache.
"""

import hashlib
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final, Literal

from app.domain.skills import normalize_skill

Strength = Literal["none", "thin", "strong"]
DICE_THRESHOLD: Final = 0.6
_WORDS: Final = re.compile(r"[^\W_]+", re.UNICODE)
_STOPWORDS: Final = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "with",
        "you",
        "your",
        "we",
        "our",
        "will",
        "have",
        "has",
        "must",
        "should",
        "required",
        "experience",
        "ability",
    }
)


@dataclass(frozen=True)
class CoverageRequirement:
    id: str
    job_id: str
    kind: str
    text: str
    job_state: str  # saved | preparing | active


@dataclass(frozen=True)
class CoverageSkill:
    name: str
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class CoverageEvidence:
    id: str
    type: str
    status: str
    privacy: str


@dataclass(frozen=True)
class CoverageLink:
    requirement_id: str
    fact: CoverageEvidence


@dataclass(frozen=True)
class CoverageData:
    requirements: tuple[CoverageRequirement, ...]
    skills: tuple[CoverageSkill, ...]
    evidence: tuple[CoverageLink, ...]


@dataclass(frozen=True)
class RequirementCluster:
    key: str
    id: str
    label: str
    requirements: tuple[CoverageRequirement, ...]


def strength(evidence: Iterable[CoverageEvidence]) -> Strength:
    usable = {
        fact.id: fact
        for fact in evidence
        if fact.status == "confirmed" and fact.privacy != "never_use"
    }
    if sum(fact.type == "story" for fact in usable.values()) >= 2:
        return "strong"
    return "thin" if usable else "none"


def _skill_patterns(skills: Iterable[CoverageSkill]) -> tuple[tuple[str, re.Pattern[str]], ...]:
    phrases = {
        (normalize_skill(phrase), normalize_skill(skill.name))
        for skill in skills
        for phrase in (skill.name, *skill.aliases)
        if normalize_skill(phrase) and normalize_skill(skill.name)
    }
    # Prefer the longest named phrase; the normalized canonical name resolves ties.
    return tuple(
        (key, re.compile(r"(?<![\w+#])" + re.escape(phrase) + r"(?![\w+#])"))
        for phrase, key in sorted(phrases, key=lambda pair: (-len(pair[0]), pair[1], pair[0]))
    )


def _skill_key(text: str, patterns: Sequence[tuple[str, re.Pattern[str]]]) -> str | None:
    return next(("skill:" + key for key, pattern in patterns if pattern.search(text)), None)


def _tokens(text: str) -> frozenset[str]:
    return frozenset(_WORDS.findall(text)) - _STOPWORDS


def _signature(words: frozenset[str], folded: str) -> str:
    # Stopword-only strings must not all become one cluster with an empty signature.
    return " ".join(sorted(words)) if words else "raw:" + folded


def cluster_requirements(
    requirements: Iterable[CoverageRequirement], skills: Iterable[CoverageSkill]
) -> tuple[RequirementCluster, ...]:
    patterns = _skill_patterns(skills)
    by_text: dict[str, list[CoverageRequirement]] = defaultdict(list)
    for requirement in requirements:
        by_text[requirement.text].append(requirement)
    grouped: dict[str, list[CoverageRequirement]] = defaultdict(list)
    signatures: dict[str, tuple[frozenset[str], list[CoverageRequirement]]] = {}
    for text, rows in by_text.items():
        folded = normalize_skill(text)
        key = _skill_key(folded, patterns)
        if key is not None:
            grouped[key].extend(rows)
        else:
            words = _tokens(folded)
            signature = _signature(words, folded)
            if signature not in signatures:
                signatures[signature] = (words, [])
            signatures[signature][1].extend(rows)

    # Inverted token index limits comparisons to representatives sharing at least a word.
    representatives: dict[str, frozenset[str]] = {}
    word_index: dict[str, set[str]] = defaultdict(set)
    for signature, (words, rows) in sorted(signatures.items()):
        candidates = {key for word in words for key in word_index[word]}
        scored = [
            (2 * len(words & representatives[key]) / (len(words) + len(representatives[key])), key)
            for key in candidates
        ]
        matches = [(similarity, key) for similarity, key in scored if similarity >= DICE_THRESHOLD]
        if matches:
            key = min(matches, key=lambda pair: (-pair[0], pair[1]))[1]
        else:
            key = "text:" + signature
            representatives[key] = words
            for word in words:
                word_index[word].add(key)
        grouped[key].extend(rows)

    result = []
    for key, members in sorted(grouped.items()):
        counts = Counter(row.text for row in members)
        label = min(counts, key=lambda wording: (-counts[wording], len(wording), wording))
        result.append(
            RequirementCluster(
                key,
                hashlib.sha256(key.encode()).hexdigest()[:12],
                label,
                tuple(
                    sorted(
                        members,
                        key=lambda row: (row.job_id, row.id, row.kind, row.text, row.job_state),
                    )
                ),
            )
        )
    return tuple(result)


def coverage_data(
    requirements: tuple[CoverageRequirement, ...],
    skills: tuple[CoverageSkill, ...],
    citations: Iterable[CoverageLink],
    skill_facts: Iterable[tuple[str, CoverageEvidence]],
) -> CoverageData:
    """Attach skill-tagged facts to requirements by the same skill rule as clustering."""
    patterns = _skill_patterns(skills)
    facts_by_skill: dict[str, list[CoverageEvidence]] = defaultdict(list)
    for normalized_name, fact in skill_facts:
        facts_by_skill["skill:" + normalized_name].append(fact)
    matches = {
        text: _skill_key(normalize_skill(text), patterns)
        for text in {row.text for row in requirements}
    }
    links = {(link.requirement_id, link.fact.id): link for link in citations}
    for row in requirements:
        for fact in facts_by_skill.get(matches[row.text] or "", ()):
            links[row.id, fact.id] = CoverageLink(row.id, fact)
    return CoverageData(requirements, skills, tuple(links[key] for key in sorted(links)))
