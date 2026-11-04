"""M9.3 part A: prompt library status, save and reset (spec 3.3 and 3.6, ADR-055).

Assumed interface (the contract these tests pin; see the M9.3 task):

- ``app.modules.ai.prompts`` defines ``PromptStatus`` (frozen dataclass: ``default_version: int``,
  ``overridden: bool``, ``base_version: int | None``, ``error: str | None``,
  ``content_version: str``), ``PromptText`` (frozen: ``system: str``, ``user: str``, the plain
  section texts) and the errors ``PromptConflict(PromptError)`` (stale content version) and
  ``PromptRefused(PromptError)`` with a ``reason`` code. ``app.modules.ai`` re-exports all of
  them, plus the ``PromptEditor`` protocol, so services never import the prompt module.
- ``PromptLibrary.status(task)`` never raises for a broken override (it reports ``error``);
  ``default_text(task)`` and ``override_text(task)`` (``None`` without an override) give the
  section texts; ``save_override(task, system, user, *, expected_content_version)`` returns the
  new status; ``reset_override(task)`` never loads the override.
- ``content_version`` is the hex SHA-256 of the override file's bytes, or ``"none"``.

All content is fictional.
"""

import os
import shutil
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from app.domain.settings import AIProviderSettings
from app.modules.ai.executor import TaskExecutor
from app.modules.ai.prompts import (
    UNTRUSTED_NOTICE,
    PromptError,
    PromptLibrary,
    render_prompt,
)
from app.modules.ai.providers.fake import FAKE_FAST_MODEL, FAKE_THOROUGH_MODEL, FakeAIProvider
from app.modules.ai.tasks import TaskSpec
from tests.fakes.ai import NotCancelled, load_ai_fixture
from tests.fakes.usage import RecordingMeter
from tests.prompt_override_support import (
    DEFAULT_SYSTEM,
    DEFAULT_USER,
    EDIT_SYSTEM,
    EDIT_USER,
    MARKER,
    MARKER_NAME,
    MAX_FILE_BYTES,
    TASK,
    files_under,
    prompt_file,
    sha256,
    snapshot,
    write_default,
    write_override_bytes,
)

INPUTS: dict[str, object] = {
    "job_text": "Example Ltd is hiring a fictional lighthouse keeper in a fictional town.",
    "preferences": {"target_roles": ["Lighthouse Keeper"]},
}


def public(name: str) -> Any:
    import importlib

    module = importlib.import_module("app.modules.ai.prompts")
    assert hasattr(module, name), f"app.modules.ai.prompts.{name} doesn't exist yet"
    return getattr(module, name)


class Folders:
    """A defaults folder and an overrides folder under one temporary data folder."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.data = root / "data"
        self.data.mkdir()
        self.defaults = write_default(root)
        self.overrides = self.data / "prompts" / "overrides"
        self.library = PromptLibrary(self.defaults, self.overrides)

    @property
    def task_folder(self) -> Path:
        return self.overrides / TASK

    @property
    def prompt_path(self) -> Path:
        return self.task_folder / "prompt.md"

    def save(self, system: str = EDIT_SYSTEM, user: str = EDIT_USER, expected: str = "none") -> Any:
        return self.library.save_override(TASK, system, user, expected_content_version=expected)

    def bump_default(self, **meta: Any) -> None:
        write_default(self.root, **meta)


@pytest.fixture
def folders(tmp_path: Path) -> Folders:
    return Folders(tmp_path)


@pytest.fixture
def open_umask() -> Iterator[None]:
    """No umask, so a private mode can only come from the code asking for it."""
    previous = os.umask(0)
    try:
        yield
    finally:
        os.umask(previous)


# --- status and text ---------------------------------------------------------------------------


def test_status_without_override_and_default_text(folders: Folders) -> None:
    status = folders.library.status(TASK)

    assert status.default_version == 3
    assert status.overridden is False
    assert status.base_version is None
    assert status.error is None
    assert status.content_version == "none"
    text = folders.library.default_text(TASK)
    assert text.system == DEFAULT_SYSTEM and text.user == DEFAULT_USER
    assert folders.library.override_text(TASK) is None


def test_status_names_the_defaults_inputs_and_which_are_untrusted(folders: Folders) -> None:
    """The editor shows the names a prompt may use (M9.3 fix pass 2): they are the default's,
    with or without an override, and also when the override doesn't load."""
    expected = (("job_text", "preferences"), ("job_text",))

    without = folders.library.status(TASK)
    assert (without.inputs, without.untrusted_inputs) == expected

    folders.save()
    saved = folders.library.status(TASK)
    assert (saved.inputs, saved.untrusted_inputs) == expected

    folders.prompt_path.write_bytes(b"this is not a prompt file")
    broken = folders.library.status(TASK)
    assert broken.error
    assert (broken.inputs, broken.untrusted_inputs) == expected


