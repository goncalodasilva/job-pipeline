"""Step 2b of the setup guide: test the connection, mapped messages (M1.18; AC-M1.3, AC-M1.5).

``POST /setup/provider/test`` (launch token and origin; not page-tagged) takes the provider
form (``kind``, ``credential``), first saves it exactly like the provider step's Save (a
refused save answers 422 and runs no test), then awaits the provider's ``test_connection`` with
the Fast tier model inside the request, with a timeout (ADR-044 item 6, ADR-058). With the fake
the pasted text only picks the outcome (ADR-054); with a Claude kind wiring builds the provider
for the workspace folder (ADR-059). "Connected" marks the provider step done, refreshes the
setup decision and shows a Continue link to the next step (ADR-052); a failure shows the
spec 2.11 message and leaves the step not done. Either result answers 200: for htmx the provider
step's guide fragment with an ``announce`` trigger carrying the result text and focus on
``#connection-result``; for a plain request the full page (no redirect).

Builds on the M1.17 helpers in ``tests/integration/test_setup_provider.py``. Providers are the
app-selectable fake, or the Claude provider over the SDK's real client with the replaying
transport (``tests/fakes/claude_query.py``), so nothing starts a CLI or uses the network. Every
folder lives under ``tmp_path``; every value is fictional and built at run time.
"""

import json
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import CLINotFoundError
from httpx import Response

from app.domain.errors import (
    NetworkUnavailable,
    ProviderAuthError,
    ProviderNotInstalled,
    UsageLimitReached,
)
from app.domain.settings import AIProviderSettings, ProviderKind, Tier, claude_auth_mode
from app.modules.ai import AIProvider, ProviderHealth
from app.modules.ai.providers.claude_sdk import CLAUDE_FAST_DEFAULT, ClaudeSdkProvider
from app.modules.ai.providers.fake import FAKE_FAST_MODEL, FakeAIProvider
from app.onboarding.detection import StartupDecision
from app.wiring import AI_TEMP_FOLDER, Wiring
from tests.conftest import ALLOW_FAKES_VARIABLE
from tests.fakes.claude_query import ReplayingQuery, replaying, success_frames
from tests.fakes.claude_transport import load_frames
from tests.fakes.keyring_backends import MemoryKeyring
from tests.fakes.sdk_install import install_fake_sdk
from tests.integration.test_setup_provider import (
    API_KEY_KIND,
    API_KEY_VARIABLE,
    EXISTING_KIND,
    FAKE_KIND,
    FICTIONAL_TOKEN,
    PROVIDER,
    TOKEN_KIND,
    assert_credential_error,
    assert_not_in_files,
    assert_not_in_logs,
    assert_not_in_responses,
    by_id,
    data_folder,
    keychain,
    live_statuses,
    make_wiring,
    only,
    page,
    required,
    running,
    stored,
    submit,
)
from tests.integration.test_setup_redirect import (
    BASE_URL,
    Element,
    Running,
    assert_see_other,
    outline,
)
from tests.setup_harness import seed_workspace

pytestmark = pytest.mark.integration

TEST_PATH = "/setup/provider/test"
PREFERENCES = "/setup/preferences"
NEXT_STEP_ANNOUNCE = " Next, test the connection."
GENERIC_FAILURE = "The connection test didn't finish. Nothing was lost; try again."
TESTING_NOTE = (
    "Testing the connection… This sends one very small request, which uses a tiny amount of "
    "your plan or credit. This can take up to a minute."
)
# Failure lines start with this, in the result and the announcement alike (M1.18 review).
NOT_CONNECTED = "Not connected: "
REPORTED_MODEL = "claude-haiku-m118"  # shares no 6-character piece with the fictional keys


def fake_key(keyword: str) -> str:
    """A fictional key for the fake provider: the keyword picks the outcome (ADR-054)."""
    return "-".join([keyword, "FICTIONAL", "m118", "Tq4Wz8Kd2Rv"])


FAKE_CONNECTED = fake_key("fake-connected")
ENV_API_KEY = "-".join(["FictionalEnvApiKey", "0118", "FICTIONAL", "notreal"])
# A pasted API key of a valid shape (not an Anthropic-looking prefix), fictional.
PASTED_API_KEY = "-".join(["FictionalPastedApiKey", "0118", "FICTIONAL", "Zr4Wq"])

# The fake keeps the domain messages (spec 2.11); Claude routes get route-specific words.
FAILURES: dict[str, tuple[str, str]] = {
    "auth": ("fake-auth-rejected", NOT_CONNECTED + ProviderAuthError().user_message),
    "not_installed": ("fake-not-installed", NOT_CONNECTED + ProviderNotInstalled().user_message),
    "usage_limit": ("fake-usage-limit", NOT_CONNECTED + UsageLimitReached().user_message),
    "network": ("fake-offline", NOT_CONNECTED + NetworkUnavailable().user_message),
}


