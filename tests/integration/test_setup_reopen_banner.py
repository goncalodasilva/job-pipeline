"""``--setup`` reopening, the case 4 banners and resuming after a restart (M1.20).

AC-M1.7: ``jobpipeline --setup`` reopens a finished guide at its first step without changing
any stored data. Page loads redirect to the guide until the user chooses "Back to the app"
(``POST /setup/leave``: launch token and origin checked, not page-tagged; 303 to ``/``, or for
htmx 200 with ``HX-Redirect: /``), or finishes the Done step again; either clears the reopening
for the rest of this run. ``--setup`` on a guide never finished simply resumes (ADR-050 rule 3).

AC-M1.1 case 4: setup is complete but the provider is broken (here a token route with no saved
token and no environment variable). The app opens, never the wizard, and every app page (not
the guide, where banners lead) shows one banner per entry in ``StartupDecision.banners`` (a
broken provider first, then a required step registered since; ADR-050 rule 5). A banner is an
element whose ``class`` has the token ``banner`` and holds a link to ``/setup/<step>``. The
guide's step list says "Needs attention" for those steps, and offers "Back to the app". The
banner state is the cached decision and provider health: GET requests read no keychain (spec
2.9.12; ADR-059).

AC-M1.6: restarting the server mid-setup resumes at the first unfinished required step; an
optional step left pending never blocks.

Every folder lives under ``tmp_path``; every value is fictional. The autouse fixtures in
``tests/conftest.py`` allow fakes, remove credential variables and pin an in-memory keyring.
"""

from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from httpx import Response

from app import __version__
from app.config import RuntimeConfig, write_bootstrap
from app.domain.settings import AI_PROVIDER, JOB_PREFERENCES, AIProviderSettings
from app.main import create_app
from app.modules.ai import ProviderHealth
from app.modules.storage import Database
from app.onboarding.detection import StartupDecision
from app.onboarding.registry import StepDefinition, StepRegistry, m1_registry
from app.services.settings import SettingsService
from app.wiring import Wiring
from tests.fakes.clock import FrozenClock
from tests.fakes.keyring_backends import CountingKeyring
from tests.fakes.sdk_install import install_fake_sdk
from tests.integration.test_setup_preferences_done import (
    FICTIONAL_ROLE,
    SAMPLE_OPTIONAL,
    SAVED,
    finish,
    live_statuses,
    valid_form,
)
from tests.integration.test_setup_provider import make_wiring
from tests.integration.test_setup_provider_test import FAKE_CONNECTED, post_test
from tests.integration.test_setup_redirect import (
    BASE_URL,
    PAGE_PATHS,
    PORT,
    START,
    Element,
    Running,
    assert_hx_redirect,
    assert_not_redirected,
    assert_see_other,
    outline,
)
from tests.setup_harness import HARNESS_PROVIDER, complete_setup, seed_workspace

pytestmark = pytest.mark.integration

WELCOME = "/setup/welcome"
PROVIDER_STEP = "/setup/provider"
PREFERENCES_STEP = "/setup/preferences"
DONE_STEP = "/setup/done"
BACK_TO_APP_PATH = "/setup/leave"
BACK_TO_APP_TEXT = "Back to the app"
TOKEN_KIND = "claude_subscription_token"  # noqa: S105 - a provider kind, not a secret
ALL_M1_DONE = {step.id: "done" for step in m1_registry().steps}
SAMPLE_REQUIRED = StepDefinition(
    "sample_required", "Sample required step", True, 55, "fragments/setup/_done.html"
)


# --- helpers -------------------------------------------------------------------------------