def test_status_inputs_default_to_none_for_a_status_made_without_them() -> None:
    from app.modules.ai import PromptStatus

    bare = PromptStatus(1, False, None, None, "none")
    assert bare.inputs == () and bare.untrusted_inputs == ()


def test_override_records_base_version_and_status(folders: Folders) -> None:
    saved = folders.save()

    data = folders.prompt_path.read_bytes()
    assert saved == folders.library.status(TASK)
    assert saved.overridden is True
    assert saved.default_version == 3 and saved.base_version == 3
    assert saved.error is None
    assert saved.content_version == sha256(data)
    # the front matter is the default's, with the version the edit is based on
    front, _, _ = data.decode().removeprefix("---\n").partition("\n---\n")
    meta = yaml.safe_load(front)
    assert meta == {
        "id": TASK,
        "version": 3,
        "task": TASK,
        "default_tier": "fast",
        "output_schema": "schema.json",
        "inputs": ["job_text", "preferences"],
        "untrusted_inputs": ["job_text"],
    }
    # only the two sections are the editor's; the loader sees them as saved
    template = folders.library.load(TASK)
    assert template.overridden is True and template.version == "3+override"
    assert template.system_source == EDIT_SYSTEM and template.user_source == EDIT_USER
    text = folders.library.override_text(TASK)
    assert text is not None and text.system == EDIT_SYSTEM and text.user == EDIT_USER
    assert folders.library.default_text(TASK).system == DEFAULT_SYSTEM
    # the folder holds the one file: no temporary file is left behind
    assert [p.name for p in folders.task_folder.iterdir()] == ["prompt.md"]


def test_ac_m9_2_override_survives_updated_default(folders: Folders) -> None:
    folders.save()
    folders.bump_default(version=4, system="A newer fictional default system text.")

    status = folders.library.status(TASK)
    assert status.overridden is True and status.error is None
    assert status.default_version == 4 and status.base_version == 3
    assert status.base_version != status.default_version  # "The default was updated (v3 -> v4)"
    template = folders.library.load(TASK)
    assert template.version == "4+override"
    assert template.system_source == EDIT_SYSTEM and template.user_source == EDIT_USER
    rendered = render_prompt(template, INPUTS)
    assert rendered.prompt_version == "4+override"
    assert rendered.system.startswith(UNTRUSTED_NOTICE)
    assert "A newer fictional default system text." not in rendered.system
    assert "A newer fictional default system text." in folders.library.default_text(TASK).system

    # saving again bases the edit on the current default, so the notice goes away
    again = folders.save(expected=status.content_version)
    assert again.base_version == 4 and again.default_version == 4


def test_ac_m9_2_reset_restores_default(folders: Folders) -> None:
    default = folders.library.load(TASK)
    folders.save()
    assert folders.library.load(TASK).overridden is True

    folders.library.reset_override(TASK)

    assert folders.library.load(TASK) == default
    status = folders.library.status(TASK)
    assert status.overridden is False and status.base_version is None
    assert status.content_version == "none" and status.error is None
    assert not folders.task_folder.exists()
    folders.library.reset_override(TASK)  # nothing to reset is not an error
    assert folders.library.override_text(TASK) is None


def test_reset_keeps_unrelated_files_and_reports_plainly(folders: Folders) -> None:
    folders.save()
    note = folders.task_folder / "notes.txt"
    note.write_bytes(b"fictional note kept by the user")

    with pytest.raises(PromptError) as caught:
        folders.library.reset_override(TASK)

    assert str(caught.value)
    assert not folders.prompt_path.exists()  # the override is no longer in effect
    assert note.read_bytes() == b"fictional note kept by the user"
    assert folders.library.load(TASK).overridden is False
    assert folders.library.status(TASK).overridden is False