# --- helpers -------------------------------------------------------------------------------


@pytest.fixture
def bundled_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The SDK looks installed with its bundled CLI (a placeholder that is never run), so a
    Claude kind's provider health is ok."""
    return install_fake_sdk(monkeypatch, tmp_path / "sdk", with_cli=True)


class RecordingFactory:
    """The app's wiring with every built provider recorded. ``query`` swaps the Claude
    provider's SDK client for the replaying one (no CLI, no network); the provider keeps
    wiring's secret store, sign-in mode and ``<data>/tmp/ai`` folder. Everything else is
    the wrapped wiring's."""

    def __init__(
        self, wiring: Wiring, *, query: ReplayingQuery | None = None, cli: Path | None = None
    ) -> None:
        self._wiring = wiring
        self._query = query
        self._cli = cli
        self.built: list[tuple[str | None, Path]] = []  # (kind, data_dir) per Claude/fake build
        self.fakes = 0  # fakes built from a pasted key (the key itself is never kept)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._wiring, name)

    @property
    def calls(self) -> int:
        return len(self.built) + self.fakes

    def ai_provider(self, settings: AIProviderSettings, *, data_dir: Path) -> AIProvider:
        provider = self._wiring.ai_provider(settings, data_dir=data_dir)  # wiring's own checks
        self.built.append((settings.kind, data_dir))
        if self._query is None or not isinstance(provider, ClaudeSdkProvider):
            return provider
        mode = claude_auth_mode(settings.kind) if settings.kind is not None else None
        assert mode is not None
        cli = self._cli if self._cli is not None else data_dir / "fictional-cli" / "claude"
        return ClaudeSdkProvider(
            secrets=self._wiring.secrets,
            mode=mode,
            client_factory=self._query,
            cli_path=lambda: cli,
            environ={},  # never the developer's environment
            temp_root=data_dir.joinpath(*AI_TEMP_FOLDER),
        )

    def fake_for_key(self, key: str | None) -> FakeAIProvider:
        self.fakes += 1
        return self._wiring.fake_for_key(key)

    def default_models(self, kind: ProviderKind | None) -> Mapping[Tier, str]:
        return self._wiring.default_models(kind)


def recording(environ: dict[str, str] | None = None, **kwargs: Any) -> RecordingFactory:
    return RecordingFactory(make_wiring(environ), **kwargs)


def post_test(
    run: Running, kind: str | None, credential: str | None = None, *, htmx: bool = True
) -> Response:
    form: dict[str, str] = {}
    if kind is not None:
        form["kind"] = kind
    if credential is not None:
        form["credential"] = credential
    headers = run.action(**({"HX-Request": "true"} if htmx else {}))
    return run.client.post(TEST_PATH, data=form, headers=headers)


def announced(response: Response) -> str:
    trigger = json.loads(response.headers["hx-trigger"])
    assert set(trigger) == {"announce"}, trigger
    message: str = trigger["announce"]
    return message


def result_element(response: Response) -> Element:
    return required(outline(response.text), "connection-result")


def continue_links(elements: list[Element]) -> list[Element]:
    return [e for e in elements if e.tag == "a" and e.text == "Continue"]


def provider_item(elements: list[Element]) -> Element:
    steps = only([e for e in elements if e.tag == "ol"], "step list")
    return only(
        [e for e in elements if e.tag == "li" and e.within(steps) and "AI provider" in e.text],
        "AI provider in the step list",
    )


def assert_connection_answer(response: Response, *, htmx: bool = True) -> Element:
    """200 with the provider step and its result; htmx: the fragment, an announce trigger
    and no push URL; plain: the full page, no redirect."""
    assert response.status_code == 200, (response.status_code, response.text[:300])
    assert "location" not in response.headers
    assert "hx-push-url" not in response.headers, "the guide stays on the provider step"
    elements = outline(response.text)
    current = only([e for e in elements if e.attrs.get("aria-current") == "step"], "current")
    assert current.text.startswith("AI provider"), current.text
    if htmx:
        for marker in ("<html", "<body", 'id="announcer"'):
            assert marker not in response.text, marker
    else:
        assert "<html" in response.text
        assert 'id="announcer"' in response.text
    return result_element(response)


def assert_result_focused_and_announced(response: Response) -> str:
    """The result has the focus mark (and nothing else does), is not itself a live region,
    and the announcement is its text; returns that text."""
    elements = outline(response.text)
    result = required(elements, "connection-result")
    focused = [e.attrs.get("id") for e in elements if "data-autofocus" in e.attrs]
    assert focused == ["connection-result"], focused
    assert result.attrs.get("role") in (None, "none"), "announced once, through the announcer"
    assert "aria-live" not in result.attrs
    message = announced(response)
    assert result.text == message, (result.text, message)
    return message


