"""Settings > Providers: map the Fast and Thorough tiers to models (M1.22; spec 3.7; ADR-051,
ADR-067, ADR-068).

Inside the Providers section (``#providers``, so a provider-kind change re-renders it), after
``<form id="provider-form">`` and not nested in it, an element ``#tiers`` holds an ``<h3
id="tiers-heading">`` "Model per tier" and a form posting to ``/settings/tiers`` with two
selects, ``fast`` ("Fast tier model") and ``thorough`` ("Thorough tier model"). Their options
are exactly the active kind's ``known_models()`` (value = id, text = label, in order); the
selected one is the stored tier model, else the provider's default for that tier ("nothing
chosen" counts as Claude). A button "Save tier models" submits. The per-task list of M9.2
part C (``#task-tiers``, outside ``#tiers``) replaces the old line that said moving tasks
between tiers comes later. The guide's provider step never shows this form.

``POST /settings/tiers`` (launch token and origin; POST only): both ids offered by the active
kind save ``TierMap(fast, thorough)`` into the AI provider setting, leaving every other field
alone; a plain request answers 303 to ``/settings``, htmx 200 with the Providers section and an
``announce`` trigger saying it was saved. Anything else (a field missing, an unknown id, the
other provider's id) answers 422, saves nothing, marks the offending select invalid with an
error tied by ``aria-describedby`` and never shows the rejected value back. A saved map is what
model resolution and the connection test use.

Providers are the app-selectable fake or Claude kinds whose health is decided from the
settings row (no keychain, no CLI run, no network). Every value is fictional.
"""

from pathlib import Path

import pytest
from httpx import Response

from app.domain.settings import AIProviderSettings, Tier, TierMap
from app.domain.tiers import resolve_model
from app.modules.ai import ModelChoice
from app.modules.ai.providers import claude_sdk, fake
from app.modules.ai.providers.claude_sdk import (
    CLAUDE_FAST_DEFAULT,
    CLAUDE_MOST_CAPABLE,
    CLAUDE_THOROUGH_DEFAULT,
)
from app.modules.ai.providers.fake import FAKE_FAST_MODEL, FAKE_THOROUGH_MODEL
from tests.fakes.sdk_install import install_fake_sdk
from tests.integration.test_settings_providers import (
    SETTINGS,
    assert_section_fragment,
    post_settings,
    post_settings_test,
    section,
    settings_page,
    settings_version,
)
from tests.integration.test_setup_provider import (
    API_KEY_KIND,
    EXISTING_KIND,
    FAKE_KIND,
    TOKEN_KIND,
    described_by,
    live_statuses,
    only,
    required,
    stored,
)
from tests.integration.test_setup_provider_test import FAKE_CONNECTED, announced
from tests.integration.test_setup_redirect import (
    BASE_URL,
    Element,
    Running,
    assert_see_other,
    outline,
)
from tests.integration.test_setup_reopen_banner import ALL_M1_DONE, finished_guide, started

pytestmark = pytest.mark.integration

SETTINGS_TIERS = "/settings/tiers"
PROVIDER_STEP = "/setup/provider"
TIERS_ID = "tiers"
TIERS_HEADING = "Model per tier"
SAVE_TIERS = "Save tier models"
LABELS: dict[Tier, str] = {"fast": "Fast tier model", "thorough": "Thorough tier model"}
CLAUDE_MODELS: tuple[ModelChoice, ...] = tuple(claude_sdk.KNOWN_MODELS)
FAKE_MODELS: tuple[ModelChoice, ...] = tuple(fake.KNOWN_MODELS)
CLAUDE_DEFAULTS: dict[Tier, str] = {
    "fast": CLAUDE_FAST_DEFAULT,
    "thorough": CLAUDE_THOROUGH_DEFAULT,
}
FAKE_DEFAULTS: dict[Tier, str] = {"fast": FAKE_FAST_MODEL, "thorough": FAKE_THOROUGH_MODEL}
UNKNOWN_MODEL = "fictional-model-9"
HOSTILE_MODEL = '"><b id="injected">fictional</b>'


# --- helpers -------------------------------------------------------------------------------


