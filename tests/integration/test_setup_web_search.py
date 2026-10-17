"""Step 3 of the setup guide: web search and its optional budget (M2.25; AC-M1.7 with a real
skipped step; ADR-052, ADR-053, ADR-064, ADR-076 items 4 and 13, ADR-081 to ADR-084; detailed
design "Today and the setup guide").

``GET /setup/web_search`` (a page, like every guide step; no side effects) explains in plain
words that searches go through the connected AI provider (with Claude, Claude's built-in web
search) and run only when the user asks, says in one status line what applies now, and holds
one form posting to ``/setup/web_search`` (``hx-post`` too, swapping ``#setup-guide`` like the
other steps) with the M2.22 budget fields for the Web search service only: ``allowance``,
``first_warning``, ``second_warning``, ``block_at``, ``reset_day``, ``fast_cap``,
``thorough_cap`` and a hidden ``version`` (the usage-budgets version, ADR-081). The hint and
the fields sit in a ``<details>`` ("Set a monthly budget (optional)"), closed unless there is
a stored budget, unreadable stored budgets, an error or a stale version; closed fields are
still sent, empty. Two submit buttons outside it: the primary ``name="action" value="save"``,
"Continue", and ``name="action" value="skip"``, "Skip for now".

- A valid budget is saved for ``web_search`` only, marks the step done and moves on to job
  preferences (303; htmx: the guide fragment, ``HX-Push-Url`` and "Web search budget saved.
  Step 4 of 6: Import CV" announced; the next step is CV import since M3.7).
- Continuing with every field empty marks the step done with no budget ("Now: no limit on web
  searches."); a stored budget is kept (ADR-083), and the announcement says so.
- A budget saved while the step couldn't be marked done answers 422 saying it was saved, with
  the stored version, so sending the form again goes through.
- A refused budget answers 422, saves nothing, leaves the step as it was, keeps what was typed
  and ties each error to its field (``aria-invalid``, ``aria-describedby``); the first refused
  field takes focus and the title starts with "Error: ".
- "Skip for now" marks the step skipped, saves no budget and moves on to job preferences
  ("Web search skipped." announced first).
- A stale ``version`` answers 409 with the reload prompt (linking back to this step), saves
  nothing and leaves the step as it was; the title starts with "Error: " too.
- Done lists the step with "Skipped", "Done" or "Optional, not set up" and a link; a skipped
  step never blocks finishing. A setup finished before the step existed sees no banner.
- After a restart the guide resumes at job preferences, never at web search; reopening the
  step (also with ``--setup``) loses nothing.

Every POST below sends the hidden inputs the rendered form holds, as a browser would. A real
migrated SQLite database under ``tmp_path``, the frozen clock, the fake provider, no network,
no provider call; every value is fictional.
"""

import asyncio
import json
import re
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from httpx import Response

from app.domain.budgets import Budget, validate_budget
from app.domain.errors import VersionConflict
from app.onboarding.detection import StartupDecision
from app.onboarding.progress import SetupProgress
from app.onboarding.registry import Form, StepHandler, StepResult
from app.onboarding.steps.web_search import WebSearchStep
from app.services.setup_state import SetupStateService
from app.services.usage import UsageService
from tests.fakes.clock import FrozenClock
from tests.integration.test_settings_usage_budgets import (
    UNREADABLE,
    assert_budget_shown,
    assert_no_limit,
    settings_page,
    store_unreadable_budgets,
)
from tests.integration.test_setup_preferences_done import (
    DONE_PATH,
    PREFERENCES_PATH,
    SAVED,
    finish,
    live_statuses,
    post_preferences,
    running,
    seed,
    started,
    step_list_item,
    summary,
    title_of,
    valid_form,
)
from tests.integration.test_setup_provider import by_id, described_by, only, required
from tests.integration.test_setup_provider_test import announced
from tests.integration.test_setup_redirect import (
    BASE_URL,
    START,
    Element,
    Running,
    assert_not_redirected,
    assert_see_other,
    outline,
)
from tests.integration.test_setup_reopen_banner import banners
from tests.integration.test_setup_reopen_banner import started as started_with
from tests.integration.test_task_routes import database_state

pytestmark = pytest.mark.integration

STEP = "/setup/web_search"
STEP_ID = "web_search"
TITLE = "Web search"
SERVICE = "web_search"
OTHER_SERVICES = ("claude_subscription", "claude_api_key", "fake")
STEP_ANNOUNCEMENT = "Step 3 of 6: Web search"
# The step after web search: CV import (step 4, M3.7; ADR-089 item 4).
NEXT_PATH = "/setup/cv_import"
NEXT_TITLE = "Import CV"
NEXT_ANNOUNCEMENT = "Step 4 of 6: Import CV"
SAVED_SAID = "Web search budget saved."
SKIPPED_SAID = "Web search skipped."
KEPT_SAID = "Your web search budget of 100 requests was kept."
CONTINUE_TEXT = "Continue"
SKIP_TEXT = "Skip for now"
SUMMARY_TEXT = "Set a monthly budget (optional)"
# The status line, whole. "no limit" appears nowhere else in the step (any letter case).
STATUS_NO_LIMIT = "Now: no limit on web searches."
STATUS_BUDGET = "Now: a budget of 100 requests per month."
STATUS_UNREADABLE = (
    "Not known: the saved budgets couldn't be read. Saving a budget here replaces them."
)
NO_LIMIT = "no limit"  # compared in lower case
PARAGRAPHS = (
    "Some features will look things up on the web for you, such as facts about a company "
    "you're applying to. Searches go through the AI provider you connected in the previous "
    "step; with Claude, that is Claude's built-in web search.",
    "A search runs only when you ask for something that needs one. The app never searches on "
    "its own or in the background.",
    "Nothing in this version searches the web yet, so there's nothing you need to do here "
    "today. You can set a budget now so it's ready, continue without one, or choose Skip for "
    "now; a skipped step stays marked on the last page so you can find it again.",
)
HINT_START = (
    "A budget is a number of web searches per month. You're warned twice on the way, and at "
    "the blocking point the app stops searching until the reset day. A budget you fill in is "
    "saved when you continue."
)
HINT_EMPTY_MEANS_NONE = "To go on without a budget, leave every field empty."
HINT_EMPTY_KEEPS = (
    "Emptying the fields doesn't remove your budget; remove it in Settings > Usage budgets."
)
HINT_END = "Skip for now saves nothing."
SAVED_NOT_MOVED_ON = "Your budget was saved, but the app couldn't move on. Try again."
NOT_MOVED_ON = "The app couldn't move on. Try again."
OPTIONAL_TEXT = "Optional, not set up"

