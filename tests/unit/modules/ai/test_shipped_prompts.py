"""M9.3 fix pass 1: the shipped default prompts are pinned, and each survives the editor (spec
3.6; ADR-055).

An override records the default version it was written against, and "Default updated" compares
that with the current default's version. That only works when a changed default always carries a
new ``version``, so a fixture pins every default's version and content digest and the first test
fails when one changes on its own. The second test saves every default unchanged as an
override and shows the executor sends exactly the same request, whatever the task kind.
"""

import contextlib
import hashlib
import json
from pathlib import Path

import pytest

from app.domain.settings import AIProviderSettings
from app.modules.ai.executor import TaskExecutor
from app.modules.ai.prompts import PromptLibrary
from app.modules.ai.providers.fake import (
    FAKE_FAST_MODEL,
    FAKE_THOROUGH_MODEL,
    FakeAIProvider,
    ScriptedResponse,
)
from app.modules.ai.tasks import TASK_SPECS
from tests.fakes.ai import NotCancelled
from tests.fakes.usage import RecordingMeter

REPO_ROOT = Path(__file__).resolve().parents[4]
PROMPTS = REPO_ROOT / "prompts"
PINS = REPO_ROOT / "tests" / "fixtures" / "prompt_default_pins.json"

CHANGED = (
    "The default prompt of {task!r} changed (version {pinned_version} -> {version}) but not "
    "together with its pin. Overrides record the default version they were written against, so "
    "a changed prompt.md or schema.json needs a higher `version:` in prompts/{task}/prompt.md "
    "in the same change. Then update the entry for {task!r} in {pins} (version and sha256 of "
    "prompt.md, a NUL byte and schema.json, line ends as LF)."
)


def digest(task: str) -> str:
    folder = PROMPTS / task
    prompt = (folder / "prompt.md").read_bytes().replace(b"\r\n", b"\n")
    schema = (folder / "schema.json").read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(prompt + b"\0" + schema).hexdigest()


def pins() -> dict[str, dict[str, object]]:
    loaded: dict[str, dict[str, object]] = json.loads(PINS.read_text(encoding="utf-8"))
    return loaded


def test_the_pins_cover_exactly_the_shipped_prompts() -> None:
    shipped = {path.parent.name for path in PROMPTS.glob("*/prompt.md")}
    assert set(pins()) == shipped
    assert set(TASK_SPECS) <= shipped


@pytest.mark.parametrize("task", sorted(path.parent.name for path in PROMPTS.glob("*/prompt.md")))
def test_a_default_prompt_changes_only_together_with_its_version_and_pin(task: str) -> None:
    pinned = pins()[task]
    version = int(PromptLibrary(PROMPTS).load(task).version)

    message = CHANGED.format(
        task=task, pinned_version=pinned["version"], version=version, pins=PINS.name
    )
    assert version == pinned["version"], message
    assert digest(task) == pinned["sha256"], message


def test_the_guard_message_tells_what_to_do() -> None:
    text = CHANGED.format(task="x", pinned_version=1, version=1, pins="pins.json")
    assert "`version:`" in text and "update the entry" in text


def inputs_for(library: PromptLibrary, task: str) -> dict[str, object]:
    """Fictional text for every declared input: valid as text, as an id and for ``tojson``."""
    template = library.load(task)
    return {
        name: (f"Fictional text for {name}." if name in template.untrusted_inputs else f"{name}-1")
        for name in template.inputs
    }


async def requests_for(library: PromptLibrary, task: str) -> list[object]:
    provider = FakeAIProvider()
    provider.script(task, ScriptedResponse(data={}))
    settings = AIProviderSettings()
    executor = TaskExecutor(
        provider=provider,
        prompts=library,
        settings=lambda: settings,
        provider_defaults={"fast": FAKE_FAST_MODEL, "thorough": FAKE_THOROUGH_MODEL},
        specs=TASK_SPECS,
    )
    # The scripted answer need not satisfy the task's checks: the request is sent either way.
    with contextlib.suppress(Exception):
        await executor.run_task(
            task, inputs_for(library, task), NotCancelled(), meter=RecordingMeter()
        )
    return list(provider.requests)


@pytest.mark.parametrize("task", sorted(TASK_SPECS))
async def test_a_default_saved_unchanged_sends_the_same_request(task: str, tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    library = PromptLibrary(PROMPTS, tmp_path / "data" / "prompts" / "overrides")
    (plain,) = (await requests_for(library, task))[:1]
    text = library.default_text(task)

    saved = library.save_override(task, text.system, text.user, expected_content_version="none")
    assert saved.error is None
    (edited,) = (await requests_for(library, task))[:1]

    assert edited.system == plain.system  # type: ignore[attr-defined]
    assert edited.user == plain.user  # type: ignore[attr-defined]
    assert plain.prompt_version == str(saved.default_version)  # type: ignore[attr-defined]
    assert edited.prompt_version == f"{saved.default_version}+override"  # type: ignore[attr-defined]
    assert plain.system and plain.user  # type: ignore[attr-defined]
