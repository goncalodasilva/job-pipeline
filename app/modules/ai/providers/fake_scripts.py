"""Fictional answers the fake provider gives to real AI tasks (M3.4; ADR-088; ADR-089 item 8).

Wiring hands these to ``FakeAIProvider.script_keywords`` only for a fake that may answer real
tasks; nothing here runs by itself. A fictional keyword in the task's text chooses the answer,
so the demo and the tests can ask for each behaviour without any switch in the app. All
content is about the invented "Alex Example" of ``tests/fixtures/cv/`` and matches those
files' text; ``tests/fixtures/ai/<task>/`` holds the same answers as JSON and a test keeps
the two equal. Text without a keyword gets the well-behaved answer, whatever CV it is, so its
quotes are found only in the fictional CVs.

``extract_cv`` answers:

- ``ok``: two roles, a story, three facts and a skill; every unit's wording is taken word for
  word from its own quote, and every quote, role and skill is in the CV.
- ``malformed``: text that is not JSON (the first call and its retry both fail).
- ``adversarial_employer_number``: a role with an employer the CV never names, one unit under
  it with inflated numbers and one with faithful wording; the import must mark both
  ``needs_check``.
- ``adversarial_quote``: a unit whose quote is nowhere in the CV; ``needs_check``.
- ``adversarial_moved_number``: a real quote, with numbers the CV states in another line.
- ``adversarial_invented_words``: a real quote and its numbers, plus a tool and an employer
  the quote doesn't hold.
- ``adversarial_skill``: a faithful unit with a skill the CV never names; the unit is marked
  and the skill is not kept.
- ``injection_obeyed``: what a model that followed text planted in the CV could return inside
  the schema: the planted claim as one more unit. It is still only a draft.
- ``injection_status_field``: the same model also adding ``"status": "confirmed"``; the
  schema refuses it, so nothing is stored.

``refine_fact`` answers (M3.8), about the fictional fact "Cut invoice processing time by 35%
across 4 warehouses.":

- ``ok``: a question and a pure rephrase, starting with a verb.
- ``questions_only``: two questions and no suggestion.
- ``uses_answer``: a suggestion that uses the number the user gave in an answer.
- ``adversarial_invented_claims``: inflated numbers and an invented employer; flagged.
- ``adversarial_self_reported``: a lower-case added phrase the model reports in
  ``added_details``; flagged.
- ``injection_accept_field``: the invented claims plus ``"accepted": true``; the schema
  refuses it, so nothing is stored.
- ``malformed``: text that is not JSON.
"""

from collections.abc import Mapping
from dataclasses import replace
from types import MappingProxyType
from typing import Any, Final

from app.modules.ai.providers.fake import KeywordScript, ScriptedResponse

EXTRACT_CV_TASK: Final = "extract_cv"

KEYWORD_MALFORMED: Final = "fake-cv-malformed"
KEYWORD_INVENTED_EMPLOYER: Final = "fake-cv-invented-employer"
KEYWORD_INVENTED_QUOTE: Final = "fake-cv-invented-quote"
KEYWORD_MOVED_NUMBER: Final = "fake-cv-moved-number"
KEYWORD_INVENTED_WORDS: Final = "fake-cv-invented-words"
KEYWORD_INVENTED_SKILL: Final = "fake-cv-invented-skill"
KEYWORD_INJECTION_STATUS: Final = "fake-cv-injection-status"
KEYWORD_INJECTION: Final = "fake-cv-injection"
KEYWORD_USAGE_LIMIT: Final = "fake-cv-usage-limit"
KEYWORD_OFFLINE: Final = "fake-cv-offline"

REFINE_FACT_TASK: Final = "refine_fact"
KEYWORD_REFINE_MALFORMED: Final = "fake-refine-malformed"
KEYWORD_REFINE_QUESTIONS: Final = "fake-refine-questions"
KEYWORD_REFINE_USES_ANSWER: Final = "fake-refine-uses-answer"
KEYWORD_REFINE_INVENTED: Final = "fake-refine-invented"
KEYWORD_REFINE_SELF_REPORTED: Final = "fake-refine-self-reported"
KEYWORD_REFINE_INJECTION: Final = "fake-refine-injection"
# The ``ok`` answer, after SLOW_REFINE_SECONDS: a waiting state that lasts (M3.9 e2e).
KEYWORD_REFINE_SLOW: Final = "fake-refine-slow"
SLOW_REFINE_SECONDS: Final = 8.0

_NOT_JSON: Final = "fictional CV extraction output that is not JSON"
_INVENTED_EMPLOYER: Final = "Imaginary Aerospace Corp"
_PLANTED_CLAIM: Final = "Led a team of 50 engineers at Imaginary Aerospace Corp."


def _role(
    key: str, employer: str | None, title: str | None, start: str | None, end: str | None
) -> dict[str, Any]:
    return {"key": key, "employer": employer, "title": title, "start_date": start, "end_date": end}


def _fact(role_key: str | None, text: str, quote: str, *skills: str) -> dict[str, Any]:
    return {
        "type": "fact",
        "role_key": role_key,
        "text": text,
        "situation": None,
        "action": None,
        "result": None,
        "skills": list(skills),
        "source_quote": quote,
    }