REQUIRED_FIELDS = ("allowance", "first_warning", "second_warning", "block_at", "reset_day")
CAP_FIELDS = ("fast_cap", "thorough_cap")
FIELDS = (*REQUIRED_FIELDS, *CAP_FIELDS)
# Words each field's visible label must hold (case-insensitive), as in Settings (M2.22).
LABEL_WORDS = {
    "allowance": "allowance",
    "first_warning": "first warning",
    "second_warning": "second warning",
    "block_at": "block",
    "reset_day": "reset day",
    "fast_cap": "fast",
    "thorough_cap": "thorough",
}
EMPTY = dict.fromkeys(FIELDS, "")
VALID = {
    "allowance": "100",
    "first_warning": "50",
    "second_warning": "80",
    "block_at": "100",
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
OTHER_BUDGET = Budget(allowance=20, first_warning=5, second_warning=10, block_at=15, reset_day=1)
HOSTILE = '"><b id="injected">fictional</b>'
ALL_DONE = ("welcome", "provider", "preferences")


# --- helpers -------------------------------------------------------------------------------


def usage(run: Running) -> UsageService:
    service: UsageService = run.app.state.usage
    return service


def budgets(run: Running) -> tuple[dict[str, Budget], int]:
    loaded = usage(run).load_budgets()
    return dict(loaded.value), loaded.version


def step_page(run: Running, *, htmx: bool = False) -> Response:
    """The step, shown: 200 and no redirect. Fails here while the step doesn't exist."""
    response = run.client.get(STEP, headers=run.htmx() if htmx else {})
    assert response.status_code == 200, (
        f"GET {STEP} answered {response.status_code}: the web search step isn't in the guide"
    )
    assert_not_redirected(response)
    return response


def section(elements: list[Element]) -> Element:
    return required(elements, "setup-step")


def step_forms(elements: list[Element]) -> list[Element]:
    inside = section(elements)
    return [e for e in elements if e.tag == "form" and e.within(inside)]


def budget_inputs(elements: list[Element], name: str) -> list[Element]:
    inside = section(elements)
    return [
        e
        for e in elements
        if e.tag == "input"
        and e.attrs.get("name") == name
        and e.attrs.get("type") != "hidden"
        and e.within(inside)
    ]


def form_of(element: Element) -> Element:
    node = element.parent
    while node is not None and node.tag != "form":
        node = node.parent
    assert node is not None, f"<{element.tag}> is inside a form"
    return node


def assert_posts_to_step(form: Element) -> None:
    """Like the other guide steps: a POST to the step, with htmx swapping the whole guide."""
    assert form.attrs.get("action") == STEP, form.attrs
    assert (form.attrs.get("method") or "").lower() == "post", form.attrs
    assert form.attrs.get("hx-post") == STEP, form.attrs
    assert form.attrs.get("hx-target") == "#setup-guide", form.attrs
    assert form.attrs.get("hx-swap") == "outerHTML", form.attrs


def labels_of(elements: list[Element], control: Element) -> list[str]:
    control_id = control.attrs.get("id")
    return [
        e.text
        for e in elements
        if e.tag == "label"
        and ((control_id and e.attrs.get("for") == control_id) or control.within(e))
    ]


def budget_field(elements: list[Element], name: str) -> Element:
    """The step's one ``<input name=...>``, in a form posting to the step, with its label."""
    field = only(budget_inputs(elements, name), f"{name} input in the step")
    assert_posts_to_step(form_of(field))
    labels = labels_of(elements, field)
    assert any(LABEL_WORDS[name] in label.lower() for label in labels), (name, labels)
    return field


def budget_form(elements: list[Element]) -> Element:
    forms = {id(form_of(budget_field(elements, name))) for name in FIELDS}
    assert len(forms) == 1, "every budget field is in the same form"
    return form_of(budget_field(elements, FIELDS[0]))


def shown_values(elements: list[Element]) -> dict[str, str]:
    return {name: budget_field(elements, name).attrs.get("value") or "" for name in FIELDS}


def invalid_fields(elements: list[Element]) -> set[str]:
    return {
        name for name in FIELDS if budget_field(elements, name).attrs.get("aria-invalid") == "true"
    }


def action_buttons(elements: list[Element], value: str) -> list[Element]:
    inside = section(elements)
    return [
        e
        for e in elements
        if e.tag == "button"
        and e.within(inside)
        and e.attrs.get("name") == "action"
        and e.attrs.get("value") == value
    ]


def save_button(elements: list[Element]) -> Element:
    button = only(action_buttons(elements, "save"), "save button")
    assert form_of(button) is budget_form(elements), "the save button submits the budget form"
    assert (button.attrs.get("type") or "submit") == "submit", button.attrs
    assert button.text == CONTINUE_TEXT, button.text
    return button


def skip_button(elements: list[Element]) -> Element:
    button = only(action_buttons(elements, "skip"), "skip button")
    assert button.text == SKIP_TEXT, button.text
    assert (button.attrs.get("type") or "submit") == "submit", button.attrs
    assert_posts_to_step(form_of(button))
    return button


def hidden_inputs(elements: list[Element], form: Element) -> dict[str, str]:
    """What a browser sends along with the visible fields of ``form``."""
    return {
        str(e.attrs.get("name")): e.attrs.get("value") or ""
        for e in elements
        if e.tag == "input" and e.attrs.get("type") == "hidden" and e.within(form)
    }


def form_version(elements: list[Element]) -> str:
    sent = hidden_inputs(elements, budget_form(elements))
    assert "version" in sent, f"the budget form carries the budgets version (ADR-081): {sent}"
    return sent["version"]


def post_step(
    run: Running,
    values: dict[str, str] | None = None,
    *,
    action: str = "save",
    htmx: bool = False,
    version: str | None = None,
    extra: dict[str, str] | None = None,
) -> Response:
    """Submit the step as a browser would: the rendered form's hidden inputs, the typed
    ``values`` and the pressed button. ``version`` replaces the form's (a tab left open)."""
    elements = outline(step_page(run).text)
    button = save_button(elements) if action == "save" else skip_button(elements)
    form = {**hidden_inputs(elements, form_of(button)), **(values or {}), "action": action}
    if version is not None:
        form["version"] = version
    form.update(extra or {})
    headers = run.action(**({"HX-Request": "true"} if htmx else {}))
    return run.client.post(STEP, data=form, headers=headers)


def status_of(run: Running) -> str:
    return live_statuses(run).get(STEP_ID, "pending")


def assert_moved_to_next_step(response: Response, *, htmx: bool, said: str | None = None) -> None:
    """303 to the next step (CV import since M3.7); for htmx the guide fragment at that step
    (ADR-053), announcing ``said`` (what the submit did, when there is something to say) and
    then the step."""
    if not htmx:
        assert_see_other(response, NEXT_PATH)
        return
    assert response.status_code == 200, (response.status_code, response.text[:300])
    assert "location" not in response.headers
    assert "hx-redirect" not in response.headers
    assert response.headers.get("hx-push-url") == NEXT_PATH
    expected = f"{said} {NEXT_ANNOUNCEMENT}" if said else NEXT_ANNOUNCEMENT
    assert json.loads(response.headers["hx-trigger"]) == {"announce": expected}
    for marker in ("<html", "<body", 'id="announcer"'):
        assert marker not in response.text, marker
    assert title_of(response).startswith(NEXT_TITLE), title_of(response)
    elements = outline(response.text)
    current = only([e for e in elements if e.attrs.get("aria-current") == "step"], "current")
    assert current.text.startswith(NEXT_TITLE)
    focused = [e.attrs.get("id") for e in elements if "data-autofocus" in e.attrs]
    assert focused == ["step-heading"], focused


def assert_step_shown_again(response: Response, *, htmx: bool) -> list[Element]:
    """A refused or stale submit shows the web search step again: the guide fragment for htmx,
    the full page otherwise; never a step change."""
    assert_not_redirected(response)
    assert "hx-push-url" not in response.headers
    assert ("<html" in response.text) is (not htmx), "htmx gets the guide fragment"
    elements = outline(response.text)
    current = only([e for e in elements if e.attrs.get("aria-current") == "step"], "current")
    assert current.text.startswith(TITLE), current.text
    return elements


def status_line(elements: list[Element]) -> str:
    return required(elements, "web-search-budget-status").text


def hint(elements: list[Element]) -> str:
    return required(elements, "web-search-budget-hint").text


def disclosure(elements: list[Element]) -> Element:
    """The step's one ``<details>``: its summary, with the hint and every budget field inside
    it and the paragraphs, the status line and both buttons outside."""
    inside = section(elements)
    found = only([e for e in elements if e.tag == "details" and e.within(inside)], "details")
    assert found.within(budget_form(elements)), "closed fields are still sent with the form"
    summaries = [e for e in elements if e.tag == "summary" and e.parent is found]
    assert [e.text for e in summaries] == [SUMMARY_TEXT]
    assert required(elements, "web-search-budget-hint").within(found)
    for name in FIELDS:
        assert budget_field(elements, name).within(found), name
    assert not required(elements, "web-search-budget-status").within(found)
    assert not save_button(elements).within(found)
    assert not skip_button(elements).within(found)
    for paragraph in step_paragraphs(elements):
        assert not paragraph.within(found)
    return found


def is_open(elements: list[Element]) -> bool:
    return "open" in disclosure(elements).attrs


def step_paragraphs(elements: list[Element]) -> list[Element]:
    """The step's own paragraphs: directly in the section, before the status line."""
    inside = section(elements)
    direct = [e for e in elements if e.tag == "p" and e.parent is inside]
    return [e for e in direct if e.attrs.get("id") != "web-search-budget-status"]


def progress_of(run: Running) -> SetupProgress:
    state = SetupStateService(run.app.state.workspace.db, FrozenClock(START))
    return SetupProgress(run.app.state.setup_registry, state)


def fail_the_first_mark(run: Running) -> None:
    """Swap in the step with a ``mark_done`` and a ``skip`` that fail once between them, as a
    locked database would, and afterwards record the step as wiring does."""
    failed: list[bool] = []

    def record(done: bool) -> None:
        if not failed:
            failed.append(True)
            raise sqlite3.OperationalError("database is locked")
        progress = progress_of(run)
        progress.mark_done(STEP_ID) if done else progress.skip(STEP_ID)
        run.app.state.onboarding.refresh(progress)

    flaky = WebSearchStep(
        run.app.state.usage_settings,
        mark_done=lambda: record(True),
        skip=lambda: record(False),
    )
    run.app.state.setup_handlers = {**run.app.state.setup_handlers, STEP_ID: flaky}


def resend(run: Running, elements: list[Element], *, action: str, htmx: bool) -> Response:
    """Press a button on the form exactly as a response showed it (no reload in between)."""
    form = {
        **hidden_inputs(elements, budget_form(elements)),
        **shown_values(elements),
        "action": action,
    }
    headers = run.action(**({"HX-Request": "true"} if htmx else {}))
    return run.client.post(STEP, data=form, headers=headers)


class LoopSpy:
    """A step handler that notes every call made on the event loop instead of a worker."""

    def __init__(self, inner: StepHandler, name: str, on_loop: list[str]) -> None:
        self._inner = inner
        self._name = name
        self._on_loop = on_loop
        self.echo_fields: tuple[str, ...] = getattr(inner, "echo_fields", ())
        self.echo_multi_fields: tuple[str, ...] = getattr(inner, "echo_multi_fields", ())

    def _note(self, call: str) -> None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        self._on_loop.append(f"{self._name}.{call}")

    def context(self) -> Mapping[str, object]:
        self._note("context")
        return self._inner.context()

    def submit(self, form: Form) -> StepResult:
        self._note("submit")
        return self._inner.submit(form)


def done_rows(response: Response) -> dict[str, str]:
    rows = summary(response)
    assert [href for href, _ in rows] == [
        "/setup/welcome",
        "/setup/provider",
        STEP,
        NEXT_PATH,
        "/setup/preferences",
    ], "Done lists the steps in order, web search third, each with its link"
    texts = dict(rows)
    assert TITLE in texts[STEP], texts[STEP]
    return texts


# --- the step ------------------------------------------------------------------------------


def test_web_search_step_explains_built_in_search(tmp_path: Path) -> None:
    """The step says what web search is and when it runs, and offers the optional budget
    with a way on and a way to skip."""
    with running(tmp_path) as run:
        page = step_page(run)
        fragment = step_page(run, htmx=True)
        assert announced(fragment) == STEP_ANNOUNCEMENT
        assert "hx-trigger" not in page.headers
        assert status_of(run) == "pending"

    assert "<html" in page.text and "<html" not in fragment.text
    for shown in (page, fragment):
        assert title_of(shown).startswith(TITLE), title_of(shown)
        elements = outline(shown.text)
        heading = required(elements, "step-heading")
        assert heading.tag == "h2" and heading.text == TITLE
        focused = [e.attrs.get("id") for e in elements if "data-autofocus" in e.attrs]
        assert focused == ["step-heading"], focused
        current = step_list_item(shown, TITLE)
        assert current.attrs.get("aria-current") == "step"
        assert "Done" not in current.text and "Skipped" not in current.text, current.text

        # Plain words: Claude's built-in search, only when the user asks (key words only).
        words = section(elements).text.lower()
        for word in ("claude", "built-in", "search", "ask"):
            assert word in words, f"the step's copy doesn't mention {word!r}"
        assert "optional" in words, "the step says the budget is optional"
        assert NO_LIMIT in words, "no budget stored: the step says so"
        # The copy itself: true with any provider (the tests run with the fake one).
        assert [p.text for p in step_paragraphs(elements)] == list(PARAGRAPHS)
        assert "Claude account" not in section(elements).text
        assert status_line(elements) == STATUS_NO_LIMIT
        assert words.count(NO_LIMIT) == 1, "only the status line says it"
        following = [e for e in elements if e.parent is section(elements)]
        after = following[following.index(step_paragraphs(elements)[-1]) + 1]
        assert after.attrs.get("id") == "web-search-budget-status", "right under the paragraphs"

        # The budget form, for web search only, with a way on and a way to skip. Its fields
        # are folded away until asked for, with the hint.
        form = budget_form(elements)
        assert form_version(elements) == "0"
        assert shown_values(elements) == EMPTY
        assert invalid_fields(elements) == set()
        assert is_open(elements) is False, "closed by default: most people pass through"
        assert hint(elements) == f"{HINT_START} {HINT_EMPTY_MEANS_NONE} {HINT_END}"
        primary = save_button(elements)
        assert "button-primary" in (primary.attrs.get("class") or "").split(), primary.attrs
        skip = skip_button(elements)
        assert "button-primary" not in (skip.attrs.get("class") or "").split(), skip.attrs
        assert hidden_inputs(elements, form).get("service", SERVICE) == SERVICE
        for other in OTHER_SERVICES:
            assert by_id(elements, f"budget-{other}") is None, f"{other} has no form here"
        named = [e for e in elements if e.attrs.get("name") == "max_concurrent"]
        assert named == [], "the task setting stays in Settings"
        assert len(step_forms(elements)) <= 2, "one budget form (and at most a skip form)"

        ids = [str(e.attrs["id"]) for e in elements if e.attrs.get("id")]
        assert sorted({i for i in ids if ids.count(i) > 1}) == [], "ids never repeat"


def test_web_search_budget_fields_labelled_with_reset_day_note(tmp_path: Path) -> None:
    """The same seven fields as Settings > Usage budgets, each labelled; the reset-day field
    is described by the note about days 29 to 31 (ADR-076 item 8)."""
    with running(tmp_path) as run:
        elements = outline(step_page(run).text)

    for name in FIELDS:
        field = budget_field(elements, name)
        assert field.attrs.get("id"), name
        assert field.attrs.get("autocomplete") == "off", name
    # Every count says what it counts, the blocking point too.
    assert labels_of(elements, budget_field(elements, "block_at")) == ["Blocking point (requests)"]
    notes = [
        e
        for e in described_by(elements, budget_field(elements, "reset_day"))
        if "29" in e.text and "31" in e.text and "last day" in e.text.lower()
    ]
    assert len(notes) == 1, "the reset-day note is tied to the field by aria-describedby"
    assert notes[0].within(section(elements))


def test_web_search_get_has_no_side_effects(tmp_path: Path) -> None:
    """Spec 2.9 rule 12: showing the step (page, htmx, HEAD; with a query that looks like a
    submit) changes no row, creates no file and leaves the cached decision alone."""
    with running(tmp_path) as run:
        data_dir = run.app.state.workspace.db.path.parent
        before = database_state(data_dir)
        decision = run.decision

        step_page(run)
        for _ in range(2):
            step_page(run, htmx=True)
            assert run.client.get(f"{STEP}?action=skip&allowance=5").status_code == 200
            assert run.client.get(f"{STEP}?action=save&allowance=100").status_code == 200
            assert run.client.head(STEP).status_code == 200

        assert database_state(data_dir) == before, "nothing written"
        assert budgets(run) == ({}, 0)
        assert status_of(run) == "pending"
        assert run.decision == decision
        assert shown_values(outline(step_page(run).text)) == EMPTY, "a query fills nothing in"


# --- saving a budget -------------------------------------------------------------------------


def test_web_search_budget_saved_and_step_done(tmp_path: Path) -> None:
    """A valid budget is stored for the Web search service only, the step is done, the guide
    moves on to job preferences, and Settings > Usage budgets shows the budget later."""
    with running(tmp_path, done=ALL_DONE, preferences=SAVED) as run:
        assert run.decision == StartupDecision("guide", "done")

        response = post_step(run, CAPPED)

        assert_moved_to_next_step(response, htmx=False)
        assert budgets(run) == ({SERVICE: CAPPED_BUDGET}, 1)
        assert usage(run).status(SERVICE).budget == CAPPED_BUDGET
        assert status_of(run) == "done"
        assert run.decision == StartupDecision("guide", "done"), "an optional step: no detour"

        # The cached statuses were refreshed: the step list and Done say so, in text.
        assert "Done" in step_list_item(run.client.get(PREFERENCES_PATH), TITLE).text
        done = run.client.get(DONE_PATH)
        assert done_rows(done)[STEP].endswith("Done"), done_rows(done)[STEP]

        # Reopening the step shows the stored budget and its new version.
        reopened = outline(step_page(run).text)
        assert shown_values(reopened) == CAPPED
        assert form_version(reopened) == "1"
        assert NO_LIMIT not in section(reopened).text.lower()
        assert status_line(reopened) == STATUS_BUDGET
        assert is_open(reopened) is True, "a stored budget is shown, not folded away"
        # ADR-083: the hint never promises that emptying the fields removes the budget.
        assert hint(reopened) == f"{HINT_START} {HINT_EMPTY_KEEPS} {HINT_END}"
        assert HINT_EMPTY_MEANS_NONE not in section(reopened).text

        # Later, in Settings > Usage budgets: the same budget, on Web search only.
        assert_see_other(finish(run, "today"), "/")
        settings = outline(settings_page(run).text)
        assert_budget_shown(settings, SERVICE, CAPPED)
        for other in OTHER_SERVICES:
            assert_no_limit(settings, other)


def test_web_search_budget_saved_with_htmx_moves_on_and_announces(tmp_path: Path) -> None:
    with running(tmp_path) as run:
        response = post_step(run, VALID, htmx=True)

        assert_moved_to_next_step(response, htmx=True, said=SAVED_SAID)
        assert budgets(run) == ({SERVICE: VALID_BUDGET}, 1)
        assert status_of(run) == "done"
        assert run.decision == StartupDecision("guide", "preferences")
        assert "Done" in step_list_item(response, TITLE).text, "the swapped list says Done"


def test_web_search_budget_changed_from_the_step(tmp_path: Path) -> None:
    """Reopening the step and saving other numbers replaces the Web search budget alone."""
    with running(tmp_path) as run:
        usage(run).save_budget("claude_api_key", OTHER_BUDGET, expected_version=0)
        assert_moved_to_next_step(post_step(run, VALID), htmx=False)
        assert_moved_to_next_step(post_step(run, CAPPED), htmx=False)

        assert budgets(run) == ({"claude_api_key": OTHER_BUDGET, SERVICE: CAPPED_BUDGET}, 3)
        assert status_of(run) == "done"


def test_web_search_budget_never_saved_for_another_service(tmp_path: Path) -> None:
    """The step is about web search only: a forged ``service`` field never moves the budget
    to another service."""
    with running(tmp_path) as run:
        for saves, forged in enumerate(
            ("claude_api_key", "fake", "fictional_service", HOSTILE), start=1
        ):
            response = post_step(run, VALID, extra={"service": forged}, htmx=True)
            assert 'id="injected"' not in response.text
            assert_moved_to_next_step(response, htmx=True, said=SAVED_SAID)
            assert budgets(run) == ({SERVICE: VALID_BUDGET}, saves), forged
        # The untouched form still saves web search's.
        assert_moved_to_next_step(post_step(run, VALID), htmx=False)
        assert budgets(run) == ({SERVICE: VALID_BUDGET}, 5)


def test_continue_without_a_budget_marks_done_with_no_limit(tmp_path: Path) -> None:
    """The budget is optional: continuing with every field empty finishes the step with no
    budget, which is not the same as skipping it. Nothing was saved, so nothing but the next
    step is announced."""
    with running(tmp_path, done=ALL_DONE, preferences=SAVED) as run:
        for htmx in (False, True):
            assert_moved_to_next_step(post_step(run, EMPTY, htmx=htmx), htmx=htmx)
            assert budgets(run) == ({}, 0), "no budget saved"
            assert status_of(run) == "done"

        assert done_rows(run.client.get(DONE_PATH))[STEP].endswith("Done")
        reopened = outline(step_page(run).text)
        assert shown_values(reopened) == EMPTY
        assert status_line(reopened) == STATUS_NO_LIMIT
        assert is_open(reopened) is False


def test_closed_budget_fields_are_sent_empty_and_continue_means_no_limit(
    tmp_path: Path,
) -> None:
    """The folded fields are part of the form (a browser sends them, empty), and a form
    without them at all means the same: done, with no limit."""
    with running(tmp_path) as run:
        elements = outline(step_page(run).text)
        assert is_open(elements) is False
        assert shown_values(elements) == EMPTY

        assert_moved_to_next_step(resend(run, elements, action="save", htmx=True), htmx=True)
        assert (budgets(run), status_of(run)) == (({}, 0), "done")

    with running(tmp_path / "other") as run:
        bare = run.client.post(STEP, data={"action": "save"}, headers=run.action())
        assert_moved_to_next_step(bare, htmx=False)
        assert (budgets(run), status_of(run)) == (({}, 0), "done")


def test_emptied_form_never_removes_a_stored_budget(tmp_path: Path) -> None:
    """ADR-083: emptying the fields doesn't remove a budget; only Remove does. The step
    isn't silent about it: moving on says the budget was kept."""
    with running(tmp_path) as run:
        usage(run).save_budget(SERVICE, VALID_BUDGET, expected_version=0)
        stored = budgets(run)
        assert stored == ({SERVICE: VALID_BUDGET}, 1)
        assert status_of(run) == "pending"

        assert_moved_to_next_step(post_step(run, EMPTY), htmx=False)
        assert budgets(run) == stored
        assert status_of(run) == "done"
        assert_moved_to_next_step(post_step(run, EMPTY, htmx=True), htmx=True, said=KEPT_SAID)
        assert budgets(run) == stored
        assert status_of(run) == "done"

        # Still there, still shown, and the hint says where a budget is removed.
        reopened = outline(step_page(run).text)
        assert shown_values(reopened) == VALID
        assert status_line(reopened) == STATUS_BUDGET
        assert HINT_EMPTY_KEEPS in hint(reopened)


@pytest.mark.parametrize("htmx", [True, False], ids=["htmx", "plain"])
def test_budget_saved_but_step_not_marked_says_saved_and_retry_goes_through(
    tmp_path: Path, htmx: bool
) -> None:
    """The budget was stored and only marking the step failed: the answer says it was saved,
    shows the stored budget with its new version (never the stale one, which would answer
    "isn't saved"), and pressing Continue again on that very form moves on."""
    with running(tmp_path) as run:
        fail_the_first_mark(run)

        response = post_step(run, CAPPED, htmx=htmx)

        assert response.status_code == 422, (response.status_code, response.text[:300])
        assert budgets(run) == ({SERVICE: CAPPED_BUDGET}, 1), "the save went through"
        assert status_of(run) == "pending"
        elements = assert_step_shown_again(response, htmx=htmx)
        assert title_of(response).startswith(f"Error: {TITLE}"), title_of(response)
        error = required(elements, "form-error")
        assert error.text == SAVED_NOT_MOVED_ON
        assert error.attrs.get("role") == "alert"
        focused = [e.attrs.get("id") for e in elements if "data-autofocus" in e.attrs]
        assert focused == ["form-error"], focused
        assert shown_values(elements) == CAPPED
        assert form_version(elements) == "1", "the version the save produced"
        assert status_line(elements) == STATUS_BUDGET
        assert is_open(elements) is True
        assert VersionConflict.message not in response.text

        again = resend(run, elements, action="save", htmx=htmx)

        assert_moved_to_next_step(again, htmx=htmx, said=SAVED_SAID)
        assert budgets(run) == ({SERVICE: CAPPED_BUDGET}, 2)
        assert status_of(run) == "done"


def test_skip_that_could_not_be_recorded_keeps_what_was_typed(tmp_path: Path) -> None:
    """Nothing is saved by Skip, so the message claims no save; the form comes back as typed
    and open, and Skip again goes through."""
    with running(tmp_path) as run:
        fail_the_first_mark(run)

        response = post_step(run, VALID, action="skip", htmx=True)

        assert response.status_code == 422, response.status_code
        assert (budgets(run), status_of(run)) == (({}, 0), "pending")
        elements = assert_step_shown_again(response, htmx=True)
        assert required(elements, "form-error").text == NOT_MOVED_ON
        assert shown_values(elements) == VALID
        assert is_open(elements) is True, "what was typed stays in sight"

        again = resend(run, elements, action="skip", htmx=True)
        assert_moved_to_next_step(again, htmx=True, said=SKIPPED_SAID)
        assert (budgets(run), status_of(run)) == (({}, 0), "skipped")


# --- refused budgets (the AC-M2.7 rules) ------------------------------------------------------


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
        pytest.param({"reset_day": "32"}, {"reset_day"}, id="reset_day_above_31"),
        pytest.param({"reset_day": "0"}, {"reset_day"}, id="reset_day_below_1"),
    ],
)
def test_web_search_budget_errors_tied_to_fields(
    tmp_path: Path, changes: dict[str, str], offending: set[str]
) -> None:
    """first < second < block ≤ allowance, caps ≤ allowance, reset day 1 to 31: a broken rule
    answers 422, saves nothing, leaves the step not done, keeps what was typed, marks the
    fields the domain names and moves focus to the first of them."""
    values = {**VALID, **changes}
    numbers = {name: int(text) if text else None for name, text in values.items()}
    assert {error.field for error in validate_budget(**numbers)} == offending

    with running(tmp_path) as run:
        usage(run).save_budget("claude_api_key", OTHER_BUDGET, expected_version=0)
        before = budgets(run)

        for htmx in (True, False):
            response = post_step(run, values, htmx=htmx)

            assert response.status_code == 422, (htmx, response.status_code, response.text[:300])
            assert budgets(run) == before, "nothing saved"
            assert status_of(run) == "pending", "the step isn't done"
            assert run.decision == StartupDecision("guide", "preferences")
            elements = assert_step_shown_again(response, htmx=htmx)
            assert title_of(response).startswith(f"Error: {TITLE}"), title_of(response)

            assert invalid_fields(elements) == offending
            assert is_open(elements) is True, "an error opens the folded fields"
            assert shown_values(elements) == values, "what was typed is kept"
            assert form_version(elements) == str(before[1])
            error_ids: list[str] = []
            for name in offending:
                field = budget_field(elements, name)
                errors = [
                    e
                    for e in described_by(elements, field)
                    if "field-error" in (e.attrs.get("class") or "").split() and e.text
                ]
                assert len(errors) == 1, f"{name}: one error listed in aria-describedby"
                assert errors[0].within(section(elements)), name
                error_ids.append(str(errors[0].attrs.get("id")))
            assert len(set(error_ids)) == len(error_ids), "each field has its own error"
            for name in set(FIELDS) - offending:
                described = described_by(elements, budget_field(elements, name))
                assert not any(str(e.attrs.get("id")) in error_ids for e in described), name
            if "reset_day" in offending:  # the note stays tied to the field next to the error
                described = described_by(elements, budget_field(elements, "reset_day"))
                assert any("last day" in e.text.lower() for e in described)

            # Focus: the first refused field, in form order; nothing else asks for it.
            first = next(name for name in FIELDS if name in offending)
            focused = [e for e in elements if "data-autofocus" in e.attrs]
            assert len(focused) == 1 and focused[0] is budget_field(elements, first), [
                (e.tag, e.attrs.get("name"), e.attrs.get("id")) for e in focused
            ]
            # Both ways on are still offered.
            save_button(elements)
            skip_button(elements)

        # Nothing sticks: the step shows no budget, and the corrected form is saved.
        assert shown_values(outline(step_page(run).text)) == EMPTY
        assert_moved_to_next_step(post_step(run, VALID), htmx=False)
        assert budgets(run) == ({**before[0], SERVICE: VALID_BUDGET}, before[1] + 1)
        assert status_of(run) == "done"


