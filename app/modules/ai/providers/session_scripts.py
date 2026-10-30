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


def structure_answer_scripts() -> KeywordScript:
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
        )
    )
