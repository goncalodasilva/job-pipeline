"""Settings > Usage budgets (M2.22; AC-M2.7, AC-M2.10; spec 3.7; ADR-076 items 4, 5, 8, 12;
ADR-079; detailed design "Settings and usage budgets").

The Usage budgets area page ``/settings/budgets`` (M9.2; was the Settings page) and its htmx
page fragment hold ``<section id="usage-budgets">`` labelled
by an ``<h2>`` "Usage budgets". Inside it, one block per service, ``#budget-<service>``, in the
order of ``SERVICE_NAMES`` (``fake`` only while fakes are allowed), names the service and holds
one form:

- ``POST /settings/usage/budgets`` (launch token and origin; POST only). Fields: hidden
  ``service`` and ``version`` (the usage-budgets setting version the page was rendered with),
  ``allowance``, ``first_warning``, ``second_warning``, ``block_at``, ``reset_day`` and the
  optional ``fast_cap`` and ``thorough_cap`` (empty means no cap), each with a visible label.
  The submit button ``name="action" value="save"`` saves; ``value="remove"`` (shown only while a
  budget is stored) removes the budget at once.
- ``#budget-<service>-status`` says "No limit" while the service has no budget, and the fields
  are then empty.
- The reset-day field is described (``aria-describedby``) by a note saying days 29-31 move to
  the month's last day.

A valid save or a removal answers 303 to ``/settings/budgets`` (a query or ``#fragment`` is
allowed), or for htmx 200 with a fragment holding the service's block and an ``announce`` trigger. A
refused save answers 422 (htmx: the fragment; plain: the full page), saves nothing, keeps what
was typed, and marks exactly the fields ``validate_budget`` names with ``aria-invalid="true"``
and an error element listed in the field's ``aria-describedby``. A stale ``version`` answers
409, changes nothing and shows ``VersionConflict.message`` with a "Reload" link to
``/settings/budgets`` (spec 2.11 and 3.5: "shown to the user as a reload prompt").

"Background tasks at once" is a form posting to ``POST /settings/usage/concurrency`` with
``max_concurrent`` (an input or a select) and a hidden ``version`` (the task-runner setting's);
1, 2 and 3 are stored in ``TaskRunnerSettings.max_concurrent``, anything else is refused (422)
with an error tied to the control.

GET writes nothing (spec 2.9 rule 12). A real migrated SQLite database in a temp folder, the
frozen clock, no network, no provider call; every value is fictional.
"""

import asyncio
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from httpx import Response

from app.domain.budgets import Allow, Budget, validate_budget
from app.domain.errors import UsageLimitsUnreadable, VersionConflict
from app.domain.settings import TASK_RUNNER, AIProviderSettings
from app.domain.usage import SERVICE_NAMES
from app.modules.storage import Database
from app.services.settings import SettingsService, Versioned
from app.services.usage import UsageService
from app.services.usage_settings import UsageSettingsView
from tests.conftest import ALLOW_FAKES_VARIABLE
from tests.fakes.clock import FrozenClock
from tests.fakes.sdk_install import install_fake_sdk
from tests.integration.test_setup_provider import (
    EXISTING_KIND,
    by_id,
    described_by,
    only,
    required,
)
from tests.integration.test_setup_provider_test import announced
from tests.integration.test_setup_redirect import (
    BASE_URL,
    START,
    Element,
    Running,
    assert_not_redirected,
    outline,
)
from tests.integration.test_setup_reopen_banner import finished_guide, started
from tests.integration.test_task_routes import database_state

pytestmark = pytest.mark.integration

SETTINGS = "/settings/budgets"  # M9.2: the Usage budgets area (was /settings)
BUDGETS_PATH = "/settings/usage/budgets"
CONCURRENCY_PATH = "/settings/usage/concurrency"
SECTION_ID = "usage-budgets"
SECTION_HEADING = "Usage budgets"
NO_LIMIT = "No limit"
CONCURRENCY_LABEL = "Background tasks at once"
CONCURRENCY_FIELD = "max_concurrent"

REAL_SERVICES = ("claude_subscription", "claude_api_key", "web_search")
FAKE_SERVICE = "fake"
ALL_SERVICES = (*REAL_SERVICES, FAKE_SERVICE)

REQUIRED_FIELDS = ("allowance", "first_warning", "second_warning", "block_at", "reset_day")
CAP_FIELDS = ("fast_cap", "thorough_cap")
FIELDS = (*REQUIRED_FIELDS, *CAP_FIELDS)
# Words each field's visible label must hold (case-insensitive); the rest of the copy is free.
LABEL_WORDS = {
    "allowance": "allowance",
    "first_warning": "first warning",
    "second_warning": "second warning",
    "block_at": "block",
    "reset_day": "reset day",
    "fast_cap": "fast",
    "thorough_cap": "thorough",
}
VALID = {
    "allowance": "100",
    "first_warning": "50",
    "second_warning": "80",
    "block_at": "100",  # block ≤ allowance: equal is allowed
    "reset_day": "15",
    "fast_cap": "",
    "thorough_cap": "",
}
VALID_BUDGET = Budget(
    allowance=100, first_warning=50, second_warning=80, block_at=100, reset_day=15
)
CAPPED = {**VALID, "reset_day": "31", "fast_cap": "40", "thorough_cap": "60"}
CAPPED_BUDGET = Budget(
    allowance=100,
    first_warning=50,
    second_warning=80,
    block_at=100,
    reset_day=31,
    fast_cap=40,
    thorough_cap=60,
)
HOSTILE = '"><b id="injected">fictional</b>'

# Copy the section pins (review of M2.22).
SAVED_LINE = "Saved."
REMOVED_LINE = "Removed. This service has no limit now."
CONCURRENCY_SAVED_LINE = "Saved. It applies the next time you start the app."
TYPED_NOT_SAVED = "What you typed here isn't saved."
BUDGET_NOT_REMOVED = "The budget wasn't removed."
CHOICE_NOT_SAVED = "What you chose here isn't saved."
UNREADABLE_NOTICE = (
    "Your saved budgets couldn't be read, so AI actions are paused. Save a budget again to "
    "fix this; you'll need to re-enter the others."
)
NOTICE_ID = "usage-budgets-notice"
ENTER_A_NUMBER = "Enter a number."
DIGITS_ONLY = "Enter a whole number, 1 or more, using digits only (for example 1000)."
ONE_OR_MORE = "Enter a whole number, 1 or more."
NOTHING_ENTERED = "Enter a monthly allowance to set a budget."
ORDER_HINT = (
    "The second warning must be higher than the first, and the blocking point higher than "
    "the second. The blocking point can equal the allowance."
)
BLOCK_HINT = "The app stops using this service here until the reset day."
CAP_HINT = "Leave empty for no cap."
# A stored value the app can't read (as in tests/integration/test_usage_service.py).
UNREADABLE = '{"budgets": {"claude_api_key": {"allowance": "ten"}}}'
UNREADABLE_VERSION = 3


# --- helpers -------------------------------------------------------------------------------


@pytest.fixture
def bundled_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The SDK looks installed with its bundled CLI (a placeholder that is never run), so a
    Claude kind's provider health is decided by its sign-in alone."""
    return install_fake_sdk(monkeypatch, tmp_path / "sdk", with_cli=True)


def usage(run: Running) -> UsageService:
    service: UsageService = run.app.state.usage
    return service


def budgets(run: Running) -> tuple[dict[str, Budget], int]:
    loaded = usage(run).load_budgets()
    return dict(loaded.value), loaded.version


def runner_setting(run: Running) -> Versioned[object]:
    db: Database = run.app.state.workspace.db
    loaded = SettingsService(db, FrozenClock(START)).load(TASK_RUNNER)
    return Versioned(loaded.value.max_concurrent, loaded.version)


def settings_page(run: Running, *, htmx: bool = False) -> Response:
    response = run.client.get(SETTINGS, headers=run.htmx() if htmx else {})
    assert response.status_code == 200, (response.status_code, response.text[:300])
    assert_not_redirected(response)
    return response