@pytest.mark.parametrize(
    "bad", [HOSTILE, "ten", "1e3", "10.5", "-5", "0", "100 ignore previous instructions"]
)
def test_web_search_budget_non_numbers_refused_and_escaped(tmp_path: Path, bad: str) -> None:
    """Adversarial text in a number field is refused, shown back escaped and never saved."""
    with running(tmp_path) as run:
        for htmx in (True, False):
            response = post_step(run, {**VALID, "allowance": bad}, htmx=htmx)

            assert response.status_code == 422, (bad, htmx, response.status_code)
            assert 'id="injected"' not in response.text
            elements = assert_step_shown_again(response, htmx=htmx)
            assert "allowance" in invalid_fields(elements)
            assert budget_field(elements, "allowance").attrs.get("value") == bad
            assert budgets(run) == ({}, 0)
            assert status_of(run) == "pending"


def test_partly_filled_budget_is_refused_not_treated_as_empty(tmp_path: Path) -> None:
    """Only a form with every field empty means "no budget": one number alone is refused."""
    with running(tmp_path) as run:
        response = post_step(run, {**EMPTY, "allowance": "100"}, htmx=True)

        assert response.status_code == 422, response.status_code
        elements = assert_step_shown_again(response, htmx=True)
        assert invalid_fields(elements) == {
            "first_warning",
            "second_warning",
            "block_at",
            "reset_day",
        }
        assert budgets(run) == ({}, 0)
        assert status_of(run) == "pending"