@pytest.fixture
def bundled_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The SDK looks installed with its bundled CLI (a placeholder that is never run), so a
    Claude kind's provider health is decided by its sign-in alone."""
    return install_fake_sdk(monkeypatch, tmp_path / "sdk", with_cli=True)


def post_tiers(run: Running, form: dict[str, str], *, htmx: bool = False) -> Response:
    headers = run.action(**({"HX-Request": "true"} if htmx else {}))
    return run.client.post(SETTINGS_TIERS, data=form, headers=headers)


def tiers_block(elements: list[Element]) -> Element:
    """``#tiers`` inside the Providers section, headed by its ``<h3>``, outside the provider
    form."""
    block = required(elements, TIERS_ID)
    assert block.within(section(elements)), "the tier map sits in the Providers section"
    provider_form = required(elements, "provider-form")
    assert not block.within(provider_form), "the tier form is not nested in the provider form"
    heading = required(elements, "tiers-heading")
    assert heading.tag == "h3", heading.tag
    assert heading.within(block)
    assert heading.text == TIERS_HEADING, heading.text
    return block


def tiers_form(elements: list[Element]) -> Element:
    block = tiers_block(elements)
    form = only([e for e in elements if e.tag == "form" and e.within(block)], "tier form in #tiers")
    assert form.attrs.get("action") == SETTINGS_TIERS, form.attrs
    assert (form.attrs.get("method") or "").lower() == "post", form.attrs
    assert form.attrs.get("hx-post") == SETTINGS_TIERS, form.attrs
    return form


def tier_select(elements: list[Element], tier: Tier) -> Element:
    """The tier's ``<select>`` in the tier form, with its visible label."""
    form = tiers_form(elements)
    select = only(
        [
            e
            for e in elements
            if e.tag == "select" and e.attrs.get("name") == tier and e.within(form)
        ],
        f"{tier} select",
    )
    select_id = select.attrs.get("id")
    labels = [
        e
        for e in elements
        if e.tag == "label"
        and ((select_id and e.attrs.get("for") == select_id) or select.within(e))
    ]
    assert any(LABELS[tier] in label.text for label in labels), (
        tier,
        [label.text for label in labels],
    )
    return select


def options(elements: list[Element], select: Element) -> list[tuple[str, str]]:
    return [
        (str(e.attrs.get("value")), e.text)
        for e in elements
        if e.tag == "option" and e.within(select)
    ]


def selected(elements: list[Element], select: Element) -> list[str]:
    return [
        str(e.attrs.get("value"))
        for e in elements
        if e.tag == "option" and e.within(select) and "selected" in e.attrs
    ]


def assert_tier_selects(
    elements: list[Element], models: tuple[ModelChoice, ...], chosen: dict[Tier, str]
) -> None:
    """Both selects offer exactly ``models`` (id and label, in order), each with ``chosen``
    selected."""
    expected = [(model.id, model.label) for model in models]
    for tier in ("fast", "thorough"):
        select = tier_select(elements, tier)
        assert options(elements, select) == expected, (tier, options(elements, select))
        assert selected(elements, select) == [chosen[tier]], (tier, selected(elements, select))


def assert_task_tiers_follow_the_model_map(elements: list[Element]) -> None:
    """M9.2 part C (ADR-089 item 9, ADR-069 item 6): moving tasks between tiers is built, so
    the "comes later" empty-state line is gone from #tiers and the per-task list (the
    collapsed ``#task-tiers``, in the Providers section but outside #tiers) follows the map."""
    block = tiers_block(elements)
    assert not [e for e in elements if e.within(block) and "comes later" in e.text.lower()], (
        "the model map no longer says that moving tasks between tiers comes later"
    )
    task_list = required(elements, "task-tiers")
    assert task_list.within(section(elements)) and not task_list.within(block)


def save_button(elements: list[Element]) -> Element:
    form = tiers_form(elements)
    button = only(
        [e for e in elements if e.tag == "button" and e.within(form) and e.text == SAVE_TIERS],
        "Save tier models button",
    )
    assert (button.attrs.get("type") or "submit") == "submit", button.attrs
    return button