def usage_section(elements: list[Element]) -> Element:
    """The Usage budgets section, labelled by its ``<h2>``."""
    found = required(elements, SECTION_ID)
    assert found.tag == "section", found.tag
    heading = required(elements, str(found.attrs.get("aria-labelledby")))
    assert heading.tag == "h2", heading.tag
    assert heading.within(found)
    assert heading.text == SECTION_HEADING, heading.text
    return found


def budget_block(elements: list[Element], service: str) -> Element:
    """``#budget-<service>``: it names the service."""
    block = required(elements, f"budget-{service}")
    assert SERVICE_NAMES[service] in block.text, (service, block.text[:200])
    return block


def shown_services(elements: list[Element]) -> list[str]:
    """The services with a budget block, in page order."""
    ids = [str(e.attrs.get("id") or "") for e in elements]
    return [i.removeprefix("budget-") for i in ids if i.removeprefix("budget-") in SERVICE_NAMES]


def budget_form(elements: list[Element], service: str) -> Element:
    block = budget_block(elements, service)
    form = only(
        [e for e in elements if e.tag == "form" and e.within(block)], f"form in #budget-{service}"
    )
    assert form.attrs.get("action") == BUDGETS_PATH, form.attrs
    assert (form.attrs.get("method") or "").lower() == "post", form.attrs
    assert form.attrs.get("hx-post") == BUDGETS_PATH, form.attrs
    assert hidden(elements, form, "service") == service
    return form


def hidden(elements: list[Element], form: Element, name: str) -> str | None:
    field = only(
        [
            e
            for e in elements
            if e.tag == "input" and e.attrs.get("name") == name and e.within(form)
        ],
        f"hidden {name}",
    )
    assert field.attrs.get("type") == "hidden", field.attrs
    return field.attrs.get("value")


def labels_of(elements: list[Element], control: Element) -> list[str]:
    control_id = control.attrs.get("id")
    return [
        e.text
        for e in elements
        if e.tag == "label"
        and ((control_id and e.attrs.get("for") == control_id) or control.within(e))
    ]


def budget_field(elements: list[Element], service: str, name: str) -> Element:
    """The service's ``<input name=...>`` with its visible label."""
    form = budget_form(elements, service)
    field = only(
        [
            e
            for e in elements
            if e.tag == "input" and e.attrs.get("name") == name and e.within(form)
        ],
        f"{name} input for {service}",
    )
    assert field.attrs.get("type") != "hidden", name
    labels = labels_of(elements, field)
    assert any(LABEL_WORDS[name] in label.lower() for label in labels), (service, name, labels)
    return field


def shown_values(elements: list[Element], service: str) -> dict[str, str]:
    return {name: budget_field(elements, service, name).attrs.get("value") or "" for name in FIELDS}


def status_text(elements: list[Element], service: str) -> str:
    status = required(elements, f"budget-{service}-status")
    assert status.within(budget_block(elements, service))
    return status.text


def action_buttons(elements: list[Element], service: str, value: str) -> list[Element]:
    form = budget_form(elements, service)
    return [
        e
        for e in elements
        if e.tag == "button"
        and e.within(form)
        and e.attrs.get("name") == "action"
        and e.attrs.get("value") == value
    ]


def invalid_fields(elements: list[Element], service: str) -> set[str]:
    return {
        name
        for name in FIELDS
        if budget_field(elements, service, name).attrs.get("aria-invalid") == "true"
    }


def assert_no_limit(elements: list[Element], service: str) -> None:
    """AC-M2.10 on the page: "No limit", nothing shown as set, nothing to remove."""
    assert NO_LIMIT in status_text(elements, service), status_text(elements, service)
    assert shown_values(elements, service) == dict.fromkeys(FIELDS, ""), service
    assert invalid_fields(elements, service) == set(), service
    assert len(action_buttons(elements, service, "save")) == 1, service
    assert action_buttons(elements, service, "remove") == [], service


def assert_budget_shown(elements: list[Element], service: str, values: dict[str, str]) -> None:
    assert NO_LIMIT not in status_text(elements, service), status_text(elements, service)
    assert shown_values(elements, service) == values, service
    assert invalid_fields(elements, service) == set(), service
    assert len(action_buttons(elements, service, "save")) == 1, service
    assert len(action_buttons(elements, service, "remove")) == 1, service


def post_budget(
    run: Running,
    service: str,
    values: dict[str, str] | None = None,
    *,
    version: int | str | None = None,
    action: str = "save",
    htmx: bool = False,
) -> Response:
    """Submit the service's form; ``version`` defaults to the stored one."""
    if version is None:
        version = budgets(run)[1]
    form = {"service": service, "version": str(version), "action": action, **(values or {})}
    headers = run.action(**({"HX-Request": "true"} if htmx else {}))
    return run.client.post(BUDGETS_PATH, data=form, headers=headers)


def post_concurrency(
    run: Running, value: str, *, version: int | str | None = None, htmx: bool = False
) -> Response:
    if version is None:
        version = runner_setting(run).version
    form = {CONCURRENCY_FIELD: value, "version": str(version)}
    headers = run.action(**({"HX-Request": "true"} if htmx else {}))
    return run.client.post(CONCURRENCY_PATH, data=form, headers=headers)


def assert_back_to_settings(response: Response) -> None:
    """303 to the Settings page (a query or ``#fragment`` is allowed)."""
    assert response.status_code == 303, (response.status_code, response.text[:300])
    location = response.headers["location"]
    if location.startswith(BASE_URL):
        location = location[len(BASE_URL) :]
    assert location.startswith("/") and not location.startswith("//"), location
    assert urlsplit(location).path == SETTINGS, location
    assert "hx-redirect" not in response.headers


def assert_fragment(response: Response) -> list[Element]:
    assert "<html" not in response.text, "htmx gets a fragment, not the page"
    assert "location" not in response.headers
    assert "hx-redirect" not in response.headers
    assert "hx-push-url" not in response.headers
    return outline(response.text)


def assert_full_page(response: Response) -> list[Element]:
    assert "<html" in response.text, "a plain request gets the full Settings page"
    assert "location" not in response.headers
    return outline(response.text)


def concurrency_form(elements: list[Element]) -> Element:
    form = only(
        [e for e in elements if e.tag == "form" and e.attrs.get("action") == CONCURRENCY_PATH],
        "concurrency form",
    )
    assert (form.attrs.get("method") or "").lower() == "post", form.attrs
    assert form.attrs.get("hx-post") == CONCURRENCY_PATH, form.attrs
    return form


def concurrency_control(elements: list[Element]) -> Element:
    """The ``max_concurrent`` input or select, labelled "Background tasks at once"."""
    form = concurrency_form(elements)
    control = only(
        [
            e
            for e in elements
            if e.tag in ("input", "select")
            and e.attrs.get("name") == CONCURRENCY_FIELD
            and e.within(form)
        ],
        "max_concurrent control",
    )
    labels = labels_of(elements, control)
    assert any(CONCURRENCY_LABEL in label for label in labels), labels
    return control


def concurrency_shown(elements: list[Element]) -> str:
    control = concurrency_control(elements)
    if control.tag == "input":
        return control.attrs.get("value") or ""
    chosen = [
        str(e.attrs.get("value"))
        for e in elements
        if e.tag == "option" and e.within(control) and "selected" in e.attrs
    ]
    return only_value(chosen)


def only_value(values: list[str]) -> str:
    assert len(values) == 1, values
    return values[0]


def assert_reload_prompt(elements: list[Element], prompt_id: str, detail: str) -> None:
    """The version-conflict copy (spec 2.11), what it means for this form (``detail``) and a
    way to reload Settings without scripts, in one alert that takes focus after the swap."""
    prompt = only([e for e in elements if e.attrs.get("role") == "alert"], "alert")
    assert prompt.attrs.get("id") == prompt_id, prompt.attrs
    assert prompt.attrs.get("tabindex") == "-1", prompt.attrs
    assert "data-autofocus" in prompt.attrs, prompt.attrs
    inside = [e for e in elements if e.within(prompt)]
    assert any(e.text == VersionConflict.message for e in inside), "the conflict message"
    assert any(e.text == detail for e in inside), (detail, prompt.text)
    links = [
        e
        for e in inside
        if e.tag == "a"
        and "reload" in e.text.lower()
        and urlsplit(e.attrs.get("href") or "").path == SETTINGS
    ]
    assert links, "a Reload link back to /settings/budgets"
    assert only([e for e in elements if "data-autofocus" in e.attrs], "focus target") is prompt


