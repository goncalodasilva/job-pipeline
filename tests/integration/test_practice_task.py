"""The practice task: a fake-gated vehicle for the runner, usage and tasks UI (M2.17; ADR-076
item 2; groundwork for AC-M2.1 to AC-M2.9; ADR-034, ADR-054, ADR-059, ADR-079, ADR-080).

Assumed interface:

- Task type ``practice`` and AI task ``practice`` (prompt id ``practice``; fixed fictional text;
  no web search). It is offered only while fakes are allowed
  (``JOBPIPELINE_ALLOW_FAKE_PROVIDERS=1``) AND the saved provider kind is ``fake``.
- ``POST /tasks/practice`` (launch token and origin; not a page) with the URL-encoded fields
  ``duration`` (``quick`` | ``slow``), ``outcome`` (``ok`` | ``usage_limit`` | ``network`` |
  ``invalid_output``) and ``tier`` (``fast`` | ``thorough``). A missing field or any other
  value answers 422 and enqueues nothing; while the task isn't offered the route answers 404.
  A valid start enqueues through the task service with a fixed dedupe target (so a second
  start while one is queued or running creates nothing) and answers 200 or 303.
- The handler gets its provider from ``wiring.ai_provider(saved settings, data_dir=...)`` and
  scripts that ``FakeAIProvider`` for the AI task ``practice`` from the form values: N >= 2
  steps of one call each through the usage meter (service ``fake``, the chosen tier); ``slow``
  gives every call a delay through the fake's injected ``sleep`` (``quick`` may have none); a
  failing ``outcome`` fails the first call (``invalid_output``: the first call and its one
  retry). It beats with rising progress around every step, checks for a cancel between steps
  and ends through ``ctx.complete`` with ``result_ref`` ``practice:<N>`` (a no-op writer: no
  results table until M3).
- ``GET /settings`` shows a "Testing tools" section (three labelled selects and a Start
  button in a form posting to ``/tasks/practice``) only while the task is offered.

The tests swap wiring's fake for one that records requests and whose ``sleep`` waits for the
test (no real time). Real SQLite under ``tmp_path``; ``FrozenClock``; deadline waits; every
value is fictional. New names are imported inside the tests.
"""

import asyncio
import json
import re
import threading
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response

from app.domain.errors import (
    InvalidModelOutput,
    NetworkUnavailable,
    PracticeNotOffered,
    TaskFailed,
    UsageLimitReached,
)
from app.domain.settings import AI_PROVIDER, AIProviderSettings
from app.main import create_app
from app.modules.ai import AIProvider
from app.modules.ai.providers.fake import FAKE_FAST_MODEL, FAKE_THOROUGH_MODEL, FakeAIProvider
from app.services.settings import SettingsService
from app.tasks import HandlerRegistry
from app.wiring import Wiring
from tests.fakes.clock import FrozenClock
from tests.fakes.sdk_install import install_fake_sdk
from tests.integration.test_setup_preferences_done import SAVED
from tests.integration.test_setup_provider import FICTIONAL_TOKEN, make_wiring
from tests.integration.test_setup_redirect import BASE_URL, ORIGIN, Element, Running, outline
from tests.integration.test_setup_reopen_banner import TOKEN_KIND, finished_guide, make_config
from tests.waiting import wait_until

pytestmark = pytest.mark.integration

START = datetime(2026, 4, 6, 10, 0, 0, tzinfo=UTC)
DEADLINE_S = 10.0
PRACTICE = "practice"
PRACTICE_PATH = "/tasks/practice"
SETTINGS = "/settings"
SECTION_HEADING = "Testing tools"
FAKE_SERVICE = "fake"
DURATIONS = ("quick", "slow")
OUTCOMES = ("ok", "usage_limit", "network", "invalid_output")
TIERS = ("fast", "thorough")
FIELDS = {"duration": DURATIONS, "outcome": OUTCOMES, "tier": TIERS}
MODELS = {"fast": FAKE_FAST_MODEL, "thorough": FAKE_THOROUGH_MODEL}
TERMINAL = ("succeeded", "failed", "cancelled", "interrupted")
STARTED = (200, 303)
RESULT_REF = re.compile(r"practice:(\d+)")
MAX_STEPS = 50  # a guard for the step loop, far above any sensible N
NO_FAKES: dict[str, str] = {}  # an environment without the allow flag
SETTINGS_PROVIDERS = "/settings/providers"
TOOLS_ID = "testing-tools"
STATUS_ID = "practice-status"
MSG_STARTED = "Practice task started."
MSG_ALREADY = "A practice task is already running. Wait for it to finish, then start another."
MSG_NOT_OFFERED = (
    "The practice task is only available while the fake provider is active. Reload the page."
)
HINT = (
    "Shown because the fake provider is active. A practice task runs a few steps on the fake "
    "provider. It uses none of your data and counts only against the fake provider's usage."
)