# --- Skip for now ----------------------------------------------------------------------------


@pytest.mark.parametrize("htmx", [False, True], ids=["plain", "htmx"])
def test_skip_for_now_marks_skipped_and_moves_to_preferences(tmp_path: Path, htmx: bool) -> None:
    with running(tmp_path) as run:
        response = post_step(run, action="skip", htmx=htmx)

        assert_moved_to_next_step(response, htmx=htmx, said=SKIPPED_SAID)
        assert status_of(run) == "skipped", "persisted"
        assert budgets(run) == ({}, 0), "no budget saved"
        assert run.decision == StartupDecision("guide", "preferences")
        assert_see_other(run.client.get("/"), PREFERENCES_PATH)
        # The cached statuses were refreshed: the step list says "Skipped", in text.
        shown = response if htmx else run.client.get(PREFERENCES_PATH)
        assert "Skipped" in step_list_item(shown, TITLE).text

        # The step can be opened again: still there, nothing filled in.
        reopened = step_page(run)
        assert "Skipped" in step_list_item(reopened, TITLE).text
        assert shown_values(outline(reopened.text)) == EMPTY


def test_skip_for_now_ignores_what_was_typed(tmp_path: Path) -> None:
    """Skipping saves no budget, whatever the fields hold, valid or not."""
    with running(tmp_path) as run:
        for typed in (CAPPED, {**VALID, "reset_day": "32"}, {**VALID, "allowance": HOSTILE}):
            response = post_step(run, typed, action="skip")
            assert_moved_to_next_step(response, htmx=False)
            assert budgets(run) == ({}, 0)
            assert status_of(run) == "skipped"