# --- AC-M2.10: no budget by default ------------------------------------------------------------


def test_ac_m2_10_no_budget_by_default(tmp_path: Path) -> None:
    """Nothing is stored, every service says "No limit" with empty fields (page and htmx page
    fragment), and a service without a budget is never warned or blocked, however much it is
    used."""
    finished_guide(tmp_path)
    with started(tmp_path) as run:
        assert budgets(run) == ({}, 0)
        for _ in range(3):  # usage without a budget
            usage(run).reserve(FAKE_SERVICE, tier="fast", task_type="fictional_echo")

        page = settings_page(run)
        fragment = settings_page(run, htmx=True)
        for response in (page, fragment):
            elements = outline(response.text)
            section = usage_section(elements)
            assert shown_services(elements) == list(ALL_SERVICES)
            for service in ALL_SERVICES:
                assert budget_block(elements, service).within(section)
                assert_no_limit(elements, service)
                assert hidden(elements, budget_form(elements, service), "version") == "0"
        assert "<html" in page.text and "<html" not in fragment.text

        for service in ALL_SERVICES:
            for tier in ("fast", "thorough"):
                assert usage(run).preflight(service, tier=tier) == Allow(), (service, tier)
            assert usage(run).status(service).budget is None
        assert budgets(run) == ({}, 0), "showing the page stored nothing"


