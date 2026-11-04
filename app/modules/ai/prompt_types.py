"""Prompt library types that carry no template engine: errors, status and section texts.

They live apart from ``prompts`` (which imports Jinja2) so ``app.modules.ai`` can re-export
them to services without pulling the template engine in (spec 2.3 rule 4; ADR-055). Callers
use them through ``app.modules.ai``; ``app.modules.ai.prompts`` re-exports them too.
"""

import re
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
    "unsupported_expression",
    "invalid_template",
]

# The most a prompt file may hold (the front matter and both sections); a larger text is refused
# as "oversize" (spec 3.3). The editor shows it next to the size of what is typed.
MAX_PROMPT_FILE_BYTES: Final = 256 * 1024

# A temporary file the editor's atomic write may leave behind if the app stops mid-save
# (``.prompt.md.<16 hex>.tmp``). It is never content: save and reset clear it, an export skips it.
OVERRIDE_TEMP_PATTERN: Final = re.compile(r"\.prompt\.md\.[0-9a-f]{16}\.tmp")


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
    "unsupported_expression": (
        "the text uses a filter, method, operation or loop that prompts don't allow."
    ),
    "invalid_template": "the text isn't a valid prompt.",
}


@dataclass(frozen=True)
class PromptStatus:
    """Where a task's prompt stands (spec 3.6).

    ``base_version`` is the default version the override was written against (``None``
    without an override or when it can't be read). ``error`` is a plain message when the
    override can't be loaded.

    ``content_version`` is what the editor sends back to detect a change in between. It is the
    hex SHA-256 of the override file's bytes up to 1 MiB; a larger file is never read, and its
    version is a digest of its size, modification time and identity instead (so it still changes
    when the file does). It is ``"none"`` when there is no ``prompt.md``, and also when the entry
    is not a readable regular file (a link, or a folder in its place): both read as "none", so a
    page that carried "none" can still reset that entry (a save over it is refused).

    ``inputs`` and ``untrusted_inputs`` are the names the default declares (an override keeps
    them): the names a text may use, and those that carry text from outside the app.
    """

    default_version: int
    overridden: bool
    base_version: int | None
    error: str | None
    content_version: str
    inputs: tuple[str, ...] = ()
    untrusted_inputs: tuple[str, ...] = ()


@dataclass(frozen=True)
class PromptText:
    """The two editable sections of a prompt, as plain template text."""

    system: str
    user: str