# --- the form ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "tiers", "models", "chosen"),
    [
        pytest.param(FAKE_KIND, TierMap(), FAKE_MODELS, FAKE_DEFAULTS, id="fake-defaults"),
        pytest.param(
            FAKE_KIND,
            TierMap(fast=FAKE_THOROUGH_MODEL),
            FAKE_MODELS,
            {"fast": FAKE_THOROUGH_MODEL, "thorough": FAKE_THOROUGH_MODEL},
            id="fake-stored-fast",
        ),
        pytest.param(
            EXISTING_KIND, TierMap(), CLAUDE_MODELS, CLAUDE_DEFAULTS, id="claude-defaults"
        ),
        pytest.param(
            EXISTING_KIND,
            TierMap(fast=CLAUDE_MOST_CAPABLE, thorough=CLAUDE_FAST_DEFAULT),
            CLAUDE_MODELS,
            {"fast": CLAUDE_MOST_CAPABLE, "thorough": CLAUDE_FAST_DEFAULT},
            id="claude-stored",
        ),
        pytest.param(None, TierMap(), CLAUDE_MODELS, CLAUDE_DEFAULTS, id="nothing-chosen"),
    ],
)
def test_tier_mapping_offers_provider_known_models(
    tmp_path: Path,
    bundled_cli: Path,
    kind: str | None,
    tiers: TierMap,
    models: tuple[ModelChoice, ...],
    chosen: dict[Tier, str],
) -> None:
    """Both selects list the active kind's ``known_models()``, the stored model (else the
    provider's default) selected; a Save button and no "comes later" line (the task list
    follows it); on the full page and the htmx page fragment, and never on the guide's provider
    step."""
    finished_guide(tmp_path, provider=AIProviderSettings(kind=kind, tiers=tiers))  # type: ignore[arg-type]
    with started(tmp_path) as run:
        assert run.app.state.wiring.known_models(kind) == models, "offered from known_models"
        version = settings_version(run)
        page = settings_page(run)
        fragment = run.client.get(SETTINGS, headers=run.htmx())
        assert fragment.status_code == 200, fragment.text[:300]
        step = run.client.get(PROVIDER_STEP)
        assert step.status_code == 200, step.text[:300]
        assert settings_version(run) == version, "GET saves nothing"

    for response in (page, fragment):
        elements = outline(response.text)
        assert_tier_selects(elements, models, chosen)
        save_button(elements)
        assert_task_tiers_follow_the_model_map(elements)

    guide = outline(step.text)
    assert [e for e in guide if e.attrs.get("id") == TIERS_ID] == []
    assert [e for e in guide if e.tag == "select"] == [], "no tier selects in the guide"
    assert SETTINGS_TIERS not in step.text
    assert SAVE_TIERS not in step.text


def test_tier_mapping_follows_provider_kind_change(tmp_path: Path, bundled_cli: Path) -> None:
    """Switching Claude to the fake in Settings re-renders the section with the fake's
    models in both selects (the map itself is re-checked by M1.21's tests)."""
    finished_guide(
        tmp_path,
        provider=AIProviderSettings(kind=EXISTING_KIND, tiers=TierMap(fast=CLAUDE_MOST_CAPABLE)),
    )
    with started(tmp_path) as run:
        before = outline(settings_page(run).text)
        assert_tier_selects(
            before,
            CLAUDE_MODELS,
            {"fast": CLAUDE_MOST_CAPABLE, "thorough": CLAUDE_THOROUGH_DEFAULT},
        )

        switched = post_settings(run, FAKE_KIND, "", htmx=True)
        assert switched.status_code == 200, switched.text[:300]
        assert_tier_selects(assert_section_fragment(switched), FAKE_MODELS, FAKE_DEFAULTS)
        assert stored(run).kind == FAKE_KIND

        shown = outline(settings_page(run).text)
        assert_tier_selects(shown, FAKE_MODELS, FAKE_DEFAULTS)
        for model in CLAUDE_MODELS:
            assert all(value != model.id for value, _ in options(shown, tier_select(shown, "fast")))


# --- save ----------------------------------------------------------------------------------


