"""Settings > Providers: change the provider, replace the key, test the connection (M1.21;
AC-M1.3, AC-M1.4; ADR-059, ADR-062, ADR-063, ADR-066).

``/settings`` gains a Providers section (``<section id="providers">`` labelled by an ``<h2>``
"Providers") that reuses the provider step's service (``ProviderSetupService``) and its
fragment: the same kind radios, password field, ``#credential-status`` line, Save and
"Test connection" buttons and "Remove saved ..." buttons, inside ``<form id="provider-form">``.
The form posts to the Settings routes, never to the guide's:

- ``POST /settings/providers`` (launch token and origin; not page-tagged). Fields as on the
  provider step: ``kind``, ``credential``, ``action=save`` (default) or ``remove=<kind>``. A
  valid save answers 303 to ``/settings``, or for htmx 200 with the ``#providers`` section and
  an ``announce`` trigger (no ``HX-Push-Url``). A refused save answers 422 with the section and
  the field error; the credential is never shown back.
- ``POST /settings/providers/test`` (launch token and origin; POST only, GET and HEAD 405)
  saves like Save, then tests the saved configuration (ADR-063). A result, connected or not,
  answers 200: htmx gets the ``#providers`` section with ``#connection-result`` focused and an
  ``announce`` trigger carrying its text; a plain request gets the full Settings page.

Settings is not the guide: no step list, no Continue link, and nothing in the guide's state
changes (all steps stay done; the decision stays "app" unless provider health says otherwise).
The credential status comes from the settings row and the cached provider health: GET reads no
keychain and changes nothing (spec 2.9.12; ADR-044, ADR-059).

Providers are the app-selectable fake, or the Claude provider over the SDK's real client with
the replaying transport (``tests/fakes/claude_query.py``), so nothing starts a CLI or uses the
network. Every folder lives under ``tmp_path``; every value is fictional and built at run time.
"""

import json
import logging
from pathlib import Path

import pytest
from httpx import Response

from app.domain.settings import AI_PROVIDER, AIProviderSettings
from app.modules.ai import ProviderHealth
from app.modules.ai.providers.fake import FAKE_FAST_MODEL
from app.modules.storage import Database
from app.onboarding.detection import StartupDecision
from app.services.settings import SettingsService
from tests.fakes.claude_query import replaying, success_frames
from tests.fakes.clock import FrozenClock
from tests.fakes.keyring_backends import CountingKeyring, MemoryKeyring
from tests.fakes.sdk_install import install_fake_sdk
from tests.integration.test_setup_provider import (
    EXISTING_KIND,
    FAKE_KIND,
    FICTIONAL_TOKEN,
    SECOND_TOKEN,
    SERVICE,
    TERMS_URL,
    TOKEN_KEY,
    TOKEN_KIND,
    assert_credential_error,
    assert_not_in_files,
    assert_not_in_logs,
    assert_not_in_responses,
    assert_password_field,
    by_id,
    credential_input,
    keychain,
    kind_radios,
    leaked_pieces,
    live_statuses,
    make_wiring,
    only,
    remove_buttons,
    required,
    stored,
)
from tests.integration.test_setup_provider_test import (
    FAILURES,
    FAKE_CONNECTED,
    REPORTED_MODEL,
    RecordingFactory,
    announced,
    fake_key,
    recording,
)
from tests.integration.test_setup_redirect import (
    BASE_URL,
    START,
    Element,
    Running,
    assert_not_redirected,
    assert_see_other,
    outline,
)
from tests.integration.test_setup_reopen_banner import (
    ALL_M1_DONE,
    banner_links,
    banners,
    broken_provider,
    finished_guide,
    started,
)

pytestmark = pytest.mark.integration

SETTINGS = "/settings"
SETTINGS_PROVIDERS = "/settings/providers"
SETTINGS_TEST = "/settings/providers/test"
SECTION_ID = "providers"
SECTION_HEADING = "Providers"
PROVIDER_STEP = "/setup/provider"
TOKEN_STATUS = "A token is saved in your system keychain."  # noqa: S105 - copy, not a secret
NO_TOKEN_STATUS = "No token saved yet."  # noqa: S105 - copy, not a secret
FAKE_STATUS = "The fake provider needs no key."