def test_skip_after_a_saved_budget_keeps_the_budget_and_done(tmp_path: Path) -> None:
    """ADR-052: a done step stays done; skipping later removes nothing."""
    with running(tmp_path) as run:
        assert_moved_to_next_step(post_step(run, VALID), htmx=False)
        assert_moved_to_next_step(post_step(run, action="skip"), htmx=False)

        assert status_of(run) == "done"
        assert budgets(run) == ({SERVICE: VALID_BUDGET}, 1)


def test_budget_saved_after_skipping_marks_done(tmp_path: Path) -> None:
    with running(tmp_path) as run:
        assert_moved_to_next_step(post_step(run, action="skip"), htmx=False)
        assert_moved_to_next_step(post_step(run, VALID), htmx=False)

        assert status_of(run) == "done"
        assert budgets(run) == ({SERVICE: VALID_BUDGET}, 1)
        assert "Done" in step_list_item(step_page(run), TITLE).text


def test_web_search_post_needs_token_and_origin(tmp_path: Path) -> None:
    """Saving and skipping are state-changing POSTs: launch token and origin, like every
    step submit."""
    with running(tmp_path) as run:
        elements = outline(step_page(run).text)
        sent = hidden_inputs(elements, budget_form(elements))
        for action in ("save", "skip"):
            form = {**sent, **VALID, "action": action}
            no_token = run.client.post(STEP, data=form, headers={"origin": BASE_URL})
            bad_origin = run.client.post(
                STEP, data=form, headers=run.action(origin="http://evil.example")
            )
            assert no_token.status_code == 403, action
            assert bad_origin.status_code == 403, action
            assert budgets(run) == ({}, 0)
            assert status_of(run) == "pending"

        # The same forms with the token and this origin are accepted.
        assert_moved_to_next_step(post_step(run, action="skip"), htmx=False)
        assert status_of(run) == "skipped"