@pytest.fixture
def bundled_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The SDK looks installed with its bundled CLI (never run), so a Claude kind's health is
    decided by its sign-in alone."""
    return install_fake_sdk(monkeypatch, tmp_path / "sdk", with_cli=True)


def make_config(tmp_path: Path, *, setup: bool = False) -> RuntimeConfig:
    return RuntimeConfig(
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "cfg",
        port=PORT,
        log_level="INFO",
        demo=False,
        setup=setup,
        no_browser=False,
        data_dir_source="bootstrap",
    )


@contextmanager
def started(
    tmp_path: Path,
    *,
    setup: bool = False,
    registry: StepRegistry | None = None,
    wiring: Wiring | None = None,
) -> Iterator[Running]:
    """The app on folders seeded beforehand (nothing is written here)."""
    extra: dict[str, object] = {}
    if registry is not None:
        extra["setup_registry"] = registry
    if wiring is not None:
        extra["wiring"] = wiring
    app = create_app(make_config(tmp_path, setup=setup), clock=FrozenClock(START), **extra)  # type: ignore[arg-type]
    with TestClient(app, base_url=BASE_URL, follow_redirects=False) as client:
        yield Running(app=app, client=client, hits=Counter())


def finished_guide(
    tmp_path: Path, *, provider: AIProviderSettings | None = None, preferences: bool = True
) -> None:
    """A finished guide (every M1 step done) with the fake provider by default and, unless
    ``preferences=False``, the fictional job preferences saved."""
    clock = FrozenClock(START)
    if provider is None:
        complete_setup(tmp_path / "data", tmp_path / "cfg", clock=clock)
    else:
        complete_setup(tmp_path / "data", tmp_path / "cfg", clock=clock, provider=provider)
    if preferences:
        seed_workspace(tmp_path / "data", clock=clock, preferences=SAVED)


def broken_provider() -> AIProviderSettings:
    """The token route with no saved token (and no environment variable): missing sign-in."""
    return AIProviderSettings(kind=TOKEN_KIND)


def stored_settings(run: Running) -> tuple[object, object]:
    db: Database = run.app.state.workspace.db
    settings = SettingsService(db, FrozenClock(START))
    return settings.load(AI_PROVIDER).value, settings.load(JOB_PREFERENCES).value


def back_to_app(run: Running, *, htmx: bool = False) -> Response:
    headers = run.action(**({"HX-Request": "true"} if htmx else {}))
    return run.client.post(BACK_TO_APP_PATH, headers=headers)


def get_ok(run: Running, path: str) -> Response:
    response = run.client.get(path)
    assert response.status_code == 200, (path, response.status_code, response.text[:300])
    assert_not_redirected(response)
    return response


def class_tokens(element: Element) -> list[str]:
    return (element.attrs.get("class") or "").split()


def banners(page: str) -> list[Element]:
    """Banner elements in document order (class token ``banner``)."""
    return [e for e in outline(page) if "banner" in class_tokens(e)]


def banner_links(page: str) -> list[str]:
    """For each banner, in order, the ``/setup/...`` link it holds (exactly one each)."""
    elements = outline(page)
    found: list[str] = []
    for banner in [e for e in elements if "banner" in class_tokens(e)]:
        links = [
            str(e.attrs.get("href"))
            for e in elements
            if e.tag == "a"
            and e.within(banner)
            and str(e.attrs.get("href") or "").startswith("/setup/")
        ]
        assert len(links) == 1, f"a banner holds one link to its step, found {links}"
        link = next(e for e in elements if e.tag == "a" and e.within(banner))
        assert link.text, "the banner link has text"
        assert banner.text, "the banner says what is wrong"
        found.append(links[0])
    return found


def back_to_app_forms(page: str) -> list[Element]:
    """Forms posting to "Back to the app" (``action`` or ``hx-post``) with that button."""
    elements = outline(page)
    forms = [
        e
        for e in elements
        if e.tag == "form" and BACK_TO_APP_PATH in (e.attrs.get("action"), e.attrs.get("hx-post"))
    ]
    return [
        form
        for form in forms
        if any(
            e.tag == "button" and e.within(form) and e.text == BACK_TO_APP_TEXT for e in elements
        )
    ]


# --- AC-M1.7: --setup reopens the guide without losing data ----------------------------------


def test_ac_m1_7_setup_flag_reopens_guide_without_losing_data(tmp_path: Path) -> None:
    finished_guide(tmp_path)

    with started(tmp_path, setup=True) as run:
        assert run.decision == StartupDecision("guide", "welcome", reopened=True)
        before = stored_settings(run)
        assert before[1] == SAVED
        assert live_statuses(run) == ALL_M1_DONE

        # Every page goes to the guide's first step while it is reopened.
        for path in PAGE_PATHS:
            assert_see_other(run.client.get(path), WELCOME)
        assert_hx_redirect(run.client.get("/inbox", headers=run.htmx()), WELCOME)

        # Every step stays editable with its saved values; the list still says Done.
        welcome = get_ok(run, WELCOME)
        assert back_to_app_forms(welcome.text), "the reopened guide offers Back to the app"
        preferences = get_ok(run, PREFERENCES_STEP)
        # TD-342 (spec 2.9 rule 13): the token-free page is a shell with no stored value; the
        # saved preferences pre-fill the form that its protected fragment serves.
        assert FICTIONAL_ROLE not in preferences.text, "the token-free page holds no preference"
        form = run.client.get(f"{PREFERENCES_STEP}/form", headers=run.with_token())
        assert form.status_code == 200
        assert FICTIONAL_ROLE in form.text, "saved preferences are pre-filled"
        done = get_ok(run, DONE_STEP)
        assert "Not done yet" not in done.text

        # Nothing was reset by reopening or by loading the guide.
        assert stored_settings(run) == before
        assert live_statuses(run) == ALL_M1_DONE


def test_setup_flag_back_to_app_post_clears_reopen(tmp_path: Path) -> None:
    finished_guide(tmp_path)

    with started(tmp_path, setup=True) as run:
        before = stored_settings(run)
        # The POST is behind the launch token and origin checks (not a page route).
        no_token = run.client.post(BACK_TO_APP_PATH, headers={"origin": BASE_URL})
        assert no_token.status_code == 403
        bad_origin = run.client.post(
            BACK_TO_APP_PATH, headers=run.action(origin="http://evil.example")
        )
        assert bad_origin.status_code == 403
        assert run.decision.reopened, "a refused request changes nothing"
        # A GET never leaves the guide (spec 2.9 rule 12).
        assert run.client.get(BACK_TO_APP_PATH).status_code in (404, 405)
        assert run.decision.reopened

        response = back_to_app(run)

        assert_see_other(response, "/")
        assert run.decision == StartupDecision("app")
        for path in PAGE_PATHS:
            get_ok(run, path)
        assert stored_settings(run) == before
        assert live_statuses(run) == ALL_M1_DONE
        # Choosing it again is harmless.
        assert_see_other(back_to_app(run), "/")


def test_setup_flag_back_to_app_htmx_gets_hx_redirect(tmp_path: Path) -> None:
    finished_guide(tmp_path)

    with started(tmp_path, setup=True) as run:
        response = back_to_app(run, htmx=True)

        assert_hx_redirect(response, "/")
        assert run.decision == StartupDecision("app")
        get_ok(run, "/inbox")


def test_finishing_done_while_reopened_clears_reopen(tmp_path: Path) -> None:
    finished_guide(tmp_path)

    with started(tmp_path, setup=True) as run:
        response = finish(run, "today")

        assert_see_other(response, "/")
        assert not run.decision.reopened
        assert run.decision == StartupDecision("app")
        get_ok(run, "/")
        assert live_statuses(run) == ALL_M1_DONE


def test_finishing_done_with_htmx_while_reopened_opens_inbox(tmp_path: Path) -> None:
    finished_guide(tmp_path)

    with started(tmp_path, setup=True) as run:
        response = finish(run, "add_job", htmx=True)

        assert_hx_redirect(response, "/inbox")
        assert run.decision == StartupDecision("app")
        get_ok(run, "/inbox")


def test_setup_flag_on_unfinished_guide_resumes_instead_of_reopening(tmp_path: Path) -> None:
    write_bootstrap(tmp_path / "cfg", data_dir=tmp_path / "data", version=__version__)
    seed_workspace(tmp_path / "data", done=("welcome",), clock=FrozenClock(START))

    with started(tmp_path, setup=True) as run:
        assert run.decision == StartupDecision("guide", "provider", reopened=False)
        assert_see_other(run.client.get("/"), PROVIDER_STEP)
        assert not back_to_app_forms(get_ok(run, PROVIDER_STEP).text), (
            "nothing to go back to: the guide was never finished"
        )

        # "Back to the app" can't skip an unfinished guide.
        back_to_app(run)
        assert run.decision == StartupDecision("guide", "provider")
        assert_see_other(run.client.get("/"), PROVIDER_STEP)
        assert live_statuses(run)["welcome"] == "done"


def test_setup_flag_on_unfinished_guide_then_finishing_done_reaches_app(tmp_path: Path) -> None:
    """``--setup`` never reopens a guide finished during this run (ADR-050 rule 3)."""
    write_bootstrap(tmp_path / "cfg", data_dir=tmp_path / "data", version=__version__)
    seed_workspace(
        tmp_path / "data",
        done=("welcome", "provider", "web_search", "cv_import", "preferences"),
        clock=FrozenClock(START),
        provider=HARNESS_PROVIDER,
        preferences=SAVED,
    )

    with started(tmp_path, setup=True) as run:
        assert run.decision == StartupDecision("guide", "done")
        response = finish(run, "today")

        assert_see_other(response, "/")
        assert run.decision == StartupDecision("app")
        get_ok(run, "/")
        assert live_statuses(run) == ALL_M1_DONE


# --- AC-M1.1 case 4: complete setup, broken provider: banners, never the wizard -------------


def test_ac_m1_1_complete_setup_with_missing_key_shows_banner_not_wizard(
    tmp_path: Path, bundled_cli: Path
) -> None:
    finished_guide(tmp_path, provider=broken_provider())

    with started(tmp_path) as run:
        assert run.app.state.provider_health.status == "missing_sign_in"
        assert run.decision == StartupDecision("app_with_banner", "provider", banners=("provider",))
        for path in PAGE_PATHS:
            page = get_ok(run, path)
            assert len(banners(page.text)) == 1, path
        # The guide still opens on request.
        get_ok(run, PROVIDER_STEP)


def test_banner_links_to_provider_step(tmp_path: Path, bundled_cli: Path) -> None:
    broken = tmp_path / "broken"
    finished_guide(broken, provider=broken_provider())
    with started(broken) as run:
        for path in PAGE_PATHS:
            assert banner_links(get_ok(run, path).text) == [PROVIDER_STEP], path

    healthy = tmp_path / "healthy"
    finished_guide(healthy)
    with started(healthy) as run:
        assert run.decision == StartupDecision("app")
        for path in PAGE_PATHS:
            assert banners(get_ok(run, path).text) == [], f"no banner when healthy: {path}"


def test_one_banner_per_entry_provider_first(tmp_path: Path, bundled_cli: Path) -> None:
    """A broken provider and a required step registered since the guide was finished."""
    registry = StepRegistry((*m1_registry().steps, SAMPLE_REQUIRED))
    finished_guide(tmp_path, provider=broken_provider())

    with started(tmp_path, registry=registry) as run:
        assert run.decision.banners == ("provider", "sample_required")
        for path in PAGE_PATHS:
            links = banner_links(get_ok(run, path).text)
            assert links == [PROVIDER_STEP, "/setup/sample_required"], (path, links)


def test_banner_state_computed_without_secret_reads_on_get(
    tmp_path: Path, bundled_cli: Path
) -> None:
    backend = CountingKeyring()
    finished_guide(tmp_path, provider=broken_provider())

    with started(tmp_path, wiring=make_wiring(backend=backend)) as run:
        assert backend.calls == 0, "start-up health is configuration only"
        decision = run.decision
        assert decision.kind == "app_with_banner"
        for _ in range(2):
            for path in PAGE_PATHS:
                assert banner_links(get_ok(run, path).text) == [PROVIDER_STEP], path
                run.client.get(path, headers=run.htmx())
                run.client.head(path)
        assert backend.calls == 0, "GET and HEAD never read the keychain"
        assert run.decision == decision, "GET never recomputes the banner state"


# --- AC-M1.6: restart mid-setup resumes at the first unfinished required step ---------------


def test_ac_m1_6_restart_resumes_at_first_unfinished_required_step(tmp_path: Path) -> None:
    # An optional step left pending before Done must never be where the guide resumes.
    registry = StepRegistry((*m1_registry().steps, SAMPLE_OPTIONAL))
    write_bootstrap(tmp_path / "cfg", data_dir=tmp_path / "data", version=__version__)
    seed_workspace(
        tmp_path / "data", done=("welcome",), clock=FrozenClock(START), registry=registry
    )

    with started(tmp_path, registry=registry) as run:
        assert_see_other(run.client.get("/"), PROVIDER_STEP)
        connected = post_test(run, "fake", FAKE_CONNECTED, htmx=False)
        assert connected.status_code == 200, connected.text[:300]
        assert live_statuses(run)["provider"] == "done"

    with started(tmp_path, registry=registry) as run:
        assert run.decision == StartupDecision("guide", "preferences")
        assert_see_other(run.client.get("/"), PREFERENCES_STEP)
        saved = run.client.post(PREFERENCES_STEP, data=valid_form(), headers=run.action())
        assert_see_other(saved, DONE_STEP)

    with started(tmp_path, registry=registry) as run:
        assert run.decision == StartupDecision("guide", "done")
        assert_see_other(run.client.get("/"), DONE_STEP)
        assert live_statuses(run).get("sample_optional", "pending") == "pending"


def test_banner_link_page_offers_back_to_app_and_post_lands_on_app(
    tmp_path: Path, bundled_cli: Path
) -> None:
    """A banner leads into the guide; once setup is finished there's always a way back."""
    finished_guide(tmp_path, provider=broken_provider())

    with started(tmp_path) as run:
        decision = run.decision
        page = get_ok(run, PROVIDER_STEP).text
        assert back_to_app_forms(page), "the guide opened from a banner offers Back to the app"

        assert_see_other(back_to_app(run), "/")
        assert_hx_redirect(back_to_app(run, htmx=True), "/")
        assert run.decision == decision, "nothing to end: the banner stays"
        assert banner_links(get_ok(run, "/").text) == [PROVIDER_STEP]


