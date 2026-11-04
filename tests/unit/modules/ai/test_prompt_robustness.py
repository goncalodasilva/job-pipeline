"""M9.3 fix pass 1: the prompt library's robustness (spec 3.3 and 3.6, ADR-055).

A user-written prompt, or a file restored from an export, must never take the app down or tie it
up: deep nesting (``RecursionError``), render-time resource bombs (a huge ``%`` width, allocation
filters and methods, doubling ``{% set %}``, nested literal loops) and crash leftovers are all
handled by the one checking path that save and load share. Also here: the reset version guard,
the editor's rarely taken file branches, and the "default updated" rule for a base version above
the default's. Everything is fictional; a temporary data folder, no network.
"""

import dataclasses
import errno
import os
from pathlib import Path
from typing import Any

import pytest
from jinja2 import nodes
from jinja2.exceptions import SecurityError

from app.modules.ai import prompts
from app.modules.ai.prompt_types import REFUSAL_PROBLEMS
from app.modules.ai.prompts import (
    PromptConflict,
    PromptError,
    PromptLibrary,
    PromptRefused,
    _PromptEnvironment,
    render_prompt,
)
from tests.prompt_override_support import (
    EDIT_SYSTEM,
    EDIT_USER,
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
TEMP_NAME = ".prompt.md.0123456789abcdef.tmp"


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


@pytest.fixture
def folders(tmp_path: Path) -> Folders:
    return Folders(tmp_path)


class RenderedAnyway(BaseException):
    """Not an ``Exception``: the renderer's catch-all can't swallow it."""


def never_render(*args: object, **kwargs: object) -> None:
    raise RenderedAnyway


def fixed(reason: str) -> str:
    """The message the editor gives for a refusal: fixed words, never the user's text."""
    return f"Prompt override {TASK!r}: {REFUSAL_PROBLEMS[reason]}"  # type: ignore[index]


# --- deep nesting is a refusal, never a crash -------------------------------------------------

DEEP = {
    "parentheses": "{{ " + "(" * 5000 + "1" + ")" * 5000 + " }}",
    "ifs": "{% if preferences %}" * 3000 + "x" + "{% endif %}" * 3000,
    "unary_minus": "{{ " + "-" * 5000 + "1 }}",
    "brackets": "{{ " + "[" * 5000 + "]" * 5000 + " }}",
}


@pytest.mark.parametrize("section", ["system", "user"])
@pytest.mark.parametrize("name", sorted(DEEP))
def test_deeply_nested_text_is_refused_at_save(folders: Folders, name: str, section: str) -> None:
    before = snapshot(folders.overrides)
    system, user = EDIT_SYSTEM, EDIT_USER
    if section == "system":
        system += "\n" + DEEP[name]
    else:
        user += "\n" + DEEP[name]

    with pytest.raises(PromptRefused) as refused:
        folders.save(system, user)

    assert refused.value.reason == "invalid_template"
    assert str(refused.value) == fixed("invalid_template")
    assert snapshot(folders.overrides) == before and not folders.task_folder.exists()


@pytest.mark.parametrize("name", sorted(DEEP))
def test_deeply_nested_file_reports_an_error_and_can_be_reset(folders: Folders, name: str) -> None:
    write_override_bytes(folders.overrides, prompt_file(EDIT_SYSTEM, EDIT_USER + "\n" + DEEP[name]))

    status = folders.library.status(TASK)
    assert status.overridden is True and status.base_version == 3
    assert status.error == fixed("invalid_template")
    with pytest.raises(PromptError) as raised:
        folders.library.load(TASK)
    assert not isinstance(raised.value, RecursionError)
    text = folders.library.override_text(TASK)  # the text can still be read, to be fixed
    assert text is not None and text.system == EDIT_SYSTEM

    folders.library.reset_override(TASK)

    assert folders.library.status(TASK).overridden is False
    assert folders.library.load(TASK).overridden is False


@pytest.mark.parametrize("name", sorted(DEEP))
def test_render_prompt_maps_deep_nesting_to_a_prompt_error(folders: Folders, name: str) -> None:
    template = dataclasses.replace(folders.library.load(TASK), user_source=DEEP[name])

    with pytest.raises(PromptError, match=TASK) as raised:
        render_prompt(template, {"job_text": "x", "preferences": "y"})

    assert not isinstance(raised.value, RecursionError)


@pytest.mark.parametrize(
    "inputs",
    [
        pytest.param("[" * 5000 + "]" * 5000, id="nested_list"),
        pytest.param("{" * 5000 + "}" * 5000, id="nested_mapping"),
    ],
)
def test_deeply_nested_front_matter_is_an_error_not_a_crash(folders: Folders, inputs: str) -> None:
    data = prompt_file(EDIT_SYSTEM, EDIT_USER).replace(
        b"inputs: [job_text, preferences]", b"inputs: " + inputs.encode()
    )
    write_override_bytes(folders.overrides, data)

    status = folders.library.status(TASK)

    assert status.overridden is True and isinstance(status.error, str) and status.error
    with pytest.raises(PromptError):
        folders.library.load(TASK)
    folders.library.reset_override(TASK)
    assert folders.library.status(TASK).overridden is False


def test_memory_error_while_checking_is_an_error_too(
    folders: Folders, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = prompts.find_undeclared_variables

    def runs_out_on_the_marker(ast: Any) -> Any:
        if any(node.value == "memory-marker" for node in ast.find_all(nodes.Const)):
            raise MemoryError
        return real(ast)

    monkeypatch.setattr(prompts, "find_undeclared_variables", runs_out_on_the_marker)
    marked = EDIT_USER + '\n{{ "memory-marker" }}'

    with pytest.raises(PromptRefused) as refused:
        folders.save(EDIT_SYSTEM, marked)
    assert refused.value.reason == "invalid_template"
    write_override_bytes(folders.overrides, prompt_file(EDIT_SYSTEM, marked))
    assert folders.library.status(TASK).error == fixed("invalid_template")


# --- render-time resource bombs are refused by the static checks ------------------------------

BOMBS = {
    "percent_format": "{{ '%2000000000d' % 1 }}",
    "percent_format_input": "{{ job_text % 1 }}",
    "center_filter": "{{ 'a' | center(1900000000) }}",
    "center_method": "{{ 'a'.center(1900000000) }}",
    "center_by_subscript": "{{ 'a'['center'](1900000000) }}",
    "ljust_method": "{{ preferences.ljust(1900000000) }}",
    "zfill_method": "{{ preferences.zfill(1900000000) }}",
    "format_method": "{{ '{:2000000000}'.format(1) }}",
    "format_filter": "{{ '%2000000000d' | format(1) }}",
    "indent_filter": "{{ preferences | indent(1900000000) }}",
    "wordwrap_filter": "{{ preferences | wordwrap(1) }}",
    "truncate_filter": "{{ preferences | truncate(1900000000) }}",
    "replace_filter": "{{ preferences | replace('a', 'b') }}",
    "map_filter": "{{ preferences | map('upper') | list }}",
    "filesizeformat_filter": "{{ 5 | filesizeformat }}",
    "urlize_filter": "{{ preferences | urlize }}",
    "xmlattr_filter": "{{ preferences | xmlattr }}",
    "called_name": "{% set f = preferences.center %}{{ f(1900000000) }}",
    "repeat_string": "{{ 'a' * 99999 }}",
    "repeat_input": "{{ job_text * 99999 }}",
    "power_string": "{{ 'a' ** 2 }}",
    "set_doubling": "{% set s = 'ab' %}" + "{% set s = s ~ s %}" * 22 + "{{ s | length }}",
    "set_concat": "{% set s = preferences ~ preferences %}{{ s | length }}",
    "set_add": "{% set s = [1] + [2] %}{{ s | length }}",
    "set_self_reference": "{% set s = [s, s] %}{{ s | length }}",
    "set_chain": "{% set a = [preferences, preferences] %}{% set b = [a, a] %}{{ b | length }}",
    "many_sets": "".join(f"{{% set v{n} = preferences %}}" for n in range(9)),
    "three_level_literal_loops": (
        "{% for a in [1, 2, 3] %}{% for b in [1, 2, 3] %}{% for c in [1, 2, 3] %}x"
        "{% endfor %}{% endfor %}{% endfor %}"
    ),
    "literal_loop": "{% for a in [1, 2, 3] %}x{% endfor %}",
    "string_loop": "{% for a in 'abc' %}x{% endfor %}",
    "concatenated_loop": "{% for a in preferences + preferences %}x{% endfor %}",
    "product_loop": "{% for a in preferences %}{% for b in preferences %}x{% endfor %}{% endfor %}",
    "loop_over_set_literal": "{% set v = [1, 2] %}{% for a in v %}x{% endfor %}",
    "three_rooted_loops": (
        "{% for a in preferences %}{% for b in a %}{% for c in b %}x{% endfor %}{% endfor %}"
        "{% endfor %}"
    ),
    "wide_join_separator": "{{ preferences | join('" + "x" * 65 + "') }}",
    "join_separator_from_input": "{{ preferences | join(job_text) }}",
}


@pytest.mark.parametrize("name", sorted(BOMBS))
def test_resource_bomb_is_refused_at_save_with_a_fixed_message(
    folders: Folders, name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("jinja2.environment.Template.render", never_render)
    before = snapshot(folders.overrides)

    with pytest.raises(PromptRefused) as refused:
        folders.save(EDIT_SYSTEM, EDIT_USER + "\n" + BOMBS[name])

    assert refused.value.reason == "unsupported_expression"
    assert str(refused.value) == fixed("unsupported_expression")
    assert snapshot(folders.overrides) == before and not folders.task_folder.exists()


@pytest.mark.parametrize("name", sorted(BOMBS))
def test_resource_bomb_file_reports_an_error_without_rendering_anything(
    folders: Folders, name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("jinja2.environment.Template.render", never_render)
    write_override_bytes(
        folders.overrides, prompt_file(EDIT_SYSTEM, EDIT_USER + "\n" + BOMBS[name])
    )

    status = folders.library.status(TASK)
    assert status.overridden is True
    assert status.error == fixed("unsupported_expression")
    with pytest.raises(PromptRefused):
        folders.library.load(TASK)
    assert folders.library.override_text(TASK) is not None
    folders.library.reset_override(TASK)
    assert folders.library.status(TASK).overridden is False


@pytest.mark.parametrize("name", ["percent_format", "center_filter", "set_doubling"])
def test_render_prompt_refuses_a_bomb_in_a_template_built_by_hand(
    folders: Folders, name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``render_prompt`` runs the same static checks, so a template that skipped the loader
    still renders nothing."""

    monkeypatch.setattr("jinja2.environment.Template.render", never_render)
    template = dataclasses.replace(folders.library.load(TASK), user_source=BOMBS[name])

    with pytest.raises(PromptError, match=TASK):
        render_prompt(template, {"job_text": "x", "preferences": "y"})


ALLOWED = {
    "items_loop": "{% for key, value in preferences.items() %}{{ key }}: {{ value | trim }}\n"
    "{% endfor %}",
    "nested_loop_over_the_item": "{% for a in preferences %}{% for b in a %}{{ b }}{% endfor %}"
    "{% endfor %}",
    "loop_over_an_attribute": "{% for role in preferences.target_roles %}{{ role }}{% endfor %}",
    "loop_over_a_subscript": "{% for role in preferences['target_roles'] %}{{ role }}{% endfor %}",
    "numbers": "{{ 7 % 3 }} {{ 2 * 3 }} {{ 2 ** 3 }} {{ -2 * 3 }}",
    "set_from_input": "{% set total = preferences | length %}{{ total }}",
    "join": '{{ preferences | join(", ") }}',
    "filters": '{{ preferences | string | lower | upper | trim | default("none") }}',
    "list_filters": "{{ preferences.target_roles | sort | unique | list | first }}"
    "{{ preferences.target_roles | last }}",
    "methods": "{% for role in preferences.target_roles %}{{ role.strip().lower() }}"
    "{{ role.splitlines() | length }}{% endfor %}"
    '{{ preferences.get("target_roles") | length }}',
    "concat_outside_set": '{{ "a" ~ preferences | length }}',
    "numeric_filters": '{{ "3" | int }}{{ "3" | float }}',
    "eight_sets": "".join(f"{{% set v{n} = preferences | length %}}" for n in range(8)),
    "escape": "{{ preferences | e }}",
}


@pytest.mark.parametrize("name", sorted(ALLOWED))
def test_ordinary_template_features_are_still_allowed(folders: Folders, name: str) -> None:
    saved = folders.save(EDIT_SYSTEM, EDIT_USER + "\n" + ALLOWED[name])

    assert saved.error is None
    template = folders.library.load(TASK)
    render_prompt(template, INPUTS)  # and it renders


def test_the_runtime_sandbox_still_refuses_percent_formatting_and_repetition() -> None:
    env = _PromptEnvironment()

    with pytest.raises(SecurityError):
        env.from_string("{{ text % 1 }}").render(text="%100000000d")
    with pytest.raises(SecurityError):
        env.from_string("{{ text * 100000000 }}").render(text="a")
    with pytest.raises(SecurityError):
        env.from_string("{{ 9 ** 9 ** 9 }}").render()
    assert env.from_string("{{ 7 % 3 }}").render() == "1"


# --- crash leftovers --------------------------------------------------------------------------


def leave_temp(folders: Folders, name: str = TEMP_NAME) -> Path:
    folders.task_folder.mkdir(parents=True, exist_ok=True)
    path = folders.task_folder / name
    path.write_bytes(b"half a prompt")
    return path


def test_save_removes_a_crash_leftover_in_the_task_folder(folders: Folders) -> None:
    leftover = leave_temp(folders)
    unrelated = folders.task_folder / "notes.txt"
    unrelated.write_text("fictional note", encoding="utf-8")

    folders.save()

    assert not leftover.exists()
    assert unrelated.read_text(encoding="utf-8") == "fictional note"
    assert folders.prompt_path.is_file()


def test_a_refused_save_may_leave_the_leftover_but_never_fails_on_it(folders: Folders) -> None:
    leave_temp(folders)

    with pytest.raises(PromptRefused):
        folders.save(EDIT_SYSTEM, "{{ job_text }}")  # unwrapped


def test_reset_removes_leftovers_and_reports_no_error(folders: Folders) -> None:
    folders.save()
    leave_temp(folders)
    leave_temp(folders, ".prompt.md.fedcba9876543210.tmp")

    folders.library.reset_override(TASK)  # no PromptError

    assert not folders.task_folder.exists()
    assert folders.library.status(TASK).overridden is False


def test_reset_with_only_a_leftover_removes_the_folder(folders: Folders) -> None:
    leave_temp(folders)

    folders.library.reset_override(TASK)

    assert not folders.task_folder.exists()


def test_reset_keeps_other_files_and_still_says_so(folders: Folders) -> None:
    folders.save()
    leave_temp(folders)
    (folders.task_folder / "notes.txt").write_text("fictional note", encoding="utf-8")

    with pytest.raises(PromptError):
        folders.library.reset_override(TASK)

    assert files_under(folders.task_folder) == ["notes.txt"]  # leftover gone, note kept


def test_a_leftover_that_is_a_link_is_removed_without_following_it(
    folders: Folders, tmp_path: Path
) -> None:
    target = tmp_path / "outside.txt"
    target.write_text("fictional outside file", encoding="utf-8")
    folders.save()
    os.symlink(target, folders.task_folder / TEMP_NAME)

    folders.library.reset_override(TASK)

    assert target.read_text(encoding="utf-8") == "fictional outside file"
    assert not folders.task_folder.exists()


# --- the reset version guard ------------------------------------------------------------------


def test_reset_with_a_stale_expected_version_conflicts_and_changes_nothing(
    folders: Folders,
) -> None:
    first = folders.save()
    folders.save(EDIT_SYSTEM + "\nA second fictional line.", expected=first.content_version)
    before = snapshot(folders.overrides)

    with pytest.raises(PromptConflict):
        folders.library.reset_override(TASK, expected_content_version=first.content_version)

    assert snapshot(folders.overrides) == before


def test_reset_with_the_current_version_resets(folders: Folders) -> None:
    saved = folders.save()

    folders.library.reset_override(TASK, expected_content_version=saved.content_version)

    assert not folders.task_folder.exists()


def test_reset_expecting_none_conflicts_once_an_override_exists(folders: Folders) -> None:
    folders.save()

    with pytest.raises(PromptConflict):
        folders.library.reset_override(TASK, expected_content_version="none")
    assert folders.prompt_path.is_file()


def test_reset_without_an_expected_version_is_unconditional(folders: Folders) -> None:
    folders.save()

    folders.library.reset_override(TASK)

    assert not folders.task_folder.exists()


def test_reset_expecting_none_removes_an_override_that_cannot_be_read(folders: Folders) -> None:
    """A link in the place of ``prompt.md`` reads as "none" (like the status says), so the
    page that carried "none" may reset it."""
    folders.task_folder.mkdir(parents=True)
    os.symlink(folders.root / "elsewhere", folders.prompt_path)
    assert folders.library.status(TASK).content_version == "none"

    folders.library.reset_override(TASK, expected_content_version="none")

    assert not folders.task_folder.exists()


# --- rarely taken branches --------------------------------------------------------------------


def test_a_file_that_changes_between_the_two_reads_is_a_conflict(
    folders: Folders, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = folders.save()
    real = prompts._read_prompt_file
    calls: list[int] = []

    def changing(folder: int, task: str) -> Any:
        found = real(folder, task)
        calls.append(1)
        if len(calls) == 1:  # after the check made before the folder is opened for writing
            folders.prompt_path.write_bytes(prompt_file(EDIT_SYSTEM + " changed", EDIT_USER))
        return found

    monkeypatch.setattr(prompts, "_read_prompt_file", changing)

    with pytest.raises(PromptConflict):
        folders.save(EDIT_SYSTEM + "\nMine.", expected=first.content_version)

    monkeypatch.undo()
    assert b"changed" in folders.prompt_path.read_bytes()  # the other writer's file stays
    assert files_under(folders.task_folder) == ["prompt.md"]  # and no temporary file is left


def test_a_folder_that_cannot_be_made_is_a_prompt_error_and_leaves_nothing(
    folders: Folders, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise PermissionError(errno.EACCES, "denied")

    monkeypatch.setattr(prompts.os, "mkdir", refuse)

    with pytest.raises(PromptError, match="could not be made"):
        folders.save()

    monkeypatch.undo()
    assert not (folders.data / "prompts").exists()


def test_a_folder_that_cannot_be_made_private_is_a_prompt_error_and_cleans_up(
    folders: Folders, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise PermissionError(errno.EPERM, "denied")

    monkeypatch.setattr(prompts.os, "fchmod", refuse)

    with pytest.raises(PromptError) as raised:
        folders.save()

    monkeypatch.undo()
    assert type(raised.value) is PromptError
    assert not (folders.data / "prompts").exists(), "folders made by the failed save are removed"


def test_a_file_that_grows_while_it_is_read_gets_an_unread_version(
    folders: Folders, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = prompt_file(EDIT_SYSTEM, EDIT_USER + "\n" + "x" * 3000)
    write_override_bytes(folders.overrides, data)
    monkeypatch.setattr(prompts, "MAX_HASHED_OVERRIDE_BYTES", 1000)
    real_fstat = os.fstat
    calls: list[int] = []

    def small_first(fd: int) -> os.stat_result:
        info = real_fstat(fd)
        calls.append(1)
        if len(calls) > 1:
            return info
        fields = list(info)
        fields[6] = 10  # st_size: it was small when first looked at
        return os.stat_result(fields)

    monkeypatch.setattr(prompts.os, "fstat", small_first)

    status = folders.library.status(TASK)

    assert status.overridden is True
    assert status.content_version not in ("none", sha256(data))
    assert len(status.content_version) == 64
    assert status.error is not None and "larger than" in status.error


def test_reset_with_prompt_md_as_a_folder_is_an_error_and_removes_nothing(
    folders: Folders,
) -> None:
    folders.prompt_path.mkdir(parents=True)
    before = snapshot(folders.overrides)

    with pytest.raises(PromptError, match="isn't a file"):
        folders.library.reset_override(TASK)

    assert snapshot(folders.overrides) == before


def test_reset_with_a_regular_file_in_the_task_folders_place_is_an_error(
    folders: Folders,
) -> None:
    folders.overrides.mkdir(parents=True)
    folders.task_folder.write_text("fictional stray file", encoding="utf-8")

    with pytest.raises(PromptError, match="isn't a folder"):
        folders.library.reset_override(TASK)

    assert folders.task_folder.read_text(encoding="utf-8") == "fictional stray file"


def test_reset_without_an_overrides_folder_does_nothing(folders: Folders) -> None:
    folders.library.reset_override(TASK)
    folders.library.reset_override(TASK, expected_content_version="none")

    assert not (folders.data / "prompts").exists()


def test_status_docstring_documents_the_content_version() -> None:
    doc = prompts.PromptStatus.__doc__ or ""
    assert "SHA-256" in doc and "1 MiB" in doc
    assert "none" in doc and "link" in doc and "folder" in doc


# --- "default updated" needs a base version below the default's --------------------------------


@pytest.mark.parametrize(
    ("base", "default", "updated"),
    [(1, 2, True), (2, 2, False), (5, 2, False)],
)
def test_the_base_version_is_unknown_not_old_when_it_is_above_the_default(
    folders: Folders, base: int, default: int, updated: bool
) -> None:
    write_default(folders.root, version=default)
    data = prompt_file(EDIT_SYSTEM, EDIT_USER, version=base)
    write_override_bytes(folders.overrides, data)

    status = folders.library.status(TASK)

    assert status.default_version == default and status.base_version == base
    assert status.error is None
    assert (status.base_version < status.default_version) is updated


def test_an_unsafe_call_is_refused_before_rendering_and_by_the_sandbox_anyway(
    folders: Folders,
) -> None:
    text = "{{ ().__class__.__base__.__subclasses__() }}"

    with pytest.raises(PromptRefused) as refused:
        folders.save(EDIT_SYSTEM, EDIT_USER + "\n" + text)
    assert refused.value.reason == "unsupported_expression"
    with pytest.raises(SecurityError):
        _PromptEnvironment().from_string(text).render()


@pytest.mark.parametrize("error", [RecursionError, MemoryError])
def test_a_render_that_runs_out_of_resources_is_a_prompt_error(
    folders: Folders, monkeypatch: pytest.MonkeyPatch, error: type[Exception]
) -> None:
    """The macro that called itself is now refused before it runs; the renderer's own
    catch-all for a recursion or memory failure is still pinned here."""
    template = folders.library.load(TASK)

    def fail(self: object, source: str) -> None:
        raise error

    monkeypatch.setattr(_PromptEnvironment, "from_string", fail)

    with pytest.raises(PromptError, match="could not be rendered"):
        render_prompt(template, INPUTS)