@pytest.mark.parametrize("change", ["inputs", "untrusted_inputs"])
async def test_incompatible_default_reports_error_and_offers_reset(
    folders: Folders, change: str
) -> None:
    folders.save()
    if change == "inputs":
        folders.bump_default(version=4, inputs=("job_text", "preferences", "locale"))
    else:
        folders.bump_default(
            version=4,
            untrusted=("job_text", "preferences"),
            user=(
                '{{ preferences | untrusted("preferences", "pref-1") }}\n\n'
                '{{ job_text | untrusted("job_posting", "posting-1") }}'
            ),
        )

    status = folders.library.status(TASK)  # does not raise
    assert status.overridden is True
    assert status.default_version == 4 and status.base_version == 3
    assert isinstance(status.error, str) and status.error
    assert status.content_version == sha256(folders.prompt_path.read_bytes())
    # no silent fallback: the load fails, and so does a task that needs the prompt
    with pytest.raises(PromptError):
        folders.library.load(TASK)
    provider = FakeAIProvider()
    provider.script(TASK, load_ai_fixture(TASK, "ok"))
    settings = AIProviderSettings()
    executor = TaskExecutor(
        provider=provider,
        prompts=folders.library,
        settings=lambda: settings,
        provider_defaults={"fast": FAKE_FAST_MODEL, "thorough": FAKE_THOROUGH_MODEL},
        specs={TASK: TaskSpec(TASK, "extraction")},
    )
    with pytest.raises(PromptError):
        await executor.run_task(TASK, INPUTS, NotCancelled(), meter=RecordingMeter())
    assert provider.requests == []
    # the text can still be read, so the user can see what they wrote before resetting
    text = folders.library.override_text(TASK)
    assert text is not None and text.system == EDIT_SYSTEM

    folders.library.reset_override(TASK)  # never loads the override

    assert folders.library.load(TASK).version == "4"
    clean = folders.library.status(TASK)
    assert clean.overridden is False and clean.error is None and clean.default_version == 4


# --- broken or odd override files ----------------------------------------------------------------


def _bad_override_bytes(name: str) -> bytes:
    body = f"{MARKER} {{{{ job_text }}}}"
    if name == "garbage":
        return b"\xff\xfe\x00 not text " + MARKER.encode()
    if name == "empty":
        return b""
    if name == "no_front_matter":
        return f"Just fictional text {MARKER}.".encode()
    if name == "unwrapped_untrusted":
        return prompt_file(EDIT_SYSTEM, body)
    if name == "oversize":
        return prompt_file(EDIT_SYSTEM, EDIT_USER + "\n" + "x" * MAX_FILE_BYTES)
    if name == "other_id":
        return prompt_file(EDIT_SYSTEM, EDIT_USER, prompt_id="other_thing")
    if name == "tier_changed":
        return prompt_file(EDIT_SYSTEM, EDIT_USER, tier="thorough")
    if name == "dropped_untrusted":
        return prompt_file(EDIT_SYSTEM, f"{{{{ preferences | tojson }}}} {MARKER}", untrusted=())
    raise AssertionError(name)


BAD_OVERRIDES = [
    "garbage",
    "empty",
    "no_front_matter",
    "unwrapped_untrusted",
    "oversize",
    "other_id",
    "tier_changed",
    "dropped_untrusted",
]


@pytest.mark.parametrize("name", BAD_OVERRIDES)
def test_status_reports_an_unloadable_override_and_reset_still_works(
    folders: Folders, name: str
) -> None:
    data = _bad_override_bytes(name)
    write_override_bytes(folders.overrides, data)

    status = folders.library.status(TASK)  # never raises

    assert status.overridden is True
    assert status.default_version == 3
    assert isinstance(status.error, str) and status.error
    assert MARKER not in status.error and MARKER_NAME not in status.error
    assert status.content_version == sha256(data)
    with pytest.raises(PromptError):
        folders.library.load(TASK)
    folders.library.reset_override(TASK)
    assert folders.library.load(TASK).overridden is False
    after = folders.library.status(TASK)
    assert after.overridden is False and after.error is None and after.content_version == "none"