def test_fake_service_offered_only_when_fakes_allowed(
    tmp_path: Path, bundled_cli: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-076 item 4: without the fake gate there is no Fake budget, and a forged save for
    it (or for an unknown service) is refused and stores nothing."""
    monkeypatch.delenv(ALLOW_FAKES_VARIABLE)
    finished_guide(tmp_path, provider=AIProviderSettings(kind=EXISTING_KIND))
    with started(tmp_path) as run:
        elements = outline(settings_page(run).text)
        usage_section(elements)
        assert shown_services(elements) == list(REAL_SERVICES)
        assert by_id(elements, f"budget-{FAKE_SERVICE}") is None
        for service in REAL_SERVICES:
            assert_no_limit(elements, service)

        for service in (FAKE_SERVICE, "fictional_service", HOSTILE):
            for htmx in (True, False):
                forged = post_budget(run, service, VALID, htmx=htmx)
                assert forged.status_code == 404, (service, forged.status_code)
                assert "fictional_service" not in forged.text, "never shown back"
                assert 'id="injected"' not in forged.text
        assert budgets(run) == ({}, 0)


def test_unknown_service_refused_when_fakes_allowed(tmp_path: Path) -> None:
    finished_guide(tmp_path)
    with started(tmp_path) as run:
        for service in ("fictional_service", "", HOSTILE):
            forged = post_budget(run, service, VALID, htmx=True)
            assert forged.status_code == 404, (service, forged.status_code)
            assert "fictional_service" not in forged.text
            assert 'id="injected"' not in forged.text
        assert budgets(run) == ({}, 0)
        # The route exists: the same form for a real service is saved.
        assert post_budget(run, FAKE_SERVICE, VALID, htmx=True).status_code == 200
        assert budgets(run) == ({FAKE_SERVICE: VALID_BUDGET}, 1)


# --- save ------------------------------------------------------------------------------------


def test_budget_saved_per_service(tmp_path: Path) -> None:
    """A valid budget is stored for its service only and shown on the next GET; the others
    stay "No limit". Plain: 303 to Settings. htmx: 200 with the service's block and a "saved"
    announcement. Empty caps mean no cap."""
    finished_guide(tmp_path)
    with started(tmp_path) as run:
        saved = post_budget(run, "claude_api_key", CAPPED)
        assert_back_to_settings(saved)
        assert budgets(run) == ({"claude_api_key": CAPPED_BUDGET}, 1)

        elements = outline(settings_page(run).text)
        assert_budget_shown(elements, "claude_api_key", CAPPED)
        for service in ("claude_subscription", "web_search", FAKE_SERVICE):
            assert_no_limit(elements, service)
        for service in ALL_SERVICES:  # every form carries the new version
            assert hidden(elements, budget_form(elements, service), "version") == "1"

        swapped = post_budget(run, "web_search", VALID, htmx=True)
        assert swapped.status_code == 200, (swapped.status_code, swapped.text[:300])
        fragment = assert_fragment(swapped)
        assert_budget_shown(fragment, "web_search", VALID)
        assert "saved" in announced(swapped).lower(), announced(swapped)
        assert budgets(run) == ({"claude_api_key": CAPPED_BUDGET, "web_search": VALID_BUDGET}, 2)

        # Changing one budget leaves the other alone; clearing the caps removes them.
        changed = post_budget(run, "claude_api_key", {**VALID, "allowance": "250"})
        assert_back_to_settings(changed)
        expected = Budget(
            allowance=250, first_warning=50, second_warning=80, block_at=100, reset_day=15
        )
        assert budgets(run) == ({"claude_api_key": expected, "web_search": VALID_BUDGET}, 3)

        for response in (settings_page(run), settings_page(run, htmx=True)):
            elements = outline(response.text)
            assert_budget_shown(elements, "claude_api_key", {**VALID, "allowance": "250"})
            assert_budget_shown(elements, "web_search", VALID)
            assert_no_limit(elements, "claude_subscription")
            assert_no_limit(elements, FAKE_SERVICE)

        # The stored budget is the one the usage service applies.
        assert usage(run).status("web_search").budget == VALID_BUDGET
        assert usage(run).status(FAKE_SERVICE).budget is None


def test_reset_day_note_says_29_to_31_move_to_last_day(tmp_path: Path) -> None:
    """ADR-076 item 8: every reset-day field is described by a note about days 29-31, with
    or without a budget, and still when the field has an error."""
    finished_guide(tmp_path)
    with started(tmp_path) as run:
        assert_back_to_settings(post_budget(run, "web_search", CAPPED))
        shown = outline(settings_page(run).text)
        refused = post_budget(run, FAKE_SERVICE, {**VALID, "reset_day": "32"}, htmx=True)
        assert refused.status_code == 422, refused.status_code

    def notes(elements: list[Element], service: str) -> list[Element]:
        field = budget_field(elements, service, "reset_day")
        return [
            e
            for e in described_by(elements, field)
            if "29" in e.text and "31" in e.text and "last day" in e.text.lower()
        ]

    seen: set[str] = set()
    for service in ALL_SERVICES:
        (note,) = notes(shown, service)
        assert note.within(budget_block(shown, service)), "each form has a note of its own"
        seen.add(str(note.attrs.get("id")))
    assert len(seen) == len(ALL_SERVICES), "ids are unique across the four forms"

    errored = outline(refused.text)
    (note,) = notes(errored, FAKE_SERVICE)
    field = budget_field(errored, FAKE_SERVICE, "reset_day")
    others = [e for e in described_by(errored, field) if e is not note and e.text]
    assert others, "the error and the note are both tied to the field"


def test_every_control_id_is_unique(tmp_path: Path) -> None:
    """Four forms with the same field names: ids (labels, errors, notes) never repeat."""
    finished_guide(tmp_path)
    with started(tmp_path) as run:
        elements = outline(settings_page(run).text)
        usage_section(elements)
        refused = post_budget(run, FAKE_SERVICE, {**VALID, "reset_day": "0"})
        assert refused.status_code == 422
    for shown in (elements, outline(refused.text)):
        ids = [str(e.attrs["id"]) for e in shown if e.attrs.get("id")]
        repeated = sorted({i for i in ids if ids.count(i) > 1})
        assert repeated == [], repeated
        for service in shown_services(shown):
            for name in FIELDS:
                assert budget_field(shown, service, name).attrs.get("id"), (service, name)


# --- AC-M2.7: validation --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("changes", "offending"),
    [
        pytest.param(
            {"first_warning": "80", "second_warning": "80"},
            {"second_warning"},
            id="first_not_below_second",
        ),
        pytest.param(
            {"second_warning": "90", "block_at": "90"},
            {"block_at"},
            id="second_not_below_block",
        ),
        pytest.param({"block_at": "101"}, {"block_at"}, id="block_above_allowance"),
        pytest.param(
            {"fast_cap": "101", "thorough_cap": "150"},
            {"fast_cap", "thorough_cap"},
            id="cap_above_allowance",
        ),
        pytest.param({"reset_day": "32"}, {"reset_day"}, id="reset_day_out_of_range"),
    ],
)
def test_ac_m2_7_threshold_errors_tied_to_fields(
    tmp_path: Path, changes: dict[str, str], offending: set[str]
) -> None:
    """first < second < block ≤ allowance, caps ≤ allowance, reset day 1-31: a broken rule
    answers 422, saves nothing, keeps what was typed and marks the fields the domain names,
    each with an error listed in its ``aria-describedby``."""
    values = {**VALID, **changes}
    numbers = {name: int(text) if text else None for name, text in values.items()}
    domain = {error.field for error in validate_budget(**numbers)}
    assert domain == offending, "the page marks the fields the domain rule names"

    finished_guide(tmp_path)
    with started(tmp_path) as run:
        # Another service's budget is stored: it must survive untouched.
        assert_back_to_settings(post_budget(run, "claude_subscription", CAPPED))
        before = budgets(run)

        for htmx in (True, False):
            response = post_budget(run, FAKE_SERVICE, values, htmx=htmx)
            assert response.status_code == 422, (htmx, response.status_code, response.text[:300])
            assert budgets(run) == before, "nothing saved"
            elements = assert_fragment(response) if htmx else assert_full_page(response)

            assert invalid_fields(elements, FAKE_SERVICE) == offending
            block = budget_block(elements, FAKE_SERVICE)
            error_ids: list[str] = []
            for name in offending:
                field = budget_field(elements, FAKE_SERVICE, name)
                errors = [e for e in described_by(elements, field) if e.text]
                if name == "reset_day":  # its note is listed too
                    errors = [e for e in errors if "last day" not in e.text.lower()]
                assert errors, f"{name}: an error listed in aria-describedby"
                assert all(e.within(block) for e in errors), name
                error_ids += [str(e.attrs.get("id")) for e in errors]
            assert len(set(error_ids)) == len(error_ids), "each field has its own error"
            for name in set(FIELDS) - offending:
                field = budget_field(elements, FAKE_SERVICE, name)
                described = [e for e in described_by(elements, field) if e.attrs.get("id")]
                assert not any(str(e.attrs["id"]) in error_ids for e in described), name

            assert shown_values(elements, FAKE_SERVICE) == values, "what was typed is kept"
            assert NO_LIMIT in status_text(elements, FAKE_SERVICE), "still no budget"
            if not htmx:  # the other forms are untouched
                assert_budget_shown(elements, "claude_subscription", CAPPED)
                assert_no_limit(elements, "web_search")

        if "reset_day" in changes:  # the other edge of 1-31
            low = post_budget(run, FAKE_SERVICE, {**VALID, "reset_day": "0"}, htmx=True)
            assert low.status_code == 422, low.status_code
            assert invalid_fields(outline(low.text), FAKE_SERVICE) == {"reset_day"}
            assert budgets(run) == before

        # A refused form shows nothing afterwards, and the corrected form is saved.
        assert_no_limit(outline(settings_page(run).text), FAKE_SERVICE)
        assert post_budget(run, FAKE_SERVICE, VALID, htmx=True).status_code == 200
        assert budgets(run)[0][FAKE_SERVICE] == VALID_BUDGET


@pytest.mark.parametrize(
    ("day", "block"),
    [("1", "100"), ("29", "100"), ("30", "81"), ("31", "100")],
    ids=["day-1", "day-29", "day-30-block-just-above-second", "day-31"],
)
def test_ac_m2_7_boundary_values_are_accepted(tmp_path: Path, day: str, block: str) -> None:
    """The edges of the rule are valid: block = allowance, block = second + 1, days 1 and
    29-31 (stored as typed; the cycle moves them, not the form)."""
    values = {**VALID, "reset_day": day, "block_at": block, "fast_cap": "100"}
    finished_guide(tmp_path)
    with started(tmp_path) as run:
        assert_back_to_settings(post_budget(run, "web_search", values))
        stored = budgets(run)[0]["web_search"]
        assert (stored.reset_day, stored.block_at, stored.fast_cap) == (int(day), int(block), 100)
        assert_budget_shown(outline(settings_page(run).text), "web_search", values)


@pytest.mark.parametrize(
    "bad",
    ["", "ten", "10.5", "-5", "0", HOSTILE],
    ids=["empty", "word", "decimal", "negative", "zero", "markup"],
)
def test_non_number_refused_escaped_and_tied_to_field(tmp_path: Path, bad: str) -> None:
    """Whatever isn't a whole number above zero is refused on its own field, shown back
    escaped, and stores nothing."""
    finished_guide(tmp_path)
    with started(tmp_path) as run:
        for name in ("allowance", "thorough_cap"):
            if name == "thorough_cap" and bad == "":
                continue  # an empty cap means no cap
            values = {**VALID, name: bad}
            for htmx in (True, False):
                response = post_budget(run, FAKE_SERVICE, values, htmx=htmx)
                assert response.status_code == 422, (name, bad, response.status_code)
                assert 'id="injected"' not in response.text
                elements = outline(response.text)
                assert name in invalid_fields(elements, FAKE_SERVICE)
                field = budget_field(elements, FAKE_SERVICE, name)
                assert [e for e in described_by(elements, field) if e.text], name
                assert (field.attrs.get("value") or "") == bad, "kept as typed, escaped"
        assert budgets(run) == ({}, 0)


# --- remove ----------------------------------------------------------------------------------


@pytest.mark.parametrize("htmx", [False, True], ids=["plain", "htmx"])
def test_remove_budget_returns_to_no_limit(tmp_path: Path, htmx: bool) -> None:
    """Removing a budget needs only the service and the version: the service is back to
    "No limit", ``load_budgets()`` no longer has it, and the other budget stays."""
    finished_guide(tmp_path)
    with started(tmp_path) as run:
        assert_back_to_settings(post_budget(run, FAKE_SERVICE, CAPPED))
        assert_back_to_settings(post_budget(run, "web_search", VALID))
        assert_budget_shown(outline(settings_page(run).text), FAKE_SERVICE, CAPPED)

        removed = post_budget(run, FAKE_SERVICE, action="remove", htmx=htmx)
        if htmx:
            assert removed.status_code == 200, (removed.status_code, removed.text[:300])
            assert_no_limit(assert_fragment(removed), FAKE_SERVICE)
            assert announced(removed), "the removal is announced"
        else:
            assert_back_to_settings(removed)
        assert budgets(run) == ({"web_search": VALID_BUDGET}, 3)

        elements = outline(settings_page(run).text)
        assert_no_limit(elements, FAKE_SERVICE)
        assert_budget_shown(elements, "web_search", VALID)
        assert usage(run).status(FAKE_SERVICE).budget is None
        assert usage(run).preflight(FAKE_SERVICE, tier="thorough") == Allow()


def test_remove_ignores_the_typed_fields(tmp_path: Path) -> None:
    """Remove is not a save: invalid values in the fields don't stop it and are not stored."""
    finished_guide(tmp_path)
    with started(tmp_path) as run:
        assert_back_to_settings(post_budget(run, FAKE_SERVICE, VALID))
        removed = post_budget(
            run, FAKE_SERVICE, {**VALID, "block_at": "999", "reset_day": "40"}, action="remove"
        )
        assert_back_to_settings(removed)
        assert budgets(run) == ({}, 2)


# --- concurrency -----------------------------------------------------------------------------


def test_concurrency_setting_1_to_3(tmp_path: Path) -> None:
    """ "Background tasks at once" shows 1 by default (nothing stored), stores 1, 2 and 3 in
    ``TaskRunnerSettings.max_concurrent`` and refuses the rest with an error on the control."""
    finished_guide(tmp_path)
    with started(tmp_path) as run:
        assert runner_setting(run) == Versioned(1, 0)
        for response in (settings_page(run), settings_page(run, htmx=True)):
            elements = outline(response.text)
            assert concurrency_shown(elements) == "1"
            assert hidden(elements, concurrency_form(elements), "version") == "0"
            assert concurrency_control(elements).attrs.get("aria-invalid") != "true"
        assert runner_setting(run) == Versioned(1, 0), "GET stored nothing"

        version = 0
        for value, htmx in (("2", False), ("3", True), ("1", False), ("3", False)):
            saved = post_concurrency(run, value, htmx=htmx)
            version += 1
            if htmx:
                assert saved.status_code == 200, (saved.status_code, saved.text[:300])
                assert concurrency_shown(assert_fragment(saved)) == value
                assert "saved" in announced(saved).lower(), announced(saved)
            else:
                assert_back_to_settings(saved)
            assert runner_setting(run) == Versioned(int(value), version)
            shown = outline(settings_page(run).text)
            assert concurrency_shown(shown) == value
            assert hidden(shown, concurrency_form(shown), "version") == str(version)

        for bad in ("0", "4", "two", "", "2.5", "-1", HOSTILE):
            for htmx in (True, False):
                refused = post_concurrency(run, bad, htmx=htmx)
                assert refused.status_code == 422, (bad, htmx, refused.status_code)
                assert 'id="injected"' not in refused.text
                elements = assert_fragment(refused) if htmx else assert_full_page(refused)
                control = concurrency_control(elements)
                assert control.attrs.get("aria-invalid") == "true", (bad, control.attrs)
                errors = [e for e in described_by(elements, control) if e.text]
                assert errors, f"{bad!r}: an error listed in aria-describedby"
                assert runner_setting(run) == Versioned(3, version), "nothing saved"

        assert budgets(run) == ({}, 0), "the budgets are another setting"
        assert concurrency_shown(outline(settings_page(run).text)) == "3"


# --- stale version -----------------------------------------------------------------------------


def test_stale_version_shows_reload_prompt(tmp_path: Path) -> None:
    """A save or a removal carrying a version older than the stored one changes nothing and
    answers 409 with the reload prompt (htmx: a fragment; plain: the page)."""
    finished_guide(tmp_path)
    with started(tmp_path) as run:
        stale = budgets(run)[1]
        assert_back_to_settings(post_budget(run, FAKE_SERVICE, VALID, version=stale))
        current = ({FAKE_SERVICE: VALID_BUDGET}, 1)
        assert budgets(run) == current

        attempts = (
            ("claude_api_key", CAPPED, "save"),  # another service: one setting, one version
            (FAKE_SERVICE, {**VALID, "allowance": "500"}, "save"),
            (FAKE_SERVICE, None, "remove"),
        )
        for service, values, action in attempts:
            for htmx in (True, False):
                response = post_budget(
                    run, service, values, version=stale, action=action, htmx=htmx
                )
                assert response.status_code == 409, (action, htmx, response.status_code)
                assert budgets(run) == current, "nothing changed"
                elements = assert_fragment(response) if htmx else assert_full_page(response)
                detail = BUDGET_NOT_REMOVED if action == "remove" else TYPED_NOT_SAVED
                assert_reload_prompt(elements, f"budget-{service}-conflict", detail)
                assert "hx-trigger" not in response.headers

        # A version that was never issued, or isn't a number, is refused too.
        for version in ("7", "", "one", "-1", HOSTILE):
            response = post_budget(run, FAKE_SERVICE, CAPPED, version=version, htmx=True)
            assert response.status_code == 409, (version, response.status_code)
            assert 'id="injected"' not in response.text
            assert budgets(run) == current

        # After a reload the form carries the stored version and saves.
        shown = outline(settings_page(run).text)
        fresh = hidden(shown, budget_form(shown, "claude_api_key"), "version")
        assert fresh == "1"
        assert_back_to_settings(post_budget(run, "claude_api_key", CAPPED, version=fresh))
        assert budgets(run) == ({FAKE_SERVICE: VALID_BUDGET, "claude_api_key": CAPPED_BUDGET}, 2)


def test_stale_version_concurrency_shows_reload_prompt(tmp_path: Path) -> None:
    finished_guide(tmp_path)
    with started(tmp_path) as run:
        stale = runner_setting(run).version
        assert_back_to_settings(post_concurrency(run, "2", version=stale))
        assert runner_setting(run) == Versioned(2, 1)

        for htmx in (True, False):
            response = post_concurrency(run, "3", version=stale, htmx=htmx)
            assert response.status_code == 409, (htmx, response.status_code)
            assert runner_setting(run) == Versioned(2, 1), "nothing changed"
            elements = assert_fragment(response) if htmx else assert_full_page(response)
            assert_reload_prompt(elements, "usage-concurrency-conflict", CHOICE_NOT_SAVED)
            assert concurrency_shown(elements) == "2", "the select shows what is saved"

        # The two settings are versioned apart: saving a budget doesn't stale this form.
        assert_back_to_settings(post_budget(run, FAKE_SERVICE, VALID))
        assert_back_to_settings(post_concurrency(run, "3", version=1))
        assert runner_setting(run) == Versioned(3, 2)


# --- no side effects ---------------------------------------------------------------------------


def test_settings_usage_get_has_no_side_effects(tmp_path: Path) -> None:
    """Spec 2.9 rule 12: showing the section (with and without budgets, after usage) changes
    no row (settings, usage records, budget notices) and creates no file."""
    finished_guide(tmp_path)
    with started(tmp_path) as run:
        data_dir = run.app.state.workspace.db.path.parent

        def look() -> None:
            for _ in range(2):
                for htmx in (False, True):
                    elements = outline(settings_page(run, htmx=htmx).text)
                    usage_section(elements)
                    assert shown_services(elements) == list(ALL_SERVICES)
                    concurrency_control(elements)
                head = run.client.head(SETTINGS)
                assert head.status_code == 200, head.status_code

        empty = database_state(data_dir)
        look()
        assert database_state(data_dir) == empty, "no budgets: nothing written"
        assert budgets(run) == ({}, 0)
        assert runner_setting(run) == Versioned(1, 0)

        # A budget near its first warning, with usage counted against it.
        near = Budget(allowance=10, first_warning=2, second_warning=3, block_at=4, reset_day=31)
        usage(run).save_budget(FAKE_SERVICE, near, expected_version=0)
        for _ in range(3):
            usage(run).reserve(FAKE_SERVICE, tier="fast", task_type="fictional_echo")
        before = database_state(data_dir)
        assert "fictional_echo" in before[0], "the dump holds the usage rows"
        look()
        assert database_state(data_dir) == before, "budgets and usage: nothing written"
        assert usage(run).status(FAKE_SERVICE).shown_thresholds == frozenset()


# --- token and origin ----------------------------------------------------------------------------


def test_usage_routes_need_token_and_origin(tmp_path: Path) -> None:
    """Both routes are POST only and guarded like every non-page route: no token, a foreign
    origin or no origin is 403 and saves nothing; with both, the same form is saved."""
    finished_guide(tmp_path)
    with started(tmp_path) as run:
        budget_form_data = {"service": FAKE_SERVICE, "version": "0", "action": "save", **VALID}
        forms = {
            BUDGETS_PATH: budget_form_data,
            CONCURRENCY_PATH: {CONCURRENCY_FIELD: "2", "version": "0"},
        }
        for path, form in forms.items():
            no_token = run.client.post(path, data=form, headers={"origin": BASE_URL})
            bad_origin = run.client.post(
                path, data=form, headers=run.action(origin="http://evil.example")
            )
            no_origin = run.client.post(path, data=form, headers=run.with_token())
            wrong_token = run.client.post(
                path, data=form, headers={**run.action(), "X-JP-Token": "fictional-token"}
            )
            statuses = [r.status_code for r in (no_token, bad_origin, no_origin, wrong_token)]
            assert statuses == [403, 403, 403, 403], (path, statuses)
            for headers in (run.with_token(), run.htmx(), run.action()):
                got = run.client.get(path, params=form, headers=headers)
                assert got.status_code == 405, (path, got.status_code)
        assert budgets(run) == ({}, 0)
        assert runner_setting(run) == Versioned(1, 0)

        # The routes exist: with the token and the origin the same forms are saved.
        for path, form in forms.items():
            saved = run.client.post(path, data=form, headers=run.action())
            assert_back_to_settings(saved)
        assert budgets(run) == ({FAKE_SERVICE: VALID_BUDGET}, 1)
        assert runner_setting(run) == Versioned(2, 1)


# --- review of M2.22 -----------------------------------------------------------------------------


def oob_versions(elements: list[Element]) -> dict[str, str | None]:
    """The hidden version inputs an answer swaps out of band, by service."""
    found: dict[str, str | None] = {}
    for e in elements:
        if e.tag != "input" or e.attrs.get("name") != "version" or "hx-swap-oob" not in e.attrs:
            continue
        assert e.attrs.get("hx-swap-oob") == "true", e.attrs
        assert e.attrs.get("type") == "hidden", e.attrs
        element_id = str(e.attrs.get("id"))
        service = element_id.removeprefix("budget-").removesuffix("-version")
        assert element_id == f"budget-{service}-version", element_id
        assert service not in found, service
        found[service] = e.attrs.get("value")
    return found


def kept_version(elements: list[Element], service: str) -> str:
    version = hidden(elements, budget_form(elements, service), "version")
    assert version is not None
    return version


def error_text(elements: list[Element], service: str, name: str) -> str:
    """The one error the field is described by (its hints and notes are not errors)."""
    field = budget_field(elements, service, name)
    errors = [e for e in described_by(elements, field) if str(e.attrs.get("id")).endswith("-error")]
    return only(errors, f"error for {name}").text


def done_line(elements: list[Element], element_id: str) -> str | None:
    line = by_id(elements, element_id)
    return None if line is None else line.text


def position(elements: list[Element], element: Element) -> int:
    return next(i for i, e in enumerate(elements) if e is element)


def store_unreadable_budgets(run: Running) -> None:
    db: Database = run.app.state.workspace.db
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO settings (key, value, created_at, updated_at, version) "
            "VALUES ('usage_budgets', ?, ?, ?, ?)",
            (UNREADABLE, START.isoformat(), START.isoformat(), UNREADABLE_VERSION),
        )


