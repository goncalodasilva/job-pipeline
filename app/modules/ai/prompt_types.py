"""Prompt library types that carry no template engine: errors, status and section texts.

They live apart from ``prompts`` (which imports Jinja2) so ``app.modules.ai`` can re-export
them to services without pulling the template engine in (spec 2.3 rule 4; ADR-055). Callers
use them through ``app.modules.ai``; ``app.modules.ai.prompts`` re-exports them too.
"""

from dataclasses import dataclass
from typing import Final, Literal

RefusalReason = Literal[
    "unwrapped_untrusted",
    "include",
    "undeclared_input",
    "oversize",
    "front_matter_in_body",
    "stray_section_header",
    "template_syntax",
    "invalid_template",
]


class PromptError(Exception):
    """A prompt file is missing, malformed or unsafe, or could not be rendered.

    Messages name the task and the problem, never input values.
    """


class PromptConflict(PromptError):
    """The override changed since the editor loaded it (a stale content version)."""


class PromptRefused(PromptError):
    """A prompt text the safety checks refuse (spec 3.3). ``reason`` is a stable code."""

    def __init__(self, reason: RefusalReason, message: str) -> None:
        super().__init__(message)
        self.reason: RefusalReason = reason


# What the editor says when it refuses a text: fixed words, never the user's text.
REFUSAL_PROBLEMS: Final[dict[RefusalReason, str]] = {
    "unwrapped_untrusted": (
        "untrusted inputs may only be used in the user section, and only as "
        "'{{ name | untrusted(source, id) }}'."
    ),
    "include": "templates may not include, import or extend other templates.",
    "undeclared_input": "the text uses a name that isn't one of this prompt's inputs.",
    "oversize": "the prompt is larger than allowed.",
    "front_matter_in_body": "a line made of '---' isn't allowed in a section.",
    "stray_section_header": "a line '## system' or '## user' isn't allowed inside a section.",
    "template_syntax": "the text has a template syntax error.",
    "invalid_template": "the text isn't a valid prompt.",
}


@dataclass(frozen=True)
class PromptStatus:
    """Where a task's prompt stands (spec 3.6).

    ``base_version`` is the default version the override was written against (``None``
    without an override or when it can't be read). ``error`` is a plain message when the
    override can't be loaded. ``content_version`` is the hex SHA-256 of the override file's
    bytes, or ``"none"``: the editor sends it back to detect a change in between.
    """

    default_version: int
    overridden: bool
    base_version: int | None
    error: str | None
    content_version: str


@dataclass(frozen=True)
class PromptText:
    """The two editable sections of a prompt, as plain template text."""

    system: str
    user: str
