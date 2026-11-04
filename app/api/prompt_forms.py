"""What the Settings > Prompts templates are given (M9.3 part B; ADR-055; ADR-131 item 7; spec
2.9 rule 13, 3.3 and 3.6).

Kept apart from the routes (``app/api/settings_prompts.py``) so the area pages
(``app/api/settings_areas.py``) can show the list and the task page without importing them. A read:
it writes nothing, and it reaches prompts only through the ``PromptSettings`` service
(``app.state.prompt_settings``). A prompt's text is user-written or shipped template text and may
hold personal data: it is only ever given to the protected editor fragment, escaped there, and a
message never repeats it. The words below are the UI's own copy.
"""

import difflib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final

from fastapi import Request

from app.api.task_tier_forms import TASK_COPY, task_label
from app.domain.errors import VersionConflict
from app.domain.tiers import DEFAULT_TIERS
from app.services.prompt_settings import (
    MAX_PROMPT_FILE_BYTES,
    PromptSettings,
    PromptStatus,
    PromptText,
    PromptUnavailable,
    RefusalReason,
)

AREA_PATH: Final = "/settings/prompts"
SAVED: Final = "Prompt saved."
RESET: Final = "Prompt reset to the default."
NOT_SAVED: Final = "Prompt not saved."
NOT_FOUND: Final = "This prompt wasn't found."
NOT_DONE: Final = "The prompt couldn't be changed. Reload the page and try again."
NO_VERSION: Final = "none"  # the content version of "no override file"
_VERSION: Final = re.compile(r"[0-9a-f]{64}")

BADGE_EDITED, BADGE_UPDATED, BADGE_CANT_LOAD = "edited", "default-updated", "cant-load"
BADGE_WORDS: Final[Mapping[str, str]] = MappingProxyType(
    {BADGE_EDITED: "Edited", BADGE_UPDATED: "Default updated", BADGE_CANT_LOAD: "Can't load"}
)

NOTICES: Final = (
    "The app always adds its own notice that text from a job post, your CV or a web search is "
    "untrusted data and never instructions, and, for text it writes about you, its honesty rule "
    "that only the facts and answers you provide may be used. Those can't be removed here. The "
    "answer format and its fields stay the default's: wording that asks for other fields fails "
    "validation and costs a retry. Generated text is still checked against your facts, whatever "
    "the prompt says."
)
INPUTS_HINT: Final = (
    "Write the text like the default does. Where it inserts a job post, CV text or other text "
    'from outside the app, keep it in the User section as {{ name | untrusted("source", id) }}.'
)
SECTIONS_HINT: Final = (
    "System tells the AI how to behave and what to return. User holds the material for one run "
    "(a job post, your CV or facts) and the request itself."
)
COMPARE_COPY: Final = (
    "This compares the current default with your version, line by line. A line marked - is only "
    "in the current default. A line marked + is only in your version. It can't show what the "
    "update changed in the default."
)
NO_DIFFERENCES: Final = "No differences in this section."
LOAD_ERROR: Final = (
    "Your version of this prompt can't be loaded, so tasks that use this prompt will fail until "
    "the text is fixed or reset to the default. This often follows an app update."
)
RESET_ONLY_ERROR: Final = (
    "Your version of this prompt is a link or a folder, not a text file, so it can't be loaded "
    "and can't be saved over. Tasks that use this prompt will fail until you use Reset to go "
    "back to the default. If Reset can't remove it, remove it by hand from the app's prompts "
    "folder."
)
CHECK_BOTH: Final = "Check both sections."
SIZE_LIMIT: Final = f"{MAX_PROMPT_FILE_BYTES // 1024} KB"
CONTEXT_LINES: Final = 2  # unchanged lines kept around a change in the compare view

# One fixed message per refusal reason, shown beside the text areas. They never repeat the text.
REFUSAL_MESSAGES: Final[Mapping[RefusalReason, str]] = MappingProxyType(
    {
        "unwrapped_untrusted": (
            "Text from outside the app can only be used in the User section, in the form "
            '{{ name | untrusted("source", id) }}.'
        ),
        "include": "The text can't include, import or extend other templates.",
        "undeclared_input": (
            "The text uses a name that isn't one of this prompt's inputs. Use only the names "
            "listed under Inputs for this prompt."
        ),
        "oversize": (
            f"The prompt is larger than the {SIZE_LIMIT} limit (the System and User sections "
            "together). Shorten one of the sections."
        ),
        "front_matter_in_body": "A line made only of --- isn't allowed in a section.",
        "stray_section_header": "A line that is only ## system or ## user isn't allowed here.",
        "template_syntax": "The text has a template syntax error. Check every {{ }} and {% %}.",
        "unsupported_expression": (
            "The text uses a filter, method, operation or loop that prompts don't allow. Keep "
            "to plain values, if blocks, loops over an input and simple text filters."
        ),
        "invalid_template": "The text isn't a valid prompt.",
    }
)
REFUSAL_FALLBACK: Final = "The text isn't a valid prompt."