def test_second_budget_save_after_a_conflict_still_conflicts(tmp_path: Path) -> None:
    """A refused form keeps the version it was sent with (409 and 422, htmx and plain), so the
    tab is told to reload until it does; nothing is refreshed out of band."""
    finished_guide(tmp_path)
    with started(tmp_path) as run:
        stale = budgets(run)[1]
        assert_back_to_settings(post_budget(run, FAKE_SERVICE, VALID, version=stale))
        current = ({FAKE_SERVICE: VALID_BUDGET}, 1)
        changed = {**VALID, "allowance": "500"}

        attempts = (
            (FAKE_SERVICE, changed, "save", 409),
            (FAKE_SERVICE, None, "remove", 409),
            ("web_search", CAPPED, "save", 409),
            (FAKE_SERVICE, {**VALID, "block_at": "101"}, "save", 422),  # refused before stale
        )
        for service, values, action, status in attempts:
            for htmx in (True, False):
                first = post_budget(run, service, values, version=stale, action=action, htmx=htmx)
                assert first.status_code == status, (action, htmx, first.status_code)
                elements = outline(first.text)
                kept = kept_version(elements, service)
                assert kept == str(stale), "the version the form was sent with"
                assert oob_versions(elements) == {}, "no version is refreshed"

                again = post_budget(run, service, changed, version=kept, htmx=htmx)
                assert again.status_code == 409, (action, htmx, again.status_code)
                assert kept_version(outline(again.text), service) == str(stale)
                assert budgets(run) == current, "nothing changed"

        # A version that isn't a number is never shown back, and conflicts again.
        for version in ("7", "", "one", "-1", HOSTILE):
            first = post_budget(run, FAKE_SERVICE, changed, version=version, htmx=True)
            assert first.status_code == 409, (version, first.status_code)
            kept = kept_version(outline(first.text), FAKE_SERVICE)
            assert kept in ("7", "-1", ""), kept
            again = post_budget(run, FAKE_SERVICE, changed, version=kept, htmx=True)
            assert again.status_code == 409, (version, again.status_code)
        assert budgets(run) == current

        # A plain 409 is a new page: only the refused form keeps its version.
        plain = outline(post_budget(run, FAKE_SERVICE, changed, version=stale).text)
        assert kept_version(plain, FAKE_SERVICE) == "0"
        assert kept_version(plain, "web_search") == "1"
        assert shown_values(plain, FAKE_SERVICE) == changed, "what was typed is kept"


