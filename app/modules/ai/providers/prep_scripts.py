"""Fictional prep wording. Only code-owned requirement ids and requested count are read."""

import json
import re
from typing import Any, Final

FICTIONAL_SOURCE: Final = "https://example.test/interviews"
_QUESTIONS: Final = (
    "How would you approach an unfamiliar challenge?",
    "What would you consider when choosing an approach?",
    "Tell me about a time you learned something new.",
    "How would you measure whether your approach worked?",
    "How would you decide where to begin?",
    "How would you explain your decisions to a colleague?",
    "Tell me about a problem you would approach differently now.",
    "What would you do if your initial plan failed?",
    "How would you ask for help with a difficult task?",
    "What would you review before starting this work?",
    "What tradeoffs would you weigh before making a decision?",
    "How would you check the information available to you?",
    "What might you change after hearing another perspective?",
    "How would you work with others on a shared goal?",
    "What would you do to understand the problem more clearly?",
)
_ASK_THEM: Final = (
    "What would success in this role look like?",
    "How does the team share knowledge?",
    "What challenges would someone in this role focus on?",
)


def question_data(user: str) -> dict[str, Any]:
    # The renderer escapes forged closing tags; source wording stays inside this JSON.
    block = re.search(r"<untrusted_document[^>]*>\n(.*?)\n</untrusted_document>", user, re.S)
    try:
        context = json.loads(block[1]) if block else {}
    except ValueError:
        context = {}
    requirements = context.get("uncovered_requirements", []) if isinstance(context, dict) else []
    ids = [r["id"] for r in requirements if isinstance(r, dict) and isinstance(r.get("id"), str)]
    # Only the trusted tail, after the last closing tag, sets the batch shape.
    tail = user.rsplit("</untrusted_document>", 1)[-1]
    match = re.search(r"Write up to ([0-9]+) interview questions", tail)
    count = min(10, int(match[1])) if match else 10
    ask_match = re.search(r"up to ([0-9]+) questions to ask them", tail)
    ask_count = min(3, int(ask_match[1])) if ask_match else 3
    # A Show-more batch (no questions to ask, at most five) takes the unused wording.
    offset = 10 if ask_count == 0 and count <= 5 else 0
    questions = [
        {
            "text": _QUESTIONS[offset + i],
            "type": "behavioral",
            "requirement_ids": [rid],
            "company_specific": False,
        }
        for i, rid in enumerate(ids[:count])
    ]
    asks: list[dict[str, Any]] = [
        {"text": text, "requirement_ids": [], "company_specific": False}
        for text in _ASK_THEM[:ask_count]
    ]
    return {"questions": questions, "ask_them": asks}


def web_data() -> dict[str, Any]:
    return {
        "questions": [
            {
                "text": "How would you approach an unfamiliar challenge?",
                "type": "behavioral",
                "source_url": FICTIONAL_SOURCE,
                "source_date": None,
            }
        ]
    }
