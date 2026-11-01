"""Pure feedback on a practice answer (M8.2 part A; spec 3.1 items 5 and 6; ADR-124 items 7, 8).

The model only writes words about the answer. Everything a person could be misled by is decided
here, in code: the speaking length, the consistency with the CV that was sent, which fact ids
may be pointed at, and which claims are not in the fact bank. Nothing here applies or accepts
anything; a claim becomes a draft fact only when the person ticks it (the service does that).
"""

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, cast, get_args

from app.domain.fact_selection import AIFact, fact_text
from app.domain.honesty import Claim, ClaimKind, new_claims

WORDS_PER_MINUTE: Final = 130
SHORT_SECONDS: Final = 30
LONG_SECONDS: Final = 120
MAX_SUGGESTIONS: Final = 3
MAX_ANSWER_CHARS: Final = 4000
MAX_FLAGGED: Final = 20  # claims listed under "not in your facts" and "not in the CV sent"
MAX_WORDING_CLAIMS: Final = 20
MAX_SUGGESTION_CLAIMS: Final = 10
# Every list above is bounded, so a stored feedback stays far under the 50,000 characters the
# database allows; the domain test pins the worst case under this figure.
MAX_FEEDBACK_CHARS: Final = 45_000
MIN_REPORTED_CLAIM_CHARS: Final = 2

MODEL_CHECKS: Final = ("answers", "structure", "evidence")
CHECKS: Final = (*MODEL_CHECKS, "length", "consistent_with_cv_sent")
VERDICTS: Final = frozenset({"yes", "partly", "no"})
UNCLEAR: Final = "unclear"

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


@dataclass(frozen=True)
class CheckItem:
    verdict: str
    comment: str


@dataclass(frozen=True)
class Suggestion:
    """Advice the model wrote, with the claims it makes that no sent fact states. A suggestion
    is words to read, never applied; a flagged one is shown with the same flag as the wording."""

    text: str
    fact_ids: tuple[str, ...]
    claims: tuple[Claim, ...] = ()

    @property
    def flagged(self) -> bool:
        return bool(self.claims)


@dataclass(frozen=True)
class SuggestedWording:
    """The wording as the model wrote it, with the claims it adds to the facts. It is shown
    with its flags and never applied."""

    text: str
    claims: tuple[Claim, ...]

    @property
    def flagged(self) -> bool:
        return bool(self.claims)


@dataclass(frozen=True)
class Feedback:
    checklist: Mapping[str, CheckItem]
    seconds: int
    suggestions: tuple[Suggestion, ...]
    wording: SuggestedWording
    unsupported: tuple[Claim, ...]
    not_in_cv: tuple[Claim, ...]
    unsupported_more: int = 0  # found but not listed: the list is capped at ``MAX_FLAGGED``
    not_in_cv_more: int = 0

    @property
    def spoken(self) -> str:
        return spoken_time(self.seconds)

    @property
    def fact_refs(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(ref for item in self.suggestions for ref in item.fact_ids))

    def to_data(self) -> dict[str, Any]:
        return {
            "checklist": {
                key: {"verdict": item.verdict, "comment": item.comment}
                for key, item in self.checklist.items()
            },
            "seconds": self.seconds,
            "suggestions": [
                {
                    "text": item.text,
                    "fact_ids": list(item.fact_ids),
                    "claims": _claims_data(item.claims),
                }
                for item in self.suggestions
            ],
            "wording": {"text": self.wording.text, "claims": _claims_data(self.wording.claims)},
            "unsupported": _claims_data(self.unsupported),
            "not_in_cv": _claims_data(self.not_in_cv),
            "unsupported_more": self.unsupported_more,
            "not_in_cv_more": self.not_in_cv_more,
        }


def _claims_data(claims: Iterable[Claim]) -> list[dict[str, str]]:
    return [{"kind": claim.kind, "text": claim.text} for claim in claims]