# --- helpers -----------------------------------------------------------------------------------


class Gate:
    """The fake's ``sleep``: counts the calls that reached it and, while ``hold`` is set,
    keeps each one waiting until the test lets a step through. Never waits for real time
    beyond the deadline."""

    def __init__(self) -> None:
        self.hold = False
        self.entered = 0
        self.delays: list[float] = []
        self._permits = threading.Semaphore(0)

    async def sleep(self, seconds: float) -> None:
        self.delays.append(seconds)
        self.entered += 1
        if self.hold:
            await asyncio.to_thread(self._permits.acquire, True, DEADLINE_S)

    def step(self) -> None:
        self._permits.release()

    def open(self) -> None:
        self.hold = False
        for _ in range(MAX_STEPS):
            self._permits.release()


class PracticeWiring:
    """The app's wiring, except that the fake it builds is the test's own (recording, with
    the gate as its sleep). Everything else, wiring's own checks included, is the wrapped
    wiring's."""

    def __init__(self, wiring: Wiring, fake: FakeAIProvider) -> None:
        self._wiring = wiring
        self._fake = fake

    def __getattr__(self, name: str) -> Any:
        return getattr(self._wiring, name)

    def ai_provider(self, settings: AIProviderSettings, *, data_dir: Path) -> AIProvider:
        provider = self._wiring.ai_provider(settings, data_dir=data_dir)
        return self._fake if isinstance(provider, FakeAIProvider) else provider


class Practice:
    def __init__(self, run: Running, fake: FakeAIProvider, gate: Gate, clock: FrozenClock) -> None:
        self.run = run
        self.fake = fake
        self.gate = gate
        self.clock = clock

    @property
    def app(self) -> FastAPI:
        return self.run.app

    def start(self, *, htmx: bool = False, **changes: str) -> Response:
        headers = self.run.action(**({"HX-Request": "true"} if htmx else {}))
        return self.run.client.post(PRACTICE_PATH, data=form(**changes), headers=headers)

    def tasks(self) -> list[Any]:
        rows = self.app.state.task_service.recent(20)
        return [row for row in rows if row.type == PRACTICE]

    def only_task(self) -> Any:
        rows = self.tasks()
        assert len(rows) == 1, [(row.id, str(row.status)) for row in rows]
        return rows[0]

    def row(self, task_id: str) -> Any:
        return self.app.state.task_service.get(task_id)

    def status(self, task_id: str) -> str:
        return str(self.row(task_id).status.value)

    def wait_terminal(self, task_id: str) -> Any:
        def ended() -> Any:
            return self.row(task_id) if self.status(task_id) in TERMINAL else None

        return wait_until(ended, timeout=DEADLINE_S, message=f"practice task {task_id} ended")

    def wait_entered(self, calls: int) -> None:
        wait_until(
            lambda: self.gate.entered >= calls,
            timeout=DEADLINE_S,
            message=f"{calls} provider call(s) reached the fake's sleep",
        )

    def usage_rows(self) -> list[tuple[str | None, str, int, int]]:
        rows = self.app.state.usage.breakdown(FAKE_SERVICE)
        return [(row.tier, row.task_type, row.requests, row.unfinished) for row in rows]


def form(**changes: str) -> dict[str, str]:
    values = {"duration": "quick", "outcome": "ok", "tier": "fast"}
    values.update(changes)
    return values


@contextmanager
def practising(
    tmp_path: Path,
    *,
    provider: AIProviderSettings | None = None,
    environ: dict[str, str] | None = None,
    task_registry: HandlerRegistry | None = None,
) -> Iterator[Practice]:
    """A finished setup (the fake saved unless ``provider`` says otherwise; fakes allowed
    unless ``environ`` says otherwise) and the running app (with the app's own task registry
    unless ``task_registry`` is given)."""
    finished_guide(tmp_path, provider=provider)
    gate = Gate()
    fake = FakeAIProvider(sleep=gate.sleep)
    clock = FrozenClock(START)
    wiring = PracticeWiring(make_wiring(environ), fake)
    app = create_app(
        make_config(tmp_path),
        clock=clock,
        wiring=wiring,  # type: ignore[arg-type]
        task_registry=task_registry,
    )
    try:
        with TestClient(app, base_url=BASE_URL, follow_redirects=False) as client:
            yield Practice(Running(app=app, client=client, hits=Counter()), fake, gate, clock)
    finally:
        gate.open()


def started(practice: Practice, **changes: str) -> str:
    """Start one practice task and return its id."""
    before = {row.id for row in practice.tasks()}
    response = practice.start(**changes)
    assert response.status_code in STARTED, (response.status_code, response.text[:300])
    new = [row for row in practice.tasks() if row.id not in before]
    assert len(new) == 1, "one practice task was enqueued"
    task_id: str = new[0].id
    return task_id