def test_status_of_an_absurdly_large_override_does_not_read_the_file(
    folders: Folders, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A status check stays cheap whatever is on disk: above the hashing cap the file is not
    opened for reading at all. It reports an error, a content version that is still a 64-char
    hex string and changes with the file, and Reset and Save (with that version) still work."""
    from app.modules.ai import prompts

    size = prompts.MAX_HASHED_OVERRIDE_BYTES + 1
    assert size > MAX_FILE_BYTES
    path = write_override_bytes(folders.overrides, b"")
    with path.open("r+b") as stream:
        stream.truncate(size)  # a sparse file: no data is written
    monkeypatch.setattr(
        prompts.os, "fdopen", lambda *args, **kwargs: pytest.fail("the large file was read")
    )

    status = folders.library.status(TASK)

    assert status.overridden is True and status.error
    assert "KiB" in status.error
    assert len(status.content_version) == 64 and status.content_version != "none"
    assert int(status.content_version, 16) >= 0
    assert folders.library.status(TASK).content_version == status.content_version
    with path.open("r+b") as stream:
        stream.truncate(size + 1)
    assert folders.library.status(TASK).content_version != status.content_version
    monkeypatch.undo()
    again = folders.library.status(TASK)
    saved = folders.save(expected=again.content_version)  # replaces it, as the editor would
    assert saved.error is None and path.stat().st_size < size


def test_unsafe_body_still_reports_the_version_it_was_based_on(folders: Folders) -> None:
    write_override_bytes(folders.overrides, _bad_override_bytes("unwrapped_untrusted"))

    status = folders.library.status(TASK)

    assert status.base_version == 3 and status.error


def test_restored_override_that_is_unsafe_reports_error_in_text_too(folders: Folders) -> None:
    write_override_bytes(folders.overrides, _bad_override_bytes("unwrapped_untrusted"))

    text = folders.library.override_text(TASK)

    assert text is not None and MARKER in text.user  # shown to the user, escaped by the page


@pytest.mark.parametrize("case", ["prompt_file_link", "task_folder_file", "prompt_file_folder"])
def test_status_reports_links_and_odd_entries_without_following_them(
    folders: Folders, tmp_path: Path, case: str
) -> None:
    outside = tmp_path / "outside.md"
    outside.write_bytes(prompt_file(EDIT_SYSTEM, EDIT_USER) + MARKER.encode())
    folders.overrides.mkdir(parents=True)
    if case == "prompt_file_link":
        folders.task_folder.mkdir()
        folders.prompt_path.symlink_to(outside)
    elif case == "task_folder_file":
        folders.task_folder.write_bytes(b"fictional file where a folder belongs")
    else:
        folders.prompt_path.mkdir(parents=True)

    status = folders.library.status(TASK)  # never raises

    assert isinstance(status.error, str) and status.error
    assert MARKER not in status.error
    with pytest.raises(PromptError):
        folders.library.load(TASK)
    assert outside.read_bytes().endswith(MARKER.encode())


def test_status_and_text_reads_write_nothing(folders: Folders, tmp_path: Path) -> None:
    # no overrides folder at all: reading creates none
    before = snapshot(tmp_path)
    folders.library.status(TASK)
    folders.library.default_text(TASK)
    assert folders.library.override_text(TASK) is None
    assert snapshot(tmp_path) == before
    assert not (folders.data / "prompts").exists()

    # a valid override and then a broken one
    folders.save()
    for _ in range(2):
        before = snapshot(tmp_path)
        folders.library.status(TASK)
        folders.library.default_text(TASK)
        folders.library.override_text(TASK)
        assert snapshot(tmp_path) == before
        folders.prompt_path.write_bytes(_bad_override_bytes("unwrapped_untrusted"))

    # no overrides folder configured
    assert PromptLibrary(folders.defaults).status(TASK).overridden is False


def test_content_version_is_the_hash_of_the_file_bytes(folders: Folders) -> None:
    first = folders.save()
    assert first.content_version == sha256(folders.prompt_path.read_bytes())
    assert len(first.content_version) == 64 and first.content_version != "none"

    same = folders.save(expected=first.content_version)
    assert same.content_version == first.content_version  # same text, same bytes

    changed = folders.save(user=EDIT_USER + " Please be brief.", expected=first.content_version)
    assert changed.content_version != first.content_version
    assert changed.content_version == sha256(folders.prompt_path.read_bytes())

    folders.prompt_path.write_bytes(folders.prompt_path.read_bytes() + b"\n")
    assert folders.library.status(TASK).content_version != changed.content_version


def test_save_refuses_a_stale_content_version(folders: Folders) -> None:
    conflict = public("PromptConflict")
    refused = public("PromptRefused")
    assert issubclass(conflict, PromptError) and not issubclass(conflict, refused)
    first = folders.save()
    saved = folders.prompt_path.read_bytes()

    with pytest.raises(conflict):  # "none" no longer matches: an override appeared
        folders.save(user=EDIT_USER + " Another.", expected="none")
    assert folders.prompt_path.read_bytes() == saved

    second = folders.save(user=EDIT_USER + " Second.", expected=first.content_version)
    after_second = folders.prompt_path.read_bytes()
    with pytest.raises(conflict):  # the old version is stale now
        folders.save(user=EDIT_USER + " Third.", expected=first.content_version)
    assert folders.prompt_path.read_bytes() == after_second

    folders.library.reset_override(TASK)
    with pytest.raises(conflict):  # the override the form was based on is gone
        folders.save(expected=second.content_version)
    assert not folders.prompt_path.exists()


# --- the same safety checks as the loader ----------------------------------------------


def _unsafe_texts(case: str) -> tuple[str, str, str | None]:
    """(system, user, expected refusal reason or None when only a PromptError is promised)."""
    if case == "unwrapped_untrusted":
        return EDIT_SYSTEM, f"{{{{ job_text }}}} {MARKER}", "unwrapped_untrusted"
    if case == "include":
        return EDIT_SYSTEM, EDIT_USER + f'\n{{% include "{MARKER_NAME}.md" %}}', "include"
    if case == "undeclared_input":
        return EDIT_SYSTEM, EDIT_USER + f"\n{{{{ {MARKER_NAME} }}}}", "undeclared_input"
    if case == "oversize":
        return EDIT_SYSTEM, EDIT_USER + f"\n{MARKER}\n" + "x" * MAX_FILE_BYTES, "oversize"
    if case == "front_matter_in_body":
        return EDIT_SYSTEM, EDIT_USER + f"\n---\nid: {MARKER_NAME}\n---\n", "front_matter_in_body"
    if case == "front_matter_in_system":
        return EDIT_SYSTEM + "\n---  \n" + MARKER, EDIT_USER, "front_matter_in_body"
    if case == "stray_user_header":
        return EDIT_SYSTEM + f"\n## user\n{MARKER}", EDIT_USER, "stray_section_header"
    if case == "stray_system_header":
        return EDIT_SYSTEM, EDIT_USER + f"\n## system\n{MARKER}", "stray_section_header"
    if case == "untrusted_in_system":
        return EDIT_SYSTEM + " {{ job_text }} " + MARKER, EDIT_USER, None
    if case == "import":
        return EDIT_SYSTEM, EDIT_USER + f'\n{{% import "{MARKER_NAME}.md" as x %}}', None
    if case == "extends":
        return EDIT_SYSTEM, f'{{% extends "{MARKER_NAME}.md" %}}' + EDIT_USER, None
    if case == "syntax_error":
        return EDIT_SYSTEM, EDIT_USER + f"\n{{% if %}} {MARKER}", None
    raise AssertionError(case)


UNSAFE_REQUIRED = [
    "unwrapped_untrusted",
    "include",
    "undeclared_input",
    "oversize",
    "front_matter_in_body",
]
UNSAFE_MORE = [
    "front_matter_in_system",
    "stray_user_header",
    "stray_system_header",
    "untrusted_in_system",
    "import",
    "extends",
    "syntax_error",
]


@pytest.mark.parametrize("prior", [False, True], ids=["no_override", "valid_override"])
@pytest.mark.parametrize("case", UNSAFE_REQUIRED + UNSAFE_MORE)
def test_prompt_editor_refuses_unsafe_template(folders: Folders, case: str, prior: bool) -> None:
    refused_error = public("PromptRefused")
    system, user, reason = _unsafe_texts(case)
    expected = "none"
    if prior:
        expected = folders.save().content_version
    before = snapshot(folders.root)

    with pytest.raises(refused_error) as caught:
        folders.save(system, user, expected=expected)

    assert isinstance(caught.value, PromptError)
    if reason is not None:
        assert caught.value.reason == reason
    message = str(caught.value)
    assert message and "kestrel" not in message.lower()  # a fixed message, no user text
    assert snapshot(folders.root) == before  # the old file (or none) and no temporary file
    if prior:
        assert folders.library.load(TASK).user_source == EDIT_USER
    else:
        assert not folders.overrides.exists() or files_under(folders.overrides) == []


def test_a_safe_template_with_loops_and_conditions_is_accepted(folders: Folders) -> None:
    user = (
        "{% if preferences %}Preferences: {{ preferences | tojson }}{% endif %}\n"
        "{% for n in preferences.target_roles %}Line {{ n }}\n{% endfor %}\n"
        '{{ job_text | untrusted("job_posting", "posting-1") }}'
    )

    saved = folders.save(user=user)

    assert saved.error is None
    inputs = {**INPUTS, "preferences": {"target_roles": ["Lighthouse Keeper", "Chart Keeper"]}}
    assert render_prompt(folders.library.load(TASK), inputs).user.count("Line") == 2


@pytest.mark.parametrize(
    "task", ["../escape", "Upper", "", "a/b", "with space", "no_such_task", "x" * 80]
)
def test_task_names_are_validated_and_need_a_default(folders: Folders, task: str) -> None:
    before = snapshot(folders.root)
    calls = (
        lambda: folders.library.status(task),
        lambda: folders.library.default_text(task),
        lambda: folders.library.override_text(task),
        lambda: folders.library.save_override(
            task, EDIT_SYSTEM, EDIT_USER, expected_content_version="none"
        ),
        lambda: folders.library.reset_override(task),
    )
    for call in calls:
        with pytest.raises(PromptError):
            call()
    assert snapshot(folders.root) == before
    assert not (folders.data / "prompts").exists()


# --- writing -----------------------------------------------------------------------------


def test_override_write_atomic_private_and_links_refused(
    folders: Folders, monkeypatch: pytest.MonkeyPatch, open_umask: None
) -> None:
    assert not (folders.data / "prompts").exists()

    first = folders.save()

    # modes: file 0600; the folders it made 0700, whatever the umask
    def mode(path: Path) -> int:
        return stat.S_IMODE(os.lstat(path).st_mode)

    assert mode(folders.prompt_path) == 0o600
    assert mode(folders.task_folder) == 0o700
    assert mode(folders.overrides) == 0o700
    assert mode(folders.data / "prompts") == 0o700
    assert files_under(folders.overrides) == [f"{TASK}/prompt.md"]  # no temporary file
    old = folders.prompt_path.read_bytes()

    # a replace that fails leaves the old file and no temporary file
    def broken_replace(*args: object, **kwargs: object) -> None:
        raise OSError(f"disk full {MARKER} {folders.data}")

    monkeypatch.setattr(os, "replace", broken_replace)
    with pytest.raises(PromptError) as caught:
        folders.save(user=EDIT_USER + " Edited.", expected=first.content_version)
    assert MARKER not in str(caught.value) and str(folders.data) not in str(caught.value)
    assert folders.prompt_path.read_bytes() == old
    assert files_under(folders.overrides) == [f"{TASK}/prompt.md"]
    assert folders.library.status(TASK) == first
    monkeypatch.undo()

    # the same on a first save: nothing is left
    folders.library.reset_override(TASK)
    monkeypatch.setattr(os, "replace", broken_replace)
    with pytest.raises(PromptError):
        folders.save()
    monkeypatch.undo()
    assert files_under(folders.overrides) == []
    assert folders.library.status(TASK).overridden is False


def _link_case(root: Path, case: str) -> tuple[Path, Path]:
    """(overrides folder, outside folder) with a link at the named component."""
    data = root / "data"
    data.mkdir(exist_ok=True)
    outside = root / "outside"
    outside.mkdir()
    overrides = data / "prompts" / "overrides"
    if case == "prompts_link":
        (outside / "overrides" / TASK).mkdir(parents=True)
        (outside / "overrides" / TASK / "prompt.md").write_bytes(b"fictional outside bytes")
        (data / "prompts").symlink_to(outside, target_is_directory=True)
    elif case == "overrides_link":
        (outside / TASK).mkdir()
        (outside / TASK / "prompt.md").write_bytes(b"fictional outside bytes")
        (data / "prompts").mkdir()
        overrides.symlink_to(outside, target_is_directory=True)
    elif case == "task_link":
        (outside / "prompt.md").write_bytes(b"fictional outside bytes")
        overrides.mkdir(parents=True)
        (overrides / TASK).symlink_to(outside, target_is_directory=True)
    elif case == "file_link":
        (outside / "target.md").write_bytes(b"fictional outside bytes")
        (overrides / TASK).mkdir(parents=True)
        (overrides / TASK / "prompt.md").symlink_to(outside / "target.md")
    else:
        raise AssertionError(case)
    return overrides, outside


LINK_CASES = ["prompts_link", "overrides_link", "task_link", "file_link"]


@pytest.mark.parametrize("case", LINK_CASES)
def test_override_write_refuses_links(tmp_path: Path, case: str) -> None:
    write_default(tmp_path)
    overrides, outside = _link_case(tmp_path, case)
    library = PromptLibrary(tmp_path / "defaults", overrides)
    before = snapshot(outside)

    with pytest.raises(PromptError):
        library.save_override(TASK, EDIT_SYSTEM, EDIT_USER, expected_content_version="none")

    assert snapshot(outside) == before  # nothing was written through the link


@pytest.mark.parametrize("case", ["prompts_link", "overrides_link"])
def test_override_reset_refuses_links_in_the_folders_above(tmp_path: Path, case: str) -> None:
    write_default(tmp_path)
    overrides, outside = _link_case(tmp_path, case)
    library = PromptLibrary(tmp_path / "defaults", overrides)
    before_outside = snapshot(outside)
    before_data = snapshot(tmp_path / "data")

    with pytest.raises(PromptError):
        library.reset_override(TASK)

    assert snapshot(outside) == before_outside  # nothing was deleted through the link
    assert snapshot(tmp_path / "data") == before_data  # and the link itself is left alone


@pytest.mark.parametrize("case", ["task_link", "file_link"])
def test_override_reset_removes_a_link_entry_itself_and_never_its_target(
    tmp_path: Path, case: str
) -> None:
    """Changed from "refuses every link" (M9.3 A decision): a link in the override's place
    (``prompt.md`` or the task folder) would leave the user stuck with a broken prompt they
    can't reset. Spec 3.3 says links are never followed; removing the link entry itself
    follows nothing. Only that entry goes; the target and anything outside stay as they are."""
    write_default(tmp_path)
    overrides, outside = _link_case(tmp_path, case)
    library = PromptLibrary(tmp_path / "defaults", overrides)
    before_outside = snapshot(outside)
    assert library.status(TASK).error  # the link is reported, not followed

    library.reset_override(TASK)

    assert snapshot(outside) == before_outside  # the target is untouched
    assert not os.path.lexists(overrides / TASK / "prompt.md")
    assert not os.path.lexists(overrides / TASK)  # the link, or the folder it left empty
    status = library.status(TASK)
    assert status.overridden is False and status.error is None
    assert library.load(TASK).overridden is False
    library.reset_override(TASK)  # and again is a no-op


def test_save_never_follows_a_link_swapped_in_after_a_good_save(tmp_path: Path) -> None:
    (tmp_path / "again").mkdir()
    folders = Folders(tmp_path / "again")
    folders.save()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    shutil.rmtree(folders.task_folder)
    folders.task_folder.symlink_to(outside, target_is_directory=True)

    with pytest.raises(PromptError):
        folders.save()

    assert list(outside.iterdir()) == []