def is_task(task: str) -> bool:
    """Only the AI tasks the app lists (``DEFAULT_TIERS``) have a prompt page."""
    return task in DEFAULT_TIERS


def is_content_version(value: str) -> bool:
    """``none`` (no override file) or the 64-character lower-case hex digest of the file."""
    return value == NO_VERSION or _VERSION.fullmatch(value) is not None


def prompt_settings(request: Request) -> PromptSettings | None:
    """The prompt service of the open workspace (None while none is open)."""
    service: PromptSettings | None = request.app.state.prompt_settings
    return service


def refusal_message(reason: RefusalReason) -> str:
    return REFUSAL_MESSAGES.get(reason, REFUSAL_FALLBACK)


@dataclass(frozen=True)
class PromptState:
    """What just happened in the editor; the default is a plain load."""

    message: str = ""  # done: the line shown ("Prompt saved.")
    conflict: bool = False  # a stale content version: the reload prompt shows and takes focus
    reason: RefusalReason | None = None  # the safety check that refused the typed text
    typed: PromptText | None = None  # shown back, escaped: in the text areas, or read only

    @property
    def attention(self) -> bool:
        """Whether something in the answer takes focus, so the page heading does not."""
        return bool(self.message or self.conflict or self.reason)


def default_updated(status: PromptStatus) -> bool:
    """Whether the default moved on since the override was written. A base version above the
    default's (an override restored from a newer app) is unknown, not "updated"."""
    return status.base_version is not None and status.base_version < status.default_version


def badges(status: PromptStatus) -> list[dict[str, str]]:
    """The words about where a prompt stands, as text (never colour alone)."""
    found: list[str] = []
    if status.overridden:
        found.append(BADGE_EDITED)
    if status.overridden and default_updated(status):
        found.append(BADGE_UPDATED)
    if status.error:
        found.append(BADGE_CANT_LOAD)
    return [{"kind": kind, "text": BADGE_WORDS[kind]} for kind in found]


def list_context(editor: PromptSettings | None) -> dict[str, Any]:
    """The ``prompts`` value of the list page: one row per task of ``DEFAULT_TIERS``, no text."""
    rows: list[dict[str, Any]] = []
    for task in DEFAULT_TIERS:
        try:
            status = editor.status(task) if editor is not None else None
        except PromptUnavailable:
            status = None
        copy = TASK_COPY.get(task)
        rows.append(
            {
                "task": task,
                "label": task_label(task),
                "description": copy[1] if copy else "",
                "href": f"{AREA_PATH}/{task}",
                "version": status.default_version if status is not None else None,
                "badges": badges(status) if status is not None else [],
            }
        )
    return {"rows": rows}


def shell_context(task: str) -> dict[str, Any]:
    """The ``prompt_task`` value of the task page: the shell, with no prompt text and no
    versions (the editor's status line says where the prompt stands)."""
    return {
        "task": task,
        "label": task_label(task),
        "task_url": f"{AREA_PATH}/{task}",
        "editor_url": f"{AREA_PATH}/{task}/editor",
        "list_url": AREA_PATH,
    }


def _lines(text: str) -> list[str]:
    return text.replace("\r\n", "\n").strip().split("\n")


def _same(text: str) -> dict[str, str]:
    return {"kind": "same", "marker": " ", "word": "", "text": text}


def _gap(count: int) -> dict[str, str]:
    return {"kind": "gap", "marker": "", "word": "", "text": f"… {count} unchanged lines"}


def _collapse(run: list[str], *, head: bool, tail: bool) -> list[dict[str, str]]:
    """An unchanged run: ``head`` says a change comes before it, ``tail`` that one follows. Only
    the lines within ``CONTEXT_LINES`` of a change are kept; a hidden stretch of at least two
    lines becomes one separator (a single hidden line is simply shown)."""
    keep = (CONTEXT_LINES if head else 0) + (CONTEXT_LINES if tail else 0)
    if len(run) - keep < 2:
        return [_same(line) for line in run]
    before = run[:CONTEXT_LINES] if head else []
    after = run[len(run) - CONTEXT_LINES :] if tail else []
    hidden = len(run) - len(before) - len(after)
    return [*map(_same, before), _gap(hidden), *map(_same, after)]