def test_web_search_without_workspace_asks_for_step_1(tmp_path: Path) -> None:
    """Before step 1 there is nowhere to save: the step says so and creates nothing."""
    with started(tmp_path, source="default") as run:
        assert run.app.state.workspace is None
        shown = step_page(run)
        assert budget_inputs(outline(shown.text), "allowance") == [], "no form before step 1"
        links = [e for e in outline(shown.text) if e.attrs.get("href") == "/setup/welcome"]
        assert links, "a link to step 1"

        for action in ("save", "skip"):
            response = run.client.post(
                STEP, data={**VALID, "version": "0", "action": action}, headers=run.action()
            )
            assert response.status_code == 422, (action, response.status_code)
            assert "step 1" in required(outline(response.text), "form-error").text.lower()
        assert run.app.state.workspace is None
    assert not (tmp_path / "data").exists(), "nothing is created"


# --- stale version and unreadable budgets (ADR-081, ADR-084) ----------------------------------


def test_stale_budget_version_shows_reload_prompt_and_saves_nothing(tmp_path: Path) -> None:
    """The budgets changed elsewhere (Settings in another tab) after the step was shown: the
    save answers 409 with the reload prompt, pointing back at this step; nothing is saved and
    the step isn't done. The form keeps the version it was sent with, so it keeps conflicting
    until the step is loaded again."""
    with running(tmp_path) as run:
        stale = form_version(outline(step_page(run).text))
        usage(run).save_budget("claude_api_key", OTHER_BUDGET, expected_version=0)
        current = ({"claude_api_key": OTHER_BUDGET}, 1)

        for htmx in (True, False):
            for _ in range(2):  # a second press conflicts again
                response = post_step(run, CAPPED, version=stale, htmx=htmx)

                assert response.status_code == 409, (htmx, response.status_code)
                assert budgets(run) == current, "nothing saved"
                assert status_of(run) == "pending", "the step isn't done"
                elements = assert_step_shown_again(response, htmx=htmx)
                alerts = [
                    e
                    for e in elements
                    if e.attrs.get("role") == "alert" and VersionConflict.message in e.text
                ]
                prompt = only(alerts, "reload prompt")
                assert prompt.within(section(elements))
                assert prompt.attrs.get("tabindex") == "-1", prompt.attrs
                focused = [e for e in elements if "data-autofocus" in e.attrs]
                assert len(focused) == 1 and focused[0] is prompt, "the prompt takes focus"
                links = [
                    urlsplit(e.attrs.get("href") or "").path
                    for e in elements
                    if e.tag == "a" and e.within(prompt) and "reload" in e.text.lower()
                ]
                assert links == [STEP], "Reload leads back to this step"
                assert form_version(elements) == stale, "the stale form stays stale"
                assert shown_values(elements) == CAPPED, "what was typed is still there"
                assert is_open(elements) is True, "and in sight"
                assert not prompt.within(disclosure(elements))
                assert title_of(response).startswith(f"Error: {TITLE}"), title_of(response)

        # A version that was never issued, or isn't a number, is refused too.
        for version in ("7", "", "one", "-1", HOSTILE):
            response = post_step(run, CAPPED, version=version, htmx=True)
            assert response.status_code == 409, (version, response.status_code)
            assert 'id="injected"' not in response.text
            assert budgets(run) == current

        # Loading the step again gives the stored version, and the save goes through.
        assert form_version(outline(step_page(run).text)) == "1"
        assert_moved_to_next_step(post_step(run, CAPPED), htmx=False)
        assert budgets(run) == ({"claude_api_key": OTHER_BUDGET, SERVICE: CAPPED_BUDGET}, 2)
        assert status_of(run) == "done"