# --- AC-M1.3: the fake's outcomes -----------------------------------------------------------------


def test_ac_m1_3_fake_connected_shows_connected(
    tmp_path: Path, memory_keyring: MemoryKeyring
) -> None:
    factory = recording()
    with running(tmp_path, wiring=factory) as run:  # type: ignore[arg-type]
        response = post_test(run, FAKE_KIND, FAKE_CONNECTED)
        result = assert_connection_answer(response)
        assert result.text.startswith("Connected"), result.text
        assert FAKE_FAST_MODEL in result.text, "the model the provider reported"
        assert announced(response) == result.text
        assert factory.fakes == 1 and factory.built == [], "the fake for the pasted key"

        # Saved first, like Save: the fake kind, and no key kept anywhere.
        assert stored(run).kind == FAKE_KIND
        assert stored(run).saved_sign_ins == ()

        elements = outline(response.text)
        links = continue_links(elements)
        assert [link.attrs.get("href") for link in links] == [PREFERENCES]
        assert "Done" in provider_item(elements).text, "the step list says the step is done"

        # A plain request gets the full page with the result, not a redirect.
        plain = post_test(run, FAKE_KIND, FAKE_CONNECTED, htmx=False)
        assert assert_connection_answer(plain, htmx=False).text.startswith("Connected")
    assert keychain(memory_keyring) == {}


@pytest.mark.parametrize("outcome", sorted(FAILURES))
def test_ac_m1_3_failures_show_mapped_message(
    tmp_path: Path, memory_keyring: MemoryKeyring, outcome: str
) -> None:
    keyword, message = FAILURES[outcome]
    with running(tmp_path) as run:
        response = post_test(run, FAKE_KIND, fake_key(keyword))
        result = assert_connection_answer(response)
        assert result.text == message
        assert not result.text.startswith("Connected")
        assert announced(response) == message
        assert continue_links(outline(response.text)) == [], "no way on after a failure"
        assert "Done" not in provider_item(outline(response.text)).text

        plain = post_test(run, FAKE_KIND, fake_key(keyword), htmx=False)
        assert assert_connection_answer(plain, htmx=False).text == message

        assert stored(run).kind == FAKE_KIND, "the choice was saved before the test"
        assert live_statuses(run).get("provider") != "done"
        assert run.decision == StartupDecision("guide", "provider")
    assert keychain(memory_keyring) == {}


def test_fake_unknown_key_reads_as_rejected(tmp_path: Path) -> None:
    """Anything without a known keyword is rejected like a bad key (ADR-054)."""
    with running(tmp_path) as run:
        response = post_test(run, FAKE_KIND, "-".join(["just", "FICTIONAL", "text", "0118"]))
        rejected = NOT_CONNECTED + ProviderAuthError().user_message
        assert assert_connection_answer(response).text == rejected
        empty = post_test(run, FAKE_KIND, "")
        assert assert_connection_answer(empty).text == rejected
        assert live_statuses(run).get("provider") != "done"


# --- the route ------------------------------------------------------------------------------


def test_test_connection_is_post_only(tmp_path: Path) -> None:
    factory = recording()
    with running(tmp_path, wiring=factory) as run:  # type: ignore[arg-type]
        query = {"kind": FAKE_KIND, "credential": FAKE_CONNECTED}
        for headers in (run.with_token(), run.htmx(), run.action()):
            got = run.client.get(TEST_PATH, params=query, headers=headers)
            head = run.client.head(TEST_PATH, params=query, headers=headers)
            assert got.status_code == 405, (got.status_code, got.text[:200])
            assert head.status_code == 405, head.status_code
        assert factory.calls == 0, "no test runs on GET or HEAD"
        assert stored(run).kind is None, "nothing saved"
        assert live_statuses(run).get("provider") != "done"
        assert run.decision == StartupDecision("guide", "provider")

    routes = [
        route
        for route in run.app.routes
        if getattr(route, "path", None) == TEST_PATH
        or getattr(route, "path_format", None) == TEST_PATH
    ]
    route = only(routes, f"route {TEST_PATH}")  # type: ignore[arg-type]
    assert getattr(route, "methods", None) == {"POST"}
    assert "page" not in (getattr(route, "tags", None) or []), "token and origin apply"