def test_tier_mapping_saved_and_used_by_resolution(tmp_path: Path, bundled_cli: Path) -> None:
    """A valid map is saved (only the tiers change), shown selected, answered 303 or by htmx
    with the section and a "saved" announcement, and used by model resolution."""
    original = AIProviderSettings(
        kind=API_KEY_KIND,
        saved_sign_ins=(API_KEY_KIND, TOKEN_KIND),
        task_tiers={"fictional_task": "thorough"},
    )
    claude_dir = tmp_path / "claude"
    finished_guide(claude_dir, provider=original)
    with started(claude_dir) as run:
        version = settings_version(run)

        # Plain request: 303 back to Settings, only the tiers changed.
        saved = post_tiers(run, {"fast": CLAUDE_MOST_CAPABLE, "thorough": CLAUDE_FAST_DEFAULT})
        assert_see_other(saved, SETTINGS)
        settings = stored(run)
        mapped = TierMap(fast=CLAUDE_MOST_CAPABLE, thorough=CLAUDE_FAST_DEFAULT)
        assert settings == original.model_copy(update={"tiers": mapped})
        assert settings_version(run) > version

        # Resolution reads the saved map, so does the connection test's model.
        assert resolve_model("fast", settings, CLAUDE_DEFAULTS) == CLAUDE_MOST_CAPABLE
        assert resolve_model("thorough", settings, CLAUDE_DEFAULTS) == CLAUDE_FAST_DEFAULT
        assert run.app.state.wiring.connection_model(settings) == CLAUDE_MOST_CAPABLE

        shown = outline(settings_page(run).text)
        assert_tier_selects(
            shown, CLAUDE_MODELS, {"fast": CLAUDE_MOST_CAPABLE, "thorough": CLAUDE_FAST_DEFAULT}
        )

        # htmx: 200 with the section, the new selection and a "saved" announcement.
        swapped = post_tiers(
            run, {"fast": CLAUDE_THOROUGH_DEFAULT, "thorough": CLAUDE_MOST_CAPABLE}, htmx=True
        )
        assert swapped.status_code == 200, swapped.text[:300]
        assert "location" not in swapped.headers
        elements = assert_section_fragment(swapped)
        assert_tier_selects(
            elements,
            CLAUDE_MODELS,
            {"fast": CLAUDE_THOROUGH_DEFAULT, "thorough": CLAUDE_MOST_CAPABLE},
        )
        assert "saved" in announced(swapped).lower(), announced(swapped)
        assert stored(run) == original.model_copy(
            update={"tiers": TierMap(fast=CLAUDE_THOROUGH_DEFAULT, thorough=CLAUDE_MOST_CAPABLE)}
        )
        assert live_statuses(run) == ALL_M1_DONE, "Settings never touches the guide"

    # The fake: the connection test asks the provider for the saved Fast model.
    fake_dir = tmp_path / "fake"
    finished_guide(fake_dir)
    with started(fake_dir) as run:
        saved = post_tiers(run, {"fast": FAKE_THOROUGH_MODEL, "thorough": FAKE_FAST_MODEL})
        assert_see_other(saved, SETTINGS)
        settings = stored(run)
        assert settings.tiers == TierMap(fast=FAKE_THOROUGH_MODEL, thorough=FAKE_FAST_MODEL)
        assert run.app.state.wiring.connection_model(settings) == FAKE_THOROUGH_MODEL

        tested = post_settings_test(run, FAKE_KIND, FAKE_CONNECTED)
        assert tested.status_code == 200, tested.text[:300]
        result = required(outline(tested.text), "connection-result").text
        assert result.startswith("Connected"), result
        assert FAKE_THOROUGH_MODEL in result, "the provider was asked for the saved Fast model"
        assert FAKE_FAST_MODEL not in result
        assert stored(run).tiers == settings.tiers, "testing keeps the map"


# --- refused -------------------------------------------------------------------------------