def test_second_concurrency_save_after_a_conflict_still_conflicts(tmp_path: Path) -> None:
    finished_guide(tmp_path)
    with started(tmp_path) as run:
        stale = runner_setting(run).version
        assert_back_to_settings(post_concurrency(run, "2", version=stale))

        for value, status in (("3", 409), ("4", 422)):
            for htmx in (True, False):
                first = post_concurrency(run, value, version=stale, htmx=htmx)
                assert first.status_code == status, (value, htmx, first.status_code)
                elements = outline(first.text)
                kept = hidden(elements, concurrency_form(elements), "version")
                assert kept == str(stale), "the version the form was sent with"

                again = post_concurrency(run, "3", version=str(kept), htmx=htmx)
                assert again.status_code == 409, (value, htmx, again.status_code)
                shown = outline(again.text)
                assert hidden(shown, concurrency_form(shown), "version") == str(stale)
                assert runner_setting(run) == Versioned(2, 1), "nothing changed"

        for version in ("7", "", "one", HOSTILE):
            first = post_concurrency(run, "3", version=version, htmx=True)
            assert first.status_code == 409, (version, first.status_code)
            assert 'id="injected"' not in first.text
            elements = outline(first.text)
            kept = hidden(elements, concurrency_form(elements), "version")
            assert kept in ("7", ""), kept
        assert runner_setting(run) == Versioned(2, 1)


def test_saved_and_removed_are_confirmed_in_the_block(tmp_path: Path) -> None:
    """The htmx answer to a save or a removal says so in the block, not only in the live
    region; a page load, a refused form and a stale one never do."""
    finished_guide(tmp_path)
    with started(tmp_path) as run:
        done_ids = [f"budget-{s}-done" for s in ALL_SERVICES] + ["usage-concurrency-done"]

        def assert_nothing_confirmed(elements: list[Element]) -> None:
            assert [i for i in done_ids if by_id(elements, i) is not None] == []

        saved = post_budget(run, "web_search", VALID, htmx=True)
        fragment = assert_fragment(saved)
        line = required(fragment, "budget-web_search-done")
        assert line.text == SAVED_LINE
        assert line.within(budget_block(fragment, "web_search"))
        assert announced(saved) == f"Budget for {SERVICE_NAMES['web_search']} saved."
        for htmx in (False, True):
            assert_nothing_confirmed(outline(settings_page(run, htmx=htmx).text))

        refused = post_budget(run, "web_search", {**VALID, "reset_day": "0"}, htmx=True)
        assert refused.status_code == 422
        assert_nothing_confirmed(outline(refused.text))
        stale = post_budget(run, "web_search", VALID, version=0, htmx=True)
        assert stale.status_code == 409
        assert_nothing_confirmed(outline(stale.text))

        removed = post_budget(run, "web_search", action="remove", htmx=True)
        fragment = assert_fragment(removed)
        assert done_line(fragment, "budget-web_search-done") == REMOVED_LINE
        assert NO_LIMIT in status_text(fragment, "web_search")

        chosen = post_concurrency(run, "2", htmx=True)
        fragment = assert_fragment(chosen)
        assert done_line(fragment, "usage-concurrency-done") == CONCURRENCY_SAVED_LINE
        refused = post_concurrency(run, "4", htmx=True)
        assert refused.status_code == 422
        assert_nothing_confirmed(outline(refused.text))
        for htmx in (False, True):
            assert_nothing_confirmed(outline(settings_page(run, htmx=htmx).text))