# --- helpers -------------------------------------------------------------------------------


@pytest.fixture
def bundled_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The SDK looks installed with its bundled CLI (a placeholder that is never run), so a
    Claude kind's provider health is decided by its sign-in alone."""
    return install_fake_sdk(monkeypatch, tmp_path / "sdk", with_cli=True)


def settings_page(run: Running) -> Response:
    response = run.client.get(SETTINGS)
    assert response.status_code == 200, (response.status_code, response.text[:300])
    assert_not_redirected(response)
    return response


def post_settings(
    run: Running,
    kind: str | None = None,
    credential: str | None = None,
    *,
    remove: str | None = None,
    htmx: bool = False,
) -> Response:
    form: dict[str, str] = {}
    if kind is not None:
        form["kind"] = kind
    if credential is not None:
        form["credential"] = credential
    if remove is not None:
        form["remove"] = remove
    headers = run.action(**({"HX-Request": "true"} if htmx else {}))
    return run.client.post(SETTINGS_PROVIDERS, data=form, headers=headers)


def post_settings_test(
    run: Running, kind: str, credential: str = "", *, htmx: bool = True
) -> Response:
    headers = run.action(**({"HX-Request": "true"} if htmx else {}))
    return run.client.post(
        SETTINGS_TEST, data={"kind": kind, "credential": credential}, headers=headers
    )


def section(elements: list[Element]) -> Element:
    """The Providers section, labelled by its ``<h2>``."""
    found = required(elements, SECTION_ID)
    assert found.tag == "section", found.tag
    heading = required(elements, str(found.attrs.get("aria-labelledby")))
    assert heading.tag == "h2", heading.tag
    assert heading.within(found)
    assert heading.text == SECTION_HEADING, heading.text
    return found


def provider_form(elements: list[Element]) -> Element:
    providers = section(elements)
    form = required(elements, "provider-form")
    assert form.tag == "form"
    assert form.within(providers), "the provider form sits in the Providers section"
    return form


def credential_status(response: Response) -> str:
    return required(outline(response.text), "credential-status").text


def assert_not_the_guide(elements: list[Element]) -> None:
    """Settings is not the guide: no step list position and no Continue link."""
    assert [e for e in elements if e.attrs.get("aria-current") == "step"] == []
    continues = [e for e in elements if e.tag == "a" and e.text == "Continue"]
    assert continues == [], [e.attrs.get("href") for e in continues]


def assert_section_fragment(response: Response) -> list[Element]:
    """An htmx answer: the Providers section only (no page chrome), not the guide."""
    for marker in ("<html", "<body", 'id="announcer"'):
        assert marker not in response.text, marker
    assert "hx-push-url" not in response.headers
    elements = outline(response.text)
    provider_form(elements)
    assert_not_the_guide(elements)
    return elements


def assert_saved_and_back_to_settings(response: Response) -> None:
    assert_see_other(response, SETTINGS)


def assert_settings_result(response: Response, *, htmx: bool = True) -> Element:
    """200 with the Providers section and the connection result, never a redirect."""
    assert response.status_code == 200, (response.status_code, response.text[:300])
    assert "location" not in response.headers
    assert "hx-redirect" not in response.headers
    if htmx:
        elements = assert_section_fragment(response)
        focused = [e.attrs.get("id") for e in elements if "data-autofocus" in e.attrs]
        assert focused == ["connection-result"], focused
    else:
        assert "<html" in response.text
        assert 'id="announcer"' in response.text
        elements = outline(response.text)
        provider_form(elements)
        assert_not_the_guide(elements)
    result = required(elements, "connection-result")
    assert result.within(section(elements))
    return result


def settings_version(run: Running) -> int:
    db: Database = run.app.state.workspace.db
    return SettingsService(db, FrozenClock(START)).load(AI_PROVIDER).version