def test_guide_pages_show_no_banner_and_list_says_needs_attention(
    tmp_path: Path, bundled_cli: Path
) -> None:
    finished_guide(tmp_path, provider=broken_provider())

    with started(tmp_path) as run:
        for path in (WELCOME, PROVIDER_STEP, PREFERENCES_STEP, DONE_STEP):
            assert banners(get_ok(run, path).text) == [], f"banners stay off the guide: {path}"
        steps = [
            e
            for e in outline(get_ok(run, PROVIDER_STEP).text)
            if e.tag == "li" and e.parent is not None and e.parent.tag == "ol"
        ]
        texts = {
            step.id: element.text for step, element in zip(m1_registry().steps, steps, strict=True)
        }
        assert texts["provider"].endswith("· Needs attention"), texts
        for step_id in ("welcome", "preferences"):
            assert texts[step_id].endswith("· Done"), texts
        assert sum("Needs attention" in text for text in texts.values()) == 1, texts


def test_provider_banner_names_the_cached_cause(tmp_path: Path, bundled_cli: Path) -> None:
    finished_guide(tmp_path, provider=broken_provider())

    with started(tmp_path) as run:
        assert run.app.state.provider_health == ProviderHealth("missing_sign_in")
        (banner,) = banners(get_ok(run, "/").text)
        assert "Your saved token or API key is missing." in banner.text
        assert banner.text.startswith("Needs attention:")