def test_test_route_needs_token_and_origin(tmp_path: Path, memory_keyring: MemoryKeyring) -> None:
    factory = recording()
    form = {"kind": FAKE_KIND, "credential": FAKE_CONNECTED}
    with running(tmp_path, wiring=factory) as run:  # type: ignore[arg-type]
        no_token = run.client.post(TEST_PATH, data=form, headers={"origin": BASE_URL})
        bad_origin = run.client.post(
            TEST_PATH, data=form, headers=run.action(origin="http://evil.example")
        )
        no_origin = run.client.post(TEST_PATH, data=form, headers=run.with_token())
        assert no_token.status_code == 403
        assert bad_origin.status_code == 403
        assert no_origin.status_code == 403
        assert factory.calls == 0
        assert stored(run).kind is None
        assert live_statuses(run).get("provider") != "done"
    assert keychain(memory_keyring) == {}


def test_refused_save_runs_no_test(
    tmp_path: Path, memory_keyring: MemoryKeyring, caplog: pytest.LogCaptureFixture
) -> None:
    factory = recording()
    short = "FICTIONAL-short"
    with caplog.at_level(logging.DEBUG), running(tmp_path, wiring=factory) as run:  # type: ignore[arg-type]
        refused = post_test(run, TOKEN_KIND, short)
        assert_credential_error(refused)
        assert by_id(outline(refused.text), "connection-result") is None, "no result line"
        title = only([e for e in outline(refused.text) if e.tag == "title"], "<title>")
        assert title.text.startswith("Error: AI provider"), title.text

        empty = post_test(run, API_KEY_KIND, "")  # nothing saved, nothing in env
        assert_credential_error(empty)

        unknown = post_test(run, "not-a-provider", FAKE_CONNECTED)
        assert unknown.status_code == 422

        plain = post_test(run, TOKEN_KIND, short, htmx=False)
        assert_credential_error(plain)

        assert factory.calls == 0, "a refused save runs no test"
        assert stored(run).kind is None
        assert live_statuses(run).get("provider") != "done"
        assert run.decision == StartupDecision("guide", "provider")
    assert keychain(memory_keyring) == {}
    assert_not_in_responses(short, [refused, plain])
    assert_not_in_logs(short, caplog)


def test_test_without_workspace_asks_for_step_1(tmp_path: Path) -> None:
    factory = recording()
    with running(tmp_path, wiring=factory, welcome_done=False) as run:  # type: ignore[arg-type]
        response = post_test(run, FAKE_KIND, FAKE_CONNECTED)
        assert response.status_code == 422, response.text[:300]
        assert "step 1" in response.text.lower()
        assert factory.calls == 0
        assert run.app.state.workspace is None
    assert not data_folder(tmp_path).exists(), "nothing is created"


def test_fake_key_never_in_responses_logs_or_files(
    tmp_path: Path, memory_keyring: MemoryKeyring, caplog: pytest.LogCaptureFixture
) -> None:
    keys = [FAKE_CONNECTED, *(fake_key(keyword) for keyword, _ in FAILURES.values())]
    responses: list[Response] = []
    with caplog.at_level(logging.DEBUG), running(tmp_path) as run:
        for key in keys:
            answer = post_test(run, FAKE_KIND, key)
            assert_connection_answer(answer)
            responses.append(answer)
            plain = post_test(run, FAKE_KIND, key, htmx=False)
            assert_connection_answer(plain, htmx=False)
            responses.append(plain)
        responses.append(page(run))
        responses.append(run.client.get(PROVIDER, headers=run.htmx()))
    assert keychain(memory_keyring) == {}
    for key in keys:
        # The outcome keywords may appear in the page's copy; the key as a whole never does.
        assert_not_in_responses(key, responses, pieces=False)
        assert_not_in_logs(key, caplog)
        assert_not_in_files(key, data_folder(tmp_path), tmp_path / "cfg")
    for response in responses:
        assert "m118-Tq4Wz8Kd2Rv" not in response.text, "no tail of a pasted key"


# --- AC-M1.5: the environment credential reaches the provider ------------------------------------


def test_ac_m1_5_works_with_env_variable_set(
    tmp_path: Path,
    memory_keyring: MemoryKeyring,
    caplog: pytest.LogCaptureFixture,
    bundled_cli: Path,
) -> None:
    """``ANTHROPIC_API_KEY`` set when wiring is built, nothing in the keychain: the API-key
    choice saves with an empty field, and the connection test's request carries the variable's
    value to the Claude provider (the SDK's real client over the replaying transport)."""
    query = replaying(success_frames({"ok": True}, model=REPORTED_MODEL))
    factory = recording({ALLOW_FAKES_VARIABLE: "1", API_KEY_VARIABLE: ENV_API_KEY}, query=query)
    with caplog.at_level(logging.DEBUG), running(tmp_path, wiring=factory) as run:  # type: ignore[arg-type]
        response = post_test(run, API_KEY_KIND, "")
        result = assert_connection_answer(response)
        assert result.text.startswith("Connected"), result.text
        assert REPORTED_MODEL in result.text

        workspace = run.app.state.workspace.data_dir
        assert factory.built == [(API_KEY_KIND, workspace)], "built for the workspace folder"
        assert len(query.calls) == 1, "one connection request"
        call = query.last
        assert call.env.get("ANTHROPIC_API_KEY") == ENV_API_KEY, "the variable's value is used"
        assert call.options.model == CLAUDE_FAST_DEFAULT, "the Fast tier model"

        settings = stored(run)
        assert (settings.kind, settings.saved_sign_ins) == (API_KEY_KIND, ())
        assert run.app.state.provider_health == ProviderHealth("ok")
        assert live_statuses(run).get("provider") == "done"
        assert run.decision == StartupDecision("guide", "preferences")
        responses = [response, page(run)]
    assert keychain(memory_keyring) == {}
    assert_not_in_responses(ENV_API_KEY, responses)
    assert_not_in_logs(ENV_API_KEY, caplog)
    assert_not_in_files(ENV_API_KEY, data_folder(tmp_path), tmp_path / "cfg")