def data_files(tmp_path: Path) -> list[Path]:
    """Every file under the temporary folders except the log files."""
    return sorted(
        path for path in tmp_path.rglob("*") if path.is_file() and "logs" not in path.parts
    )


# --- the page ------------------------------------------------------------------------------


def test_settings_providers_page_shows_provider_and_saved_state(
    tmp_path: Path, memory_keyring: MemoryKeyring, bundled_cli: Path
) -> None:
    """The section lists the offered routes with the saved one checked, the reused password
    field, the credential status from the settings row and a Remove button per saved key; its
    form posts to the Settings routes."""
    fake_dir = tmp_path / "fake"
    finished_guide(fake_dir)
    with started(fake_dir) as run:
        choices = run.app.state.wiring.provider_choices()
        response = settings_page(run)
        fragment = run.client.get(SETTINGS, headers=run.htmx())
        assert fragment.status_code == 200, fragment.text[:300]

    elements = outline(response.text)
    assert [e.text for e in elements if e.tag == "h1"] == ["Settings"], "one <h1> per page"
    form = provider_form(elements)
    assert form.attrs.get("action") == SETTINGS_PROVIDERS, form.attrs
    assert form.attrs.get("hx-post") == SETTINGS_PROVIDERS, form.attrs
    assert form.attrs.get("hx-disabled-elt") == "#provider-form button"

    radios = kind_radios(elements)
    assert list(radios) == [choice.kind for choice in choices], "one radio per offered choice"
    assert "checked" in radios[FAKE_KIND].attrs, "the saved provider is chosen"
    assert_password_field(elements)
    assert credential_status(response) == FAKE_STATUS

    tests = [
        e for e in elements if e.tag == "button" and e.within(form) and e.text == "Test connection"
    ]
    button = only(tests, "Test connection button")
    assert button.attrs.get("hx-post") == SETTINGS_TEST, button.attrs
    assert button.attrs.get("formaction") == SETTINGS_TEST, button.attrs
    terms = [e for e in elements if e.tag == "a" and e.attrs.get("href") == TERMS_URL]
    assert terms, "the terms note links to the Agent SDK overview"
    for e in elements:  # nothing in the section posts to the guide
        if e.within(section(elements)):
            for name in ("action", "hx-post", "formaction"):
                assert not str(e.attrs.get(name) or "").startswith("/setup"), (e.tag, e.attrs)
    assert_not_the_guide(elements)
    assert banners(response.text) == [], "a healthy provider: no banner"

    # htmx: the page's main content, with the same section.
    for marker in ("<html", "<body"):
        assert marker not in fragment.text, marker
    provider_form(outline(fragment.text))

    # A saved token: said in words with no characters, removable, still nothing echoed.
    token_dir = tmp_path / "token"
    finished_guide(
        token_dir, provider=AIProviderSettings(kind=TOKEN_KIND, saved_sign_ins=(TOKEN_KIND,))
    )
    memory_keyring.items[(SERVICE, TOKEN_KEY)] = FICTIONAL_TOKEN
    with started(token_dir) as run:
        shown = settings_page(run)
    elements = outline(shown.text)
    assert "checked" in kind_radios(elements)[TOKEN_KIND].attrs
    assert credential_status(shown) == TOKEN_STATUS
    removes = remove_buttons(elements)
    assert {kind: b.text for kind, b in removes.items()} == {TOKEN_KIND: "Remove saved token"}
    assert not leaked_pieces(FICTIONAL_TOKEN, shown.text), "no characters of the key"
    assert "sk-ant" not in shown.text

    # A broken provider (token route, nothing saved): the banner and the status say so.
    broken_dir = tmp_path / "broken"
    finished_guide(broken_dir, provider=broken_provider())
    with started(broken_dir) as run:
        assert run.app.state.provider_health == ProviderHealth("missing_sign_in")
        shown = settings_page(run)
    assert banner_links(shown.text) == [PROVIDER_STEP]
    assert credential_status(shown) == NO_TOKEN_STATUS
    assert "checked" in kind_radios(outline(shown.text))[TOKEN_KIND].attrs


# --- change the provider -------------------------------------------------------------------