def test_unreadable_stored_budgets_never_shown_as_no_limit(tmp_path: Path) -> None:
    """ADR-084: while the stored budgets can't be read the step never says "no limit" (nor
    that leaving the fields empty means no budget) and never shows the stored text; continuing
    with empty fields claims nothing about them; a budget saved here replaces them."""
    with running(tmp_path) as run:
        assert status_line(outline(step_page(run).text)) == STATUS_NO_LIMIT
        store_unreadable_budgets(run)
        assert re.search(r"\bten\b", UNREADABLE), "the stored text holds the word looked for"

        for htmx in (False, True):
            shown = step_page(run, htmx=htmx)
            elements = outline(shown.text)
            assert NO_LIMIT not in section(elements).text.lower()
            assert status_line(elements) == STATUS_UNREADABLE
            assert hint(elements) == f"{HINT_START} {HINT_END}"
            assert is_open(elements) is True, "the fields that fix it are in sight"
            # The stored value is never shown: checked on everything that was sent.
            assert re.search(r"\bten\b", shown.text) is None
            assert "claude_api_key" not in shown.text
            assert shown_values(elements) == EMPTY
        assert usage(run).status(SERVICE).budgets_unreadable is True, "GET fixed nothing"

        # Continuing with empty fields fixes nothing and says nothing about a budget; the
        # step, opened again, still says the budgets couldn't be read.
        assert_moved_to_next_step(post_step(run, EMPTY, htmx=True), htmx=True)
        assert usage(run).status(SERVICE).budgets_unreadable is True
        assert status_line(outline(step_page(run).text)) == STATUS_UNREADABLE

        assert_moved_to_next_step(post_step(run, VALID), htmx=False)
        assert usage(run).status(SERVICE).budgets_unreadable is False
        assert budgets(run)[0] == {SERVICE: VALID_BUDGET}
        assert status_of(run) == "done"


# --- handlers never run on the event loop ------------------------------------------------------


def test_step_submits_read_and_write_off_the_event_loop(tmp_path: Path) -> None:
    """Handlers block (database): after a POST the step shown next is read in a worker thread
    too, like the submit itself, whatever the answer (moved on, refused, stale, plain or
    htmx), for this step and for the one after it."""
    with running(tmp_path) as run:
        on_loop: list[str] = []
        calls: list[str] = []
        run.app.state.setup_handlers = {
            step_id: LoopSpy(handler, step_id, on_loop)
            for step_id, handler in run.app.state.setup_handlers.items()
        }

        for htmx in (True, False):
            refused = post_step(run, {**VALID, "reset_day": "32"}, htmx=htmx)
            stale = post_step(run, VALID, version="7", htmx=htmx)
            unfinished = post_preferences(run, {"cv_country": ""}, htmx=htmx)
            moved = post_step(run, EMPTY, htmx=htmx)
            calls += [str(r.status_code) for r in (refused, stale, unfinished, moved)]

        assert calls == ["422", "409", "422", "200", "422", "409", "422", "303"]
        assert on_loop == []


# --- AC-M1.7: Done lists the step ---------------------------------------------------------------