def test_htmx_save_refreshes_the_other_forms_version_out_of_band(tmp_path: Path) -> None:
    """One version for every budget (ADR-079): after an htmx save or removal the answer holds
    one out-of-band hidden version per other offered service, with the new version and the
    id of that form's own field. A refused or stale form refreshes none."""
    finished_guide(tmp_path)
    with started(tmp_path) as run:
        page = outline(settings_page(run).text)
        for service in ALL_SERVICES:  # the ids the answers aim at
            field = required(page, f"budget-{service}-version")
            assert field.within(budget_form(page, service))
            assert field.attrs.get("name") == "version"
        assert oob_versions(page) == {}, "a page load swaps nothing out of band"

        saved = post_budget(run, "web_search", VALID, htmx=True)
        assert saved.status_code == 200
        others = [s for s in ALL_SERVICES if s != "web_search"]
        assert oob_versions(outline(saved.text)) == dict.fromkeys(others, "1")
        assert kept_version(outline(saved.text), "web_search") == "1"

        refused = post_budget(run, FAKE_SERVICE, {**VALID, "reset_day": "0"}, htmx=True)
        assert refused.status_code == 422
        assert oob_versions(outline(refused.text)) == {}
        stale = post_budget(run, FAKE_SERVICE, VALID, version=0, htmx=True)
        assert stale.status_code == 409
        assert oob_versions(outline(stale.text)) == {}

        removed = post_budget(run, "web_search", action="remove", htmx=True)
        assert removed.status_code == 200
        assert oob_versions(outline(removed.text)) == dict.fromkeys(others, "2")
        assert budgets(run) == ({}, 2)


def test_fake_service_gets_no_out_of_band_version_without_fakes(
    tmp_path: Path, bundled_cli: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(ALLOW_FAKES_VARIABLE)
    finished_guide(tmp_path, provider=AIProviderSettings(kind=EXISTING_KIND))
    with started(tmp_path) as run:
        saved = post_budget(run, "web_search", VALID, htmx=True)
        assert saved.status_code == 200
        assert oob_versions(outline(saved.text)) == {
            "claude_subscription": "1",
            "claude_api_key": "1",
        }


def test_unreadable_stored_budgets_are_shown_and_cleared_by_a_save(tmp_path: Path) -> None:
    """ADR-079: stored budgets the app can't read fail closed, so the section says so (never
    "No limit") until a budget is saved again; that save replaces them."""
    finished_guide(tmp_path)
    with started(tmp_path) as run:
        clean = outline(settings_page(run).text)
        assert required(clean, NOTICE_ID).text == "", "nothing to say while they can be read"

        store_unreadable_budgets(run)
        assert usage(run).status(FAKE_SERVICE).budgets_unreadable is True
        with pytest.raises(UsageLimitsUnreadable):
            usage(run).preflight(FAKE_SERVICE, tier="fast")

        for htmx in (False, True):
            elements = outline(settings_page(run, htmx=htmx).text)
            notice = required(elements, NOTICE_ID)
            assert notice.within(usage_section(elements))
            assert notice.text == UNREADABLE_NOTICE
            first_block = budget_block(elements, ALL_SERVICES[0])
            assert position(elements, notice) < position(elements, first_block), "above the forms"
            for service in ALL_SERVICES:
                assert NO_LIMIT not in status_text(elements, service), service
                assert status_text(elements, service), service
                assert shown_values(elements, service) == dict.fromkeys(FIELDS, "")
                assert action_buttons(elements, service, "remove") == []
                assert kept_version(elements, service) == str(UNREADABLE_VERSION)
            assert "ten" not in notice.text, "the stored value is never shown"
        assert usage(run).status(FAKE_SERVICE).budgets_unreadable is True, "GET fixed nothing"

        # A refused save leaves it as it is.
        refused = post_budget(
            run, FAKE_SERVICE, {**VALID, "reset_day": "0"}, version=UNREADABLE_VERSION, htmx=True
        )
        assert refused.status_code == 422
        assert NO_LIMIT not in status_text(outline(refused.text), FAKE_SERVICE)
        assert usage(run).status(FAKE_SERVICE).budgets_unreadable is True

        saved = post_budget(run, FAKE_SERVICE, VALID, version=UNREADABLE_VERSION, htmx=True)
        assert saved.status_code == 200, (saved.status_code, saved.text[:300])
        assert budgets(run) == ({FAKE_SERVICE: VALID_BUDGET}, UNREADABLE_VERSION + 1)
        assert usage(run).status(FAKE_SERVICE).budgets_unreadable is False
        assert usage(run).preflight("web_search", tier="fast") == Allow()

        # The answer takes the notice away and corrects the other blocks' status lines.
        fragment = assert_fragment(saved)
        assert_budget_shown(fragment, FAKE_SERVICE, VALID)
        notice = required(fragment, NOTICE_ID)
        assert notice.attrs.get("hx-swap-oob") == "true" and notice.text == ""
        for service in REAL_SERVICES:
            status = required(fragment, f"budget-{service}-status")
            assert status.attrs.get("hx-swap-oob") == "true", status.attrs
            assert status.text == NO_LIMIT

        elements = outline(settings_page(run).text)
        assert required(elements, NOTICE_ID).text == ""
        assert_budget_shown(elements, FAKE_SERVICE, VALID)
        for service in REAL_SERVICES:
            assert_no_limit(elements, service)


def test_unreadable_stored_budgets_cleared_by_a_plain_save(tmp_path: Path) -> None:
    finished_guide(tmp_path)
    with started(tmp_path) as run:
        store_unreadable_budgets(run)
        saved = post_budget(run, "web_search", CAPPED, version=UNREADABLE_VERSION)
        assert_back_to_settings(saved)
        assert budgets(run) == ({"web_search": CAPPED_BUDGET}, UNREADABLE_VERSION + 1)
        elements = outline(settings_page(run).text)
        assert required(elements, NOTICE_ID).text == ""
        assert_no_limit(elements, FAKE_SERVICE)


@pytest.mark.parametrize("htmx", [True, False], ids=["htmx", "plain"])
def test_all_empty_save_is_refused_and_keeps_the_stored_budget(tmp_path: Path, htmx: bool) -> None:
    """Emptying the form doesn't remove a budget (Remove does): 422 with one error, on the
    allowance, and the stored budget stays."""
    finished_guide(tmp_path)
    with started(tmp_path) as run:
        assert_back_to_settings(post_budget(run, FAKE_SERVICE, CAPPED))
        before = budgets(run)
        assert before == ({FAKE_SERVICE: CAPPED_BUDGET}, 1)

        empty = dict.fromkeys(FIELDS, "")
        for values in (empty, dict.fromkeys(FIELDS, "   "), {}):
            response = post_budget(run, FAKE_SERVICE, values, htmx=htmx)
            assert response.status_code == 422, (values, response.status_code)
            elements = assert_fragment(response) if htmx else assert_full_page(response)
            assert invalid_fields(elements, FAKE_SERVICE) == {"allowance"}
            assert error_text(elements, FAKE_SERVICE, "allowance") == NOTHING_ENTERED
            assert budgets(run) == before, "the stored budget survives"
            assert NO_LIMIT not in status_text(elements, FAKE_SERVICE)
            assert len(action_buttons(elements, FAKE_SERVICE, "remove")) == 1

        assert_budget_shown(outline(settings_page(run).text), FAKE_SERVICE, CAPPED)


def test_usage_is_read_only_when_shown_and_never_on_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An htmx answer without the section (the Providers forms) doesn't read the budgets;
    whatever shows the section reads them once, in a worker thread."""
    finished_guide(tmp_path)
    with started(tmp_path) as run:
        settings = run.app.state.usage_settings
        real_view = settings.view
        on_loop: list[bool] = []

        def view() -> UsageSettingsView:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                on_loop.append(False)
            else:
                on_loop.append(True)
            shown: UsageSettingsView = real_view()
            return shown

        monkeypatch.setattr(settings, "view", view)

        def reads(response: Response) -> list[bool]:
            assert response.status_code in (200, 409, 422), response.status_code
            seen = list(on_loop)
            on_loop.clear()
            return seen

        provider_forms = (
            ("/settings/providers", {"kind": "fictional-kind"}),
            ("/settings/tiers", {"fast": "fictional-model", "thorough": "fictional-model"}),
            ("/settings/providers/test", {"kind": "fictional-kind"}),
        )
        for path, form in provider_forms:
            fragment = run.client.post(
                path, data=form, headers=run.action(**{"HX-Request": "true"})
            )
            assert by_id(outline(fragment.text), SECTION_ID) is None, path
            assert reads(fragment) == [], f"{path}: the section isn't in this answer"

            # M9.2: the Providers forms' full-page answer is the Providers and AI page, which
            # no longer holds Usage budgets, so it neither shows the section nor reads it.
            page = run.client.post(path, data=form, headers=run.action())
            assert by_id(outline(page.text), SECTION_ID) is None, path
            assert reads(page) == [], f"{path}: the section isn't on this page"

        assert reads(settings_page(run)) == [False]
        assert reads(settings_page(run, htmx=True)) == [False]
        restored = run.client.get(
            SETTINGS, headers=run.htmx(**{"HX-History-Restore-Request": "true"})
        )
        usage_section(outline(restored.text))
        assert reads(restored) == [False], "a history restore gets the full page"

        assert reads(post_budget(run, FAKE_SERVICE, VALID, htmx=True)) == [False]
        assert reads(post_budget(run, FAKE_SERVICE, {**VALID, "reset_day": "0"})) == [False]
        assert reads(post_budget(run, FAKE_SERVICE, VALID, version=0, htmx=True)) == [False]
        assert reads(post_concurrency(run, "2", htmx=True)) == [False]
        assert reads(post_concurrency(run, "4")) == [False]


def test_refused_save_is_announced_with_the_number_of_fields(tmp_path: Path) -> None:
    """htmx only: fixed copy, the service's display name and a count; nothing typed."""
    finished_guide(tmp_path)
    with started(tmp_path) as run:
        name = SERVICE_NAMES[FAKE_SERVICE]
        one = post_budget(run, FAKE_SERVICE, {**VALID, "allowance": HOSTILE}, htmx=True)
        assert one.status_code == 422
        assert announced(one) == f"Budget for {name} not saved. 1 field needs a change."

        three = post_budget(
            run,
            FAKE_SERVICE,
            {**VALID, "block_at": "101", "reset_day": "0", "fast_cap": "x"},
            htmx=True,
        )
        assert three.status_code == 422
        assert announced(three) == f"Budget for {name} not saved. 3 fields need a change."

        plain = post_budget(run, FAKE_SERVICE, {**VALID, "block_at": "101"})
        assert plain.status_code == 422
        assert "hx-trigger" not in plain.headers

        chosen = post_concurrency(run, HOSTILE, htmx=True)
        assert chosen.status_code == 422
        assert announced(chosen) == f"{CONCURRENCY_LABEL} not saved. 1 field needs a change."
        assert "hx-trigger" not in post_concurrency(run, "4").headers


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("1", "Saved. Up to 1 background task from the next start."),
        ("2", "Saved. Up to 2 background tasks from the next start."),
        ("3", "Saved. Up to 3 background tasks from the next start."),
    ],
)
def test_concurrency_announcement_says_when_it_applies(
    tmp_path: Path, value: str, message: str
) -> None:
    finished_guide(tmp_path)
    with started(tmp_path) as run:
        saved = post_concurrency(run, value, htmx=True)
        assert saved.status_code == 200
        assert announced(saved) == message


