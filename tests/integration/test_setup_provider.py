"""Step 2a of the setup guide: choose a provider, save the credential, environment fallback
(M1.17; AC-M1.4, AC-M1.5, AC-M1.6).

The provider step lists the sign-in routes wiring offers (``provider_choices()``), each with its
instructions, a terms note linking to the Agent SDK overview, a password field for a token or
API key and the credential status. A valid save stores the credential in the ``SecretStore``
(the keychain), records only which kinds have a saved key (``saved_sign_ins``) in the AI
provider setting and refreshes provider health; switching kind keeps the other kinds' saved
keys, and each is removed with its own button (user decision, 2026-10-05); the step stays on the
provider page and is not done until "Connected" (M1.18, ADR-052). The credential is never
echoed, shown, logged or written to the database (AC-M1.4). Without a keychain the step explains
the environment fallback (``CLAUDE_CODE_OAUTH_TOKEN`` or ``ANTHROPIC_API_KEY``), and a
credential from the environment is accepted with an empty field (AC-M1.5). A forged ``fake``
choice is refused while fakes are off, and a key pasted with the fake selected is never stored,
echoed or logged (ADR-054, ADR-059).

The keychain is the autouse in-memory keyring (``memory_keyring``), or an explicit test
backend given to ``build_wiring``. The Claude SDK's bundled CLI is a placeholder under
``tmp_path`` that is never run. Every folder lives under ``tmp_path`` and every value is
fictional: credential-shaped values are built at run time and contain ``FICTIONAL``.
"""

import json
import logging
from collections import Counter
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from httpx import Response

from app import __version__
from app.config import RuntimeConfig, write_bootstrap
from app.domain.redaction import Redactor
from app.domain.settings import AI_PROVIDER, AIProviderSettings, TierMap
from app.main import create_app
from app.modules.ai import ProviderHealth
from app.modules.secrets.providers.keyring_store import KeyringBackend
from app.modules.storage import Database
from app.onboarding.detection import StartupDecision
from app.onboarding.progress import SetupProgress
from app.services.settings import SettingsService
from app.services.setup_state import SetupStateService
from app.wiring import Wiring, build_wiring
from tests.conftest import ALLOW_FAKES_VARIABLE
from tests.fakes.clock import FrozenClock
from tests.fakes.keyring_backends import (
    CountingKeyring,
    FailingKeyring,
    LockedKeyring,
    MemoryKeyring,
)
from tests.fakes.sdk_install import install_fake_sdk
from tests.integration.test_setup_redirect import (
    BASE_URL,
    PORT,
    START,
    Element,
    Running,
    assert_see_other,
    outline,
)
from tests.setup_harness import seed_workspace

pytestmark = pytest.mark.integration

PROVIDER = "/setup/provider"
SERVICE = "jobpipeline"
DEMO_SERVICE = "jobpipeline-demo"
TOKEN_KEY = "claude_code_oauth_token"  # noqa: S105 - a key name, not a secret
API_KEY_KEY = "anthropic_api_key"
TOKEN_VARIABLE = "CLAUDE_CODE_OAUTH_TOKEN"  # noqa: S105 - a variable name, not a secret
API_KEY_VARIABLE = "ANTHROPIC_API_KEY"
TOKEN_KIND = "claude_subscription_token"  # noqa: S105 - a provider kind, not a secret
API_KEY_KIND = "claude_api_key"
EXISTING_KIND = "claude_existing_login"
FAKE_KIND = "fake"
TERMS_URL = "https://docs.claude.com/en/docs/agent-sdk/overview"
LEGEND = "How do you want to connect to Claude?"
FAKE_LABEL = "Fake (testing only)"


def anthropic_shaped(kind: str, body: str) -> str:
    """An Anthropic-shaped fictional credential (``sk-ant-<kind>-<body>``), built at run time
    so no literal in the repository looks like a real one."""
    return "-".join(["sk", "ant", kind, body])


# Distinctive bodies, so any 6-character piece of them showing up in a page is a leak.
FICTIONAL_TOKEN = anthropic_shaped("oat01", "FICTIONALqz7Wv3Kp9Xr2Lm5Jd8Tb4Nc6Hy")
FICTIONAL_API_KEY = anthropic_shaped("api03", "FICTIONAL" + "apikey" + "qz" * 12)
SECOND_TOKEN = anthropic_shaped("oat01", "FICTIONALscndXqWq3Zx8Kv2Pn6Rt9Lb4")
ENV_TOKEN = "FictionalEnvToken-0101-FICTIONAL-notreal"  # noqa: S105 - fictional
ENV_API_KEY = "FictionalEnvApiKey-0102-FICTIONAL-notreal"
FAKE_PASTED = "fake-connected-FICTIONALpastedVr8Qm2Xw5Lz"


# --- helpers -------------------------------------------------------------------------------


