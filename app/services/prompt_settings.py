"""Settings > Prompts: the user's view of the prompt library and its overrides (M9.3 part B;
ADR-055; spec 2.9 rule 13, 3.3 and 3.6).

A thin layer over the injected ``PromptEditor`` (built in wiring), so the web layer reaches
prompts through services only. It adds three things: the content version is checked first, so a
stale one is ``PromptStale`` whatever text came with it and nothing is written; the editor's
errors become this module's own (``PromptRefusal`` for a text the safety checks refuse, carrying
the stable reason code, ``PromptUnavailable`` for anything else); and an override that can't be
read as sections is simply absent from ``override_text``. The types the pages show are the
editor's own, re-exported. Every method blocks (file work): call it from a sync route or a worker
thread. Prompt text may hold personal data: nothing here logs it, and no message repeats it.
"""

from app.modules.ai import PromptConflict, PromptEditor, PromptError, PromptRefused
from app.modules.ai import PromptStatus as PromptStatus
from app.modules.ai import PromptText as PromptText
from app.modules.ai import RefusalReason as RefusalReason


class PromptStale(Exception):
    """The override changed since the page was loaded (its content version no longer matches)."""


class PromptRefusal(Exception):
    """The safety checks refuse the text; ``reason`` is a stable code (never the text)."""

    def __init__(self, reason: RefusalReason) -> None:
        super().__init__(reason)
        self.reason: RefusalReason = reason


class PromptUnavailable(Exception):
    """The prompt can't be read or changed (a default that doesn't load, a folder problem)."""


class PromptSettings:
    def __init__(self, editor: PromptEditor) -> None:
        self._editor = editor

    def status(self, task: str) -> PromptStatus:
        """Where the task's prompt stands. Raises ``PromptUnavailable`` when the default (a
        file shipped with the app) doesn't load; never for a broken override."""
        try:
            return self._editor.status(task)
        except PromptError as problem:
            raise PromptUnavailable from problem

    def default_text(self, task: str) -> PromptText:
        try:
            return self._editor.default_text(task)
        except PromptError as problem:
            raise PromptUnavailable from problem

    def override_text(self, task: str) -> PromptText | None:
        """The override's sections (even one that doesn't load); ``None`` without one or when
        the file can't be read as sections."""
        try:
            return self._editor.override_text(task)
        except PromptError:
            return None

    def save(self, task: str, system: str, user: str, *, version: str) -> PromptStatus:
        """Save the two sections, then return the new status. ``version`` is the content
        version the page carried: ``PromptStale`` (nothing written) when it isn't the file's,
        ``PromptRefusal`` for an unsafe text, ``PromptUnavailable`` otherwise."""
        self._check(task, version)
        try:
            return self._editor.save_override(task, system, user, expected_content_version=version)
        except PromptConflict as problem:
            raise PromptStale from problem
        except PromptRefused as refused:
            raise PromptRefusal(refused.reason) from None
        except PromptError as problem:
            raise PromptUnavailable from problem

    def reset(self, task: str, *, version: str) -> None:
        """Remove the override (harmless without one). ``PromptStale`` when ``version`` isn't
        the file's, ``PromptUnavailable`` when it could not be removed. The editor checks the
        version under its write lock, so a save that lands in between is stale too."""
        try:
            self._editor.reset_override(task, expected_content_version=version)
        except PromptConflict as problem:
            raise PromptStale from problem
        except PromptError as problem:
            raise PromptUnavailable from problem

    def _check(self, task: str, version: str) -> None:
        if self.status(task).content_version != version:
            raise PromptStale