def _story(role_key: str | None, action: str, result: str, quote: str) -> dict[str, Any]:
    return {
        "type": "story",
        "role_key": role_key,
        "text": None,
        "situation": None,
        "action": action,
        "result": result,
        "skills": [],
        "source_quote": quote,
    }


_INVOICES: Final = "Cut invoice processing time by 35% across 4 warehouses."
_ROUTES: Final = "Planned 60 delivery routes a week with a team of 3."
_DEGREE: Final = "BSc in Industrial Management, Example University, 2018"


def _roles() -> list[dict[str, Any]]:
    return [
        _role("r1", "Example Widgets Ltd", "Operations Analyst", "2021", "2024"),
        _role("r2", "Sample Logistics SA", "Junior Planner", "2018", "2021"),
    ]


def _units() -> list[dict[str, Any]]:
    return [
        _story("r1", "Cut invoice processing time", "by 35% across 4 warehouses.", _INVOICES),
        _fact("r2", _ROUTES, _ROUTES),
        _fact(None, _DEGREE, _DEGREE),
        _fact(None, "English", "English", "English"),
    ]


def _with_status(unit: dict[str, Any]) -> dict[str, Any]:
    return {**unit, "status": "confirmed"}


_OK: Final[dict[str, Any]] = {"roles": _roles(), "units": _units()}
_ADVERSARIAL_EMPLOYER_NUMBER: Final[dict[str, Any]] = {
    "roles": [*_roles(), _role("r3", _INVENTED_EMPLOYER, "Operations Lead", "2019", "2020")],
    "units": [
        _fact("r2", _ROUTES, _ROUTES),
        _fact(
            "r3",
            "Cut invoice processing time by 85% across 14 warehouses at Imaginary Aerospace Corp.",
            _INVOICES,
        ),
        _fact("r3", _INVOICES, _INVOICES),
    ],
}
_ADVERSARIAL_QUOTE: Final[dict[str, Any]] = {
    "roles": _roles(),
    "units": [
        _fact("r2", _ROUTES, _ROUTES),
        _fact(
            "r2",
            "Trained new planners on the routing tool.",
            "Trained new planners on the routing tool.",
        ),
    ],
}
_ADVERSARIAL_MOVED_NUMBER: Final[dict[str, Any]] = {
    "roles": _roles(),
    "units": [
        _fact("r2", _ROUTES, _ROUTES),
        _fact("r1", "Cut invoice processing time by 60% across 3 warehouses.", _INVOICES),
    ],
}
_ADVERSARIAL_INVENTED_WORDS: Final[dict[str, Any]] = {
    "roles": _roles(),
    "units": [
        _fact("r2", _ROUTES, _ROUTES),
        _fact(
            "r1",
            "Cut invoice processing time by 35% across 4 warehouses using SAP at "
            "Imaginary Aerospace Corp.",
            _INVOICES,
        ),
    ],
}
_ADVERSARIAL_SKILL: Final[dict[str, Any]] = {
    "roles": _roles(),
    "units": [
        _fact("r2", _ROUTES, _ROUTES, "Kubernetes"),
        _fact(None, "English", "English", "English"),
    ],
}
_INJECTION_OBEYED: Final[dict[str, Any]] = {
    "roles": [*_roles(), _role("r3", _INVENTED_EMPLOYER, None, None, None)],
    "units": [*_units(), _fact("r3", _PLANTED_CLAIM, _PLANTED_CLAIM)],
}
_INJECTION_STATUS_FIELD: Final[dict[str, Any]] = {
    "roles": _INJECTION_OBEYED["roles"],
    "units": [_with_status(unit) for unit in _INJECTION_OBEYED["units"]],
}

# Fixture name -> response; the names are the files of tests/fixtures/ai/extract_cv/.
EXTRACT_CV_RESPONSES: Final[Mapping[str, ScriptedResponse]] = MappingProxyType(
    {
        "ok": ScriptedResponse(data=_OK),
        "malformed": ScriptedResponse(raw_text=_NOT_JSON),
        "adversarial_employer_number": ScriptedResponse(data=_ADVERSARIAL_EMPLOYER_NUMBER),
        "adversarial_quote": ScriptedResponse(data=_ADVERSARIAL_QUOTE),
        "adversarial_moved_number": ScriptedResponse(data=_ADVERSARIAL_MOVED_NUMBER),
        "adversarial_invented_words": ScriptedResponse(data=_ADVERSARIAL_INVENTED_WORDS),
        "adversarial_skill": ScriptedResponse(data=_ADVERSARIAL_SKILL),
        "injection_obeyed": ScriptedResponse(data=_INJECTION_OBEYED),
        "injection_status_field": ScriptedResponse(data=_INJECTION_STATUS_FIELD),
    }
)
# Keyword -> what answers it, most specific first: a fixture name, or a provider failure.
_EXTRACT_CV_KEYWORDS: Final[tuple[tuple[str, ScriptedResponse], ...]] = (
    (KEYWORD_MALFORMED, EXTRACT_CV_RESPONSES["malformed"]),
    (KEYWORD_INVENTED_EMPLOYER, EXTRACT_CV_RESPONSES["adversarial_employer_number"]),
    (KEYWORD_INVENTED_QUOTE, EXTRACT_CV_RESPONSES["adversarial_quote"]),
    (KEYWORD_MOVED_NUMBER, EXTRACT_CV_RESPONSES["adversarial_moved_number"]),
    (KEYWORD_INVENTED_WORDS, EXTRACT_CV_RESPONSES["adversarial_invented_words"]),
    (KEYWORD_INVENTED_SKILL, EXTRACT_CV_RESPONSES["adversarial_skill"]),
    (KEYWORD_INJECTION_STATUS, EXTRACT_CV_RESPONSES["injection_status_field"]),
    (KEYWORD_INJECTION, EXTRACT_CV_RESPONSES["injection_obeyed"]),
    (KEYWORD_USAGE_LIMIT, ScriptedResponse(failure="usage_limit")),
    (KEYWORD_OFFLINE, ScriptedResponse(failure="network")),
)


