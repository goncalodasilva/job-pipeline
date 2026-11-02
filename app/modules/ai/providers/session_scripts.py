"""Fictional session output: the fake does not interpret the person's sources."""

from app.modules.ai.providers.fake import KeywordScript, ScriptedResponse


def enrichment_question_scripts() -> KeywordScript:
    return KeywordScript(
        default=ScriptedResponse(
            data={
                "question": "Tell me about an experience you would like to keep.",
                "why": "Practice data: this is a fictional example question.",
            }
        )
    )


def _structured(data: dict[str, object]) -> ScriptedResponse:
    base: dict[str, object] = {
        "type": "fact",
        "text": None,
        "situation": None,
        "action": None,
        "result": None,
        "role_key": None,
        "skills": [],
        "missing": [],
        "added_details": [],
    }
    return ScriptedResponse(data={**base, **data})


def structure_answer_scripts() -> KeywordScript:
    """The default answer is a practice wording to review. Two fictional sentences get answers
    that the app's own resolver accepts as grounded in them (the interview debrief's sample
    paragraphs, M8.3): the first listed keyword found wins, so the longer paragraph is first."""
    return KeywordScript(
        default=ScriptedResponse(
            data={
                "type": "fact",
                "text": "An experience to review.",
                "situation": None,
                "action": None,
                "result": None,
                "role_key": None,
                "skills": [],
                "missing": [],
                "added_details": ["Practice wording; review against your answer."],
            }
        ),
        by_keyword=(
            (
                "Everyone used the routes",
                _structured(
                    {
                        "type": "story",
                        "situation": "The team had no routes.",
                        "action": "Planned 12 routes.",
                        "result": "Everyone used the routes.",
                    }
                ),
            ),
            ("I planned 12 routes", _structured({"text": "Planned 12 routes."})),
        ),
    )