def test_done_summary_says_needs_attention_for_banner_steps(
    tmp_path: Path, bundled_cli: Path
) -> None:
    """The Done step's summary agrees with the step list: a broken provider is not "Done"."""
    finished_guide(tmp_path, provider=broken_provider())

    with started(tmp_path) as run:
        assert run.decision.banners == ("provider",)
        elements = outline(get_ok(run, DONE_STEP).text)
        (summary,) = [e for e in elements if e.tag == "ul" and "setup-summary" in class_tokens(e)]
        rows = [e.text for e in elements if e.tag == "li" and e.parent is summary]
        assert rows == [
            "Welcome and data folder: Done",
            "AI provider: Needs attention",
            "Web search: Done",
            "Import CV: Done",
            "Job preferences: Done",
        ], rows


ATTENTION_INTRO = 'Something needs attention: open the step marked "Needs attention" to fix it.'


def test_done_intro_says_something_needs_attention_and_finish_still_works(
    tmp_path: Path, bundled_cli: Path
) -> None:
    finished_guide(tmp_path, provider=broken_provider())

    with started(tmp_path) as run:
        page = get_ok(run, DONE_STEP).text
        texts = [e.text for e in outline(page) if e.tag == "p"]
        assert ATTENTION_INTRO in texts, texts
        assert not any("You're ready to start" in text for text in texts), texts

        finished = run.client.post(DONE_STEP, data={"then": "today"}, headers=run.action())
        assert_see_other(finished, "/")
        assert live_statuses(run) == ALL_M1_DONE

    healthy = tmp_path / "healthy"
    finished_guide(healthy)
    with started(healthy) as run:
        texts = [e.text for e in outline(get_ok(run, DONE_STEP).text) if e.tag == "p"]
        assert any(text.startswith("You're ready to start") for text in texts), texts
        assert ATTENTION_INTRO not in texts