def test_claude_kind_tests_the_saved_token_with_the_fast_tier_model(
    tmp_path: Path, memory_keyring: MemoryKeyring, bundled_cli: Path
) -> None:
    """A pasted token is saved to the keychain first, then read back by the provider; the
    model is the settings' Fast tier model when one is set."""
    from app.domain.settings import TierMap

    query = replaying(success_frames({"ok": True}, model=REPORTED_MODEL))
    factory = recording(query=query)
    seeded = AIProviderSettings(tiers=TierMap(fast="fictional-fast-model"))
    with running(tmp_path, wiring=factory, provider=seeded) as run:  # type: ignore[arg-type]
        response = post_test(run, TOKEN_KIND, FICTIONAL_TOKEN)
        assert assert_connection_answer(response).text.startswith("Connected")
        assert query.last.options.model == "fictional-fast-model"
        assert query.last.env.get("CLAUDE_CODE_OAUTH_TOKEN") == FICTIONAL_TOKEN
        assert stored(run).saved_sign_ins == (TOKEN_KIND,)
    assert keychain(memory_keyring) == {"claude_code_oauth_token": FICTIONAL_TOKEN}
    assert_not_in_responses(FICTIONAL_TOKEN, [response])


# --- ADR-052: done only after "Connected" -------------------------------------------------------


def test_step_2_done_only_after_successful_test(
    tmp_path: Path, memory_keyring: MemoryKeyring
) -> None:
    with running(tmp_path) as run:
        # Save alone: not done.
        saved = submit(run, FAKE_KIND, FAKE_CONNECTED, htmx=True)
        assert saved.status_code == 200, saved.text[:300]
        assert live_statuses(run).get("provider") != "done"
        assert run.decision == StartupDecision("guide", "provider")

        # A failed test: still not done.
        failed = post_test(run, FAKE_KIND, fake_key("fake-offline"))
        assert assert_connection_answer(failed).text == (
            NOT_CONNECTED + NetworkUnavailable().user_message
        )
        assert live_statuses(run).get("provider") != "done"
        assert run.decision == StartupDecision("guide", "provider")
        assert_see_other(run.client.get("/"), PROVIDER)

        # Connected: done, the decision moves on, and page loads go to the next step.
        connected = post_test(run, FAKE_KIND, FAKE_CONNECTED)
        assert assert_connection_answer(connected).text.startswith("Connected")
        assert live_statuses(run).get("provider") == "done"
        assert run.app.state.onboarding.statuses.get("provider") == "done"
        assert run.decision == StartupDecision("guide", "preferences")
        assert_see_other(run.client.get("/"), PREFERENCES)

        # A later failed test doesn't undo it (transitions are monotonic).
        post_test(run, FAKE_KIND, fake_key("fake-usage-limit"))
        assert live_statuses(run).get("provider") == "done"

    # A restart resumes at the next step.
    with running(tmp_path) as run:
        assert run.decision == StartupDecision("guide", "preferences")
        assert_see_other(run.client.get("/"), PREFERENCES)
        assert_see_other(run.client.get("/setup"), PREFERENCES)
    assert keychain(memory_keyring) == {}


def test_continue_link_only_after_connected(tmp_path: Path) -> None:
    with running(tmp_path) as run:
        assert continue_links(outline(page(run).text)) == []
        saved = submit(run, FAKE_KIND, FAKE_CONNECTED, htmx=True)
        assert continue_links(outline(saved.text)) == []
        assert by_id(outline(saved.text), "connection-result") is None, "nothing tested yet"
        failed = post_test(run, FAKE_KIND, fake_key("fake-auth-rejected"))
        assert continue_links(outline(failed.text)) == []

        connected = post_test(run, FAKE_KIND, FAKE_CONNECTED)
        links = continue_links(outline(connected.text))
        assert [link.attrs.get("href") for link in links] == [PREFERENCES]

        # Opening the step again in this run shows the last result and the way on; no test
        # runs on GET.
        for shown in (page(run), run.client.get(PROVIDER, headers=run.htmx())):
            elements = outline(shown.text)
            assert required(elements, "connection-result").text.startswith("Connected")
            assert [a.attrs.get("href") for a in continue_links(elements)] == [PREFERENCES]