def test_change_provider_saves_setting_and_rebuilds_provider(
    tmp_path: Path, memory_keyring: MemoryKeyring, bundled_cli: Path
) -> None:
    """Changing the route saves the setting, refreshes the cached health and the decision,
    forgets an earlier "Connected", and the next test builds the newly chosen provider."""
    finished_guide(tmp_path)
    query = replaying(success_frames({"ok": True}, model=REPORTED_MODEL))
    factory = recording(query=query)
    with started(tmp_path, wiring=factory) as run:  # type: ignore[arg-type]
        assert run.decision == StartupDecision("app")
        workspace = run.app.state.workspace.data_dir
        before = settings_version(run)

        # Without the launch token or with a foreign origin nothing changes.
        form = {"kind": TOKEN_KIND, "credential": FICTIONAL_TOKEN}
        no_token = run.client.post(SETTINGS_PROVIDERS, data=form, headers={"origin": BASE_URL})
        bad_origin = run.client.post(
            SETTINGS_PROVIDERS, data=form, headers=run.action(origin="http://evil.example")
        )
        assert (no_token.status_code, bad_origin.status_code) == (403, 403)
        assert settings_version(run) == before
        assert keychain(memory_keyring) == {}

        # The fake, tested: "Connected" is shown.
        connected = post_settings_test(run, FAKE_KIND, FAKE_CONNECTED)
        assert assert_settings_result(connected).text.startswith("Connected")
        assert factory.fakes == 1 and factory.built == []

        # Change to the token route with a pasted token (plain request).
        saved = post_settings(run, TOKEN_KIND, FICTIONAL_TOKEN)
        assert_saved_and_back_to_settings(saved)
        settings = stored(run)
        assert (settings.kind, settings.saved_sign_ins) == (TOKEN_KIND, (TOKEN_KIND,))
        assert keychain(memory_keyring) == {TOKEN_KEY: FICTIONAL_TOKEN}
        assert run.app.state.provider_health == ProviderHealth("ok"), "health refreshed"
        assert run.decision == StartupDecision("app")
        assert live_statuses(run) == ALL_M1_DONE, "the guide's state is untouched"

        shown = settings_page(run)
        elements = outline(shown.text)
        assert "checked" in kind_radios(elements)[TOKEN_KIND].attrs
        assert credential_status(shown) == TOKEN_STATUS
        assert by_id(elements, "connection-result") is None, "the tested provider changed"

        # The next test builds the new provider, for the workspace, with the saved token.
        tested = post_settings_test(run, TOKEN_KIND, "")
        assert assert_settings_result(tested).text.startswith("Connected")
        assert REPORTED_MODEL in tested.text
        assert factory.built == [(TOKEN_KIND, workspace)]
        assert query.last.env.get("CLAUDE_CODE_OAUTH_TOKEN") == FICTIONAL_TOKEN

        # Change again, by htmx, to the existing sign-in: the token is kept for later.
        switched = post_settings(run, EXISTING_KIND, "", htmx=True)
        assert switched.status_code == 200, switched.text[:300]
        assert_section_fragment(switched)
        assert announced(switched).startswith("Saved."), announced(switched)
        settings = stored(run)
        assert (settings.kind, settings.saved_sign_ins) == (EXISTING_KIND, (TOKEN_KIND,))
        assert "checked" in kind_radios(outline(switched.text))[EXISTING_KIND].attrs
        assert by_id(outline(switched.text), "connection-result") is None

        again = post_settings_test(run, EXISTING_KIND, "")
        assert assert_settings_result(again).text.startswith("Connected")
        assert factory.built == [(TOKEN_KIND, workspace), (EXISTING_KIND, workspace)]

        # A kind not on offer is refused and changes nothing.
        refused = post_settings(run, "not-a-provider", "")
        assert refused.status_code == 422, refused.text[:300]
        assert required(outline(refused.text), "kind-error").text
        assert stored(run).kind == EXISTING_KIND
        assert len(factory.built) == 2
        assert live_statuses(run) == ALL_M1_DONE
    assert_not_in_responses(FICTIONAL_TOKEN, [saved, shown, tested, switched, again, refused])

    # Persisted: a restart reads the new choice.
    with Database.open(tmp_path / "data" / "app.db") as db:
        reloaded = SettingsService(db, FrozenClock(START)).load(AI_PROVIDER).value
    assert (reloaded.kind, reloaded.saved_sign_ins) == (EXISTING_KIND, (TOKEN_KIND,))