@pytest.mark.parametrize("htmx", [True, False], ids=["htmx", "plain"])
@pytest.mark.parametrize(
    ("kind", "form", "offending"),
    [
        pytest.param(
            EXISTING_KIND,
            {"fast": UNKNOWN_MODEL, "thorough": CLAUDE_THOROUGH_DEFAULT},
            "fast",
            id="unknown-id",
        ),
        pytest.param(
            EXISTING_KIND,
            {"fast": CLAUDE_FAST_DEFAULT, "thorough": FAKE_THOROUGH_MODEL},
            "thorough",
            id="fake-id-for-claude",
        ),
        pytest.param(
            FAKE_KIND,
            {"fast": CLAUDE_MOST_CAPABLE, "thorough": FAKE_THOROUGH_MODEL},
            "fast",
            id="claude-id-for-fake",
        ),
        pytest.param(
            EXISTING_KIND, {"thorough": CLAUDE_THOROUGH_DEFAULT}, "fast", id="missing-fast"
        ),
        pytest.param(
            EXISTING_KIND, {"fast": CLAUDE_FAST_DEFAULT}, "thorough", id="missing-thorough"
        ),
        pytest.param(
            EXISTING_KIND,
            {"fast": "", "thorough": CLAUDE_THOROUGH_DEFAULT},
            "fast",
            id="empty-fast",
        ),
        pytest.param(
            EXISTING_KIND,
            {"fast": CLAUDE_FAST_DEFAULT, "thorough": HOSTILE_MODEL},
            "thorough",
            id="markup",
        ),
    ],
)
def test_tier_mapping_rejects_unknown_model(
    tmp_path: Path,
    bundled_cli: Path,
    kind: str,
    form: dict[str, str],
    offending: Tier,
    htmx: bool,
) -> None:
    """A value the active kind doesn't offer, or a missing one: 422, nothing saved, the
    offending select marked invalid with a described error, the value never shown back."""
    original = AIProviderSettings(
        kind=kind,  # type: ignore[arg-type]
        tiers=TierMap(thorough=FAKE_FAST_MODEL if kind == FAKE_KIND else CLAUDE_MOST_CAPABLE),
    )
    finished_guide(tmp_path, provider=original)
    with started(tmp_path) as run:
        version = settings_version(run)
        response = post_tiers(run, form, htmx=htmx)
        assert response.status_code == 422, (response.status_code, response.text[:300])
        assert "location" not in response.headers
        assert "hx-redirect" not in response.headers
        assert settings_version(run) == version, "nothing saved"
        assert stored(run) == original
        assert live_statuses(run) == ALL_M1_DONE

    elements = assert_section_fragment(response) if htmx else outline(response.text)
    if not htmx:
        assert "<html" in response.text, "a plain refusal shows the full Settings page"
    field = tier_select(elements, offending)
    assert field.attrs.get("aria-invalid") == "true", field.attrs
    errors = described_by(elements, field)
    assert any(e.text for e in errors), "the error is tied to the select by aria-describedby"
    assert all(e.within(tiers_block(elements)) for e in errors)
    other: Tier = "thorough" if offending == "fast" else "fast"
    assert tier_select(elements, other).attrs.get("aria-invalid") != "true"

    rejected = form.get(offending, "")
    if rejected:
        assert rejected not in response.text, "the rejected value is never shown back"
    assert 'id="injected"' not in response.text


def test_tier_mapping_route_is_post_only_with_token_and_origin(tmp_path: Path) -> None:
    finished_guide(tmp_path)
    with started(tmp_path) as run:
        version = settings_version(run)
        form = {"fast": FAKE_THOROUGH_MODEL, "thorough": FAKE_FAST_MODEL}
        for headers in (run.with_token(), run.htmx(), run.action()):
            assert run.client.get(SETTINGS_TIERS, params=form, headers=headers).status_code == 405
        no_token = run.client.post(SETTINGS_TIERS, data=form, headers={"origin": BASE_URL})
        bad_origin = run.client.post(
            SETTINGS_TIERS, data=form, headers=run.action(origin="http://evil.example")
        )
        no_origin = run.client.post(SETTINGS_TIERS, data=form, headers=run.with_token())
        assert (no_token.status_code, bad_origin.status_code, no_origin.status_code) == (
            403,
            403,
            403,
        )
        assert settings_version(run) == version
        assert stored(run).tiers == TierMap()
