"""The demo offers the fake provider by itself, on the demo's own folder only (M2.26; ADR-077
update of the M2.20 checkpoint, relaxing ADR-076 item 2 and ADR-054; user decision of
2026-10-11).

Contract:

- ``is_demo_workspace(config)`` is true only for ``--demo`` running on the folder the app
  chose for the demo (``data_dir_source == "demo"``). ``--demo`` with ``--data-dir`` or
  ``JOBPIPELINE_DATA_DIR`` is not: the folder was named by the user.
- ``build_wiring(..., demo=True, demo_workspace=True)`` answers a wiring whose ``allow_fakes``
  is true whatever ``JOBPIPELINE_ALLOW_FAKE_PROVIDERS`` says: the fake is among
  ``provider_choices()``, is healthy and is built by ``ai_provider``.
- Everywhere else nothing changes: the variable (exactly ``"1"``) is the only way to the
  fake. That includes ``--demo`` on an explicit folder, ``demo_workspace`` left out, and
  ``demo_workspace`` without ``demo``.
- In a demo workspace the fake's connection test connects with an empty credential (there
  is nothing to paste in the demo), and with any text that holds no keyword: the field is
  hidden while the fake is chosen and may still hold what was typed before. A pasted
  fictional keyword still picks the outcome. Outside a demo workspace an empty credential
  is rejected as before, with or without the variable.
- The demo rules of ADR-044 item 7 stay: the demo keychain, an empty environment and no use
  of the computer's own sign-in.

The decision is made from the start-up configuration alone. The keyring is an in-memory test
backend; every value is fictional.
"""

import dataclasses
from pathlib import Path

import pytest

from app.config import (
    ENV_DATA_DIR,
    CliOptions,
    DataDirSource,
    RuntimeConfig,
    is_demo_workspace,
    resolve_config,
)
from app.domain.redaction import Redactor
from app.domain.settings import AIProviderSettings
from app.modules.ai import ProviderHealth
from app.modules.ai.providers.fake import FAKE_FAST_MODEL, PRACTICE_TASK, FakeAIProvider
from app.wiring import ALLOW_FAKES_VARIABLE, Wiring, build_wiring
from tests.fakes.keyring_backends import MemoryKeyring

FAKE_KIND = "fake"
FAKE = AIProviderSettings(kind=FAKE_KIND)
NOT_ONE = ("", "0", "true", "yes", " 1", "1 ")
EMPTY_CREDENTIALS = (None, "", "   ", "\t\n")
SOURCES: tuple[DataDirSource, ...] = ("cli", "environment", "bootstrap", "demo", "default")


def wiring_for(
    environ: dict[str, str], *, demo: bool, demo_workspace: bool | None = None
) -> Wiring:
    extra = {} if demo_workspace is None else {"demo_workspace": demo_workspace}
    return build_wiring(
        environ=environ,
        demo=demo,
        redactor=Redactor(),
        keyring_backend=MemoryKeyring(),
        **extra,
    )


def kinds(wiring: Wiring) -> list[str]:
    return [choice.kind for choice in wiring.provider_choices()]


def assert_no_fake(wiring: Wiring, tmp_path: Path) -> None:
    assert wiring.allow_fakes is False
    assert FAKE_KIND not in kinds(wiring)
    assert wiring.provider_health(FAKE) == ProviderHealth("not_allowed")
    with pytest.raises(ValueError):
        wiring.ai_provider(FAKE, data_dir=tmp_path)
    with pytest.raises(ValueError):
        wiring.fake_for_key(None)
    with pytest.raises(ValueError):
        wiring.script_practice(FakeAIProvider(), outcome="ok", delay_seconds=0.0)
    runner = wiring.task_runner(FakeAIProvider(), lambda: FAKE, data_dir=tmp_path)
    assert PRACTICE_TASK not in runner._specs, "no practice task without the fake"  # type: ignore[attr-defined]


# --- which run is a demo workspace --------------------------------------------------------------