def section(elements: list[Element]) -> Element | None:
    headings = [e for e in elements if e.tag == "h2" and e.text == SECTION_HEADING]
    if not headings:
        return None
    (heading,) = headings
    node = heading.parent
    while node is not None and node.tag != "section":
        node = node.parent
    assert node is not None, "the Testing tools heading sits in a <section>"
    return node


def assert_not_offered(practice: Practice) -> None:
    page = practice.run.client.get(SETTINGS)
    assert page.status_code == 200, page.status_code
    assert section(outline(page.text)) is None
    assert SECTION_HEADING not in page.text
    assert PRACTICE_PATH not in page.text
    response = practice.start()
    assert response.status_code == 404, response.status_code
    assert practice.tasks() == []
    assert practice.fake.requests == []


def assert_offered_control(tmp_path: Path) -> None:
    """The same checks answer the other way in an app where the task is offered, so the
    not-offered tests can't pass just because nothing exists."""
    folder = tmp_path / "control"
    folder.mkdir()
    with practising(folder) as control:
        page = control.run.client.get(SETTINGS)
        assert section(outline(page.text)) is not None, "offered: the section is shown"
        row = control.wait_terminal(started(control))
        assert str(row.status.value) == "succeeded", row.error


# --- offered only with the flag and the fake ---------------------------------------------------


def test_practice_not_offered_without_fake_flag(tmp_path: Path) -> None:
    """A saved fake without the allow flag: no section and no route."""
    with practising(tmp_path, environ=NO_FAKES) as practice:
        assert practice.app.state.wiring.allow_fakes is False
        assert_not_offered(practice)
    assert_offered_control(tmp_path)


def test_practice_not_offered_with_claude_provider(tmp_path: Path) -> None:
    """Fakes allowed, but the saved provider is a Claude one."""
    with practising(tmp_path, provider=AIProviderSettings(kind=TOKEN_KIND)) as practice:
        assert practice.app.state.wiring.allow_fakes is True
        assert_not_offered(practice)
    assert_offered_control(tmp_path)


@pytest.mark.parametrize("case", ["no_flag", "claude", "nothing_saved"])
@pytest.mark.parametrize("htmx", [False, True], ids=["plain", "htmx"])
def test_practice_route_404_when_not_offered(tmp_path: Path, case: str, htmx: bool) -> None:
    provider = {
        "no_flag": None,
        "claude": AIProviderSettings(kind=TOKEN_KIND),
        "nothing_saved": AIProviderSettings(),
    }[case]
    environ = NO_FAKES if case == "no_flag" else None
    with practising(tmp_path, provider=provider, environ=environ) as practice:
        for values in (form(), form(outcome="fictional-free-text")):
            headers = practice.run.action(**({"HX-Request": "true"} if htmx else {}))
            response = practice.run.client.post(PRACTICE_PATH, data=values, headers=headers)
            assert response.status_code == 404, (values, response.status_code)
            assert SECTION_HEADING not in response.text
        assert practice.tasks() == []
        assert practice.fake.requests == []
        assert practice.usage_rows() == []
    if case == "claude" and not htmx:
        assert_offered_control(tmp_path)
    else:  # the same request is accepted where the task is offered: the route exists
        folder = tmp_path / "control"
        folder.mkdir()
        with practising(folder) as control:
            assert control.start(htmx=htmx).status_code in STARTED


# --- the Settings section ----------------------------------------------------------------------


@pytest.mark.parametrize("htmx", [False, True], ids=["page", "htmx"])
def test_settings_shows_testing_tools_only_when_offered(tmp_path: Path, htmx: bool) -> None:
    with practising(tmp_path) as practice:
        headers = practice.run.htmx() if htmx else {}
        response = practice.run.client.get(SETTINGS, headers=headers)
        assert response.status_code == 200
        elements = outline(response.text)
        tools = section(elements)
        assert tools is not None, "a Testing tools section"
        inside = [e for e in elements if e.within(tools)]

        forms = [e for e in inside if e.tag == "form"]
        assert len(forms) == 1
        (start_form,) = forms
        assert (start_form.attrs.get("method") or "").lower() == "post"
        assert start_form.attrs.get("action") == PRACTICE_PATH, "works without JavaScript"
        if "hx-post" in start_form.attrs:
            assert start_form.attrs["hx-post"] == PRACTICE_PATH

        controls = [e for e in inside if e.within(start_form)]
        selects = {e.attrs.get("name"): e for e in controls if e.tag == "select"}
        assert set(selects) == set(FIELDS)
        labels = {e.attrs.get("for"): e for e in controls if e.tag == "label"}
        for name, allowed in FIELDS.items():
            select = selects[name]
            options = [e for e in controls if e.tag == "option" and e.within(select)]
            assert tuple(e.attrs.get("value") for e in options) == allowed, name
            assert select.attrs.get("id"), name
            assert labels[select.attrs["id"]].text, f"{name} has a visible label"
            assert "disabled" not in select.attrs
            assert select.attrs.get("tabindex") in (None, "0")
        free_text = [
            e
            for e in controls
            if e.tag == "textarea" or (e.tag == "input" and e.attrs.get("type") != "hidden")
        ]
        assert free_text == [], "no free text"
        buttons = [e for e in controls if e.tag == "button"]
        assert len(buttons) == 1
        assert buttons[0].text.startswith("Start")
        assert buttons[0].attrs.get("type", "submit") == "submit"
        assert buttons[0].attrs.get("tabindex") in (None, "0")

        providers = next(e for e in elements if e.attrs.get("id") == "providers")
        assert elements.index(providers) < elements.index(tools), "after Providers"
        assert not tools.within(providers)
        # GET has no side effects (spec 2.9 rule 12).
        assert practice.tasks() == []
        assert practice.fake.requests == []
        assert practice.usage_rows() == []