def speaking_seconds(answer: str) -> int:
    """The answer's words at ``WORDS_PER_MINUTE``, to the nearest second (halves up)."""
    words = len(answer.split())
    return (words * 60 + WORDS_PER_MINUTE // 2) // WORDS_PER_MINUTE


def length_verdict(seconds: int) -> str:
    if seconds < SHORT_SECONDS:
        return "too_short"
    return "good" if seconds <= LONG_SECONDS else "too_long"


def spoken_time(seconds: int) -> str:
    """ "45 seconds", "1 minute", "2 minutes 30 seconds"."""
    minutes, rest = divmod(seconds, 60)
    parts = []
    if minutes:
        parts.append(f"{minutes} minute{'' if minutes == 1 else 's'}")
    if rest or not minutes:
        parts.append(f"{rest} second{'' if rest == 1 else 's'}")
    return " ".join(parts)


def _length_item(seconds: int) -> CheckItem:
    verdict = length_verdict(seconds)
    if verdict == "too_short":
        comment = f"About {spoken_time(seconds)} spoken. Aim for 30 seconds to 2 minutes."
    elif verdict == "good":
        comment = (
            f"About {spoken_time(seconds)} spoken, within the 30 seconds to 2 minutes to aim for."
        )
    else:
        comment = f"About {spoken_time(seconds)} spoken. Aim for 2 minutes or less."
    return CheckItem(verdict, comment)


def _consistency_item(cv_text: str, not_in_cv: Sequence[Claim]) -> CheckItem:
    if not cv_text.strip():
        return CheckItem(
            "not_checked", "No CV was sent for this application, so this was not checked."
        )
    if not_in_cv:
        return CheckItem(
            "no", "Your answer says things the CV you sent does not. They are listed below."
        )
    return CheckItem("yes", "Nothing in your answer goes beyond the CV you sent.")


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _texts(value: object) -> list[str]:
    return [text for item in value if (text := _text(item))] if isinstance(value, list) else []


def _model_item(value: object) -> CheckItem:
    entry = _mapping(value)
    verdict = entry.get("verdict")
    kept = verdict if isinstance(verdict, str) and verdict in VERDICTS else UNCLEAR
    return CheckItem(kept, _text(entry.get("comment")))


def _claims_against(
    text: str, facts: Sequence[AIFact], known_skills: Sequence[str], added: Sequence[str] = ()
) -> tuple[Claim, ...]:
    """What ``text`` says that no sent fact states. Only the sent facts support a claim; a
    generalized fact never supports a name."""
    return new_claims(
        text,
        [fact_text(fact) for fact in facts if not fact.generalize],
        known_skills=known_skills,
        added_details=added,
        name_hidden_sources=[fact_text(fact) for fact in facts if fact.generalize],
    )


def _suggestions(
    value: object, facts: Sequence[AIFact], known_skills: Sequence[str]
) -> tuple[Suggestion, ...]:
    sent = frozenset(str(fact.ref) for fact in facts)
    items = value if isinstance(value, list) else []
    kept: list[Suggestion] = []
    for item in items:
        entry = _mapping(item)
        text = _text(entry.get("text"))
        if not text:
            continue
        ids = entry.get("fact_ids")
        named = [ref for ref in (ids if isinstance(ids, list) else []) if isinstance(ref, str)]
        cited = tuple(dict.fromkeys(ref for ref in named if ref in sent))
        if named and not cited:
            continue  # it points only at facts that were not sent: nothing backs it
        claims = _claims_against(text, facts, known_skills)[:MAX_SUGGESTION_CLAIMS]
        kept.append(Suggestion(text, cited, claims))
        if len(kept) == MAX_SUGGESTIONS:
            break
    return tuple(kept)


def _whole_word(needle: str) -> re.Pattern[str]:
    """``needle`` (already case-folded) as whole words: not inside a longer word or number."""
    return re.compile(r"(?<!\w)" + re.escape(needle) + r"(?!\w)")


def _unsupported(
    answer: str, facts: Sequence[AIFact], reported: object, known_skills: Sequence[str]
) -> tuple[Claim, ...]:
    found = list(_claims_against(answer, facts, known_skills))
    seen = {claim.text.casefold() for claim in found}
    folded_answer = answer.casefold()
    stated = [fact_text(fact).casefold() for fact in facts]
    for text in _texts(reported):
        folded = text.casefold()
        if len(folded) < MIN_REPORTED_CLAIM_CHARS or folded in seen:
            continue
        whole = _whole_word(folded)
        if whole.search(folded_answer) and not any(whole.search(fact) for fact in stated):
            seen.add(folded)
            found.append(Claim("added_detail", text))
    return tuple(found)


def resolve_feedback(
    data: Mapping[str, Any],
    answer: str,
    sent_facts: Sequence[AIFact],
    *,
    cv_text: str,
    known_skills: Sequence[str] = (),
) -> Feedback:
    """The model's output as the person is shown it. ``sent_facts`` are the facts of the
    request; only they support a claim or may be pointed at. ``cv_text`` is the CV text sent
    ("" when none)."""
    checklist_in = _mapping(data.get("checklist"))
    seconds = speaking_seconds(answer)
    not_in_cv = new_claims(answer, [cv_text]) if cv_text.strip() else ()
    checklist = {
        **{key: _model_item(checklist_in.get(key)) for key in MODEL_CHECKS},
        "length": _length_item(seconds),
        "consistent_with_cv_sent": _consistency_item(cv_text, not_in_cv),
    }
    wording_in = _mapping(data.get("suggested_wording"))
    wording_text = _text(wording_in.get("text"))
    wording_claims = _claims_against(
        wording_text, sent_facts, known_skills, _texts(wording_in.get("added_details"))
    )
    unsupported = _unsupported(answer, sent_facts, data.get("unsupported_claims"), known_skills)
    return Feedback(
        checklist=checklist,
        seconds=seconds,
        suggestions=_suggestions(data.get("suggestions"), sent_facts, known_skills),
        wording=SuggestedWording(wording_text, wording_claims[:MAX_WORDING_CLAIMS]),
        unsupported=unsupported[:MAX_FLAGGED],
        not_in_cv=not_in_cv[:MAX_FLAGGED],
        unsupported_more=max(0, len(unsupported) - MAX_FLAGGED),
        not_in_cv_more=max(0, len(not_in_cv) - MAX_FLAGGED),
    )


def current_cv_lines(snapshot: str, current: str) -> str:
    """The lines of the CV text kept with an attempt that the CV's current text still holds
    (their edges aside); "" when none is left. A line that is gone since, such as one built on
    a fact marked never use after the CV was sent, must not be sent."""
    now = {line.strip() for line in current.splitlines()}
    kept = [line for line in snapshot.splitlines() if not line.strip() or line.strip() in now]
    text = "\n".join(kept).strip("\n")
    return text if text.strip() else ""


def fact_wordings(facts: Sequence[AIFact]) -> dict[str, str]:
    """Each sent fact's wording, short, by its reference: what a suggestion points at."""
    wordings = {}
    for fact in facts:
        content = fact.content
        words = content.text or " ".join(
            part for part in (content.situation, content.action, content.result) if part
        )
        short = " ".join(words.split())
        wordings[str(fact.ref)] = short if len(short) <= 90 else short[:89].rstrip() + "…"
    return wordings


def sentence_of(answer: str, claim: Claim) -> str:
    """The sentence of ``answer`` that holds the claim's text (case aside), stripped; "" when
    none does. A full stop inside a number or between lines does not end a sentence."""
    needle = claim.text.casefold().strip()
    if not needle:
        return ""
    sentences = [part.strip() for part in _SENTENCE_END.split(answer) if part.strip()]
    whole = _whole_word(needle)
    for sentence in sentences:
        if whole.search(sentence.casefold()):
            return sentence
    return next((s for s in sentences if needle in s.casefold()), "")


def feedback_from_data(data: Mapping[str, Any]) -> Feedback:
    """A stored ``Feedback.to_data()`` back; ``ValueError`` when it is not one."""
    try:
        checklist_in = _mapping(data["checklist"])
        checklist = {
            key: CheckItem(
                str(_mapping(checklist_in[key])["verdict"]),
                str(_mapping(checklist_in[key])["comment"]),
            )
            for key in CHECKS
        }
        wording_in = _mapping(data["wording"])
        return Feedback(
            checklist=checklist,
            seconds=int(data["seconds"]),
            suggestions=tuple(
                Suggestion(
                    str(_mapping(item)["text"]),
                    tuple(map(str, _mapping(item)["fact_ids"])),
                    _claims_from(_mapping(item).get("claims", [])),
                )
                for item in data["suggestions"]
            ),
            wording=SuggestedWording(str(wording_in["text"]), _claims_from(wording_in["claims"])),
            unsupported=_claims_from(data["unsupported"]),
            not_in_cv=_claims_from(data["not_in_cv"]),
            unsupported_more=int(data.get("unsupported_more", 0)),
            not_in_cv_more=int(data.get("not_in_cv_more", 0)),
        )
    except (KeyError, TypeError, ValueError, AttributeError):
        raise ValueError("Stored feedback is not readable.") from None


def _claims_from(value: object) -> tuple[Claim, ...]:
    if not isinstance(value, list):
        raise ValueError("claims")
    claims = []
    for item in value:
        kind = _mapping(item)["kind"]
        if kind not in get_args(ClaimKind):
            raise ValueError("claim kind")
        claims.append(Claim(cast(ClaimKind, kind), str(_mapping(item)["text"])))
    return tuple(claims)