def config(*, demo: bool, source: DataDirSource, tmp_path: Path) -> RuntimeConfig:
    return RuntimeConfig(
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "cfg",
        port=8000,
        log_level="INFO",
        demo=demo,
        setup=False,
        no_browser=True,
        data_dir_source=source,
    )


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("demo", [True, False])
def test_only_demo_on_its_own_folder_is_a_demo_workspace(
    demo: bool, source: DataDirSource, tmp_path: Path
) -> None:
    expected = demo and source == "demo"

    assert is_demo_workspace(config(demo=demo, source=source, tmp_path=tmp_path)) is expected


def test_demo_flag_alone_resolves_to_a_demo_workspace(tmp_path: Path) -> None:
    env = {"JOBPIPELINE_CONFIG_DIR": str(tmp_path / "cfg")}

    resolved = resolve_config(CliOptions(demo=True), env)

    assert resolved.data_dir_source == "demo"
    assert is_demo_workspace(resolved) is True


def test_demo_flag_with_an_explicit_folder_is_not_a_demo_workspace(tmp_path: Path) -> None:
    env = {"JOBPIPELINE_CONFIG_DIR": str(tmp_path / "cfg")}
    demo_folder = resolve_config(CliOptions(demo=True), env).data_dir

    by_flag = resolve_config(CliOptions(demo=True, data_dir=tmp_path / "mine"), env)
    by_variable = resolve_config(CliOptions(demo=True), {**env, ENV_DATA_DIR: str(tmp_path)})
    # Even when the folder named is the demo's own: what counts is who chose it.
    same_path = resolve_config(CliOptions(demo=True, data_dir=demo_folder), env)

    for resolved in (by_flag, by_variable, same_path):
        assert resolved.demo is True
        assert is_demo_workspace(resolved) is False, resolved.data_dir_source


def test_without_the_demo_flag_nothing_is_a_demo_workspace(tmp_path: Path) -> None:
    env = {"JOBPIPELINE_CONFIG_DIR": str(tmp_path / "cfg")}
    plain = resolve_config(CliOptions(), env)

    assert is_demo_workspace(plain) is False
    # A config whose source says "demo" without the flag (never built by the app) isn't one.
    assert is_demo_workspace(dataclasses.replace(plain, data_dir_source="demo")) is False


# --- the fake in a demo workspace --------------------------------------------------------------


def test_demo_workspace_offers_the_fake_without_the_allow_variable(tmp_path: Path) -> None:
    demo = wiring_for({}, demo=True, demo_workspace=True)

    assert demo.allow_fakes is True, "the demo offers the fake by itself"
    assert demo.demo_workspace is True
    assert FAKE_KIND in kinds(demo)
    assert demo.provider_health(FAKE) == ProviderHealth("ok")
    assert isinstance(demo.ai_provider(FAKE, data_dir=tmp_path), FakeAIProvider)
    # The demo rules stay (ADR-044 item 7): never the computer's own sign-in.
    assert "claude_existing_login" not in kinds(demo)


@pytest.mark.parametrize("value", NOT_ONE)
def test_demo_workspace_offers_the_fake_whatever_the_variable_says(value: str) -> None:
    demo = wiring_for({ALLOW_FAKES_VARIABLE: value}, demo=True, demo_workspace=True)

    assert demo.allow_fakes is True
    assert FAKE_KIND in kinds(demo)


# --- ...and nowhere else without the variable --------------------------------------------------


def test_demo_on_an_explicit_folder_never_offers_the_fake_by_itself(tmp_path: Path) -> None:
    explicit = wiring_for({}, demo=True, demo_workspace=False)

    assert explicit.demo is True and explicit.demo_workspace is False
    assert_no_fake(explicit, tmp_path)
    assert "claude_existing_login" not in kinds(explicit), "still a demo: its rules stay"


def test_demo_workspace_must_be_said_it_is_never_assumed(tmp_path: Path) -> None:
    """``demo_workspace`` left out: the demo flag alone decides nothing."""
    assumed = wiring_for({}, demo=True)

    assert assumed.demo_workspace is False
    assert_no_fake(assumed, tmp_path)