def test_continue_is_an_htmx_step_change_announced(tmp_path: Path) -> None:
    """ "Continue" moves to the next step like a submit does (ADR-053): an htmx GET of the
    guide, the URL pushed, with the href as the fallback. The htmx GET answers the fragment
    with the title, focus on the next heading and "Step 3 of 4: Job preferences" announced;
    a plain GET announces nothing. Neither changes anything."""
    with running(tmp_path) as run:
        connected = post_test(run, FAKE_KIND, FAKE_CONNECTED)
        link = only(continue_links(outline(connected.text)), "Continue")
        assert link.attrs.get("href") == PREFERENCES
        assert link.attrs.get("hx-get") == PREFERENCES
        assert link.attrs.get("hx-target") == "#setup-guide"
        assert link.attrs.get("hx-swap") == "outerHTML"
        assert link.attrs.get("hx-push-url") == "true"
        before = live_statuses(run)

        swapped = run.client.get(PREFERENCES, headers=run.htmx())
        assert swapped.status_code == 200, (swapped.status_code, swapped.text[:300])
        assert announced(swapped) == "Step 3 of 4: Job preferences"
        assert "<html" not in swapped.text
        assert "<title>Job preferences · Set up · Job Search Pipeline</title>" in swapped.text
        elements = outline(swapped.text)
        heading = required(elements, "step-heading")
        assert heading.text == "Job preferences"
        focused = [e.attrs.get("id") for e in elements if "data-autofocus" in e.attrs]
        assert focused == ["step-heading"], focused

        plain = run.client.get(PREFERENCES)
        assert plain.status_code == 200
        assert "<html" in plain.text
        assert "hx-trigger" not in plain.headers
        assert live_statuses(run) == before, "GET has no side effects"


def test_connected_shown_on_get_only_when_tested_in_this_run(tmp_path: Path) -> None:
    factory = recording()
    with running(tmp_path, wiring=factory) as run:  # type: ignore[arg-type]
        tested = post_test(run, FAKE_KIND, FAKE_CONNECTED)
        assert assert_connection_answer(tested).text.startswith("Connected")
        before = factory.calls
        page(run)
        run.client.get(PROVIDER, headers=run.htmx())
        assert factory.calls == before, "GET never runs a test"

    # After a restart the step is still done, but no result is claimed without a test.
    with running(tmp_path) as run:
        assert run.decision == StartupDecision("guide", "preferences")
        shown = page(run)
        elements = outline(shown.text)
        assert by_id(elements, "connection-result") is None
        assert "Done" in provider_item(elements).text, "the open step still says Done"


def test_step_done_in_an_earlier_run_shows_no_result(tmp_path: Path) -> None:
    """Seeding with the provider step done (another run) opens at preferences; the provider
    page shows no stale result."""
    seed_workspace(data_folder(tmp_path), done=("welcome", "provider"))
    with running(tmp_path, provider=AIProviderSettings(kind=FAKE_KIND)) as run:
        assert run.decision == StartupDecision("guide", "preferences")
        elements = outline(page(run).text)
        assert by_id(elements, "connection-result") is None
        assert "Done" in provider_item(elements).text, "the open step still says Done"


# --- the live region, the button and the "Testing…" state ------------------------------------


def test_result_message_in_live_region(tmp_path: Path) -> None:
    with running(tmp_path) as run:
        connected = post_test(run, FAKE_KIND, FAKE_CONNECTED)
        message = assert_result_focused_and_announced(connected)
        assert message.startswith("Connected")

        failed = post_test(run, FAKE_KIND, fake_key("fake-not-installed"))
        assert assert_result_focused_and_announced(failed) == (
            NOT_CONNECTED + ProviderNotInstalled().user_message
        )

        # The page's own announcer is the live region the trigger feeds.
        announcer = required(outline(page(run).text), "announcer")
        assert announcer.attrs.get("aria-live") == "polite"


def test_result_says_success_or_failure_in_text(tmp_path: Path) -> None:
    """Not colour alone: a class tells them apart for styling, and the words do for people."""
    with running(tmp_path) as run:
        good = result_element(post_test(run, FAKE_KIND, FAKE_CONNECTED))
        bad = result_element(post_test(run, FAKE_KIND, fake_key("fake-auth-rejected")))
    assert good.attrs.get("class") != bad.attrs.get("class")
    assert good.text.startswith("Connected")
    assert "rejected" in bad.text