def test_ac_m1_7_done_lists_skipped_web_search_with_link(tmp_path: Path) -> None:
    """A step really skipped through the guide is listed on Done as "Skipped" with a link back
    to it, in the list and in the step list; being optional, it never blocks finishing."""
    with running(tmp_path, done=ALL_DONE, preferences=SAVED) as run:
        assert_moved_to_next_step(post_step(run, action="skip"), htmx=False)

        for shown in (run.client.get(DONE_PATH), run.client.get(DONE_PATH, headers=run.htmx())):
            assert shown.status_code == 200, shown.status_code
            texts = done_rows(shown)
            assert texts[STEP].endswith("Skipped"), texts[STEP]
            for path in ("/setup/welcome", "/setup/provider", "/setup/preferences"):
                assert texts[path].endswith("Done"), texts[path]
            assert "Skipped" in step_list_item(shown, TITLE).text
            assert "Not done yet" not in section(outline(shown.text)).text

        # Finishing is allowed with the step skipped, and the app opens without a banner.
        assert_see_other(finish(run, "today"), "/")
        assert run.decision == StartupDecision("app")
        today = run.client.get("/")
        assert today.status_code == 200
        assert_not_redirected(today)
        assert banners(today.text) == []
        assert status_of(run) == "skipped", "finishing never rewrites the step"

        # The link leads to the step, which still offers the budget.
        reopened = outline(step_page(run).text)
        assert shown_values(reopened) == EMPTY
        save_button(reopened)


def test_ac_m1_7_done_says_done_after_a_budget_and_optional_when_untouched(
    tmp_path: Path,
) -> None:
    with running(tmp_path, done=ALL_DONE, preferences=SAVED) as run:
        untouched = done_rows(run.client.get(DONE_PATH))
        assert untouched[STEP].endswith(OPTIONAL_TEXT), untouched[STEP]

        assert_moved_to_next_step(post_step(run, VALID), htmx=False)
        saved = done_rows(run.client.get(DONE_PATH))
        assert saved[STEP].endswith("Done"), saved[STEP]
        assert "100" not in saved[STEP], "no details of the budget on Done"

        # An untouched optional step doesn't block finishing either.
    other = tmp_path / "other"
    with running(other, done=ALL_DONE, preferences=SAVED) as run:
        assert_see_other(finish(run, "add_job"), "/inbox")
        assert run.decision == StartupDecision("app")
        assert status_of(run) == "pending"


def test_finished_setup_from_before_the_step_sees_no_banner(tmp_path: Path) -> None:
    """A workspace whose guide was finished before this step existed has no row for it: the
    app opens as before (no redirect, no banner), and Done lists the step as "Optional, not
    set up" with its link."""
    seed(tmp_path, done=(*ALL_DONE, "done"), preferences=SAVED)

    with started(tmp_path) as run:
        assert "web_search" in [step.id for step in run.app.state.setup_registry.steps]
        assert STEP_ID not in live_statuses(run)
        assert run.decision == StartupDecision("app")
        for path in ("/", "/inbox", "/settings"):
            page = run.client.get(path)
            assert page.status_code == 200, (path, page.status_code)
            assert_not_redirected(page)
            assert banners(page.text) == [], f"no banner: {path}"

        rows = done_rows(run.client.get(DONE_PATH))
        assert rows[STEP].endswith(OPTIONAL_TEXT), rows[STEP]
        item = step_list_item(run.client.get(DONE_PATH), TITLE).text
        assert "Skipped" not in item and "Needs attention" not in item, item
        assert STEP_ID not in live_statuses(run), "looking wrote nothing"


# --- restart and reopening ---------------------------------------------------------------------


def test_restart_resumes_after_skip(tmp_path: Path) -> None:
    """After "Skip for now", a restart resumes at job preferences, not at web search; the
    step can be reopened, a budget saved from it is shown again after another restart, and
    ``--setup`` on the finished guide loses nothing."""
    seed(tmp_path)
    with started(tmp_path) as run:
        assert_see_other(run.client.get("/"), PREFERENCES_PATH)
        assert_moved_to_next_step(post_step(run, action="skip"), htmx=False)

    with started(tmp_path) as run:
        assert run.decision == StartupDecision("guide", "preferences")
        assert_see_other(run.client.get("/"), PREFERENCES_PATH)
        assert_see_other(run.client.get("/setup"), PREFERENCES_PATH)
        assert status_of(run) == "skipped", "the skip survived the restart"
        assert budgets(run) == ({}, 0)

        # Reopening the step later: it is shown, says "Skipped", and takes a budget.
        reopened = step_page(run)
        assert "Skipped" in step_list_item(reopened, TITLE).text
        assert shown_values(outline(reopened.text)) == EMPTY
        assert_moved_to_next_step(post_step(run, CAPPED), htmx=False)
        assert status_of(run) == "done"

    with started(tmp_path) as run:
        assert run.decision == StartupDecision("guide", "preferences")
        assert status_of(run) == "done"
        assert budgets(run) == ({SERVICE: CAPPED_BUDGET}, 1)
        elements = outline(step_page(run).text)
        assert shown_values(elements) == CAPPED, "the saved budget is shown when reopened"
        assert form_version(elements) == "1"
        # Finish the guide.
        assert_see_other(post_preferences(run, valid_form()), DONE_PATH)
        assert_see_other(finish(run, "today"), "/")
        assert run.decision == StartupDecision("app")

    # --setup reopens the finished guide at its first step; nothing was lost.
    with started_with(tmp_path, setup=True) as run:
        assert run.decision == StartupDecision("guide", "welcome", reopened=True)
        assert_see_other(run.client.get("/"), "/setup/welcome")
        assert shown_values(outline(step_page(run).text)) == CAPPED
        assert "Done" in step_list_item(step_page(run), TITLE).text
        assert done_rows(run.client.get(DONE_PATH))[STEP].endswith("Done")
        assert budgets(run) == ({SERVICE: CAPPED_BUDGET}, 1)
        assert live_statuses(run) == {
            "welcome": "done",
            "provider": "done",
            STEP_ID: "done",
            "preferences": "done",
            "done": "done",
        }


def test_restart_after_skip_then_setup_flag_keeps_skipped(tmp_path: Path) -> None:
    """A skipped step stays skipped through finishing and ``--setup``: Done still lists it as
    "Skipped" with its link."""
    seed(tmp_path, done=ALL_DONE, preferences=SAVED)
    with started(tmp_path) as run:
        assert_moved_to_next_step(post_step(run, action="skip"), htmx=False)
        assert_see_other(finish(run, "today"), "/")

    with started(tmp_path) as run:
        assert run.decision == StartupDecision("app")
        assert banners(run.client.get("/").text) == []

    with started_with(tmp_path, setup=True) as run:
        assert run.decision == StartupDecision("guide", "welcome", reopened=True)
        assert status_of(run) == "skipped"
        assert done_rows(run.client.get(DONE_PATH))[STEP].endswith("Skipped")
        assert shown_values(outline(step_page(run).text)) == EMPTY