@pytest.fixture
def bundled_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The SDK looks installed with its bundled CLI (a placeholder that is never run)."""
    return install_fake_sdk(monkeypatch, tmp_path / "sdk", with_cli=True)


def make_wiring(
    environ: dict[str, str] | None = None,
    *,
    backend: KeyringBackend | None = None,
    demo: bool = False,
) -> Wiring:
    """A wiring from an explicit environment (fakes allowed unless ``environ`` says otherwise)
    and an explicit keyring backend; ``None`` uses the autouse in-memory keyring."""
    return build_wiring(
        environ={ALLOW_FAKES_VARIABLE: "1"} if environ is None else environ,
        demo=demo,
        redactor=Redactor(),
        keyring_backend=backend,
    )


def data_folder(tmp_path: Path) -> Path:
    return tmp_path / "data"


@contextmanager
def running(
    tmp_path: Path,
    *,
    wiring: Wiring | None = None,
    demo: bool = False,
    welcome_done: bool = True,
    provider: AIProviderSettings | None = None,
) -> Iterator[Running]:
    """The app at the provider step: ``config.toml`` names a data folder whose database has
    step 1 done (``welcome_done=False``: a first run, nothing created). ``wiring`` defaults to
    the one ``create_app`` builds from the process environment."""
    data_dir = data_folder(tmp_path)
    if welcome_done:
        if not demo:
            write_bootstrap(tmp_path / "cfg", data_dir=data_dir, version=__version__)
        seed_workspace(data_dir, done=("welcome",), clock=FrozenClock(START), provider=provider)
    source = "demo" if demo else ("bootstrap" if welcome_done else "default")
    config = RuntimeConfig(
        data_dir=data_dir,
        config_dir=tmp_path / "cfg",
        port=PORT,
        log_level="INFO",
        demo=demo,
        setup=False,
        no_browser=False,
        data_dir_source=source,  # type: ignore[arg-type]
    )
    # A demo starts with no content, as delete all data leaves it (M9.5 part C seeds a new demo
    # folder; these setup tests predate it; ADR-133).
    app = create_app(config, clock=FrozenClock(START), wiring=wiring, demo_dataset=None)
    with TestClient(app, base_url=BASE_URL, follow_redirects=False) as client:
        yield Running(app=app, client=client, hits=Counter())


def submit(
    run: Running,
    kind: str | None = None,
    credential: str | None = None,
    *,
    action: str | None = None,
    remove: str | None = None,
    confirm: str | None = None,
    htmx: bool = False,
) -> Response:
    form: dict[str, str] = {}
    if kind is not None:
        form["kind"] = kind
    if credential is not None:
        form["credential"] = credential
    if action is not None:
        form["action"] = action
    if remove is not None:
        form["remove"] = remove
    if confirm is not None:  # the "Remove" answer to the question (M1.21 follow-up)
        form["confirm"] = confirm
    headers = run.action(**({"HX-Request": "true"} if htmx else {}))
    return run.client.post(PROVIDER, data=form, headers=headers)


def page(run: Running) -> Response:
    response = run.client.get(PROVIDER)
    assert response.status_code == 200, (response.status_code, response.text[:300])
    return response


def stored(run: Running) -> AIProviderSettings:
    db: Database = run.app.state.workspace.db
    return SettingsService(db, FrozenClock(START)).load(AI_PROVIDER).value


def live_statuses(run: Running) -> dict[str, str]:
    db: Database = run.app.state.workspace.db
    progress = SetupProgress(
        run.app.state.setup_registry, SetupStateService(db, FrozenClock(START))
    )
    return dict(progress.statuses())


def keychain(backend: MemoryKeyring, service: str = SERVICE) -> dict[str, str]:
    return {user: value for (svc, user), value in backend.items.items() if svc == service}


def only(elements: list[Element], description: str) -> Element:
    assert len(elements) == 1, f"expected one {description}, found {len(elements)}"
    return elements[0]


def by_id(elements: list[Element], element_id: str) -> Element | None:
    return next((e for e in elements if e.attrs.get("id") == element_id), None)


def required(elements: list[Element], element_id: str) -> Element:
    element = by_id(elements, element_id)
    assert element is not None, f"#{element_id} is missing"
    return element


def described_by(elements: list[Element], field: Element) -> list[Element]:
    ids = (field.attrs.get("aria-describedby") or "").split()
    return [e for e in (by_id(elements, i) for i in ids) if e is not None]


def kind_radios(elements: list[Element]) -> dict[str, Element]:
    return {
        str(e.attrs.get("value")): e
        for e in elements
        if e.tag == "input" and e.attrs.get("type") == "radio" and e.attrs.get("name") == "kind"
    }


def credential_input(elements: list[Element]) -> Element:
    return only(
        [e for e in elements if e.tag == "input" and e.attrs.get("id") == "credential"],
        'input id="credential"',
    )


def action_buttons(elements: list[Element]) -> dict[str, Element]:
    return {
        str(e.attrs.get("value")): e
        for e in elements
        if e.tag == "button"
        and e.attrs.get("name") == "action"
        and (e.attrs.get("type") or "submit") == "submit"
    }


def remove_buttons(elements: list[Element]) -> dict[str, Element]:
    """The "Remove saved ..." buttons, one per saved key, by the kind they remove."""
    return {
        str(e.attrs.get("value")): e
        for e in elements
        if e.tag == "button"
        and e.attrs.get("name") == "remove"
        and (e.attrs.get("type") or "submit") == "submit"
    }


def status_text(response: Response) -> str:
    return required(outline(response.text), "credential-status").text


def assert_password_field(elements: list[Element]) -> None:
    field = credential_input(elements)
    assert field.attrs.get("type") == "password"
    assert field.attrs.get("name") == "credential"
    # Password managers neither fill nor offer to save it (M1.17 security review).
    assert field.attrs.get("autocomplete") == "new-password"
    for marker in ("data-1p-ignore", "data-bwignore"):
        assert marker in field.attrs, marker
    assert field.attrs.get("data-lpignore") == "true"
    assert field.attrs.get("data-form-type") == "other"
    assert "value" not in field.attrs, "the password field never carries a value"


def assert_credential_error(response: Response, *, mentions: str | None = None) -> str:
    """422 with the credential field marked invalid and described by a visible message;
    returns the message text."""
    assert response.status_code == 422, (response.status_code, response.text[:300])
    elements = outline(response.text)
    field = credential_input(elements)
    assert field.attrs.get("aria-invalid") == "true"
    assert "value" not in field.attrs, "a refused credential is never echoed"
    text = " ".join(e.text for e in described_by(elements, field) if e.text)
    assert text, "the error is tied to the field with aria-describedby"
    if mentions is not None:
        assert mentions in text, text
    return text


def assert_kind_error(response: Response) -> None:
    """422 with the error tied to the provider choice (``#kind-error``, referenced by the
    fieldset's ``aria-describedby``)."""
    assert response.status_code == 422, (response.status_code, response.text[:300])
    elements = outline(response.text)
    error = required(elements, "kind-error")
    assert error.text
    fieldset = only([e for e in elements if e.tag == "fieldset"], "fieldset")
    assert "kind-error" in (fieldset.attrs.get("aria-describedby") or "").split()
    radios = kind_radios(elements)
    for kind, radio in radios.items():
        described = (radio.attrs.get("aria-describedby") or "").split()
        assert described == ["kind-error", f"kind-{kind}-help"], (kind, described)
    focused = [kind for kind, radio in radios.items() if "data-autofocus" in radio.attrs]
    checked = [kind for kind, radio in radios.items() if "checked" in radio.attrs]
    assert focused == (checked or list(radios)[:1]), "focus: the checked radio, else the first"


def leaked_pieces(value: str, text: str, size: int = 6) -> list[str]:
    """Every ``size``-character piece of ``value`` found in ``text``."""
    return sorted({value[i : i + size] for i in range(len(value) - size + 1)} & _pieces(text, size))


def _pieces(text: str, size: int) -> set[str]:
    return {text[i : i + size] for i in range(len(text) - size + 1)}


def assert_not_in_responses(
    value: str, responses: Iterable[Response], *, pieces: bool = True
) -> None:
    """Neither the value nor (with ``pieces``) any 6-character piece of it is in a body or
    header. ``pieces=False`` for values whose words may appear in the page's own copy (the
    fake's outcome keywords)."""
    for response in responses:
        where = f"{response.request.method} {response.request.url.path}"
        assert value not in response.text, where
        if pieces:
            assert not leaked_pieces(value, response.text), (
                where,
                leaked_pieces(value, response.text),
            )
        for name, header in response.headers.items():
            assert value not in header, (where, name)


def assert_not_in_logs(value: str, caplog: pytest.LogCaptureFixture) -> None:
    assert value not in caplog.text
    for record in caplog.records:
        assert value not in record.getMessage()
        assert value not in repr(record.args)


def assert_not_in_files(value: str, *folders: Path) -> None:
    """No file under ``folders`` (the database and its -wal/-shm, config.toml, logs)
    holds the value, in UTF-8 or UTF-16."""
    needles = (value.encode("utf-8"), value.encode("utf-16-le"))
    for folder in folders:
        if not folder.exists():
            continue
        for path in folder.rglob("*"):
            if path.is_file():
                content = path.read_bytes()
                for needle in needles:
                    assert needle not in content, path


def assert_saved_and_stayed(response: Response) -> None:
    """A valid plain submit saves and comes back to the provider step (not done yet)."""
    assert_see_other(response, PROVIDER)


# --- the page ------------------------------------------------------------------------------


def test_provider_step_lists_modes_with_instructions(tmp_path: Path) -> None:
    with running(tmp_path) as run:
        response = page(run)
        choices = run.app.state.wiring.provider_choices()

    elements = outline(response.text)
    heading = required(elements, "step-heading")
    assert heading.tag == "h2"
    assert heading.text == "AI provider"

    fieldset = only([e for e in elements if e.tag == "fieldset"], "fieldset")
    legend = only([e for e in elements if e.tag == "legend"], "legend")
    assert legend.within(fieldset)
    assert legend.text == LEGEND

    radios = kind_radios(elements)
    assert list(radios) == [choice.kind for choice in choices], "one radio per offered choice"
    assert set(radios) == {TOKEN_KIND, EXISTING_KIND, API_KEY_KIND, FAKE_KIND}
    instructions: dict[str, str] = {}
    for choice in choices:
        radio = radios[choice.kind]
        assert radio.within(fieldset)
        assert radio.attrs.get("id") == f"kind-{choice.kind}"
        label = only(
            [
                e
                for e in elements
                if e.tag == "label" and e.attrs.get("for") == f"kind-{choice.kind}"
            ],
            f"label for kind-{choice.kind}",
        )
        assert label.text == choice.label
        described = [e.text for e in described_by(elements, radio) if e.text]
        assert described, f"{choice.kind} has instructions tied with aria-describedby"
        instructions[choice.kind] = " ".join(described)
        assert "checked" not in radio.attrs, "nothing is chosen before the first save"

    assert "claude setup-token" in instructions[TOKEN_KIND]
    assert "paste" in instructions[TOKEN_KIND].lower()
    assert "Claude Console" in instructions[API_KEY_KIND]
    assert "paste" in instructions[API_KEY_KIND].lower()
    assert "sign-in" in instructions[EXISTING_KIND].lower()
    assert "nothing to paste" in instructions[EXISTING_KIND].lower()
    assert "testing" in instructions[FAKE_KIND].lower()

    assert_password_field(elements)
    label = only(
        [e for e in elements if e.tag == "label" and e.attrs.get("for") == "credential"],
        "label for credential",
    )
    assert label.text == "Token or API key"
    hint = " ".join(e.text for e in described_by(elements, credential_input(elements)))
    assert "keychain" in hint.lower(), "the hint says where the credential is kept"

    fallback = required(elements, "env-fallback")
    assert TOKEN_VARIABLE in fallback.text
    assert API_KEY_VARIABLE in fallback.text
    required(elements, "credential-status")

    buttons = action_buttons(elements)
    assert set(buttons) == {"save"}
    assert buttons["save"].text == "Save"
    assert remove_buttons(elements) == {}, "nothing saved yet: no Remove button"


def test_provider_step_shows_terms_note_link(tmp_path: Path) -> None:
    with running(tmp_path) as run:
        response = page(run)

    elements = outline(response.text)
    note = required(elements, "terms-note")
    assert "API key" in note.text
    assert "Agent SDK" in note.text
    assert "unless Anthropic has approved it" in note.text
    fieldset = only([e for e in elements if e.tag == "fieldset"], "fieldset")
    assert elements.index(note) < elements.index(fieldset), "read before choosing"
    links = [e for e in elements if e.tag == "a" and e.within(note)]
    link = only(links, "link in the terms note")
    assert link.attrs.get("href") == TERMS_URL
    rel = (link.attrs.get("rel") or "").split()
    assert "noopener" in rel
    assert "noreferrer" in rel
    assert link.text == "Agent SDK overview"
    assert "target" not in link.attrs, "same tab"


def test_provider_page_htmx_fragment(tmp_path: Path) -> None:
    with running(tmp_path) as run:
        fragment = run.client.get(PROVIDER, headers=run.htmx())
    assert fragment.status_code == 200
    for marker in ("<html", "<body", 'id="announcer"'):
        assert marker not in fragment.text, marker
    elements = outline(fragment.text)
    current = only([e for e in elements if e.attrs.get("aria-current") == "step"], "current")
    assert current.text == "AI provider"
    assert_password_field(elements)


# --- AC-M1.4: the credential is saved in the secret store and never shown -------------------


@pytest.mark.parametrize(
    ("kind", "value", "key"),
    [
        pytest.param(TOKEN_KIND, FICTIONAL_TOKEN, TOKEN_KEY, id="token"),
        pytest.param(API_KEY_KIND, FICTIONAL_API_KEY, API_KEY_KEY, id="api_key"),
    ],
)
def test_ac_m1_4_saving_token_stores_in_secret_store_never_echoes(
    tmp_path: Path,
    memory_keyring: MemoryKeyring,
    caplog: pytest.LogCaptureFixture,
    kind: str,
    value: str,
    key: str,
) -> None:
    responses: list[Response] = []
    with caplog.at_level(logging.DEBUG), running(tmp_path) as run:
        saved = submit(run, kind, f"  {value}\n", action="save")  # stripped before saving
        assert_saved_and_stayed(saved)
        responses.append(saved)

        assert keychain(memory_keyring) == {key: value}
        assert stored(run).kind == kind

        responses.append(submit(run, kind, value, htmx=True))  # saving again: same value
        for path in (PROVIDER, "/setup", "/setup/welcome", "/setup/preferences", "/"):
            responses.append(run.client.get(path))
            responses.append(run.client.get(path, headers=run.htmx()))
        responses.append(run.client.get("/health"))

    assert keychain(memory_keyring) == {key: value}
    assert_not_in_responses(value, responses)
    assert_not_in_logs(value, caplog)
    assert_not_in_files(value, data_folder(tmp_path), tmp_path / "cfg")


def test_saved_credential_shown_without_any_characters(
    tmp_path: Path, memory_keyring: MemoryKeyring
) -> None:
    with running(tmp_path) as run:
        assert_saved_and_stayed(submit(run, TOKEN_KIND, FICTIONAL_TOKEN))
        response = page(run)
        fragment = run.client.get(PROVIDER, headers=run.htmx())

    for shown in (response, fragment):
        elements = outline(shown.text)
        status = required(elements, "credential-status").text
        assert "saved" in status.lower()
        assert "system keychain" in status.lower()
        assert "token" in status.lower()
        assert not leaked_pieces(FICTIONAL_TOKEN, status, size=4), "no characters"
        assert not leaked_pieces(FICTIONAL_TOKEN, shown.text), "nowhere on the page"
        assert "sk-ant" not in shown.text, "no prefix"
        assert str(len(FICTIONAL_TOKEN)) not in status, "no length"
        for mask in ("•", "●", "***", "…"):
            assert mask not in status, f"no masked characters ({mask!r})"
        assert_password_field(elements)
        assert "checked" in kind_radios(elements)[TOKEN_KIND].attrs, "the saved kind is chosen"
        assert set(action_buttons(elements)) == {"save"}
        removes = remove_buttons(elements)
        assert set(removes) == {TOKEN_KIND}, "one Remove button per saved key"
        assert removes[TOKEN_KIND].text == "Remove saved token"
    assert keychain(memory_keyring) == {TOKEN_KEY: FICTIONAL_TOKEN}


def test_provider_get_reads_no_keychain(tmp_path: Path) -> None:
    """The status comes from the settings row and the cached result, never from the keychain
    on GET (spec 2.9.12)."""
    backend = CountingKeyring()
    with running(tmp_path, wiring=make_wiring(backend=backend)) as run:
        assert_saved_and_stayed(submit(run, TOKEN_KIND, FICTIONAL_TOKEN))
        before = backend.calls
        for _ in range(2):
            page(run)
            run.client.get(PROVIDER, headers=run.htmx())
            run.client.head(PROVIDER)
        assert backend.calls == before


# --- AC-M1.5: without a keychain, the environment fallback --------------------------------------


@pytest.mark.parametrize(
    ("kind", "value", "variable"),
    [
        pytest.param(TOKEN_KIND, FICTIONAL_TOKEN, TOKEN_VARIABLE, id="token"),
        pytest.param(API_KEY_KIND, FICTIONAL_API_KEY, API_KEY_VARIABLE, id="api_key"),
    ],
)
def test_ac_m1_5_no_keychain_explains_environment_fallback(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, kind: str, value: str, variable: str
) -> None:
    with (
        caplog.at_level(logging.DEBUG),
        running(tmp_path, wiring=make_wiring(backend=FailingKeyring())) as run,
    ):
        before = stored(run)
        response = submit(run, kind, value)
        message = assert_credential_error(response, mentions=variable)
        assert "keychain" in message.lower()

        assert stored(run) == before, "nothing saved"
        assert before.kind is None
        elements = outline(response.text)
        assert "checked" in kind_radios(elements)[kind].attrs, "the chosen kind is kept"
        fallback = required(elements, "env-fallback")
        assert variable in fallback.text

        # Shown prominently on the page after the failed save (cached; no keychain read).
        after = page(run)
        assert variable in required(outline(after.text), "env-fallback").text
        assert run.decision == StartupDecision("guide", "provider")

        responses = [response, after, run.client.get(PROVIDER, headers=run.htmx())]

    assert_not_in_responses(value, responses)
    assert_not_in_logs(value, caplog)
    assert_not_in_files(value, data_folder(tmp_path), tmp_path / "cfg")


def test_no_keychain_htmx_save_swaps_422(tmp_path: Path) -> None:
    with running(tmp_path, wiring=make_wiring(backend=FailingKeyring())) as run:
        response = submit(run, TOKEN_KIND, FICTIONAL_TOKEN, htmx=True)
    assert_credential_error(response, mentions=TOKEN_VARIABLE)
    assert "hx-push-url" not in response.headers
    assert "<html" not in response.text


def test_keychain_write_failure_explains_fallback_and_logs_no_value(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The keychain answers the probe but refuses the write (locked); the backend's error
    text echoes the value, which must go nowhere."""
    with (
        caplog.at_level(logging.DEBUG),
        running(tmp_path, wiring=make_wiring(backend=LockedKeyring())) as run,
    ):
        response = submit(run, TOKEN_KIND, FICTIONAL_TOKEN)
        assert_credential_error(response, mentions=TOKEN_VARIABLE)
        assert stored(run).kind is None
        assert stored(run).saved_sign_ins == ()
    assert_not_in_responses(FICTIONAL_TOKEN, [response])
    assert_not_in_logs(FICTIONAL_TOKEN, caplog)


@pytest.mark.parametrize(
    ("kind", "variable", "value"),
    [
        pytest.param(TOKEN_KIND, TOKEN_VARIABLE, ENV_TOKEN, id="token"),
        pytest.param(API_KEY_KIND, API_KEY_VARIABLE, ENV_API_KEY, id="api_key"),
    ],
)
def test_ac_m1_5_env_variable_set_marks_credential_available(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    memory_keyring: MemoryKeyring,
    bundled_cli: Path,
    kind: str,
    variable: str,
    value: str,
) -> None:
    monkeypatch.setenv(variable, value)  # read when create_app builds the wiring
    with running(tmp_path) as run:
        assert run.app.state.provider_health == ProviderHealth("not_configured")

        response = submit(run, kind, "")
        assert_saved_and_stayed(response)

        settings = stored(run)
        assert settings.kind == kind
        assert settings.saved_sign_ins == (), "nothing was saved in the keychain"
        assert keychain(memory_keyring) == {}
        assert run.app.state.provider_health == ProviderHealth("ok")

        shown = page(run)
        status = status_text(shown)
        assert variable in status
        assert "environment variable" in status.lower()
        assert remove_buttons(outline(shown.text)) == {}, "nothing to remove"
        responses = [response, shown, run.client.get(PROVIDER, headers=run.htmx())]

    assert_not_in_responses(value, responses)


def test_ac_m1_5_empty_credential_without_saved_or_environment_is_refused(
    tmp_path: Path, memory_keyring: MemoryKeyring
) -> None:
    with running(tmp_path) as run:
        assert_credential_error(submit(run, TOKEN_KIND, ""))
        assert_credential_error(submit(run, API_KEY_KIND, "   "))
        assert_credential_error(submit(run, API_KEY_KIND))
        assert stored(run).kind is None
    assert keychain(memory_keyring) == {}


def test_wiring_records_environment_keys_by_name_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wiring records which credential keys the environment provides, never their values."""
    monkeypatch.setenv(API_KEY_VARIABLE, ENV_API_KEY)
    with running(tmp_path) as run:
        keys = run.app.state.wiring.environment_keys()
    assert keys == frozenset({API_KEY_KEY})
    assert ENV_API_KEY not in repr(keys)


# --- fake provider gating (ADR-054, ADR-059) ----------------------------------------------------


def test_fake_choice_only_with_allow_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    memory_keyring: MemoryKeyring,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Allowed (the autouse fixture sets the flag): the fake is offered, labelled for testing.
    allowed = tmp_path / "allowed"
    with caplog.at_level(logging.DEBUG), running(allowed) as run:
        elements = outline(page(run).text)
        radio = kind_radios(elements)[FAKE_KIND]
        label = only(
            [e for e in elements if e.tag == "label" and e.attrs.get("for") == "kind-fake"],
            "fake label",
        )
        assert label.text == FAKE_LABEL
        assert radio.attrs.get("id") == "kind-fake"

        # A key pasted with the fake selected is never stored, echoed or logged.
        saved = submit(run, FAKE_KIND, FAKE_PASTED)
        assert_saved_and_stayed(saved)
        assert stored(run).kind == FAKE_KIND
        assert stored(run).saved_sign_ins == ()
        assert keychain(memory_keyring) == {}
        responses = [
            saved,
            submit(run, FAKE_KIND, FAKE_PASTED, htmx=True),
            page(run),
            run.client.get(PROVIDER, headers=run.htmx()),
        ]
        refused = submit(run, "not-a-provider", FAKE_PASTED)
        assert_kind_error(refused)
        responses.append(refused)
    assert keychain(memory_keyring) == {}
    assert_not_in_responses(FAKE_PASTED, responses, pieces=False)
    assert_not_in_logs(FAKE_PASTED, caplog)
    assert_not_in_files(FAKE_PASTED, data_folder(allowed), allowed / "cfg")

    # Not allowed: no fake on the page, and a forged fake choice is refused.
    monkeypatch.delenv(ALLOW_FAKES_VARIABLE)
    refused_root = tmp_path / "not-allowed"
    with caplog.at_level(logging.DEBUG), running(refused_root) as run:
        shown = page(run)
        assert FAKE_KIND not in kind_radios(outline(shown.text))
        assert FAKE_LABEL not in shown.text
        assert 'id="kind-fake"' not in shown.text

        forged = submit(run, FAKE_KIND, FAKE_PASTED)
        assert_kind_error(forged)
        assert stored(run).kind is None, "nothing saved"
        assert run.app.state.provider_health == ProviderHealth("not_configured")
        assert FAKE_KIND not in kind_radios(outline(forged.text)), "not offered after a refusal"
    assert keychain(memory_keyring) == {}
    assert_not_in_responses(FAKE_PASTED, [forged], pieces=False)
    assert_not_in_logs(FAKE_PASTED, caplog)


# --- settings, health and step status ----------------------------------------------------------


def test_provider_choice_saved_in_settings(
    tmp_path: Path, memory_keyring: MemoryKeyring, bundled_cli: Path
) -> None:
    tiers = TierMap(fast="fictional-fast-model", thorough="fictional-thorough-model")
    seeded = AIProviderSettings(tiers=tiers, task_tiers={"fictional_task": "thorough"})
    with running(tmp_path, provider=seeded) as run:
        assert run.app.state.provider_health == ProviderHealth("not_configured")

        assert_saved_and_stayed(submit(run, TOKEN_KIND, FICTIONAL_TOKEN))
        settings = stored(run)
        assert settings.kind == TOKEN_KIND
        assert settings.saved_sign_ins == (TOKEN_KIND,)
        assert settings.tiers == tiers
        assert dict(settings.task_tiers) == {"fictional_task": "thorough"}
        assert run.app.state.provider_health == ProviderHealth("ok"), "health refreshed"

        # Saving the same kind again with an empty field keeps the saved sign-in.
        assert_saved_and_stayed(submit(run, TOKEN_KIND, ""))
        assert stored(run).saved_sign_ins == (TOKEN_KIND,)
        assert keychain(memory_keyring) == {TOKEN_KEY: FICTIONAL_TOKEN}

        # Switching to a kind without a credential keeps the saved token (user decision,
        # 2026-10-05); tiers are kept.
        assert_saved_and_stayed(submit(run, EXISTING_KIND, "ignored-FICTIONAL-text"))
        settings = stored(run)
        assert settings.kind == EXISTING_KIND
        assert settings.saved_sign_ins == (TOKEN_KIND,)
        assert settings.tiers == tiers
        assert dict(settings.task_tiers) == {"fictional_task": "thorough"}
        assert "ignored-FICTIONAL-text" not in keychain(memory_keyring).values()
        assert keychain(memory_keyring) == {TOKEN_KEY: FICTIONAL_TOKEN}

        # Back to the token kind: the kept key is used with an empty field.
        assert_saved_and_stayed(submit(run, TOKEN_KIND, ""))
        assert stored(run).kind == TOKEN_KIND

        assert_saved_and_stayed(submit(run, API_KEY_KIND, FICTIONAL_API_KEY))
        settings = stored(run)
        assert settings.kind == API_KEY_KIND
        assert settings.saved_sign_ins == (API_KEY_KIND, TOKEN_KIND), "both kept, sorted"
        assert run.app.state.provider_health == ProviderHealth("ok")
        removes = remove_buttons(outline(page(run).text))
        assert {kind: button.text for kind, button in removes.items()} == {
            TOKEN_KIND: "Remove saved token",
            API_KEY_KIND: "Remove saved API key",
        }

        statuses = live_statuses(run)
        assert statuses.get("welcome") == "done"
        assert statuses.get("provider") != "done", "done only after Connected (M1.18)"
        assert run.decision == StartupDecision("guide", "provider")
        assert_see_other(run.client.get("/"), PROVIDER)

    # Persisted: a restart reads the same settings.
    with Database.open(data_folder(tmp_path) / "app.db") as db:
        reloaded = SettingsService(db, FrozenClock(START)).load(AI_PROVIDER).value
    assert reloaded.kind == API_KEY_KIND
    assert reloaded.saved_sign_ins == (API_KEY_KIND, TOKEN_KIND)
    assert reloaded.tiers == tiers


def test_ac_m1_6_provider_save_survives_restart_and_resumes_at_provider(
    tmp_path: Path, memory_keyring: MemoryKeyring
) -> None:
    with running(tmp_path) as run:
        assert_saved_and_stayed(submit(run, TOKEN_KIND, FICTIONAL_TOKEN))

    # A restart over the same folders (seeding again only re-marks step 1 done).
    with running(tmp_path) as run:
        assert run.decision == StartupDecision("guide", "provider")
        assert_see_other(run.client.get("/"), PROVIDER)
        shown = page(run)
        assert "checked" in kind_radios(outline(shown.text))[TOKEN_KIND].attrs
        assert "system keychain" in status_text(shown).lower()
    assert keychain(memory_keyring) == {TOKEN_KEY: FICTIONAL_TOKEN}


# --- delete -------------------------------------------------------------------------------------


def test_delete_removes_keychain_item_and_clears_saved_sign_in(
    tmp_path: Path, memory_keyring: MemoryKeyring, bundled_cli: Path
) -> None:
    with running(tmp_path) as run:
        assert_saved_and_stayed(submit(run, TOKEN_KIND, FICTIONAL_TOKEN))
        assert run.app.state.provider_health == ProviderHealth("ok")

        removed = submit(run, TOKEN_KIND, remove=TOKEN_KIND, confirm=TOKEN_KIND)
        assert_saved_and_stayed(removed)

        assert keychain(memory_keyring) == {}
        settings = stored(run)
        assert settings.kind == TOKEN_KIND
        assert settings.saved_sign_ins == ()
        assert run.app.state.provider_health == ProviderHealth("missing_sign_in")

        shown = page(run)
        assert "system keychain" not in status_text(shown).lower()
        assert remove_buttons(outline(shown.text)) == {}
    assert_not_in_responses(FICTIONAL_TOKEN, [removed, shown])


def test_remove_one_saved_key_keeps_the_other(
    tmp_path: Path, memory_keyring: MemoryKeyring, bundled_cli: Path
) -> None:
    with running(tmp_path) as run:
        assert_saved_and_stayed(submit(run, TOKEN_KIND, FICTIONAL_TOKEN))
        assert_saved_and_stayed(submit(run, API_KEY_KIND, FICTIONAL_API_KEY))

        # The token is removed while the API key is the chosen kind (action=delete + remove).
        removed = submit(
            run, API_KEY_KIND, action="delete", remove=TOKEN_KIND, confirm=TOKEN_KIND, htmx=True
        )
        assert removed.status_code == 200, removed.text[:300]
        assert "token was removed" in json.loads(removed.headers["hx-trigger"])["announce"]

        assert keychain(memory_keyring) == {API_KEY_KEY: FICTIONAL_API_KEY}
        settings = stored(run)
        assert (settings.kind, settings.saved_sign_ins) == (API_KEY_KIND, (API_KEY_KIND,))
        assert run.app.state.provider_health == ProviderHealth("ok")
        assert set(remove_buttons(outline(page(run).text))) == {API_KEY_KIND}


@pytest.mark.parametrize(
    ("remove", "action"),
    [
        pytest.param(API_KEY_KIND, None, id="kind_not_saved"),
        pytest.param("not-a-provider", None, id="unknown_kind"),
        pytest.param(None, "delete", id="no_target"),
    ],
)
def test_removing_a_key_that_is_not_saved_is_a_form_error(
    tmp_path: Path, memory_keyring: MemoryKeyring, remove: str | None, action: str | None
) -> None:
    with running(tmp_path) as run:
        assert_saved_and_stayed(submit(run, TOKEN_KIND, FICTIONAL_TOKEN))
        response = submit(run, action=action, remove=remove, htmx=True)
        assert response.status_code == 422, response.text[:300]
        assert "removed" not in response.headers.get("hx-trigger", "")
        error = required(outline(response.text), "form-error").text
        assert error == "There's no saved key to remove."
        assert stored(run).saved_sign_ins == (TOKEN_KIND,)
    assert keychain(memory_keyring) == {TOKEN_KEY: FICTIONAL_TOKEN}


def test_saved_key_gone_from_keychain_asks_to_paste_again(
    tmp_path: Path, memory_keyring: MemoryKeyring
) -> None:
    with running(tmp_path) as run:
        assert_saved_and_stayed(submit(run, TOKEN_KIND, FICTIONAL_TOKEN))
        memory_keyring.items.clear()  # removed outside the app

        message = assert_credential_error(submit(run, TOKEN_KIND, ""))
        assert "No saved token was found in your keychain. Paste it again." in message
        assert stored(run).saved_sign_ins == (), "the gone key is forgotten"
        assert remove_buttons(outline(page(run).text)) == {}


def test_delete_says_when_environment_still_provides_the_credential(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    memory_keyring: MemoryKeyring,
    bundled_cli: Path,
) -> None:
    monkeypatch.setenv(API_KEY_VARIABLE, ENV_API_KEY)
    with running(tmp_path) as run:
        assert_saved_and_stayed(submit(run, API_KEY_KIND, FICTIONAL_API_KEY))
        assert "system keychain" in status_text(page(run)).lower()

        assert_saved_and_stayed(
            submit(run, API_KEY_KIND, remove=API_KEY_KIND, confirm=API_KEY_KIND)
        )
        assert keychain(memory_keyring) == {}
        assert stored(run).saved_sign_ins == ()
        assert run.app.state.provider_health == ProviderHealth("ok")
        shown = page(run)
        assert API_KEY_VARIABLE in status_text(shown)
    assert_not_in_responses(ENV_API_KEY, [shown])


def test_delete_without_keychain_explains_fallback(tmp_path: Path) -> None:
    saved = AIProviderSettings(kind="claude_api_key", saved_sign_ins=("claude_api_key",))
    with running(tmp_path, wiring=make_wiring(backend=FailingKeyring()), provider=saved) as run:
        response = submit(run, API_KEY_KIND, remove=API_KEY_KIND, confirm=API_KEY_KIND)
        assert response.status_code == 422, response.text[:300]
        assert "keychain" in required(outline(response.text), "form-error").text.lower()
        assert stored(run).saved_sign_ins == (API_KEY_KIND,), "nothing changed"


def test_keychain_unreadable_with_environment_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bundled_cli: Path
) -> None:
    """A saved key whose keychain can't be read now, with the variable set: the status names
    the variable (ADR-047)."""
    monkeypatch.setenv(TOKEN_VARIABLE, ENV_TOKEN)
    saved = AIProviderSettings(kind=TOKEN_KIND, saved_sign_ins=(TOKEN_KIND,))
    wiring = make_wiring(
        {ALLOW_FAKES_VARIABLE: "1", TOKEN_VARIABLE: ENV_TOKEN}, backend=FailingKeyring()
    )
    with running(tmp_path, wiring=wiring, provider=saved) as run:
        assert_saved_and_stayed(submit(run, TOKEN_KIND, ""))
        shown = page(run)
        assert status_text(shown) == (
            f"Keychain couldn't be read; using the {TOKEN_VARIABLE} environment variable."
        )
        assert stored(run).saved_sign_ins == (TOKEN_KIND,), "kept: the keychain may come back"
    assert_not_in_responses(ENV_TOKEN, [shown])


# --- shape checks: refused values are never echoed --------------------------------------------

SHAPE_REFUSALS = {
    "whitespace_inside": (TOKEN_KIND, anthropic_shaped("oat01", "FICTIONAL with spaces inside")),
    "tab_inside": (API_KEY_KIND, anthropic_shaped("api03", "FICTIONAL\twithTabInsideIt")),
    "control_character": (TOKEN_KIND, anthropic_shaped("oat01", "FICTIONAL\x07bellInsideIt")),
    "too_short": (API_KEY_KIND, "FICTIONAL-short"),
    "too_long": (TOKEN_KIND, "FICTIONAL" + "x" * 1001),
    "not_ascii": (API_KEY_KIND, anthropic_shaped("api03", "FICTIONALcafé-naïve-value")),
    "api_key_in_token_mode": (TOKEN_KIND, FICTIONAL_API_KEY),
    "token_in_api_key_mode": (API_KEY_KIND, FICTIONAL_TOKEN),
}


@pytest.mark.parametrize("case", sorted(SHAPE_REFUSALS))
def test_shape_refusals_never_echo_the_value(
    tmp_path: Path,
    memory_keyring: MemoryKeyring,
    caplog: pytest.LogCaptureFixture,
    case: str,
) -> None:
    kind, value = SHAPE_REFUSALS[case]
    with caplog.at_level(logging.DEBUG), running(tmp_path) as run:
        plain = submit(run, kind, value)
        fragment = submit(run, kind, value, htmx=True)
        for response in (plain, fragment):
            message = assert_credential_error(response)
            elements = outline(response.text)
            assert "checked" in kind_radios(elements)[kind].attrs, "the kind is shown back"
            if case == "api_key_in_token_mode":
                assert "API key" in message, "points to the API-key choice"
            if case == "token_in_api_key_mode":
                assert "token" in message.lower(), "points to the token choice"
        assert stored(run).kind is None, "nothing saved"
    assert keychain(memory_keyring) == {}
    assert_not_in_responses(value, [plain, fragment])
    assert_not_in_logs(value, caplog)


def test_valid_shapes_at_the_limits_are_accepted(
    tmp_path: Path, memory_keyring: MemoryKeyring
) -> None:
    shortest = "FICTIONAL-" + "a" * 10  # 20 printable ASCII characters
    longest = "FICTIONAL-" + "b" * 990  # 1000
    with running(tmp_path) as run:
        assert_saved_and_stayed(submit(run, API_KEY_KIND, shortest))
        assert keychain(memory_keyring) == {API_KEY_KEY: shortest}
        assert_saved_and_stayed(submit(run, TOKEN_KIND, longest))
        assert keychain(memory_keyring)[TOKEN_KEY] == longest


# --- kind checks ------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", [None, "", "claude", "FAKE", "openai"])
def test_missing_or_unknown_kind_is_a_field_error(
    tmp_path: Path, memory_keyring: MemoryKeyring, kind: str | None
) -> None:
    with running(tmp_path) as run:
        response = submit(run, kind, FICTIONAL_TOKEN)
        assert_kind_error(response)
        assert stored(run).kind is None
    assert keychain(memory_keyring) == {}
    assert_not_in_responses(FICTIONAL_TOKEN, [response])


def test_unknown_kind_is_shown_back_escaped_or_not_at_all(tmp_path: Path) -> None:
    hostile = '"><script>alert(1)</script>'
    with running(tmp_path) as run:
        response = submit(run, hostile, "")
    assert_kind_error(response)
    assert "<script>alert(1)" not in response.text


def test_existing_login_refused_in_demo(tmp_path: Path, memory_keyring: MemoryKeyring) -> None:
    with running(tmp_path, demo=True, wiring=make_wiring(demo=True)) as run:
        shown = page(run)
        radios = kind_radios(outline(shown.text))
        assert EXISTING_KIND not in radios, "the computer's own sign-in is not offered in demo"
        assert TOKEN_KIND in radios

        response = submit(run, EXISTING_KIND, "")
        assert_kind_error(response)
        assert stored(run).kind is None

        # Demo saves go to the demo keychain service only.
        assert_saved_and_stayed(submit(run, TOKEN_KIND, FICTIONAL_TOKEN))
    assert keychain(memory_keyring) == {}
    assert keychain(memory_keyring, DEMO_SERVICE) == {TOKEN_KEY: FICTIONAL_TOKEN}


def test_unknown_action_is_a_form_error(tmp_path: Path, memory_keyring: MemoryKeyring) -> None:
    with running(tmp_path) as run:
        response = submit(run, TOKEN_KIND, FICTIONAL_TOKEN, action="export-everything")
        assert response.status_code == 422, response.text[:300]
        elements = outline(response.text)
        assert credential_input(elements).attrs.get("aria-invalid") != "true", "not a field error"
        assert required(elements, "form-error").text
        assert stored(run).kind is None
    assert keychain(memory_keyring) == {}
    assert_not_in_responses(FICTIONAL_TOKEN, [response])


# --- htmx: saved, stays on the step, announces ---------------------------------------------------


def test_htmx_save_answers_200_with_announce_and_no_push_url(
    tmp_path: Path, memory_keyring: MemoryKeyring
) -> None:
    with running(tmp_path) as run:
        response = submit(run, TOKEN_KIND, FICTIONAL_TOKEN, htmx=True)

    assert response.status_code == 200, response.text[:300]
    assert "location" not in response.headers
    assert "hx-push-url" not in response.headers, "the guide stays on the provider step"
    trigger = json.loads(response.headers["hx-trigger"])
    assert set(trigger) == {"announce"}
    assert trigger["announce"] == (
        "Saved. Your token is in your system keychain. Next, test the connection."
    )
    assert "Step 3" not in trigger["announce"]
    for marker in ("<html", "<body", 'id="announcer"'):
        assert marker not in response.text, marker
    elements = outline(response.text)
    current = only([e for e in elements if e.attrs.get("aria-current") == "step"], "current")
    assert current.text == "AI provider"
    title = only([e for e in elements if e.tag == "title"], "<title>")
    assert title.text.startswith("AI provider"), title.text
    status = required(elements, "credential-status")
    assert "system keychain" in status.text.lower()
    focused = [e.attrs.get("id") for e in elements if "data-autofocus" in e.attrs]
    assert focused == ["credential-status"], "focus moves to what was saved"
    assert_password_field(elements)
    assert keychain(memory_keyring) == {TOKEN_KEY: FICTIONAL_TOKEN}
    assert_not_in_responses(FICTIONAL_TOKEN, [response])


def test_refused_save_title_says_error(tmp_path: Path) -> None:
    with running(tmp_path) as run:
        response = submit(run, TOKEN_KIND, "FICTIONAL-short", htmx=True)
    assert_credential_error(response)
    title = only([e for e in outline(response.text) if e.tag == "title"], "<title>")
    assert title.text.startswith("Error: AI provider"), title.text


def test_replacing_the_token_overwrites_the_keychain_item(
    tmp_path: Path, memory_keyring: MemoryKeyring
) -> None:
    with running(tmp_path) as run:
        assert_saved_and_stayed(submit(run, TOKEN_KIND, FICTIONAL_TOKEN))
        replaced = submit(run, TOKEN_KIND, SECOND_TOKEN)
        assert_saved_and_stayed(replaced)
        shown = page(run)
    assert keychain(memory_keyring) == {TOKEN_KEY: SECOND_TOKEN}
    assert_not_in_responses(SECOND_TOKEN, [replaced, shown])
    assert_not_in_responses(FICTIONAL_TOKEN, [replaced, shown])


# --- no workspace yet ---------------------------------------------------------------------------


def test_provider_step_without_workspace_asks_for_step_1(
    tmp_path: Path, memory_keyring: MemoryKeyring
) -> None:
    with running(tmp_path, welcome_done=False) as run:
        assert run.app.state.workspace is None
        shown = page(run)
        assert "step 1" in shown.text.lower()

        response = submit(run, TOKEN_KIND, FICTIONAL_TOKEN)
        assert response.status_code == 422, response.text[:300]
        assert run.app.state.workspace is None
    assert keychain(memory_keyring) == {}
    assert not data_folder(tmp_path).exists(), "nothing is created"
    assert_not_in_responses(FICTIONAL_TOKEN, [shown, response])


def test_provider_post_needs_token_and_origin(
    tmp_path: Path, memory_keyring: MemoryKeyring
) -> None:
    form = {"kind": TOKEN_KIND, "credential": FICTIONAL_TOKEN}
    with running(tmp_path) as run:
        no_token = run.client.post(PROVIDER, data=form, headers={"origin": BASE_URL})
        bad_origin = run.client.post(
            PROVIDER, data=form, headers=run.action(origin="http://evil.example")
        )
        assert no_token.status_code == 403
        assert bad_origin.status_code == 403
        assert stored(run).kind is None
    assert keychain(memory_keyring) == {}


def test_provider_get_never_saves(tmp_path: Path, memory_keyring: MemoryKeyring) -> None:
    with running(tmp_path) as run:
        run.client.get(PROVIDER, params={"kind": TOKEN_KIND, "credential": FICTIONAL_TOKEN})
        assert stored(run).kind is None
    assert keychain(memory_keyring) == {}


@pytest.mark.parametrize(
    ("kind", "credential", "announce"),
    [
        pytest.param(
            API_KEY_KIND,
            FICTIONAL_API_KEY,
            "Saved. Your API key is in your system keychain. Next, test the connection.",
            id="api_key",
        ),
        pytest.param(
            EXISTING_KIND,
            "",
            "Saved. The app will use the Claude Code sign-in on this computer. "
            "Next, test the connection.",
            id="existing",
        ),
        pytest.param(
            EXISTING_KIND,
            "pasted-FICTIONAL-not-needed",
            "Saved. The app will use the Claude Code sign-in on this computer. "
            "The pasted text wasn't needed, so it wasn't kept. Next, test the connection.",
            id="existing_pasted",
        ),
    ],
)
def test_saved_announcement_says_what_happened(
    tmp_path: Path, memory_keyring: MemoryKeyring, kind: str, credential: str, announce: str
) -> None:
    with running(tmp_path) as run:
        response = submit(run, kind, credential, htmx=True)
    assert response.status_code == 200, response.text[:300]
    assert json.loads(response.headers["hx-trigger"]) == {"announce": announce}


def test_provider_form_disables_every_button_and_shows_a_keychain_indicator(
    tmp_path: Path, memory_keyring: MemoryKeyring
) -> None:
    with running(tmp_path) as run:
        assert_saved_and_stayed(submit(run, TOKEN_KIND, FICTIONAL_TOKEN))
        elements = outline(page(run).text)
    form = only([e for e in elements if e.tag == "form"], "form")
    # A plain selector: htmx's "find" would disable only the first button.
    assert form.attrs.get("hx-disabled-elt") == f"#{form.attrs.get('id')} button"
    buttons = [e for e in elements if e.tag == "button" and e.within(form)]
    assert len(buttons) == 3, "Save, Test connection and one Remove button"
    indicator = required(elements, str(form.attrs.get("hx-indicator")).lstrip("#"))
    assert "If your computer asks to allow access, choose Allow." in indicator.text
