"""Pure presentation helpers for the CV review screen."""

from typing import Any

from app.domain.honesty import extract_quantities
from app.services.document_views import DocumentView, lines_needing_check


def step_state(view: DocumentView) -> str:
    if view.task.active:
        return "Running"
    if view.task.attention:
        return "Needs attention"
    if not view.version:
        return "Not started"
    if view.version.status in ("approved", "sent"):
        return "Done"
    return "Needs your check" if view.blockers else "Ready to approve"


def screen_data(view: DocumentView) -> dict[str, Any]:
    marks = {line.unit_id: line for line in view.diff if line.unit_id}
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
    count = lines_needing_check(view.blockers)
    reason = (
        "There is nothing to approve yet."
        if not view.units
        else "1 line needs your check."
        if count == 1
        else f"{count} lines need your check."
        if count
        else ""
    )
    return {
        "marks": marks,
        "flags": flags,
        "removed": [line for line in view.diff if line.unit_id is None],
        "approve_reason": reason,
        "roles": tuple(dict.fromkeys(unit.role for unit in view.units if unit.role)),
    }
