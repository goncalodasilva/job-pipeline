"""Pure presentation helpers for the CV and cover letter review screens."""

from typing import Any

from app.domain.honesty import extract_quantities
from app.services.document_views import DocumentView, lines_needing_check
from app.services.letters import LetterView


def step_state(view: DocumentView | LetterView) -> str:
    if view.task.active:
        return "Running"
    if view.task.attention:
        return "Needs attention"
    if not view.version:
        return "Not started"
    if view.version.status in ("approved", "sent"):
        return "Done"
    return "Needs your check" if view.blockers else "Ready to approve"


def view_state(view: DocumentView | LetterView) -> str:
    """The step's ``data-state``: none, running, failed, draft or approved."""
    if view.task.active:
        return "running"
    if view.task.attention:
        return "failed"
    if view.version and view.version.status in ("approved", "sent"):
        return "approved"
    return "draft" if view.version else "none"


def flag_rows(view: DocumentView | LetterView) -> dict[str, list[tuple[str, str, str]]]:
    """For each unit: its flags and blockers as (kind, label, claim), each once."""
    flags: dict[str, list[tuple[str, str, str]]] = {}
    for unit in view.units:
        pairs = dict.fromkeys(
            [(flag.kind, flag.claim) for flag in unit.flags]
            + [(b.kind, b.claim) for b in view.blockers if b.unit_id == unit.unit_id]
        )
        flags[unit.unit_id] = [
            (
                kind,
                "This isn't in your fact bank."
                if kind == "unsupported"
                else "Check this number"
                if any(quantity.text == claim for quantity in extract_quantities(claim))
                else "Check this claim",
                claim,
            )
            for kind, claim in pairs
        ]
    return flags


def approve_reason(view: DocumentView | LetterView, noun: str = "line") -> str:
    """Why Approve is disabled, in words; empty when nothing blocks."""
    count = lines_needing_check(view.blockers)
    if not view.units:
        return "There is nothing to approve yet."
    if count == 1:
        return f"1 {noun} needs your check."
    return f"{count} {noun}s need your check." if count else ""


def screen_data(view: DocumentView) -> dict[str, Any]:
    marks = {line.unit_id: line for line in view.diff if line.unit_id}
    return {
        "marks": marks,
        "flags": flag_rows(view),
        "removed": [line for line in view.diff if line.unit_id is None],
        "approve_reason": approve_reason(view),
        "roles": tuple(dict.fromkeys(unit.role for unit in view.units if unit.role)),
    }


def letter_screen_data(view: LetterView) -> dict[str, Any]:
    return {"flags": flag_rows(view), "approve_reason": approve_reason(view, "paragraph")}