def test_settings_template_has_no_safe_filter_in_testing_tools() -> None:
    """Whatever template renders the section escapes everything (no ``|safe``)."""
    templates = Path(__file__).resolve().parents[2] / "app" / "templates"
    holders = [
        path
        for path in templates.rglob("*.html")
        if SECTION_HEADING in path.read_text(encoding="utf-8")
    ]
    assert holders, "a template renders the Testing tools section"
    for path in holders:
        assert not re.search(r"\|\s*safe\b", path.read_text(encoding="utf-8")), path.name


# --- the route's checks ------------------------------------------------------------------------


@pytest.mark.parametrize("missing", ["token", "origin", "foreign_origin"])
def test_practice_route_needs_token_and_origin(tmp_path: Path, missing: str) -> None:
    with practising(tmp_path) as practice:
        headers = {
            "token": {"origin": ORIGIN},
            "origin": practice.run.with_token(),
            "foreign_origin": practice.run.with_token(origin="http://fictional.example"),
        }[missing]
        response = practice.run.client.post(PRACTICE_PATH, data=form(), headers=headers)
        assert response.status_code == 403, response.status_code
        assert practice.tasks() == []
        assert practice.fake.requests == []
        # The same request with both is accepted.
        assert practice.start().status_code in STARTED


def test_practice_route_get_starts_nothing(tmp_path: Path) -> None:
    with practising(tmp_path) as practice:
        response = practice.run.client.get(PRACTICE_PATH, headers=practice.run.action())
        assert response.status_code in (404, 405), response.status_code
        assert practice.tasks() == []
        assert practice.fake.requests == []


BAD_FORMS = {
    "duration-unknown": form(duration="glacial"),
    "outcome-unknown": form(outcome="auth"),
    "tier-unknown": form(tier="fake-thorough"),
    "duration-case": form(duration="Quick"),
    "outcome-padded": form(outcome=" ok"),
    "tier-empty": form(tier=""),
    "free-text": form(outcome="A fictional sentence typed by hand."),
    "markup": form(duration="<b>slow</b>"),
    "duration-missing": {"outcome": "ok", "tier": "fast"},
    "outcome-missing": {"duration": "quick", "tier": "fast"},
    "tier-missing": {"duration": "quick", "outcome": "ok"},
    "empty": {},
}


@pytest.mark.parametrize("name", list(BAD_FORMS))
@pytest.mark.parametrize("htmx", [False, True], ids=["plain", "htmx"])
def test_practice_form_values_outside_the_lists_422(tmp_path: Path, name: str, htmx: bool) -> None:
    values = BAD_FORMS[name]
    with practising(tmp_path) as practice:
        headers = practice.run.action(**({"HX-Request": "true"} if htmx else {}))
        response = practice.run.client.post(PRACTICE_PATH, data=values, headers=headers)
        assert response.status_code == 422, (name, response.status_code)
        for value in values.values():
            if value and value not in DURATIONS + OUTCOMES + TIERS:
                assert value not in response.text, "a refused value is never shown back"
        assert "<b>slow</b>" not in response.text
        assert practice.tasks() == []
        assert practice.fake.requests == []
        assert practice.usage_rows() == []


# --- running -----------------------------------------------------------------------------------