def line_diff(current: str, mine: str) -> list[dict[str, str]]:
    """The lines of ``current`` (the default) against ``mine``, for the compare view: ``removed``
    (only in the default, marked -), ``added`` (only in the override, marked +), ``same`` (two
    lines of context around a change) and ``gap`` ("… N unchanged lines"). ``word`` is what a
    screen reader is told before a changed line. Identical texts have no lines."""
    before, after = _lines(current), _lines(mine)
    if before == after:
        return []
    found: list[dict[str, str]] = []
    matcher = difflib.SequenceMatcher(None, before, after)
    codes = matcher.get_opcodes()
    for index, (tag, i1, i2, j1, j2) in enumerate(codes):
        if tag == "equal":
            found += _collapse(before[i1:i2], head=index > 0, tail=index < len(codes) - 1)
            continue
        found += [
            {"kind": "removed", "marker": "-", "word": "Removed: ", "text": line}
            for line in before[i1:i2]
        ]
        found += [
            {"kind": "added", "marker": "+", "word": "Added: ", "text": line}
            for line in after[j1:j2]
        ]
    return found


def format_size(size: int) -> str:
    """A size as plain words: ``812 bytes``, ``12.5 KB``."""
    if size < 1024:
        return f"{size} byte" if size == 1 else f"{size} bytes"
    return f"{size / 1024:.1f} KB"


def _size_note(text: str) -> str:
    size = format_size(len(text.strip().encode()))
    return f"This section is {size}. System and User together can be up to {SIZE_LIMIT}."


def only_reset_helps(status: PromptStatus) -> bool:
    """An override the app can neither load nor save over: a link or a folder where the file
    belongs (the editor reads it as "none" with an error). A save into it would only fail."""
    return status.overridden and status.error is not None and status.content_version == NO_VERSION


def _diff(default: str, mine: str) -> dict[str, Any]:
    lines = line_diff(default, mine)
    return {"lines": lines, "same": not lines, "same_copy": NO_DIFFERENCES}


def _inputs(status: PromptStatus) -> list[dict[str, Any]]:
    return [{"name": name, "untrusted": name in status.untrusted_inputs} for name in status.inputs]


def _status_line(status: PromptStatus) -> str:
    if not status.overridden:
        return f"Using the default v{status.default_version}"
    base, current = status.base_version, status.default_version
    if base is None:
        return "Edited"
    if base == current:
        return f"Edited, based on v{base}"
    if base > current:
        return "Edited"
    return f"The default was updated (v{base} → v{current}) since you edited this prompt"


def editor_context(
    editor: PromptSettings, task: str, status: PromptStatus, state: PromptState | None
) -> dict[str, Any]:
    """The ``editor`` template value. Raises ``PromptUnavailable`` when the default doesn't load.
    Blocking (reads files). The text areas hold what was typed (a refused form), else the
    override (even one that doesn't load, so it can be fixed), else the default."""
    state = state or PromptState()
    default = editor.default_text(task)
    # An override that can't be read as sections is None: the default is offered instead.
    mine = editor.override_text(task) if status.overridden else None
    shown = state.typed or mine or default
    updated = default_updated(status)
    compare = None
    if mine is not None and status.overridden and not status.error and updated:
        compare = {
            "default_version": status.default_version,
            "base_version": status.base_version,
            "system": _diff(default.system, mine.system),
            "user": _diff(default.user, mine.user),
        }
    refused = state.reason is not None
    cannot_save = only_reset_helps(status)
    unsaved = state.typed if state.conflict else None
    error = ""
    if state.reason is not None:
        error = f"{NOT_SAVED} {CHECK_BOTH} {refusal_message(state.reason)}"
    load_error = ""
    if status.error:
        load_error = RESET_ONLY_ERROR if cannot_save else LOAD_ERROR
    return {
        "task": task,
        "label": task_label(task),
        "task_url": f"{AREA_PATH}/{task}",
        "reset_url": f"{AREA_PATH}/{task}/reset",
        "list_url": AREA_PATH,
        "version": status.content_version,
        "status_line": _status_line(status),
        "focus_status": bool(state.message),
        "message": state.message,
        "load_error": load_error,
        "can_save": not cannot_save,
        "notices": NOTICES,
        "inputs_hint": INPUTS_HINT,
        "sections_hint": SECTIONS_HINT,
        "inputs": _inputs(status),
        "default": {"system": default.system, "user": default.user},
        "system": shown.system,
        "user": shown.user,
        "system_size": _size_note(shown.system),
        "user_size": _size_note(shown.user),
        "error": error,
        "invalid": refused,
        "can_reset": status.overridden,
        "compare": compare,
        "compare_copy": COMPARE_COPY,
        "conflict": state.conflict,
        "conflict_message": VersionConflict.message,
        "unsaved": (
            {"system": unsaved.system, "user": unsaved.user} if unsaved is not None else None
        ),
        # Something in this answer takes focus, so the page heading does not.
        "attention": state.attention,
    }