@pytest.mark.parametrize("value", NOT_ONE)
def test_demo_on_an_explicit_folder_needs_the_variable_to_be_exactly_one(
    value: str, tmp_path: Path
) -> None:
    explicit = wiring_for({ALLOW_FAKES_VARIABLE: value}, demo=True, demo_workspace=False)

    assert_no_fake(explicit, tmp_path)


def test_demo_on_an_explicit_folder_with_the_variable_offers_the_fake_as_before() -> None:
    explicit = wiring_for({ALLOW_FAKES_VARIABLE: "1"}, demo=True, demo_workspace=False)

    assert explicit.allow_fakes is True
    assert explicit.demo_workspace is False, "the variable allows the fake; it makes no demo"
    assert FAKE_KIND in kinds(explicit)


def test_a_demo_workspace_without_the_demo_flag_is_none(tmp_path: Path) -> None:
    """A guard: the two must both hold."""
    odd = wiring_for({}, demo=False, demo_workspace=True)

    assert odd.demo_workspace is False
    assert_no_fake(odd, tmp_path)


def test_wiring_without_demo_and_without_the_variable_never_offers_the_fake(
    tmp_path: Path,
) -> None:
    """Unchanged (a guard): outside demo mode the variable is the only way to the fake."""
    assert_no_fake(wiring_for({}, demo=False), tmp_path)


@pytest.mark.parametrize("value", NOT_ONE)
def test_wiring_without_demo_needs_the_variable_to_be_exactly_one(value: str) -> None:
    """Unchanged (a guard)."""
    plain = wiring_for({ALLOW_FAKES_VARIABLE: value}, demo=False)

    assert plain.allow_fakes is False
    assert FAKE_KIND not in kinds(plain)


# --- the connection test with an empty field ---------------------------------------------------


@pytest.mark.parametrize("credential", EMPTY_CREDENTIALS)
async def test_in_a_demo_workspace_the_fake_connects_with_an_empty_field(
    credential: str | None,
) -> None:
    demo = wiring_for({}, demo=True, demo_workspace=True)

    result = await demo.fake_for_key(credential).test_connection(FAKE_FAST_MODEL)

    assert (result.ok, result.message, result.model) == (True, "Connected", FAKE_FAST_MODEL)


@pytest.mark.parametrize(
    ("credential", "connects"),
    [
        ("fake-connected-fictional-0001", True),
        ("fake-offline-fictional-0001", False),
        ("fake-auth-rejected-fictional-0001", False),
        # No keyword: the hidden field may hold text typed before the fake was chosen.
        ("fictional-text-without-a-keyword", True),
    ],
)
async def test_in_a_demo_workspace_a_pasted_keyword_still_picks_the_outcome(
    credential: str, connects: bool
) -> None:
    demo = wiring_for({}, demo=True, demo_workspace=True)

    result = await demo.fake_for_key(credential).test_connection(FAKE_FAST_MODEL)

    assert result.ok is connects


@pytest.mark.parametrize("credential", EMPTY_CREDENTIALS)
@pytest.mark.parametrize("demo", [True, False])
async def test_outside_a_demo_workspace_an_empty_field_is_rejected_as_before(
    demo: bool, credential: str | None
) -> None:
    """With the variable, in a demo on an explicit folder or not in a demo at all."""
    allowed = wiring_for({ALLOW_FAKES_VARIABLE: "1"}, demo=demo, demo_workspace=False)

    result = await allowed.fake_for_key(credential).test_connection(FAKE_FAST_MODEL)

    assert result.ok is False
    assert result.failure == "auth"


@pytest.mark.parametrize("credential", ["fictional-text-without-a-keyword", "x"])
async def test_outside_a_demo_workspace_text_without_a_keyword_is_rejected_as_before(
    credential: str,
) -> None:
    allowed = wiring_for({ALLOW_FAKES_VARIABLE: "1"}, demo=True, demo_workspace=False)

    result = await allowed.fake_for_key(credential).test_connection(FAKE_FAST_MODEL)

    assert (result.ok, result.failure) == (False, "auth")