# --- replace the key -----------------------------------------------------------------------


def test_replace_key_overwrites_secret_never_echoed(
    tmp_path: Path,
    memory_keyring: MemoryKeyring,
    caplog: pytest.LogCaptureFixture,
    bundled_cli: Path,
) -> None:
    """A finished guide whose token is missing (banner): pasting a token in Settings saves it
    and clears the banner; pasting another replaces it. Neither value is ever shown back,
    logged or written outside the keychain (AC-M1.4)."""
    finished_guide(tmp_path, provider=broken_provider())
    responses: list[Response] = []
    with caplog.at_level(logging.DEBUG), started(tmp_path) as run:
        assert run.decision.kind == "app_with_banner"
        responses.append(settings_page(run))

        first = post_settings(run, TOKEN_KIND, f"  {FICTIONAL_TOKEN}\n")  # stripped
        assert_saved_and_back_to_settings(first)
        responses.append(first)
        assert keychain(memory_keyring) == {TOKEN_KEY: FICTIONAL_TOKEN}
        assert stored(run).saved_sign_ins == (TOKEN_KIND,)
        assert run.app.state.provider_health == ProviderHealth("ok")
        assert run.decision == StartupDecision("app"), "the banner's cause is fixed"
        shown = settings_page(run)
        responses.append(shown)
        assert banners(shown.text) == []
        assert credential_status(shown) == TOKEN_STATUS

        replaced = post_settings(run, TOKEN_KIND, SECOND_TOKEN, htmx=True)
        assert replaced.status_code == 200, replaced.text[:300]
        assert_section_fragment(replaced)
        assert announced(replaced).startswith("Saved. Your token is in your system keychain.")
        responses.append(replaced)
        assert keychain(memory_keyring) == {TOKEN_KEY: SECOND_TOKEN}, "overwritten, not added"
        assert stored(run).saved_sign_ins == (TOKEN_KIND,)
        assert "value" not in credential_input(outline(replaced.text)).attrs

        # A refused replacement keeps the saved key and is never echoed.
        short = "FICTIONAL-short"
        refused = post_settings(run, TOKEN_KIND, short, htmx=True)
        assert_credential_error(refused)
        assert_section_fragment(refused)
        assert short not in refused.text
        responses.append(refused)
        assert keychain(memory_keyring) == {TOKEN_KEY: SECOND_TOKEN}

        for path in (SETTINGS, "/", PROVIDER_STEP):
            responses.append(run.client.get(path))
            responses.append(run.client.get(path, headers=run.htmx()))
        responses.append(run.client.get("/health"))
        assert live_statuses(run) == ALL_M1_DONE

    for value in (FICTIONAL_TOKEN, SECOND_TOKEN):
        assert_not_in_responses(value, responses)
        assert_not_in_logs(value, caplog)
        assert_not_in_files(value, tmp_path / "data", tmp_path / "cfg")
    assert_not_in_logs(short, caplog)


# --- test the connection -------------------------------------------------------------------