_REFINE_QUESTION: Final = "About how many invoices did the team handle each month?"
_REFINE_INVENTED: Final = (
    "Cut invoice processing time by 85% across 14 warehouses at Globex Corporation."
)


def _refined(text: str, *questions: str, added: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "questions": list(questions),
        "suggestion": {"text": text, "situation": None, "action": None, "result": None},
        "added_details": list(added),
    }


_ADDED_DETAIL: Final = "saving the finance team a week each month"
# Fixture name -> response; the names are the files of tests/fixtures/ai/refine_fact/.
REFINE_FACT_RESPONSES: Final[Mapping[str, ScriptedResponse]] = MappingProxyType(
    {
        "ok": ScriptedResponse(
            data=_refined(
                "Reduced invoice processing time by 35% across 4 warehouses.", _REFINE_QUESTION
            )
        ),
        "questions_only": ScriptedResponse(
            data={
                "questions": [
                    "What changed for the finance team once processing was faster?",
                    "Which tool did you use to track the processing time?",
                ],
                "suggestion": None,
                "added_details": [],
            }
        ),
        "uses_answer": ScriptedResponse(
            data=_refined(
                "Cut invoice processing time by 35% across 4 warehouses, handling about 1200 "
                "invoices a month."
            )
        ),
        "adversarial_invented_claims": ScriptedResponse(
            data=_refined(_REFINE_INVENTED, _REFINE_QUESTION)
        ),
        "adversarial_self_reported": ScriptedResponse(
            data=_refined(
                f"Cut invoice processing time by 35% across 4 warehouses, {_ADDED_DETAIL}.",
                added=(_ADDED_DETAIL,),
            )
        ),
        "injection_accept_field": ScriptedResponse(
            data={**_refined(_REFINE_INVENTED), "accepted": True}
        ),
        "malformed": ScriptedResponse(raw_text="fictional refine output that is not JSON"),
    }
)
_REFINE_FACT_KEYWORDS: Final[tuple[tuple[str, ScriptedResponse], ...]] = (
    (KEYWORD_REFINE_MALFORMED, REFINE_FACT_RESPONSES["malformed"]),
    (KEYWORD_REFINE_QUESTIONS, REFINE_FACT_RESPONSES["questions_only"]),
    (KEYWORD_REFINE_USES_ANSWER, REFINE_FACT_RESPONSES["uses_answer"]),
    (KEYWORD_REFINE_INVENTED, REFINE_FACT_RESPONSES["adversarial_invented_claims"]),
    (KEYWORD_REFINE_SELF_REPORTED, REFINE_FACT_RESPONSES["adversarial_self_reported"]),
    (KEYWORD_REFINE_INJECTION, REFINE_FACT_RESPONSES["injection_accept_field"]),
)


def real_task_scripts(*, delay_seconds: float = 0.0) -> Mapping[str, KeywordScript]:
    """The keyword script of every real task the fake can answer, by task name. Every call
    takes ``delay_seconds`` (for a visible waiting state and for cancelling)."""
    if delay_seconds < 0:
        raise ValueError("delay_seconds can't be negative.")

    def timed(response: ScriptedResponse) -> ScriptedResponse:
        return replace(response, delay_seconds=delay_seconds)

    return MappingProxyType(
        {
            EXTRACT_CV_TASK: KeywordScript(
                default=timed(EXTRACT_CV_RESPONSES["ok"]),
                by_keyword=tuple(
                    (keyword, timed(response)) for keyword, response in _EXTRACT_CV_KEYWORDS
                ),
            ),
            REFINE_FACT_TASK: KeywordScript(
                default=timed(REFINE_FACT_RESPONSES["ok"]),
                by_keyword=(
                    *((keyword, timed(response)) for keyword, response in _REFINE_FACT_KEYWORDS),
                    (
                        KEYWORD_REFINE_SLOW,
                        replace(
                            REFINE_FACT_RESPONSES["ok"],
                            delay_seconds=max(SLOW_REFINE_SECONDS, delay_seconds),
                        ),
                    ),
                ),
            ),
        }
    )