@pytest.mark.parametrize(
    ("changes", "name", "message"),
    [
        ({"allowance": ""}, "allowance", ENTER_A_NUMBER),
        ({"first_warning": "  "}, "first_warning", ENTER_A_NUMBER),
        ({"block_at": ""}, "block_at", ENTER_A_NUMBER),
        ({"allowance": "ten"}, "allowance", DIGITS_ONLY),
        ({"allowance": "10.5"}, "allowance", DIGITS_ONLY),
        ({"allowance": "1,000"}, "allowance", DIGITS_ONLY),
        ({"second_warning": "1" * 13}, "second_warning", DIGITS_ONLY),
        ({"thorough_cap": HOSTILE}, "thorough_cap", DIGITS_ONLY),
        ({"allowance": "0"}, "allowance", ONE_OR_MORE),
        ({"fast_cap": "-5"}, "fast_cap", ONE_OR_MORE),
        (
            {"second_warning": "50"},
            "second_warning",
            "Enter a number higher than the first warning (50).",
        ),
        (
            {"block_at": "80"},
            "block_at",
            "Enter a number higher than the second warning (80).",
        ),
        (
            {"block_at": "101"},
            "block_at",
            "Enter a number no higher than the monthly allowance (100).",
        ),
        (
            {"thorough_cap": "101"},
            "thorough_cap",
            "Enter a number no higher than the monthly allowance (100).",
        ),
        ({"reset_day": "32"}, "reset_day", "Enter a day from 1 to 31."),
        ({"reset_day": ""}, "reset_day", "Enter a day from 1 to 31."),
    ],
)
def test_field_errors_say_what_to_enter(
    tmp_path: Path, changes: dict[str, str], name: str, message: str
) -> None:
    """Each refused field has one error naming what to enter; an ordering error names the
    limit it is compared with."""
    finished_guide(tmp_path)
    with started(tmp_path) as run:
        for htmx in (True, False):
            response = post_budget(run, FAKE_SERVICE, {**VALID, **changes}, htmx=htmx)
            assert response.status_code == 422, response.status_code
            elements = outline(response.text)
            assert invalid_fields(elements, FAKE_SERVICE) == {name}
            assert error_text(elements, FAKE_SERVICE, name) == message
            assert 'id="injected"' not in response.text
        assert budgets(run) == ({}, 0)


def test_hints_labels_and_buttons(tmp_path: Path) -> None:
    """The ordering rule is said once under the intro; the blocking point and the caps have a
    hint tied to the field; the warnings name their unit; Save is the primary button."""
    finished_guide(tmp_path)
    with started(tmp_path) as run:
        assert_back_to_settings(post_budget(run, FAKE_SERVICE, VALID))
        shown = outline(settings_page(run).text)
        refused = outline(
            post_budget(
                run, "web_search", {**VALID, "block_at": "101", "fast_cap": "x"}, htmx=True
            ).text
        )

    section = usage_section(shown)
    hints = [e for e in shown if e.tag == "p" and e.text == ORDER_HINT]
    assert len(hints) == 1 and hints[0].within(section)
    assert not any(hints[0].within(budget_block(shown, s)) for s in ALL_SERVICES)

    def hint_texts(elements: list[Element], service: str, name: str) -> list[str]:
        field = budget_field(elements, service, name)
        block = budget_block(elements, service)
        described = described_by(elements, field)
        assert all(e.within(block) for e in described), name
        return [e.text for e in described if not str(e.attrs.get("id")).endswith("-error")]

    for service in ALL_SERVICES:
        assert hint_texts(shown, service, "block_at") == [BLOCK_HINT]
        assert hint_texts(shown, service, "fast_cap") == [CAP_HINT]
        assert hint_texts(shown, service, "thorough_cap") == [CAP_HINT]
        for name in ("allowance", "first_warning", "second_warning"):
            assert hint_texts(shown, service, name) == []
            (label,) = labels_of(shown, budget_field(shown, service, name))
            assert label.endswith("(requests)"), label
        (save,) = action_buttons(shown, service, "save")
        assert "button-primary" in str(save.attrs.get("class")).split(), save.attrs
        assert save.text == "Save budget"
    (remove,) = action_buttons(shown, FAKE_SERVICE, "remove")
    assert "button-primary" not in str(remove.attrs.get("class")).split(), remove.attrs

    # With an error showing, the hint stays tied to the field next to it.
    assert hint_texts(refused, "web_search", "block_at") == [BLOCK_HINT]
    assert hint_texts(refused, "web_search", "fast_cap") == [CAP_HINT]
    assert error_text(refused, "web_search", "block_at")
    assert error_text(refused, "web_search", "fast_cap") == DIGITS_ONLY
