"""What the Settings > Lists editor template is given (M9.2 part D; ADR-131 items 6-7; ADR-125
item 5).

Kept apart from the routes (``app/api/settings_lists.py``) so the area page
(``app/api/settings_areas.py``) can show the section without importing them. A read: it writes
nothing. Stored and typed text is untrusted: the templates escape it. Every group starts closed;
the answer to a form of a group opens that group only.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Final

from fastapi import Request

from app.domain.controlled_values import EDITABLE_FIELDS
from app.domain.errors import VersionConflict
from app.domain.ids import is_safe_id
from app.domain.removal_reasons import REMOVAL_REASONS, mapping_words
from app.services.controlled_lists import ControlledLists, ControlledValueView
from app.services.skills import SkillService

AREA_PATH: Final = "/settings/lists"
VALUES_PATH: Final = f"{AREA_PATH}/values"
SKILLS_PATH: Final = f"{AREA_PATH}/skills"
VALUES_TO_MAP_PATH: Final = "/inbox/values"
ADD, ALIASES, DELETE = "add", "aliases", "delete"
GROUP_NAMES: Final = {
    "industry": "Industry",
    "seniority": "Seniority",
    "employment_type": "Employment type",
    "work_mode": "Work mode",
    "skills": "Skills",
}
_NOTHING: Mapping[str, str] = MappingProxyType({})


def group_name(key: str) -> str:
    return GROUP_NAMES[key]


@dataclass(frozen=True)
class ListsState:
    """What just happened in the section; the default is a plain page load."""

    group: str = ""  # the group the answer opens: a field name or "skills"
    scope: str = ""  # the form that was refused: ADD, ALIASES or DELETE (empty when done)
    target: str = ""  # the value or skill the form or the done line belongs to
    errors: Mapping[str, str] = field(default_factory=lambda: _NOTHING)  # message per field
    typed: Mapping[str, str] = field(default_factory=lambda: _NOTHING)  # shown back, escaped
    message: str = ""  # done: the line shown in the section
    focus: str = ""  # done: "row" (the changed item) or "summary" (its group's summary)
    conflict: bool = False  # a stale version: the reload prompt shows and takes focus


def used_by(jobs: int) -> str:
    if not jobs:
        return "Not used."
    return "Used by 1 job." if jobs == 1 else f"Used by {jobs} jobs."


def _value_row(item: ControlledValueView, state: ListsState) -> dict[str, Any]:
    here = state.target == item.id and state.group == item.field
    refused = here and bool(state.errors) and state.scope in (ALIASES, DELETE)
    aliases_refused = refused and state.scope == ALIASES
    typed = state.typed.get("aliases") if aliases_refused else None
    return {
        "id": item.id,
        "safe": is_safe_id(item.id),
        "value": item.value,
        "label": item.label,
        "version": item.version,
        "jobs_text": used_by(item.jobs),
        "aliases_text": typed if typed is not None else "\n".join(item.aliases),
        "alias_error": state.errors.get("aliases") if aliases_refused else None,
        "delete_error": state.errors.get("delete") if refused and state.scope == DELETE else None,
        "focus_row": here and not state.scope and state.focus == "row",
    }


def _skill_row(item: Any, state: ListsState) -> dict[str, Any]:
    here = state.target == item.id and state.group == "skills"
    refused = here and state.scope == ALIASES and bool(state.errors)
    typed = state.typed.get("aliases") if refused else None
    return {
        "id": item.id,
        "safe": is_safe_id(item.id),
        "name": item.name,
        "version": item.version,
        "aliases_text": typed if typed is not None else "\n".join(item.aliases),
        "alias_error": state.errors.get("aliases") if refused else None,
        "focus_row": here and not state.scope and state.focus == "row",
    }


def lists_context(request: Request, state: ListsState | None) -> dict[str, Any]:
    """The ``lists`` template value. Blocking (it reads the database)."""
    state = state or ListsState()
    service: ControlledLists = request.app.state.controlled_lists
    skills: SkillService | None = request.app.state.skills
    listed = service.groups()
    editable = [
        {
            "key": key,
            "name": GROUP_NAMES[key],
            "lower": GROUP_NAMES[key].lower(),
            "open": state.group == key,
            "focus_summary": state.group == key and not state.scope and state.focus == "summary",
            "rows": [_value_row(item, state) for item in listed[key]],
            "add_error": state.errors.get("value")
            if state.scope == ADD and state.group == key
            else None,
            "add_typed": state.typed.get("value", "")
            if state.scope == ADD and state.group == key
            else "",
        }
        for key in EDITABLE_FIELDS
    ]
    skill_rows = [_skill_row(item, state) for item in (skills.list() if skills else ())]
    return {
        "groups": editable,
        "work_mode": [
            {"value": item.value, "label": item.label, "aliases": list(item.aliases)}
            for item in listed["work_mode"]
        ],
        "work_mode_open": state.group == "work_mode",
        "skills": skill_rows,
        "skills_open": state.group == "skills",
        "reasons": [
            {
                "key": r.key,
                "label": r.label,
                "maps_to": " ".join(r.maps_to),
                "words": mapping_words(r),
            }
            for r in REMOVAL_REASONS
        ],
        "values_url": VALUES_PATH,
        "skills_url": SKILLS_PATH,
        "values_to_map_url": VALUES_TO_MAP_PATH,
        "area_url": AREA_PATH,
        "message": state.message,
        "conflict": state.conflict,
        "conflict_message": VersionConflict.message,
        # Something in this answer takes focus, so the page heading does not.
        "attention": bool(state.errors or state.message or state.conflict),
    }
