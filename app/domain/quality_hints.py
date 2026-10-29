"""Non-blocking hints for confirmed, usable facts (M7.2). Pure."""

from app.domain.facts import FactContent, clean_part
from app.domain.honesty import extract_quantities


def quality_hints(
    fact_type: str, content: FactContent, status: str, privacy: str
) -> tuple[str, ...]:
    if status != "confirmed" or privacy == "never_use":
        return ()
    hints = []
    if fact_type == "story" and not clean_part(content.result):
        hints.append("Add a result?")
    if not extract_quantities(" ".join(content.normalized())):
        hints.append("Add a number?")
    if fact_type == "story" and not clean_part(content.action):
        hints.append("Add what you did?")
    return tuple(hints)