def test_practice_runs_to_succeeded_with_progress(tmp_path: Path) -> None:
    """One call per step, each let through by the test: progress and the heartbeat time
    rise step by step, every call is counted on the fake service, and the result reference
    names the number of steps."""
    with practising(tmp_path) as practice:
        practice.gate.hold = True
        task_id = started(practice, duration="slow")
        progress: list[int] = []
        beats: list[str | None] = []
        for step in range(1, MAX_STEPS):

            def reached(calls: int = step) -> str | None:
                if practice.status(task_id) in TERMINAL:
                    return "ended"
                return "call" if practice.gate.entered >= calls else None

            state = wait_until(reached, timeout=DEADLINE_S, message=f"call {step} or the end")
            if state == "ended":
                break
            row = practice.row(task_id)
            assert str(row.status.value) == "running"
            assert row.result_ref is None, "no result before the end"
            progress.append(row.progress)
            beats.append(row.heartbeat_at)
            practice.clock.advance(timedelta(seconds=1))
            practice.gate.step()

        row = practice.wait_terminal(task_id)
        steps = len(progress)
        assert str(row.status.value) == "succeeded", row.error
        assert steps >= 2, "more than one step"
        assert all(0 <= value < 100 for value in progress), progress
        assert progress == sorted(set(progress)), f"progress rises with every step: {progress}"
        later = beats[1:]
        assert None not in later, "a heartbeat between steps"
        assert later == sorted(set(later)), f"one heartbeat per step: {beats}"  # type: ignore[type-var]
        assert all(delay > 0 for delay in practice.gate.delays), "slow calls take time"

        match = RESULT_REF.fullmatch(row.result_ref or "")
        assert match is not None, row.result_ref
        assert int(match.group(1)) == steps
        assert row.error is None
        assert row.attempt == 1

        assert len(practice.fake.requests) == steps, "one fake call per step"
        assert {request.task for request in practice.fake.requests} == {PRACTICE}
        assert practice.usage_rows() == [("fast", PRACTICE, steps, 0)]


@pytest.mark.parametrize("tier", TIERS)
def test_practice_tier_chooses_the_model_and_the_usage_tier(tmp_path: Path, tier: str) -> None:
    with practising(tmp_path) as practice:
        task_id = started(practice, tier=tier)
        row = practice.wait_terminal(task_id)
        assert str(row.status.value) == "succeeded", row.error
        calls = len(practice.fake.requests)
        assert calls >= 2
        assert {request.model for request in practice.fake.requests} == {MODELS[tier]}
        assert practice.usage_rows() == [(tier, PRACTICE, calls, 0)]


def test_practice_quick_and_slow_have_the_same_steps(tmp_path: Path) -> None:
    """Duration changes how long a call takes, never how the task is counted."""
    with practising(tmp_path) as practice:
        quick = practice.wait_terminal(started(practice, duration="quick"))
        quick_calls = len(practice.fake.requests)
        quick_delays = sum(practice.gate.delays)
        slow = practice.wait_terminal(started(practice, duration="slow"))
        slow_calls = len(practice.fake.requests) - quick_calls
        slow_delays = sum(practice.gate.delays) - quick_delays
        assert (str(quick.status.value), str(slow.status.value)) == ("succeeded", "succeeded")
        assert quick_calls >= 2
        assert slow_delays > quick_delays, "slow asks the fake for longer calls"
        assert RESULT_REF.fullmatch(quick.result_ref or "")
        assert RESULT_REF.fullmatch(slow.result_ref or "")
        assert quick.id != slow.id, "a finished task doesn't block the next start"
        assert slow_calls >= 2


@pytest.mark.parametrize(
    ("outcome", "error", "requests"),
    [
        ("usage_limit", UsageLimitReached, 1),
        ("network", NetworkUnavailable, 1),
        ("invalid_output", InvalidModelOutput, 2),
    ],
    ids=["usage_limit", "network", "invalid_output"],
)
def test_practice_outcomes_map_to_errors(
    tmp_path: Path, outcome: str, error: type[Any], requests: int
) -> None:
    with practising(tmp_path) as practice:
        task_id = started(practice, outcome=outcome)
        row = practice.wait_terminal(task_id)
        assert str(row.status.value) == "failed"
        assert row.error == error.kind
        assert row.result_ref is None
        assert len(practice.fake.requests) == requests
        # Every attempt is counted and none is left open (the retry is a second request).
        assert practice.usage_rows() == [("fast", PRACTICE, requests, 0)]
        # The failure belongs to that run only: the next start works.
        again = practice.wait_terminal(started(practice))
        assert str(again.status.value) == "succeeded", again.error


def test_practice_duplicate_start_returns_running_task(tmp_path: Path) -> None:
    with practising(tmp_path) as practice:
        practice.gate.hold = True
        task_id = started(practice, duration="slow")
        practice.wait_entered(1)
        assert practice.status(task_id) == "running"

        for values in ({"duration": "slow"}, {"tier": "thorough", "outcome": "network"}):
            again = practice.start(**values)
            assert again.status_code in STARTED, again.status_code
            row = practice.only_task()
            assert row.id == task_id, "the running task, not a second one"
            assert str(row.status.value) == "running"
            assert row.attempt == 1
        assert practice.gate.entered == 1, "no second run started"

        practice.gate.open()
        row = practice.wait_terminal(task_id)
        assert str(row.status.value) == "succeeded", "the first start's values ran"
        assert {request.model for request in practice.fake.requests} == {FAKE_FAST_MODEL}
        assert len(practice.tasks()) == 1


