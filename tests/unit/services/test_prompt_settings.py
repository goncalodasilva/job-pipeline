"""M9.3 part B: the Settings > Prompts service over the prompt editor (ADR-055, spec 3.3 and 3.6).

It checks the content version before anything else, maps the editor's errors to its own and never
carries the typed text in an error. A fictional defaults folder in a temp folder; no network.
"""

from collections.abc import Callable
from pathlib import Path

import pytest

from app.modules.ai.prompts import PromptLibrary
from app.services.prompt_settings import (
    PromptRefusal,
    PromptSettings,
    PromptStale,
    PromptUnavailable,
)
from tests.prompt_override_support import (
    EDIT_SYSTEM,
    EDIT_USER,
    MARKER,
    TASK,
    write_default,
    write_override_bytes,
)

NO_VERSION = "none"
STALE = "0" * 64


@pytest.fixture
def service(tmp_path: Path) -> PromptSettings:
    defaults = write_default(tmp_path)
    (tmp_path / "data").mkdir()  # the data folder exists; the editor makes the folders below it
    return PromptSettings(PromptLibrary(defaults, tmp_path / "data" / "prompts" / "overrides"))


def test_save_and_reset_round_trip(service: PromptSettings) -> None:
    saved = service.save(TASK, EDIT_SYSTEM, EDIT_USER, version=NO_VERSION)
    assert saved.overridden and saved.content_version != NO_VERSION
    text = service.override_text(TASK)
    assert text is not None and text.system.strip() == EDIT_SYSTEM

    service.reset(TASK, version=saved.content_version)

    assert service.status(TASK).overridden is False
    assert service.override_text(TASK) is None


def test_a_stale_version_wins_over_an_unsafe_text_and_writes_nothing(
    service: PromptSettings, tmp_path: Path
) -> None:
    first = service.save(TASK, EDIT_SYSTEM, EDIT_USER, version=NO_VERSION)
    unsafe = f"{{% include '{MARKER}.md' %}}"

    with pytest.raises(PromptStale):
        service.save(TASK, unsafe, unsafe, version=STALE)
    with pytest.raises(PromptStale):
        service.reset(TASK, version=NO_VERSION)

    assert service.status(TASK).content_version == first.content_version


def test_a_refused_text_carries_only_its_reason(service: PromptSettings) -> None:
    unsafe = f"{{% include '{MARKER}.md' %}}"

    with pytest.raises(PromptRefusal) as caught:
        service.save(TASK, EDIT_SYSTEM, EDIT_USER + unsafe, version=NO_VERSION)

    assert caught.value.reason == "include"
    assert MARKER not in str(caught.value) and MARKER not in repr(caught.value)
    assert service.status(TASK).overridden is False


def test_an_unknown_task_is_unavailable(service: PromptSettings) -> None:
    for call in (
        lambda: service.status("no_such_task"),
        lambda: service.default_text("no_such_task"),
        lambda: service.save("no_such_task", "s", "u", version=NO_VERSION),
        lambda: service.reset("no_such_task", version=NO_VERSION),
    ):
        with pytest.raises(PromptUnavailable):
            call()


def test_an_override_that_is_not_sections_has_no_text(
    service: PromptSettings, tmp_path: Path
) -> None:
    write_override_bytes(tmp_path / "data" / "prompts" / "overrides", b"not a prompt file")

    status = service.status(TASK)

    assert status.overridden and status.error
    assert service.override_text(TASK) is None


class RacingLibrary(PromptLibrary):
    """A library where something else saves, once, just before a reset reaches the files."""

    def __init__(self, defaults: Path, overrides: Path) -> None:
        super().__init__(defaults, overrides)
        self.before_reset: Callable[[], object] | None = None

    def reset_override(self, task: str, *, expected_content_version: str | None = None) -> None:
        action, self.before_reset = self.before_reset, None
        if action is not None:
            action()
        super().reset_override(task, expected_content_version=expected_content_version)


def test_a_save_landing_just_before_the_reset_makes_the_reset_stale(tmp_path: Path) -> None:
    defaults = write_default(tmp_path)
    (tmp_path / "data").mkdir()
    library = RacingLibrary(defaults, tmp_path / "data" / "prompts" / "overrides")
    service = PromptSettings(library)
    first = service.save(TASK, EDIT_SYSTEM, EDIT_USER, version=NO_VERSION)
    racing = f"{EDIT_SYSTEM}\nA fictional line from another tab."
    library.before_reset = lambda: library.save_override(
        TASK, racing, EDIT_USER, expected_content_version=first.content_version
    )

    with pytest.raises(PromptStale):
        service.reset(TASK, version=first.content_version)

    kept = library.override_text(TASK)
    assert library.status(TASK).overridden is True
    assert kept is not None and kept.system.strip() == racing
