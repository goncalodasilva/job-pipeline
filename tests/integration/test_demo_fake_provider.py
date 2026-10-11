"""The demo offers the fake provider and the practice task by itself, on the demo's own folder
only (M2.26; ADR-077 update of the M2.20 checkpoint; relaxes ADR-076 item 2; user decision of
2026-10-11).

The app is built the way ``create_app`` builds it when nothing is injected: its wiring comes
from the ``RuntimeConfig`` and the process environment (set here with ``monkeypatch``), so
what is tested is the app's own decision.

- ``--demo`` on the folder the app chose for it (``demo=True``, source ``"demo"``), without
  ``JOBPIPELINE_ALLOW_FAKE_PROVIDERS``: with the fake provider saved (the setup harness saves
  it), Settings shows "Testing tools", Settings > Providers offers the fake, and a practice
  task runs to the end on the ``fake`` usage service.
- ``--demo`` on a folder the user named (source ``"cli"`` or ``"environment"``) without the
  variable: none of it. No Testing tools, the practice route answers 404, no fake to choose
  and no practice task type registered. Whatever the variable says short of exactly ``"1"``.
- With the variable set to ``"1"`` the fake is offered as before, demo or not.
- Without ``--demo`` and without the variable: nothing (unchanged; a guard).

Real SQLite under ``tmp_path``; the keychain is the autouse in-memory keyring; ``FrozenClock``;
every value is fictional.
"""

import dataclasses
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.modules.ai.providers.fake import FakeAIProvider
from app.wiring import ALLOW_FAKES_VARIABLE
from tests.fakes.clock import FrozenClock
from tests.integration.test_practice_task import (
    FAKE_SERVICE,
    PRACTICE,
    PRACTICE_PATH,
    SECTION_HEADING,
    SETTINGS,
    START,
    Gate,
    Practice,
    assert_not_offered,
    section,
    started,
)
from tests.integration.test_setup_redirect import BASE_URL, Running, outline
from tests.integration.test_setup_reopen_banner import finished_guide, make_config

pytestmark = pytest.mark.integration

FAKE_KIND = "fake"
DEMO_FOLDER = "demo"  # the data folder is the one the app chose for the demo
EXPLICIT_SOURCES = ("cli", "environment")
NOT_ONE = ("", "0", "true", " 1")
PRACTICE_STEPS = 5


@contextmanager
def app_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    demo: bool,
    source: str,
    allow_variable: str | None = None,
) -> Iterator[Practice]:
    """A finished setup with the fake saved, and the app with its own wiring: ``demo`` and
    ``source`` as the CLI would resolve them, the allow variable set only when given."""
    if allow_variable is None:
        monkeypatch.delenv(ALLOW_FAKES_VARIABLE, raising=False)
    else:
        monkeypatch.setenv(ALLOW_FAKES_VARIABLE, allow_variable)
    finished_guide(tmp_path)
    clock = FrozenClock(START)
    config = dataclasses.replace(make_config(tmp_path), demo=demo, data_dir_source=source)  # type: ignore[arg-type]
    app = create_app(config, clock=clock)  # no wiring injected: the app decides
    with TestClient(app, base_url=BASE_URL, follow_redirects=False) as client:
        run = Running(app=app, client=client, hits=Counter())
        yield Practice(run, FakeAIProvider(), Gate(), clock)  # the fake and gate are unused


def provider_kinds(page: str) -> set[str]:
    """The provider kinds Settings > Providers lets the user choose."""
    return {
        e.attrs.get("value") or ""
        for e in outline(page)
        if e.tag == "input" and e.attrs.get("name") == "kind"
    }


def assert_nothing_offered(practice: Practice) -> None:
    """No Testing tools, a 404 from the practice route, no fake to choose, no practice type."""
    wiring = practice.app.state.wiring
    assert wiring.allow_fakes is False
    assert FAKE_KIND not in [choice.kind for choice in wiring.provider_choices()]
    assert_not_offered(practice)  # no section, no form, and the POST answers 404
    page = practice.run.client.get(SETTINGS)
    assert page.status_code == 200, page.status_code
    assert SECTION_HEADING not in page.text
    assert PRACTICE_PATH not in page.text
    assert FAKE_KIND not in provider_kinds(page.text)
    assert practice.app.state.task_registry.get(PRACTICE) is None
    assert practice.tasks() == [], "nothing was started"


def assert_practice_runs(practice: Practice) -> None:
    page = practice.run.client.get(SETTINGS)
    assert page.status_code == 200, page.status_code
    assert section(outline(page.text)) is not None, "Settings > Testing tools is shown"
    assert PRACTICE_PATH in page.text
    assert FAKE_KIND in provider_kinds(page.text), "the fake can be chosen"

    row = practice.wait_terminal(started(practice))
    assert str(row.status.value) == "succeeded", row.error
    status = practice.app.state.usage.status(FAKE_SERVICE)
    assert status.local_count == PRACTICE_STEPS, "the practice task ran on the fake provider"


def test_demo_offers_practice_task_without_the_allow_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with app_run(tmp_path, monkeypatch, demo=True, source=DEMO_FOLDER) as practice:
        wiring = practice.app.state.wiring
        assert wiring.allow_fakes is True, "the demo offers the fake by itself"
        assert wiring.demo_workspace is True
        assert_practice_runs(practice)


def test_demo_settings_offers_the_fake_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with app_run(tmp_path, monkeypatch, demo=True, source=DEMO_FOLDER) as practice:
        page = practice.run.client.get(SETTINGS)
        assert FAKE_KIND in provider_kinds(page.text), "the fake can be chosen in the demo"


@pytest.mark.parametrize("source", EXPLICIT_SOURCES)
def test_demo_on_an_explicit_folder_offers_nothing_without_the_variable(
    source: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--demo --data-dir X`` or ``--demo`` with ``JOBPIPELINE_DATA_DIR``: the demo flag
    alone opens nothing on a folder the user named."""
    with app_run(tmp_path, monkeypatch, demo=True, source=source) as practice:
        assert practice.app.state.wiring.demo is True
        assert practice.app.state.wiring.demo_workspace is False
        assert_nothing_offered(practice)


@pytest.mark.parametrize("value", NOT_ONE)
def test_demo_on_an_explicit_folder_needs_the_variable_to_be_exactly_one(
    value: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with app_run(tmp_path, monkeypatch, demo=True, source="cli", allow_variable=value) as practice:
        assert_nothing_offered(practice)


@pytest.mark.parametrize("demo", [True, False])
def test_with_the_variable_the_fake_is_offered_as_before(
    demo: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = "cli" if demo else "bootstrap"
    with app_run(tmp_path, monkeypatch, demo=demo, source=source, allow_variable="1") as practice:
        assert practice.app.state.wiring.allow_fakes is True
        assert practice.app.state.wiring.demo_workspace is False
        assert_practice_runs(practice)


def test_without_demo_and_without_the_variable_nothing_is_offered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unchanged (a guard): no Testing tools, a 404 from the route and no fake to choose."""
    with app_run(tmp_path, monkeypatch, demo=False, source="bootstrap") as practice:
        assert_nothing_offered(practice)