def test_provider_form_offers_test_button_with_testing_state(tmp_path: Path) -> None:
    with running(tmp_path) as run:
        elements = outline(page(run).text)
    form = required(elements, "provider-form")
    buttons = [
        e for e in elements if e.tag == "button" and e.within(form) and e.text == "Test connection"
    ]
    button = only(buttons, "Test connection button")
    assert (button.attrs.get("type") or "submit") == "submit"
    target = button.attrs.get("hx-post") or button.attrs.get("formaction")
    assert target == TEST_PATH, button.attrs

    def effective(name: str) -> str | None:
        value = button.attrs.get(name)
        return value if value is not None else form.attrs.get(name)

    # Every form button is disabled while the test runs (a plain selector, ADR-062).
    assert effective("hx-disabled-elt") == "#provider-form button"
    # A test saves first, so both the saving and the testing notes show (M1.18 review).
    targets = [t.strip() for t in str(effective("hx-indicator") or "").split(",")]
    assert targets == ["#provider-saving", "#provider-testing"], targets
    indicator = required(elements, targets[-1].lstrip("#"))
    assert indicator.text == TESTING_NOTE, indicator.text
    assert "button-primary" in (button.attrs.get("class") or "").split(), "the main action"


@pytest.mark.parametrize(
    ("kind", "credential", "first"),
    [
        pytest.param(
            TOKEN_KIND, FICTIONAL_TOKEN, "Saved. Your token is in your system keychain.", id="token"
        ),
        pytest.param(
            FAKE_KIND,
            "",
            "Saved. The app will use the fake provider, for testing only.",
            id="fake",
        ),
    ],
)
def test_save_announcement_says_next_test_the_connection(
    tmp_path: Path, kind: str, credential: str, first: str
) -> None:
    with running(tmp_path) as run:
        response = submit(run, kind, credential, htmx=True)
    assert response.status_code == 200, response.text[:300]
    assert announced(response) == first + NEXT_STEP_ANNOUNCE


def test_environment_save_announcement_says_next_test_the_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(API_KEY_VARIABLE, ENV_API_KEY)
    with running(tmp_path) as run:
        response = submit(run, API_KEY_KIND, "", htmx=True)
    assert announced(response) == (
        f"Saved. The app will use the {API_KEY_VARIABLE} environment variable." + NEXT_STEP_ANNOUNCE
    )


def test_removing_a_key_does_not_ask_for_a_test(tmp_path: Path) -> None:
    with running(tmp_path) as run:
        submit(run, TOKEN_KIND, FICTIONAL_TOKEN)
        removed = submit(run, TOKEN_KIND, remove=TOKEN_KIND, confirm=TOKEN_KIND, htmx=True)
    assert announced(removed) == "The saved token was removed."


def test_existing_login_test_uses_wiring_provider_for_workspace(
    tmp_path: Path, bundled_cli: Path
) -> None:
    query = replaying(success_frames({"ok": True}, model=REPORTED_MODEL))
    factory = recording(query=query)
    with running(tmp_path, wiring=factory) as run:  # type: ignore[arg-type]
        response = post_test(run, EXISTING_KIND, "")
        assert assert_connection_answer(response).text.startswith("Connected")
        assert factory.built == [(EXISTING_KIND, run.app.state.workspace.data_dir)]
        assert query.last.options.model == CLAUDE_FAST_DEFAULT
        assert stored(run).saved_sign_ins == (), "nothing pasted, nothing kept"


# --- M1.18 review: the cached result follows the settings -------------------------------------


@pytest.mark.parametrize("change", ["save", "remove", "gone_key"])
def test_connected_is_forgotten_when_the_settings_change(
    tmp_path: Path, memory_keyring: MemoryKeyring, bundled_cli: Path, change: str
) -> None:
    """A "Connected" line is only shown for what was tested: a later save, a removal, or a
    refused save that forgot a key gone from the keychain clears it. The step stays done
    (ADR-052), so Continue and a neutral "done" line remain."""
    query = replaying(success_frames({"ok": True}, model=REPORTED_MODEL))
    factory = recording(query=query)
    with running(tmp_path, wiring=factory) as run:  # type: ignore[arg-type]
        assert submit(run, TOKEN_KIND, FICTIONAL_TOKEN, htmx=True).status_code == 200
        assert post_test(run, TOKEN_KIND, "").status_code == 200
        assert required(outline(page(run).text), "connection-result").text.startswith("Connected")

        if change == "save":
            assert submit(run, EXISTING_KIND, "", htmx=True).status_code == 200
        elif change == "remove":
            assert (
                submit(
                    run, TOKEN_KIND, remove=TOKEN_KIND, confirm=TOKEN_KIND, htmx=True
                ).status_code
                == 200
            )
        else:
            memory_keyring.items.clear()  # removed outside the app
            assert_credential_error(submit(run, TOKEN_KIND, ""))
            assert stored(run).saved_sign_ins == ()

        for shown in (page(run), run.client.get(PROVIDER, headers=run.htmx())):
            elements = outline(shown.text)
            assert by_id(elements, "connection-result") is None, change
            done = required(elements, "connection-done")
            assert done.text == "This step is done. You can test again at any time."
            assert [a.attrs.get("href") for a in continue_links(elements)] == [PREFERENCES]
        assert live_statuses(run).get("provider") == "done", "marks stay (ADR-052)"