def test_practice_cancel_mid_run_ends_cancelled(tmp_path: Path) -> None:
    with practising(tmp_path) as practice:
        practice.gate.hold = True
        task_id = started(practice, duration="slow")
        practice.wait_entered(1)
        assert practice.app.state.task_service.request_cancel(task_id) is not None
        practice.gate.open()

        row = practice.wait_terminal(task_id)
        assert str(row.status.value) == "cancelled"
        assert row.result_ref is None, "a cancelled run saves no result"
        assert row.error is None
        assert len(practice.fake.requests) == 1, "no step after the cancel"
        rows = practice.usage_rows()
        assert [(task_type, unfinished) for _tier, task_type, _n, unfinished in rows] == [
            (PRACTICE, 0)
        ], "the cancelled call is counted and completed"


# --- what reaches the provider -----------------------------------------------------------------


def test_practice_request_has_no_personal_data(tmp_path: Path) -> None:
    """The fake's recorded requests hold one fixed fictional text, whatever was chosen and
    whatever the workspace holds (the seeded job preferences, the data folder, the token)."""
    with practising(tmp_path) as practice:
        for values in ({}, {"tier": "thorough", "duration": "slow"}):
            row = practice.wait_terminal(started(practice, **values))
            assert str(row.status.value) == "succeeded", row.error
        requests = practice.fake.requests
        assert len(requests) >= 4

        personal = [
            *SAVED.target_roles,
            *SAVED.locations,
            *SAVED.must_haves,
            *SAVED.deal_breakers,
            str(SAVED.minimum_salary.amount if SAVED.minimum_salary else "45000"),
            str(tmp_path),
            tmp_path.name,
            str(Path.home()),
            practice.run.token,
        ]
        texts = set()
        for request in requests:
            assert request.task == PRACTICE
            assert request.prompt_id == PRACTICE
            assert request.allow_web_search is False
            assert request.on_tool_use is None
            for text in (request.system, request.user):
                for value in personal:
                    assert value not in text, "nothing from the workspace reaches the prompt"
            # A step number may differ; nothing else may.
            texts.add((re.sub(r"\d+", "#", request.system), re.sub(r"\d+", "#", request.user)))
        assert len(texts) == 1, "one fixed text for every step, tier and duration"
        assert len({repr(sorted(request.output_schema.items())) for request in requests}) == 1


def test_practice_invalid_output_retry_adds_only_the_fixed_correction(tmp_path: Path) -> None:
    """The retry's request is the first one plus the executor's correction note."""
    with practising(tmp_path) as practice:
        row = practice.wait_terminal(started(practice, outcome="invalid_output"))
        assert str(row.status.value) == "failed"
        first, retry = practice.fake.requests
        assert retry.system == first.system
        assert retry.user.startswith(first.user)
        assert str(tmp_path) not in retry.user


# --- review follow-ups (M2.17) -----------------------------------------------------------------


def by_id(elements: list[Element], element_id: str) -> Element | None:
    found = [e for e in elements if e.attrs.get("id") == element_id]
    assert len(found) <= 1, element_id
    return found[0] if found else None


def save_provider(practice: Practice, kind: str, credential: str | None = None) -> Response:
    """Settings > Providers, saved through htmx."""
    form = {"kind": kind} if credential is None else {"kind": kind, "credential": credential}
    headers = practice.run.action(**{"HX-Request": "true"})
    return practice.run.client.post(SETTINGS_PROVIDERS, data=form, headers=headers)


def switch_saved_kind(practice: Practice, kind: str) -> None:
    """Change the saved provider kind behind the app's back (as another tab would)."""
    settings = SettingsService(practice.app.state.workspace.db, practice.clock)
    loaded = settings.load(AI_PROVIDER)
    changed = loaded.value.model_copy(update={"kind": kind})
    settings.save(AI_PROVIDER, changed, expected_version=loaded.version)


def test_testing_tools_copy_promises_no_task_list(tmp_path: Path) -> None:
    with practising(tmp_path) as practice:
        page = practice.run.client.get(SETTINGS)
        elements = outline(page.text)
        tools = section(elements)
        assert tools is not None
        hints = [e for e in elements if e.attrs.get("id") == "practice-hint"]
        assert [e.text for e in hints] == [HINT]
        assert "task list" not in page.text