@pytest.mark.parametrize("outcome", [*sorted(FAILURES), "connected"])
def test_settings_test_connection_messages(
    tmp_path: Path, memory_keyring: MemoryKeyring, outcome: str
) -> None:
    """AC-M1.3 from Settings: the fake's outcomes show the same mapped messages as the
    provider step, focused and announced; the guide's state never changes."""
    if outcome == "connected":
        key, expected = FAKE_CONNECTED, None
    else:
        keyword, expected = FAILURES[outcome]
        key = fake_key(keyword)
    finished_guide(tmp_path)
    with started(tmp_path) as run:
        response = post_settings_test(run, FAKE_KIND, key)
        result = assert_settings_result(response)
        if expected is None:
            assert result.text.startswith("Connected"), result.text
            assert FAKE_FAST_MODEL in result.text, "the model the provider reported"
        else:
            assert result.text == expected
        assert announced(response) == result.text
        assert "hx-trigger" in response.headers
        assert set(json.loads(response.headers["hx-trigger"])) == {"announce"}

        plain = post_settings_test(run, FAKE_KIND, key, htmx=False)
        assert assert_settings_result(plain, htmx=False).text == result.text

        assert stored(run).kind == FAKE_KIND
        assert live_statuses(run) == ALL_M1_DONE, "a test from Settings never touches the guide"
        assert run.decision == StartupDecision("app")
        responses = [response, plain, settings_page(run)]
    assert keychain(memory_keyring) == {}
    assert_not_in_responses(key, responses, pieces=False)


def test_settings_test_route_is_post_only_with_token_and_origin(tmp_path: Path) -> None:
    finished_guide(tmp_path)
    factory = recording()
    with started(tmp_path, wiring=factory) as run:  # type: ignore[arg-type]
        query = {"kind": FAKE_KIND, "credential": FAKE_CONNECTED}
        for headers in (run.with_token(), run.htmx(), run.action()):
            assert run.client.get(SETTINGS_TEST, params=query, headers=headers).status_code == 405
            assert run.client.head(SETTINGS_TEST, params=query, headers=headers).status_code == 405
        form = {"kind": FAKE_KIND, "credential": FAKE_CONNECTED}
        no_token = run.client.post(SETTINGS_TEST, data=form, headers={"origin": BASE_URL})
        no_origin = run.client.post(SETTINGS_TEST, data=form, headers=run.with_token())
        assert (no_token.status_code, no_origin.status_code) == (403, 403)
        assert factory.calls == 0, "no test ran"


# --- GET has no side effects ---------------------------------------------------------------


def test_settings_get_has_no_side_effects(tmp_path: Path, bundled_cli: Path) -> None:
    """GET and HEAD of ``/settings`` (also with a forged query) read no keychain, build no
    provider, save nothing and recompute nothing: the status comes from the settings row and
    the cached health (spec 2.9.12)."""
    backend = CountingKeyring()
    backend.items[(SERVICE, TOKEN_KEY)] = FICTIONAL_TOKEN
    finished_guide(
        tmp_path, provider=AIProviderSettings(kind=TOKEN_KIND, saved_sign_ins=(TOKEN_KIND,))
    )
    factory = RecordingFactory(make_wiring(backend=backend))
    with started(tmp_path, wiring=factory) as run:  # type: ignore[arg-type]
        health = run.app.state.provider_health
        decision = run.decision
        version = settings_version(run)
        settings = stored(run)
        files = data_files(tmp_path)
        calls = backend.calls
        assert health == ProviderHealth("ok")

        forged = {"kind": EXISTING_KIND, "credential": SECOND_TOKEN, "remove": TOKEN_KIND}
        for _ in range(2):
            shown = settings_page(run)
            assert credential_status(shown) == TOKEN_STATUS
            assert run.client.get(SETTINGS, headers=run.htmx()).status_code == 200
            assert run.client.head(SETTINGS).status_code == 200
            assert run.client.get(SETTINGS, params=forged).status_code == 200
            run.client.get(SETTINGS_PROVIDERS, params=forged)
            run.client.get(SETTINGS_PROVIDERS, params=forged, headers=run.htmx())
            run.client.get(SETTINGS_TEST, params=forged, headers=run.htmx())

        assert backend.calls == calls, "GET and HEAD never touch the keychain"
        assert backend.items == {(SERVICE, TOKEN_KEY): FICTIONAL_TOKEN}
        assert factory.calls == 0, "no provider built, no test run"
        assert settings_version(run) == version
        assert stored(run) == settings
        assert run.app.state.provider_health is health, "the cached health is reused"
        assert run.decision == decision
        assert live_statuses(run) == ALL_M1_DONE
        assert data_files(tmp_path) == files, "nothing created"