def test_refused_save_that_changes_nothing_keeps_connected(tmp_path: Path) -> None:
    with running(tmp_path) as run:
        assert post_test(run, FAKE_KIND, FAKE_CONNECTED).status_code == 200
        assert_credential_error(submit(run, TOKEN_KIND, "FICTIONAL-short"))
        assert required(outline(page(run).text), "connection-result").text.startswith("Connected")


def test_save_after_done_suggests_testing_again(tmp_path: Path) -> None:
    with running(tmp_path) as run:
        assert post_test(run, FAKE_KIND, FAKE_CONNECTED).status_code == 200
        response = submit(run, FAKE_KIND, "", htmx=True)
    assert announced(response) == (
        "Saved. The app will use the fake provider, for testing only. "
        "You can test the connection again to check it."
    )


def test_done_step_from_an_earlier_run_offers_continue(tmp_path: Path) -> None:
    seed_workspace(data_folder(tmp_path), done=("welcome", "provider"))
    with running(tmp_path, provider=AIProviderSettings(kind=FAKE_KIND)) as run:
        elements = outline(page(run).text)
    assert by_id(elements, "connection-result") is None
    assert required(elements, "connection-done").text.startswith("This step is done.")
    assert [a.attrs.get("href") for a in continue_links(elements)] == [PREFERENCES]


# --- M1.18 review: failures worded for the sign-in route ----------------------------------------


@pytest.mark.parametrize(
    ("kind", "credential", "frames", "message"),
    [
        pytest.param(
            TOKEN_KIND,
            FICTIONAL_TOKEN,
            "auth_failed",
            "The token was rejected or has expired. Run claude setup-token again and paste "
            "the new token.",
            id="token_auth",
        ),
        pytest.param(
            API_KEY_KIND,
            PASTED_API_KEY,
            "auth_failed",
            "The API key was rejected. Create a new key in Claude Console and paste it.",
            id="api_key_auth",
        ),
        pytest.param(
            EXISTING_KIND,
            "",
            "auth_failed",
            "The Claude Code sign-in on this computer didn't work. Sign in to Claude Code "
            "again, then test again.",
            id="existing_auth",
        ),
        pytest.param(
            TOKEN_KIND,
            FICTIONAL_TOKEN,
            "rate_limit",
            UsageLimitReached().user_message,
            id="token_usage_limit",
        ),
        pytest.param(
            API_KEY_KIND,
            PASTED_API_KEY,
            "rate_limit",
            "Your API usage limit or credit was reached. Check your Claude Console account, "
            "then try again.",
            id="api_key_usage_limit",
        ),
        pytest.param(
            EXISTING_KIND,
            "",
            "connection",
            NetworkUnavailable().user_message,
            id="network_keeps_domain_text",
        ),
    ],
)
def test_claude_failures_are_worded_for_the_route(
    tmp_path: Path,
    memory_keyring: MemoryKeyring,
    bundled_cli: Path,
    kind: str,
    credential: str,
    frames: str,
    message: str,
) -> None:
    query = replaying(load_frames(frames))
    factory = recording(query=query)
    with running(tmp_path, wiring=factory) as run:  # type: ignore[arg-type]
        response = post_test(run, kind, credential)
        result = assert_connection_answer(response)
        assert result.text == NOT_CONNECTED + message
        assert announced(response) == result.text
        assert live_statuses(run).get("provider") != "done"
    if credential:
        assert_not_in_responses(credential, [response])


def test_claude_not_installed_is_worded_for_claude_routes(
    tmp_path: Path, memory_keyring: MemoryKeyring, bundled_cli: Path
) -> None:
    query = replaying([], raise_exc=CLINotFoundError("Fictional: no CLI"))
    factory = recording(query=query)
    with running(tmp_path, wiring=factory) as run:  # type: ignore[arg-type]
        response = post_test(run, EXISTING_KIND, "")
    assert assert_connection_answer(response).text == (
        NOT_CONNECTED
        + "Claude Code wasn't found. Reinstall the app's dependencies (uv sync), then test again."
    )