def test_settings_keeps_an_empty_testing_tools_wrapper_when_not_offered(tmp_path: Path) -> None:
    """The wrapper is always in the page, so a later provider save can fill it."""
    with practising(tmp_path, environ=NO_FAKES) as practice:
        elements = outline(practice.run.client.get(SETTINGS).text)
        wrapper = by_id(elements, TOOLS_ID)
        assert wrapper is not None
        assert [e for e in elements if e is not wrapper and e.within(wrapper)] == []
        assert "hx-swap-oob" not in wrapper.attrs
        providers = by_id(elements, "providers")
        assert providers is not None and not wrapper.within(providers)
    with practising(_folder(tmp_path, "offered")) as practice:
        elements = outline(practice.run.client.get(SETTINGS).text)
        wrapper = by_id(elements, TOOLS_ID)
        tools = section(elements)
        assert wrapper is not None and tools is not None
        assert tools.within(wrapper) or tools is wrapper
        assert "hx-swap-oob" not in wrapper.attrs


def _folder(tmp_path: Path, name: str) -> Path:
    folder = tmp_path / name
    folder.mkdir()
    return folder


def test_provider_save_refreshes_testing_tools_out_of_band(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Saving a Claude route over the fake through htmx empties the section in the same
    answer; saving the fake again brings it back. A form left over in another tab gets a
    plain 404 message."""
    install_fake_sdk(monkeypatch, tmp_path / "sdk", with_cli=True)
    with practising(tmp_path) as practice:
        away = save_provider(practice, TOKEN_KIND, FICTIONAL_TOKEN)
        assert away.status_code == 200, (away.status_code, away.text[:300])
        elements = outline(away.text)
        assert by_id(elements, "providers") is not None, "still the Providers section"
        wrapper = by_id(elements, TOOLS_ID)
        assert wrapper is not None, "the section is refreshed with the same answer"
        assert wrapper.attrs.get("hx-swap-oob") == "true"
        assert [e for e in elements if e is not wrapper and e.within(wrapper)] == []
        assert section(elements) is None
        assert PRACTICE_PATH not in away.text
        assert FICTIONAL_TOKEN not in away.text

        for htmx in (False, True):
            stale = practice.start(htmx=htmx)
            assert stale.status_code == 404
            assert stale.text == MSG_NOT_OFFERED
            assert stale.headers["content-type"].startswith("text/plain")
        assert practice.tasks() == []

        back = save_provider(practice, "fake")
        assert back.status_code == 200, (back.status_code, back.text[:300])
        elements = outline(back.text)
        wrapper = by_id(elements, TOOLS_ID)
        tools = section(elements)
        assert wrapper is not None and tools is not None
        assert wrapper.attrs.get("hx-swap-oob") == "true"
        assert tools.within(wrapper)
        assert not wrapper.within(by_id(elements, "providers"))  # type: ignore[arg-type]
        assert by_id(elements, STATUS_ID) is None, "nothing was started"
        focused = [e for e in elements if "data-autofocus" in e.attrs and e.within(wrapper)]
        assert focused == [], "the refreshed section never takes focus"
        row = practice.wait_terminal(started(practice))
        assert str(row.status.value) == "succeeded", row.error


def test_plain_start_redirects_with_a_fixed_flag_and_settings_shows_it(tmp_path: Path) -> None:
    with practising(tmp_path) as practice:
        practice.gate.hold = True
        first = practice.start(duration="slow")
        assert first.status_code == 303
        assert first.headers["location"] == f"{SETTINGS}?practice=started#{TOOLS_ID}"
        practice.wait_entered(1)
        second = practice.start()
        assert second.status_code == 303
        assert second.headers["location"] == f"{SETTINGS}?practice=already#{TOOLS_ID}"
        assert len(practice.tasks()) == 1

        for flag, message in (("started", MSG_STARTED), ("already", MSG_ALREADY)):
            page = practice.run.client.get(f"{SETTINGS}?practice={flag}")
            assert page.status_code == 200
            status = by_id(outline(page.text), STATUS_ID)
            assert status is not None and status.text == message, flag
        for other in ("", "Started", "started ", "fictional", "<b>started</b>", "already&x=1"):
            page = practice.run.client.get(SETTINGS, params={"practice": other})
            assert page.status_code == 200
            assert by_id(outline(page.text), STATUS_ID) is None, other
            assert "<b>started</b>" not in page.text
        assert by_id(outline(practice.run.client.get(SETTINGS).text), STATUS_ID) is None
        # GET has no side effects, whatever the flag says (spec 2.9 rule 12).
        assert len(practice.tasks()) == 1
        assert practice.gate.entered == 1
    with practising(_folder(tmp_path, "off"), environ=NO_FAKES) as off:
        page = off.run.client.get(f"{SETTINGS}?practice=started")
        assert page.status_code == 200
        assert MSG_STARTED not in page.text
        assert off.tasks() == []


def test_htmx_start_answers_the_section_with_status_announcement_and_choices(
    tmp_path: Path,
) -> None:
    """Like Providers: the status line takes focus and the same words are announced. The
    accepted choices stay selected."""
    with practising(tmp_path) as practice:
        practice.gate.hold = True
        chosen = {"duration": "slow", "outcome": "network", "tier": "thorough"}
        response = practice.start(htmx=True, **chosen)
        assert response.status_code == 200
        assert "<html" not in response.text
        elements = outline(response.text)
        wrapper = by_id(elements, TOOLS_ID)
        assert wrapper is not None and "hx-swap-oob" not in wrapper.attrs
        status = by_id(elements, STATUS_ID)
        assert status is not None and status.text == MSG_STARTED
        assert [e.attrs.get("id") for e in elements if "data-autofocus" in e.attrs] == [STATUS_ID]
        assert json.loads(response.headers["hx-trigger"]) == {"announce": MSG_STARTED}
        selected = {
            e.parent.attrs.get("name"): e.attrs.get("value")
            for e in elements
            if e.tag == "option" and "selected" in e.attrs and e.parent is not None
        }
        assert selected == chosen

        practice.wait_entered(1)
        again = practice.start(htmx=True)
        assert again.status_code == 200
        status = by_id(outline(again.text), STATUS_ID)
        assert status is not None and status.text == MSG_ALREADY
        assert json.loads(again.headers["hx-trigger"]) == {"announce": MSG_ALREADY}

        # A refused form: the error takes focus, nothing is announced (as in Providers) and
        # nothing is selected.
        refused = practice.start(htmx=True, outcome="fictional")
        assert refused.status_code == 422
        elements = outline(refused.text)
        assert [e.attrs.get("id") for e in elements if "data-autofocus" in e.attrs] == [
            "practice-error"
        ]
        assert "hx-trigger" not in refused.headers
        assert [e for e in elements if e.tag == "option" and "selected" in e.attrs] == []


def test_starting_note_is_a_status_region(tmp_path: Path) -> None:
    with practising(tmp_path) as practice:
        elements = outline(practice.run.client.get(SETTINGS).text)
        note = by_id(elements, "practice-starting")
        assert note is not None and note.attrs.get("role") == "status"
        form_element = next(e for e in elements if e.attrs.get("id") == "practice-form")
        assert form_element.attrs.get("hx-indicator") == "#practice-starting"


def test_practice_too_large_form_413(tmp_path: Path) -> None:
    with practising(tmp_path) as practice:
        response = practice.start(padding="x" * (70 * 1024))
        assert response.status_code == 413
        assert practice.tasks() == []
        assert practice.fake.requests == []


def test_queued_practice_fails_when_no_longer_offered(tmp_path: Path) -> None:
    """A task the runner claims after the saved provider stopped being the fake (queued
    here straight through the task service, as if it had been waiting) fails with its own
    error: no provider call and nothing counted."""
    with practising(tmp_path) as practice:
        switch_saved_kind(practice, TOKEN_KIND)
        ref = practice.app.state.task_service.enqueue(
            PRACTICE, "settings", input_ref="quick/ok/fast"
        )
        row = practice.wait_terminal(ref.id)
        assert str(row.status.value) == "failed"
        assert row.error == PracticeNotOffered.kind == "PracticeNotOffered"
        assert row.result_ref is None
        assert practice.fake.requests == []
        assert practice.usage_rows() == []
    assert PracticeNotOffered().user_message == (
        "Practice tasks are only available while the fake provider is active."
    )


@pytest.mark.parametrize(
    "input_ref", [None, "", "quick/ok", "quick/ok/fast/extra", "Quick/ok/fast", "slow/auth/fast"]
)
def test_practice_with_corrupt_input_fails_without_a_call(
    tmp_path: Path, input_ref: str | None
) -> None:
    with practising(tmp_path) as practice:
        ref = practice.app.state.task_service.enqueue(PRACTICE, "settings", input_ref=input_ref)
        row = practice.wait_terminal(ref.id)
        assert str(row.status.value) == "failed"
        assert row.error == TaskFailed.kind
        assert practice.fake.requests == []
        assert practice.usage_rows() == []


def test_practice_not_offered_when_its_type_is_not_registered(tmp_path: Path) -> None:
    """An app given its own task registry without the practice type shows no form."""
    with practising(tmp_path, task_registry=HandlerRegistry()) as practice:
        assert practice.app.state.wiring.allow_fakes is True
        assert practice.app.state.task_registry.get(PRACTICE) is None
        assert_not_offered(practice)
    assert_offered_control(tmp_path)


def test_practice_fields_are_read_only() -> None:
    from app.services.practice import PRACTICE_FIELDS

    assert dict(PRACTICE_FIELDS) == {name: values for name, values in FIELDS.items()}
    with pytest.raises(TypeError):
        PRACTICE_FIELDS["duration"] = ("fictional",)  # type: ignore[index]


def test_practice_handler_lives_in_its_own_module() -> None:
    import app.tasks.handlers as generic
    from app.tasks.practice import practice_handler

    assert callable(practice_handler)
    assert not hasattr(generic, "practice_handler")
    assert not hasattr(generic, "PracticeService")
