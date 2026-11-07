"""Budget warning dialog and blocked actions, on the practice task (M2.24; AC-M2.7, AC-M2.10;
spec 2.11, 3.7; ADR-076 items 2, 3, 4, 7, 8; ADR-079; ADR-085; detailed design "Settings and
usage budgets", budget warnings).

Assumed interface (chosen here; the practice task on the ``fake`` service is the vehicle):

- ``POST /tasks/practice`` asks ``UsageService.preflight("fake", tier=<the form's tier>)``
  before it enqueues anything.
- ``Allow`` (and a service without a budget): the start is what it was before M2.24.
- ``Warn``: nothing is enqueued and nothing is acknowledged. The htmx answer is 200 and holds
  exactly one element with ``role="dialog"`` and ``aria-modal="true"``, named by
  ``aria-labelledby`` and described by ``aria-describedby`` (both pointing inside it). Its
  text names the service, the count after this request and the allowance (ADR-085: never
  "you have used") and the reset date as ``format_day`` writes it. Its controls:
  - "Continue": a submit button in a form inside the dialog that posts to
    ``/tasks/practice``. The form's named fields carry the start's own values (``duration``,
    ``outcome``, ``tier``) and ``budget_service`` (the service the warning is for),
    ``budget_threshold`` (``first`` | ``second``) and ``budget_cycle_start`` (the cycle's
    local start date, ISO).
  - "Not now": a button or link that changes nothing (it may close the dialog in the page
    only, or ask the server for the section again). It carries ``autofocus``, so the dialog
    opens with focus on it and a doubled Enter confirms nothing.
  The description also names the blocking point when it differs from the allowance, and
  says so when this request is the last one before it.
  - "Change limits": a link whose ``href`` ends in ``#usage-budgets``.
- A start that carries ``budget_service``, ``budget_threshold`` and ``budget_cycle_start`` is
  accepted only when they name the warning ``preflight`` reports right now for the service
  the route checks (same service, same threshold, current cycle). The
  threshold is then acknowledged and exactly one task enqueued; the answer is the usual
  started answer. Anything else acknowledges nothing for the named threshold and enqueues
  nothing.
- ``Note`` (a reached warning already acknowledged this cycle): the start enqueues at once;
  the htmx answer has no dialog and holds ``#practice-budget-note`` with the count after this
  request and the allowance. Focus stays where a start puts it (the status line); the note
  is added to the start's announcement.
- ``Blocked``: the Testing tools Start button is rendered ``disabled`` (or
  ``aria-disabled="true"``) and described (``aria-describedby``) by the sentence of
  ``BudgetBlocked`` (spec 2.11), next to a link whose ``href`` ends in ``#usage-budgets``. A
  POST enqueues nothing and answers 409 with the same sentence.
- A tier cap without a free Fast tier and unreadable budgets answer 409 too, with the reason
  above the Start button, which names it in ``aria-describedby``.
- ``GET /settings/testing-tools`` (launch token; changes nothing) answers the section alone.
  The section fetches it on the body's ``usage:changed`` event, sending the form's values,
  so a budget saved or removed on the same page blocks or frees the button without a reload.
- ``CapBlocked`` for Thorough while Fast is free: a dialog (same wiring) with a "Run once on
  Fast" submit button, "Not now" and "Change limits". Pressing it enqueues one task that
  runs on Fast; nothing stored changes, and the form still shows Thorough. With Fast not
  free, or for the Fast cap, there is no such button and nothing is enqueued.
- The connection test is never warned or blocked (ADR-076 item 3).

Budget used here: allowance 40, first warning 10, second 20, blocking point 30, reset day 12.
The frozen clock starts on 6 April 2026, so the cycle is 12 March to 12 April 2026 in every
time zone. A practice task makes ``PRACTICE_STEPS`` (5) requests. Real SQLite in a temp
folder, the fake provider, no network, no fixed waits; every value is fictional.
"""

import json
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from httpx import Response

from app.api.budget_gate import dialog_context
from app.domain.budgets import Allow, Blocked, Budget, CapBlocked, Note, Warn
from app.domain.errors import UsageLimitsUnreadable, format_day
from app.domain.settings import AI_PROVIDER
from app.domain.usage import SERVICE_NAMES
from app.modules.ai.providers.fake import FAKE_FAST_MODEL
from app.services.budget_gate import BudgetGate, ShowTierCap, Start
from app.services.practice import PRACTICE_STEPS
from app.services.settings import SettingsService
from app.services.usage import UsageService
from tests.integration.test_practice_task import (
    FAKE_SERVICE,
    MSG_STARTED,
    PRACTICE,
    PRACTICE_PATH,
    SETTINGS,
    STARTED,
    STATUS_ID,
    TOOLS_ID,
    Practice,
    form,
    practising,
)
from tests.integration.test_settings_usage_budgets import store_unreadable_budgets
from tests.integration.test_setup_provider import by_id, described_by, required
from tests.integration.test_setup_provider_test import FAKE_CONNECTED
from tests.integration.test_setup_redirect import ORIGIN, TOKEN_HEADER, Element, outline

pytestmark = pytest.mark.integration

BUDGET = Budget(allowance=40, first_warning=10, second_warning=20, block_at=30, reset_day=12)
THOROUGH_CAPPED = Budget(
    allowance=40, first_warning=10, second_warning=20, block_at=30, reset_day=12, thorough_cap=3
)
BOTH_CAPPED = Budget(
    allowance=40,
    first_warning=10,
    second_warning=20,
    block_at=30,
    reset_day=12,
    fast_cap=2,
    thorough_cap=3,
)
CYCLE_START = date(2026, 3, 12)
RESET_DATE = date(2026, 4, 12)
NEXT_RESET_DATE = date(2026, 5, 12)
SERVICE_NAME = SERVICE_NAMES[FAKE_SERVICE]
OTHER_SERVICE = "claude_api_key"
ECHO = "fictional_echo"  # a task type no handler is registered for: seeded usage only

CONTINUE = "Continue"
NOT_NOW = "Not now"
CHANGE_LIMITS = "Change limits"
RUN_ON_FAST = "Run once on Fast"
LIMITS_ANCHOR = "#usage-budgets"
SERVICE_FIELD = "budget_service"
THRESHOLD_FIELD = "budget_threshold"
CYCLE_FIELD = "budget_cycle_start"
TOOLS_PATH = "/settings/testing-tools"
TOOLS_TRIGGER = "usage:changed from:body"
BUDGETS_PATH = "/settings/usage/budgets"
STOPS_AT_30 = "The app stops at 30 requests (your blocking point)."
LAST_REQUEST = "This is the last request before the blocking point."
BLOCK_AT_ALLOWANCE = Budget(
    allowance=40, first_warning=10, second_warning=20, block_at=40, reset_day=12
)
NOTE_ID = "practice-budget-note"
SETTINGS_TEST = "/settings/providers/test"
HAVE_USED = re.compile(r"you(?: have|.ve) used", re.IGNORECASE)


def blocked_message(resets_on: date = RESET_DATE) -> str:
    """Spec 2.11, ``BudgetBlocked``."""
    return f"Monthly limit reached for {SERVICE_NAME}. Resets on {format_day(resets_on)}."


# --- helpers -----------------------------------------------------------------------------------


def usage(practice: Practice) -> UsageService:
    service: UsageService = practice.app.state.usage
    return service


def set_budget(practice: Practice, budget: Budget | None, service: str = FAKE_SERVICE) -> None:
    version = usage(practice).load_budgets().version
    usage(practice).save_budget(service, budget, expected_version=version)


def seed(
    practice: Practice, count: int, *, tier: str = "fast", service: str = FAKE_SERVICE
) -> None:
    """``count`` finished requests, written through the app's own usage service."""
    for _ in range(count):
        record_id = usage(practice).reserve(service, tier=tier, task_type=ECHO)
        usage(practice).complete(record_id, success=True, tokens=None)


def used(practice: Practice, service: str = FAKE_SERVICE) -> int:
    return usage(practice).status(service).local_count


def acknowledged(practice: Practice, service: str = FAKE_SERVICE) -> frozenset[str]:
    """The warnings stored as shown this cycle (``budget_notices``)."""
    return usage(practice).status(service).shown_thresholds


def says_number(text: str, number: int) -> bool:
    """``number`` as a whole number in ``text`` (not part of a longer one)."""
    return re.search(rf"(?<![\d.,]){number}(?![\d])", text) is not None


def dialogs(elements: list[Element]) -> list[Element]:
    """The dialogs a page or answer holds besides the command palette (M9.5), which every app
    page carries in its layout and which is not a budget question."""
    return [
        e for e in elements if e.attrs.get("role") == "dialog" and e.attrs.get("id") != "palette"
    ]


def the_dialog(response: Response) -> tuple[list[Element], Element]:
    """The one dialog an htmx answer holds, with its accessibility wiring checked."""
    assert response.status_code == 200, (response.status_code, response.text[:300])
    assert "<html" not in response.text, "an htmx answer is a fragment"
    elements = outline(response.text)
    found = dialogs(elements)
    assert len(found) == 1, f"expected one role=dialog element, found {len(found)}"
    (dialog,) = found
    assert dialog.attrs.get("aria-modal") == "true", dialog.attrs
    for attribute in ("aria-labelledby", "aria-describedby"):
        ids = (dialog.attrs.get(attribute) or "").split()
        assert ids, f"the dialog has {attribute}"
        for element_id in ids:
            target = required(elements, element_id)
            assert target.within(dialog), f"{attribute} points inside the dialog"
            assert target.text, f"#{element_id} has text"
    return elements, dialog


def controls(elements: list[Element], dialog: Element, name: str) -> list[Element]:
    return [e for e in elements if e.tag in ("button", "a") and e.within(dialog) and e.text == name]


def control(elements: list[Element], dialog: Element, name: str) -> Element:
    found = controls(elements, dialog, name)
    assert len(found) == 1, f'expected one "{name}" control in the dialog, found {len(found)}'
    return found[0]


def owning_form(elements: list[Element], button: Element) -> Element | None:
    form_id = button.attrs.get("form")
    if form_id:
        return required(elements, form_id)
    node = button.parent
    while node is not None and node.tag != "form":
        node = node.parent
    return node


def submitted_by(elements: list[Element], button: Element) -> tuple[str, dict[str, str]]:
    """Where a submit button posts and the named values its form sends with it."""
    owner = owning_form(elements, button)
    assert owner is not None, f'"{button.text}" sits in (or names) a form'
    assert (button.attrs.get("type") or "submit") == "submit", button.attrs
    path = (
        button.attrs.get("formaction")
        or button.attrs.get("hx-post")
        or owner.attrs.get("hx-post")
        or owner.attrs.get("action")
    )
    assert path, "the form says where it posts"
    owner_id = owner.attrs.get("id")
    values: dict[str, str] = {}
    for element in elements:
        name = element.attrs.get("name")
        inside = element.within(owner) or (owner_id and element.attrs.get("form") == owner_id)
        if not name or not inside:
            continue
        if element.tag == "input" and element.attrs.get("type") not in ("submit", "button"):
            values[name] = element.attrs.get("value") or ""
        elif element.tag == "select":
            options = [o for o in elements if o.tag == "option" and o.within(element)]
            chosen = next((o for o in options if "selected" in o.attrs), options[0])
            values[name] = chosen.attrs.get("value") or ""
    button_name = button.attrs.get("name")
    if button_name:
        values[button_name] = button.attrs.get("value") or ""
    return path, values


def post(practice: Practice, path: str, values: dict[str, str], *, htmx: bool = True) -> Response:
    headers = practice.run.action(**({"HX-Request": "true"} if htmx else {}))
    return practice.run.client.post(path, data=values, headers=headers)


def press(practice: Practice, elements: list[Element], button: Element) -> Response:
    """Submit the form the way pressing ``button`` does (htmx)."""
    path, values = submitted_by(elements, button)
    return post(practice, path, values)


def press_not_now(practice: Practice, elements: list[Element], button: Element) -> None:
    """Whatever request "Not now" makes, if it makes one (it may only close the dialog)."""
    attrs = button.attrs
    fetched = attrs.get("hx-get") or (attrs.get("href") if button.tag == "a" else None)
    if fetched:
        if not fetched.startswith("#"):
            response = practice.run.client.get(fetched, headers=practice.run.htmx())
            assert response.status_code == 200, (fetched, response.status_code)
        return
    posts = attrs.get("hx-post") or attrs.get("formaction")
    submits = button.tag == "button" and (attrs.get("type") or "submit") == "submit"
    if posts or (submits and owning_form(elements, button) is not None):
        path, values = submitted_by(elements, button)
        assert THRESHOLD_FIELD not in values, '"Not now" never names a threshold'
        response = post(practice, path, values)
        assert response.status_code < 500, response.status_code


def start_button(elements: list[Element]) -> Element:
    wrapper = required(elements, TOOLS_ID)
    found = [
        e
        for e in elements
        if e.tag == "button" and e.within(wrapper) and e.text.startswith("Start")
    ]
    assert len(found) == 1, f"expected one Start button in Testing tools, found {len(found)}"
    return found[0]


def is_disabled(button: Element) -> bool:
    return "disabled" in button.attrs or button.attrs.get("aria-disabled") == "true"


def limits_links(elements: list[Element], inside: Element) -> list[Element]:
    return [
        e
        for e in elements
        if e.tag == "a"
        and e.within(inside)
        and str(e.attrs.get("href") or "").endswith(LIMITS_ANCHOR)
        and e.text
    ]


def selected_values(elements: list[Element]) -> dict[str, str]:
    """The option marked selected in each of the practice form's selects."""
    owner = required(elements, "practice-form")
    chosen: dict[str, str] = {}
    for select in (e for e in elements if e.tag == "select" and e.within(owner)):
        marked = [
            str(o.attrs.get("value"))
            for o in elements
            if o.tag == "option" and o.within(select) and "selected" in o.attrs
        ]
        assert len(marked) <= 1, marked
        if marked:
            chosen[str(select.attrs.get("name"))] = marked[0]
    return chosen


def assert_reason_above_the_button(elements: list[Element], message: str) -> None:
    """The reason nothing starts sits before the Start button, which is described by it."""
    button = start_button(elements)
    reasons = [e for e in described_by(elements, button) if message in e.text]
    assert len(reasons) == 1, (button.attrs.get("aria-describedby"), message)
    (reason,) = reasons
    assert reason.within(required(elements, "practice-form"))
    assert elements.index(reason) < elements.index(button), "the reason comes first"


def tools(practice: Practice, **query: str) -> Response:
    """The Testing tools section alone, as the page fetches it after a budget change."""
    return practice.run.client.get(TOOLS_PATH, params=query, headers=practice.run.htmx())


def assert_nothing_started(practice: Practice, *, count: int) -> None:
    assert practice.tasks() == [], "nothing was enqueued"
    assert practice.fake.requests == [], "no provider call"
    assert used(practice) == count, "no usage record was written"


def crossing(practice: Practice, count: int, budget: Budget = BUDGET) -> None:
    """A budget on the Fake provider with ``count`` requests already used this cycle."""
    set_budget(practice, budget)
    seed(practice, count)


def finish(practice: Practice, before: int) -> None:
    """Wait for the one practice task enqueued since ``before`` tasks to succeed.

    Every earlier task went through here too, so all of them have succeeded: waiting for
    each row needs no "newest" (the clock is frozen and ids are random, so there is none)."""
    rows = practice.tasks()
    assert len(rows) == before + 1, [(row.id, str(row.status)) for row in rows]
    for row in rows:
        ended = practice.wait_terminal(row.id)
        assert str(ended.status.value) == "succeeded", ended.error


def continue_through(practice: Practice, response: Response) -> Response:
    elements, dialog = the_dialog(response)
    return press(practice, elements, control(elements, dialog, CONTINUE))


# --- the warning dialog ------------------------------------------------------------------------


def test_ac_m2_7_crossing_threshold_returns_dialog_with_count_allowance_reset_date(
    tmp_path: Path,
) -> None:
    """The next request reaches the first warning (9 used, the 10th is the warning point):
    the start enqueues nothing and answers with the dialog."""
    with practising(tmp_path) as practice:
        crossing(practice, 9)
        assert usage(practice).preflight(FAKE_SERVICE, tier="thorough") == Warn(
            "first", 10, 40, RESET_DATE
        )

        response = practice.start(htmx=True, duration="slow", outcome="network", tier="thorough")

        elements, dialog = the_dialog(response)
        text = dialog.text
        assert SERVICE_NAME in text, text
        assert says_number(text, 10), f"the count after this request (10): {text}"
        assert says_number(text, 40), f"the allowance (40): {text}"
        assert not says_number(text, 9), f"never the count used so far (ADR-085): {text}"
        assert HAVE_USED.search(text) is None, f'never "you have used" (ADR-085): {text}'
        assert format_day(RESET_DATE) in text, text
        described = " ".join(e.text for e in described_by(elements, dialog))
        assert says_number(described, 10) and says_number(described, 40), described
        assert format_day(RESET_DATE) in described, described
        assert STOPS_AT_30 in described, "the blocking point (30) isn't the allowance (40)"
        assert LAST_REQUEST not in described, described

        proceed = control(elements, dialog, CONTINUE)
        assert proceed.tag == "button", proceed.tag
        path, values = submitted_by(elements, proceed)
        assert path == PRACTICE_PATH, path
        assert values == {
            "duration": "slow",
            "outcome": "network",
            "tier": "thorough",
            SERVICE_FIELD: FAKE_SERVICE,
            THRESHOLD_FIELD: "first",
            CYCLE_FIELD: CYCLE_START.isoformat(),
        }
        not_now = control(elements, dialog, NOT_NOW)
        # The dialog opens with focus on "Not now" (a doubled Enter confirms nothing);
        # Continue still comes first and is the primary button.
        focused_first = [e for e in elements if e.within(dialog) and "autofocus" in e.attrs]
        assert focused_first == [not_now], [e.text for e in focused_first]
        assert elements.index(proceed) < elements.index(not_now)
        assert "button-primary" in str(proceed.attrs.get("class")).split()
        assert "button-primary" not in str(not_now.attrs.get("class")).split()
        change = control(elements, dialog, CHANGE_LIMITS)
        assert change.tag == "a", change.tag
        href = str(change.attrs.get("href") or "")
        assert href == f"/settings/budgets{LIMITS_ANCHOR}", href  # M9.2: the budgets area

        # Showing it changes nothing.
        assert_nothing_started(practice, count=9)
        assert acknowledged(practice) == frozenset()
        assert MSG_STARTED not in response.text


def test_ac_m2_7_dialog_is_shown_by_a_start_only(tmp_path: Path) -> None:
    """Reading Settings at a warning point shows no dialog and stores nothing (spec 2.9 rule
    12); a plain start (no htmx) enqueues nothing either."""
    with practising(tmp_path) as practice:
        crossing(practice, 9)
        for headers in ({}, practice.run.htmx()):
            page = practice.run.client.get(SETTINGS, headers=headers)
            assert page.status_code == 200
            elements = outline(page.text)
            assert dialogs(elements) == []
            assert not is_disabled(start_button(elements)), "a warning never disables the action"
        assert acknowledged(practice) == frozenset()

        plain = practice.start()
        assert plain.status_code == 409, plain.status_code
        assert "location" not in plain.headers
        assert_nothing_started(practice, count=9)
        assert acknowledged(practice) == frozenset()


def test_continue_acknowledges_and_enqueues(tmp_path: Path) -> None:
    with practising(tmp_path) as practice:
        practice.gate.hold = True  # the task stays in its first step
        crossing(practice, 9)
        shown = practice.start(htmx=True, duration="slow", outcome="ok", tier="thorough")
        elements, dialog = the_dialog(shown)
        proceed = control(elements, dialog, CONTINUE)

        answer = press(practice, elements, proceed)

        assert answer.status_code == 200, (answer.status_code, answer.text[:300])
        answered = outline(answer.text)
        assert dialogs(answered) == [], "the dialog is gone"
        status = by_id(answered, STATUS_ID)
        assert status is not None and status.text == MSG_STARTED
        task = practice.only_task()
        assert task.input_ref == "slow/ok/thorough", "the form's values are carried over"
        assert acknowledged(practice) == frozenset({"first"})
        practice.wait_entered(1)
        assert isinstance(usage(practice).preflight(FAKE_SERVICE, tier="thorough"), Note)

        # Pressed twice: still one task, and nothing else is acknowledged.
        again = press(practice, elements, proceed)
        assert again.status_code == 200, again.status_code
        assert len(practice.tasks()) == 1
        assert acknowledged(practice) == frozenset({"first"})


def test_not_now_enqueues_nothing(tmp_path: Path) -> None:
    with practising(tmp_path) as practice:
        crossing(practice, 9)
        elements, dialog = the_dialog(practice.start(htmx=True))

        press_not_now(practice, elements, control(elements, dialog, NOT_NOW))

        assert_nothing_started(practice, count=9)
        assert acknowledged(practice) == frozenset()
        # Not acknowledged: the next start asks again, with the same figures.
        elements, dialog = the_dialog(practice.start(htmx=True))
        assert says_number(dialog.text, 10) and says_number(dialog.text, 40), dialog.text
        assert_nothing_started(practice, count=9)


def test_ac_m2_7_second_crossing_same_cycle_shows_inline_note(tmp_path: Path) -> None:
    """Once the first warning was confirmed, a later start past it (and not yet at the
    second) runs at once and shows a note with the count instead of a dialog."""
    with practising(tmp_path) as practice:
        crossing(practice, 9)
        answer = continue_through(practice, practice.start(htmx=True))
        assert answer.status_code == 200, answer.text[:300]
        finish(practice, before=0)
        assert used(practice) == 9 + PRACTICE_STEPS == 14
        assert usage(practice).preflight(FAKE_SERVICE, tier="fast") == Note(15, 40, RESET_DATE)

        practice.gate.hold = True
        later = practice.start(htmx=True, duration="slow")

        assert later.status_code == 200, (later.status_code, later.text[:300])
        elements = outline(later.text)
        assert dialogs(elements) == [], "a warning is shown as a dialog once per cycle"
        assert len(practice.tasks()) == 2, "the start went through at once"
        note = required(elements, NOTE_ID)
        assert note.within(required(elements, TOOLS_ID))
        assert says_number(note.text, 15), f"the count after this request (15): {note.text}"
        assert says_number(note.text, 40), f"the allowance (40): {note.text}"
        assert HAVE_USED.search(note.text) is None, note.text
        status = required(elements, STATUS_ID)
        assert status.text == MSG_STARTED
        focused = [e.attrs.get("id") for e in elements if "data-autofocus" in e.attrs]
        assert focused == [STATUS_ID], "focus stays where a start puts it"
        assert note.text.startswith("Past a warning point: "), note.text
        said = json.loads(later.headers["hx-trigger"])
        assert said == {"announce": f"{MSG_STARTED} {note.text}"}, "the note is announced too"
        assert acknowledged(practice) == frozenset({"first"})

        # A start that only finds the task already running says nothing about the count.
        already = practice.start(htmx=True, duration="slow")
        assert by_id(outline(already.text), NOTE_ID) is None
        assert "Past a warning point" not in already.headers["hx-trigger"]


def test_ac_m2_7_no_note_before_any_warning_point(tmp_path: Path) -> None:
    """Below the first warning a budget changes nothing about a start."""
    with practising(tmp_path) as practice:
        practice.gate.hold = True
        crossing(practice, 3)
        assert usage(practice).preflight(FAKE_SERVICE, tier="fast") == Allow()
        response = practice.start(htmx=True, duration="slow")
        assert response.status_code == 200
        elements = outline(response.text)
        assert dialogs(elements) == []
        assert by_id(elements, NOTE_ID) is None
        assert required(elements, STATUS_ID).text == MSG_STARTED
        assert json.loads(response.headers["hx-trigger"]) == {"announce": MSG_STARTED}
        assert len(practice.tasks()) == 1


@pytest.mark.parametrize(
    ("budget", "count", "stops", "last"),
    [
        (BUDGET, 28, True, False),  # request 29: below the blocking point (30)
        (BUDGET, 29, True, True),  # request 30: the last one the app lets through
        (BLOCK_AT_ALLOWANCE, 19, False, False),  # the blocking point is the allowance
        (BLOCK_AT_ALLOWANCE, 39, False, True),  # request 40 of 40
    ],
    ids=["below", "last", "block-at-allowance", "last-of-allowance"],
)
def test_warning_names_the_blocking_point_and_the_last_request(
    tmp_path: Path, budget: Budget, count: int, stops: bool, last: bool
) -> None:
    """The dialog repeats the panel's sentence about a blocking point below the allowance,
    and says when this request is the last one before the app stops."""
    with practising(tmp_path) as practice:
        crossing(practice, count, budget)
        decision = usage(practice).preflight(FAKE_SERVICE, tier="fast")
        assert decision == Warn("second", count + 1, 40, RESET_DATE)

        elements, dialog = the_dialog(practice.start(htmx=True))

        described = " ".join(e.text for e in described_by(elements, dialog))
        assert (STOPS_AT_30 in described) is stops, described
        assert ("blocking point" in described) is (stops or last), described
        assert (LAST_REQUEST in described) is last, described
        assert says_number(described, count + 1) and says_number(described, 40), described
        assert format_day(RESET_DATE) in described, described
        assert_nothing_started(practice, count=count)
        if not last:
            return
        # Confirmed, the last request starts; the next start is blocked.
        practice.gate.hold = True
        answer = continue_through(practice, practice.start(htmx=True, duration="slow"))
        assert required(outline(answer.text), STATUS_ID).text == MSG_STARTED
        assert len(practice.tasks()) == 1


def test_ac_m2_7_each_warning_once_per_cycle_and_again_after_reset(tmp_path: Path) -> None:
    """First warning (dialog), second warning (dialog), then notes only; the next cycle
    starts over."""
    with practising(tmp_path) as practice:
        crossing(practice, 9)
        first = practice.start(htmx=True)
        assert says_number(the_dialog(first)[1].text, 10)
        continue_through(practice, first)
        finish(practice, before=0)
        assert acknowledged(practice) == frozenset({"first"})

        seed(practice, 5)  # 19 used: the next request is the second warning point
        assert used(practice) == 19
        second = practice.start(htmx=True)
        elements, dialog = the_dialog(second)
        assert says_number(dialog.text, 20) and says_number(dialog.text, 40), dialog.text
        _path, values = submitted_by(elements, control(elements, dialog, CONTINUE))
        assert values[THRESHOLD_FIELD] == "second", values
        assert len(practice.tasks()) == 1, "nothing enqueued by showing it"
        assert acknowledged(practice) == frozenset({"first"})
        continue_through(practice, second)
        finish(practice, before=1)
        assert acknowledged(practice) == frozenset({"first", "second"})

        third = practice.start(htmx=True)  # 24 used: past both, both confirmed
        assert third.status_code == 200
        elements = outline(third.text)
        assert dialogs(elements) == []
        assert says_number(required(elements, NOTE_ID).text, 25)
        finish(practice, before=2)

        # The next cycle (12 April to 12 May): nothing used, nothing confirmed.
        practice.clock.advance(timedelta(days=20))
        assert used(practice) == 0
        assert acknowledged(practice) == frozenset()
        seed(practice, 9)
        again = practice.start(htmx=True)
        elements, dialog = the_dialog(again)
        assert says_number(dialog.text, 10), dialog.text
        assert format_day(NEXT_RESET_DATE) in dialog.text, dialog.text
        _path, values = submitted_by(elements, control(elements, dialog, CONTINUE))
        assert values[CYCLE_FIELD] == RESET_DATE.isoformat(), "the new cycle's start"
        assert len(practice.tasks()) == 3
        assert acknowledged(practice) == frozenset()


@pytest.mark.parametrize("reset_day", [29, 30, 31])
def test_ac_m2_7_reset_date_on_a_short_month_in_dialog_and_block(
    tmp_path: Path, reset_day: int
) -> None:
    """In February 2027 a reset day of 29, 30 or 31 is the 28th: the dialog and the blocked
    action both say so."""
    resets_on = date(2027, 2, 28)
    budget = Budget(
        allowance=40, first_warning=10, second_warning=20, block_at=30, reset_day=reset_day
    )
    with practising(tmp_path) as practice:
        practice.clock.set(datetime(2027, 2, 10, 10, 0, tzinfo=UTC))
        crossing(practice, 9, budget)
        _elements, dialog = the_dialog(practice.start(htmx=True))
        assert format_day(resets_on) in dialog.text, dialog.text
        assert_nothing_started(practice, count=9)

        seed(practice, 21)  # 30 used: the blocking point
        blocked = practice.start(htmx=True)
        assert blocked_message(resets_on) in " ".join(blocked.text.split()), blocked.text[:400]
        assert_nothing_started(practice, count=30)


# --- forged or stale acknowledgements ----------------------------------------------------------

FORGED = {
    "second-not-reached": {THRESHOLD_FIELD: "second"},
    "the-block": {THRESHOLD_FIELD: "block"},
    "unknown-threshold": {THRESHOLD_FIELD: "allowance"},
    "threshold-case": {THRESHOLD_FIELD: "First"},
    "empty-threshold": {THRESHOLD_FIELD: ""},
    "last-cycle": {CYCLE_FIELD: "2026-02-12"},
    "next-cycle": {CYCLE_FIELD: "2026-04-12"},
    "not-a-date": {CYCLE_FIELD: "fictional"},
    "empty-cycle": {CYCLE_FIELD: ""},
    "no-cycle": {CYCLE_FIELD: None},
    "no-threshold": {THRESHOLD_FIELD: None},
    "other-service": {SERVICE_FIELD: OTHER_SERVICE},
    "unknown-service": {SERVICE_FIELD: "fictional_service"},
    "service-case": {SERVICE_FIELD: "Fake"},
    "empty-service": {SERVICE_FIELD: ""},
    "no-service": {SERVICE_FIELD: None},
}


@pytest.mark.parametrize("name", list(FORGED))
def test_forged_acknowledgement_for_other_threshold_refused(tmp_path: Path, name: str) -> None:
    """Only the first warning is being crossed. A Continue post naming anything but that
    warning in the current cycle acknowledges nothing and enqueues nothing: the server
    decides from ``preflight``, never from what was posted."""
    with practising(tmp_path) as practice:
        crossing(practice, 9)
        elements, dialog = the_dialog(practice.start(htmx=True))
        path, genuine = submitted_by(elements, control(elements, dialog, CONTINUE))
        assert genuine[SERVICE_FIELD] == FAKE_SERVICE
        assert genuine[THRESHOLD_FIELD] == "first"
        assert genuine[CYCLE_FIELD] == CYCLE_START.isoformat()
        forged: dict[str, str] = dict(genuine)
        for field_name, value in FORGED[name].items():
            if value is None:
                del forged[field_name]
            else:
                forged[field_name] = value

        for htmx in (True, False):
            response = post(practice, path, forged, htmx=htmx)
            assert response.status_code == (200 if htmx else 409), (name, response.status_code)
            assert MSG_STARTED not in response.text, name
            assert "location" not in response.headers, name
            if htmx:  # asked again, with the genuine fields
                asked, again = the_dialog(response)
                assert submitted_by(asked, control(asked, again, CONTINUE)) == (path, genuine)
            assert_nothing_started(practice, count=9)
            assert acknowledged(practice) == frozenset(), name

        # The genuine post is accepted, so the refusals above aren't a route that never works.
        accepted = post(practice, path, genuine)
        assert accepted.status_code == 200, accepted.text[:300]
        assert acknowledged(practice) == frozenset({"first"})
        finish(practice, before=0)


def test_forged_acknowledgement_of_first_while_crossing_second_refused(tmp_path: Path) -> None:
    """The next request reaches the second warning and nothing was confirmed. Naming the
    first warning (which is reached too) does not get past the second: nothing is enqueued
    and the dialog for the second warning is the answer."""
    with practising(tmp_path) as practice:
        crossing(practice, 19)
        assert usage(practice).preflight(FAKE_SERVICE, tier="fast") == Warn(
            "second", 20, 40, RESET_DATE
        )
        values = {
            **form(),
            SERVICE_FIELD: FAKE_SERVICE,
            THRESHOLD_FIELD: "first",
            CYCLE_FIELD: CYCLE_START.isoformat(),
        }

        response = post(practice, PRACTICE_PATH, values)

        assert_nothing_started(practice, count=19)
        assert "second" not in acknowledged(practice)
        elements, dialog = the_dialog(response)
        assert says_number(dialog.text, 20), dialog.text
        _path, genuine = submitted_by(elements, control(elements, dialog, CONTINUE))
        assert genuine[THRESHOLD_FIELD] == "second"


def test_forged_acknowledgement_never_reaches_another_service(tmp_path: Path) -> None:
    """The practice task belongs to the Fake provider. Both services are at the same warning
    in the same cycle, so only the service's name tells the two confirmations apart: one
    carrying the other service's name is refused. Nothing is acknowledged for either
    service, nothing is enqueued, and the answer is the dialog for the Fake provider."""
    with practising(tmp_path) as practice:
        set_budget(practice, BUDGET, OTHER_SERVICE)
        seed(practice, 9, service=OTHER_SERVICE)
        crossing(practice, 9)
        for name in (FAKE_SERVICE, OTHER_SERVICE):
            assert usage(practice).preflight(name, tier="fast") == Warn("first", 10, 40, RESET_DATE)
        values = {
            **form(),
            THRESHOLD_FIELD: "first",
            CYCLE_FIELD: CYCLE_START.isoformat(),
            "service": OTHER_SERVICE,
            SERVICE_FIELD: OTHER_SERVICE,
        }

        response = post(practice, PRACTICE_PATH, values)

        elements, dialog = the_dialog(response)
        assert SERVICE_NAME in dialog.text, dialog.text
        _path, genuine = submitted_by(elements, control(elements, dialog, CONTINUE))
        assert genuine[SERVICE_FIELD] == FAKE_SERVICE, "the dialog names the service checked"
        assert acknowledged(practice, OTHER_SERVICE) == frozenset()
        assert acknowledged(practice) == frozenset()
        assert used(practice, OTHER_SERVICE) == 9
        assert_nothing_started(practice, count=9)

        plain = post(practice, PRACTICE_PATH, values, htmx=False)
        assert plain.status_code == 409, plain.status_code
        assert acknowledged(practice, OTHER_SERVICE) == frozenset()
        assert acknowledged(practice) == frozenset()
        assert_nothing_started(practice, count=9)


@pytest.mark.parametrize("missing", ["token", "origin", "foreign_origin"])
def test_continue_needs_token_and_origin(tmp_path: Path, missing: str) -> None:
    with practising(tmp_path) as practice:
        crossing(practice, 9)
        elements, dialog = the_dialog(practice.start(htmx=True))
        path, values = submitted_by(elements, control(elements, dialog, CONTINUE))
        headers = {
            "token": {"origin": ORIGIN},
            "origin": practice.run.with_token(),
            "foreign_origin": practice.run.with_token(origin="http://fictional.example"),
        }[missing]
        response = practice.run.client.post(path, data=values, headers=headers)
        assert response.status_code == 403, response.status_code
        assert_nothing_started(practice, count=9)
        assert acknowledged(practice) == frozenset()


# --- the blocking point ------------------------------------------------------------------------


def test_ac_m2_7_blocked_action_disabled_with_reset_date_and_settings_link(
    tmp_path: Path,
) -> None:
    """At the blocking point (30 used; the allowance is 40) the Start button is disabled and
    described by the reason with the reset date, next to a link to the budgets; one request
    below it the button works."""
    with practising(tmp_path) as practice:
        crossing(practice, 29)
        elements = outline(practice.run.client.get(SETTINGS).text)
        assert not is_disabled(start_button(elements)), "29 of 30: not blocked yet"
        assert blocked_message() not in required(elements, TOOLS_ID).text

        seed(practice, 1)
        assert usage(practice).preflight(FAKE_SERVICE, tier="fast") == Blocked(RESET_DATE)
        for headers in ({}, practice.run.htmx()):
            page = practice.run.client.get(SETTINGS, headers=headers)
            assert page.status_code == 200
            elements = outline(page.text)
            wrapper = required(elements, TOOLS_ID)
            button = start_button(elements)
            assert is_disabled(button), button.attrs
            reasons = described_by(elements, button)
            assert any(blocked_message() in reason.text for reason in reasons), (
                button.attrs.get("aria-describedby"),
                [reason.text for reason in reasons],
            )
            assert limits_links(elements, wrapper), "a link to Settings > Usage budgets"
            assert_reason_above_the_button(elements, blocked_message())
            assert dialogs(elements) == []
        assert_nothing_started(practice, count=30)


@pytest.mark.parametrize("htmx", [False, True], ids=["plain", "htmx"])
def test_ac_m2_7_post_while_blocked_enqueues_nothing_and_explains(
    tmp_path: Path, htmx: bool
) -> None:
    """The disabled button is not the check: a direct POST is refused by the server, also
    when it carries an acknowledgement."""
    with practising(tmp_path) as practice:
        crossing(practice, 30)
        acknowledging = {
            SERVICE_FIELD: FAKE_SERVICE,
            THRESHOLD_FIELD: "second",
            CYCLE_FIELD: CYCLE_START.isoformat(),
        }
        for extra in ({}, acknowledging):
            response = practice.start(htmx=htmx, **extra)
            assert response.status_code == 409, response.status_code
            assert MSG_STARTED not in response.text
            assert "location" not in response.headers
            assert blocked_message() in " ".join(response.text.split()), response.text[:400]
            if htmx:
                assert "<html" not in response.text
                elements = outline(response.text)
                assert dialogs(elements) == [], "a block is not a question"
                assert is_disabled(start_button(elements))
                assert_reason_above_the_button(elements, blocked_message())
            else:
                assert response.text == blocked_message()
            assert_nothing_started(practice, count=30)
            assert acknowledged(practice) == frozenset()


# --- the Thorough cap --------------------------------------------------------------------------


def test_thorough_cap_offers_run_once_on_fast_and_runs_on_fast(tmp_path: Path) -> None:
    with practising(tmp_path) as practice:
        set_budget(practice, THOROUGH_CAPPED)
        seed(practice, 3, tier="thorough")
        assert usage(practice).preflight(FAKE_SERVICE, tier="thorough") == CapBlocked(
            "thorough", True, RESET_DATE
        )
        assert usage(practice).preflight(FAKE_SERVICE, tier="fast") == Allow()
        database = practice.app.state.workspace.db
        stored_before = SettingsService(database, practice.clock).load(AI_PROVIDER)
        budgets_before = usage(practice).load_budgets()

        offered = practice.start(htmx=True, outcome="ok", tier="thorough")

        elements, dialog = the_dialog(offered)
        assert "Thorough" in dialog.text, dialog.text
        assert SERVICE_NAME in dialog.text, dialog.text
        assert format_day(RESET_DATE) in dialog.text, dialog.text
        once = control(elements, dialog, RUN_ON_FAST)
        assert once.tag == "button", once.tag
        control(elements, dialog, NOT_NOW)
        assert control(elements, dialog, CHANGE_LIMITS).tag == "a"
        assert controls(elements, dialog, CONTINUE) == [], "a cap can't be continued past"
        assert_nothing_started(practice, count=3)

        answer = press(practice, elements, once)

        assert answer.status_code == 200, (answer.status_code, answer.text[:300])
        answered = outline(answer.text)
        assert dialogs(answered) == []
        assert required(answered, STATUS_ID).text == MSG_STARTED
        # The form still shows what the user chose, so the next start is offered Fast again
        # instead of quietly running there.
        assert selected_values(answered) == {
            "duration": "quick",
            "outcome": "ok",
            "tier": "thorough",
        }
        assert practice.only_task().input_ref == "quick/ok/fast"
        finish(practice, before=0)
        assert {request.model for request in practice.fake.requests} == {FAKE_FAST_MODEL}
        assert sorted(practice.usage_rows()) == [
            ("fast", PRACTICE, PRACTICE_STEPS, 0),
            ("thorough", ECHO, 3, 0),
        ], "the run was counted on Fast"

        # This one run only: nothing stored changed, and Thorough is still at its cap.
        assert SettingsService(database, practice.clock).load(AI_PROVIDER) == stored_before
        assert usage(practice).load_budgets() == budgets_before
        again = practice.start(htmx=True, tier="thorough")
        elements, dialog = the_dialog(again)
        control(elements, dialog, RUN_ON_FAST)
        assert len(practice.tasks()) == 1


def test_thorough_cap_not_now_enqueues_nothing(tmp_path: Path) -> None:
    with practising(tmp_path) as practice:
        set_budget(practice, THOROUGH_CAPPED)
        seed(practice, 3, tier="thorough")
        elements, dialog = the_dialog(practice.start(htmx=True, tier="thorough"))
        press_not_now(practice, elements, control(elements, dialog, NOT_NOW))
        assert_nothing_started(practice, count=3)
        # A Fast start is untouched by the Thorough cap.
        practice.gate.hold = True
        fast = practice.start(htmx=True, duration="slow", tier="fast")
        assert required(outline(fast.text), STATUS_ID).text == MSG_STARTED
        assert practice.only_task().input_ref == "slow/ok/fast"


@pytest.mark.parametrize("tier", ["thorough", "fast"])
def test_tier_cap_without_a_free_fast_tier_offers_no_fast_run(tmp_path: Path, tier: str) -> None:
    """Fast is at its own cap: a Thorough start gets no "Run once on Fast" (the offer needs
    a second check on Fast), and the Fast cap never has one. Nothing is enqueued; the answer
    is 409 with the reason above the Start button, which is described by it."""
    reason = (
        f"The {tier.capitalize()} limit for {SERVICE_NAME} is reached. "
        f"It resets on {format_day(RESET_DATE)}."
    )
    with practising(tmp_path) as practice:
        set_budget(practice, BOTH_CAPPED)
        seed(practice, 3, tier="thorough")
        seed(practice, 2, tier="fast")
        assert isinstance(usage(practice).preflight(FAKE_SERVICE, tier=tier), CapBlocked)

        response = practice.start(htmx=True, tier=tier)

        assert response.status_code == 409, response.status_code
        elements = outline(response.text)
        text = " ".join(e.text for e in elements if e.parent is None)
        assert [e for e in elements if e.tag == "button" and e.text == RUN_ON_FAST] == []
        assert dialogs(elements) == []
        assert MSG_STARTED not in text
        assert tier.capitalize() in text, text[:400]
        assert format_day(RESET_DATE) in text, "the reason names the reset date"
        assert_reason_above_the_button(elements, reason)
        assert not is_disabled(start_button(elements)), "the other tier may still start"
        focused = [e for e in elements if "data-autofocus" in e.attrs]
        assert len(focused) == 1 and reason in focused[0].text, "focus on the reason"
        assert limits_links(elements, focused[0]), "the link sits with the reason"
        assert selected_values(elements)["tier"] == tier
        assert_nothing_started(practice, count=5)

        plain = practice.start(tier=tier)
        assert plain.status_code == 409, plain.status_code
        assert plain.text == reason
        assert "location" not in plain.headers
        assert_nothing_started(practice, count=5)


# --- unreadable budgets ------------------------------------------------------------------------


@pytest.mark.parametrize("htmx", [False, True], ids=["plain", "htmx"])
def test_unreadable_budgets_start_nothing_and_say_how_to_fix_it(tmp_path: Path, htmx: bool) -> None:
    """The stored budgets can't be read (ADR-079, ADR-084: fail closed): a start, with or
    without a confirmation, enqueues nothing and answers with the sentence of
    ``UsageLimitsUnreadable`` (spec 2.11) next to a link to the budgets. No dialog."""
    message = UsageLimitsUnreadable().user_message
    with practising(tmp_path) as practice:
        store_unreadable_budgets(practice.run)
        with pytest.raises(UsageLimitsUnreadable):
            usage(practice).preflight(FAKE_SERVICE, tier="fast")
        confirming = {
            SERVICE_FIELD: FAKE_SERVICE,
            THRESHOLD_FIELD: "first",
            CYCLE_FIELD: CYCLE_START.isoformat(),
        }

        for extra in ({}, confirming):
            response = practice.start(htmx=htmx, **extra)

            assert response.status_code == 409, response.status_code
            assert MSG_STARTED not in response.text
            assert "location" not in response.headers
            if not htmx:
                assert response.text == message
            else:
                assert "<html" not in response.text
                elements = outline(response.text)
                assert dialogs(elements) == [], "there is nothing to confirm"
                wrapper = required(elements, TOOLS_ID)
                assert limits_links(elements, wrapper), "a link to Settings > Usage budgets"
                focused = [e for e in elements if "data-autofocus" in e.attrs]
                assert len(focused) == 1 and message in focused[0].text, "focus on the reason"
                assert focused[0].within(wrapper)
                assert_reason_above_the_button(elements, message)
            assert practice.tasks() == [], "nothing was enqueued"
            assert practice.fake.requests == [], "no provider call"
            assert acknowledged(practice) == frozenset()

        # Reading Settings shows no dialog and leaves the Start button alone: the reason
        # comes with a start (and the Usage budgets section has its own notice).
        elements = outline(practice.run.client.get(SETTINGS).text)
        assert dialogs(elements) == []
        assert not is_disabled(start_button(elements))


# --- no budget, and the connection test --------------------------------------------------------


@pytest.mark.parametrize("htmx", [False, True], ids=["plain", "htmx"])
def test_ac_m2_10_no_budget_no_dialog_no_block(tmp_path: Path, htmx: bool) -> None:
    """The Fake provider has no budget: whatever was used, and whatever another service's
    budget says, a start is never warned, noted or blocked."""
    with practising(tmp_path) as practice:
        set_budget(practice, BUDGET, OTHER_SERVICE)
        seed(practice, 30, service=OTHER_SERVICE)  # that service is at its blocking point
        seed(practice, 60)
        assert usage(practice).preflight(FAKE_SERVICE, tier="fast") == Allow()

        elements = outline(practice.run.client.get(SETTINGS).text)
        wrapper = required(elements, TOOLS_ID)
        assert not is_disabled(start_button(elements))
        assert "Monthly limit reached" not in wrapper.text
        assert by_id(elements, NOTE_ID) is None
        assert dialogs(elements) == []

        response = practice.start(htmx=htmx)

        assert response.status_code in STARTED, (response.status_code, response.text[:300])
        if htmx:
            answered = outline(response.text)
            assert dialogs(answered) == []
            assert by_id(answered, NOTE_ID) is None
            assert required(answered, STATUS_ID).text == MSG_STARTED
            assert not is_disabled(start_button(answered))
        else:
            assert "practice=started" in response.headers["location"]
        finish(practice, before=0)
        assert used(practice) == 60 + PRACTICE_STEPS
        assert acknowledged(practice) == frozenset()


def test_ac_m2_10_removing_the_budget_lifts_the_block(tmp_path: Path) -> None:
    with practising(tmp_path) as practice:
        crossing(practice, 30)
        blocked = practice.start(htmx=True)
        assert MSG_STARTED not in blocked.text
        assert_nothing_started(practice, count=30)

        set_budget(practice, None)

        elements = outline(practice.run.client.get(SETTINGS).text)
        assert not is_disabled(start_button(elements))
        assert "Monthly limit reached" not in required(elements, TOOLS_ID).text
        response = practice.start(htmx=True)
        assert response.status_code == 200
        answered = outline(response.text)
        assert dialogs(answered) == [] and by_id(answered, NOTE_ID) is None
        finish(practice, before=0)


def test_connection_test_is_never_warned_or_blocked(tmp_path: Path) -> None:
    """ADR-076 item 3: at a warning point and at the blocking point the connection test
    still runs, is counted, and shows no dialog and no limit message."""
    with practising(tmp_path) as practice:
        for count in (9, 30):
            if count == 9:
                crossing(practice, 9)
            else:
                seed(practice, count - used(practice))
            before = used(practice)
            response = post(practice, SETTINGS_TEST, {"kind": "fake", "credential": FAKE_CONNECTED})
            assert response.status_code == 200, (response.status_code, response.text[:300])
            elements = outline(response.text)
            assert dialogs(elements) == []
            assert "Monthly limit reached" not in response.text
            assert required(elements, "connection-result").text.startswith("Connected")
            assert used(practice) == before + 1, "counted as one request"
            assert acknowledged(practice) == frozenset()
        assert practice.tasks() == []


# --- Testing tools follows a budget change (the page refetches it on usage:changed) ------------


def budget_fields(budget: Budget) -> dict[str, str]:
    return {
        "allowance": str(budget.allowance),
        "first_warning": str(budget.first_warning),
        "second_warning": str(budget.second_warning),
        "block_at": str(budget.block_at),
        "reset_day": str(budget.reset_day),
    }


def change_budget(practice: Practice, budget: Budget | None) -> Response:
    """Save or remove the Fake provider's budget the way the Settings form does (htmx)."""
    values = {"service": FAKE_SERVICE, "version": str(usage(practice).load_budgets().version)}
    values.update({"action": "remove"} if budget is None else budget_fields(budget))
    response = post(practice, BUDGETS_PATH, values)
    assert response.status_code == 200, (response.status_code, response.text[:300])
    return response


def test_testing_tools_section_refetches_itself_when_budgets_change(tmp_path: Path) -> None:
    """The wrapper is the one fetcher: it asks for the section on the body's
    ``usage:changed`` event, with the form's values, and replaces itself. The dialog's form
    doesn't inherit that (its hidden fields are all it posts). A saved budget fires the
    event once its own answer has settled."""
    with practising(tmp_path) as practice:
        crossing(practice, 9)
        for response in (
            practice.run.client.get(SETTINGS),
            tools(practice),
            practice.start(htmx=True),
        ):
            wrapper = required(outline(response.text), TOOLS_ID)
            assert wrapper.attrs.get("hx-get") == TOOLS_PATH, wrapper.attrs
            assert wrapper.attrs.get("hx-trigger") == TOOLS_TRIGGER, wrapper.attrs
            assert wrapper.attrs.get("hx-swap") == "outerHTML", wrapper.attrs
            assert wrapper.attrs.get("hx-include") == "#practice-form", wrapper.attrs
            assert "hx-include" in str(wrapper.attrs.get("hx-disinherit")).split(), wrapper.attrs

        saved = change_budget(practice, BLOCK_AT_ALLOWANCE)
        assert saved.headers["hx-trigger-after-settle"] == "usage:changed"
        removed = change_budget(practice, None)
        assert removed.headers["hx-trigger-after-settle"] == "usage:changed"


def test_ac_m2_10_testing_tools_follows_a_budget_change_without_a_reload(tmp_path: Path) -> None:
    """Blocked at 30 of 30. Raising the blocking point, then removing the budget, then
    lowering the blocking point to the count again: each time the refetched section says
    what a start would meet. Nothing in it takes focus, and reading it changes nothing."""
    raised = Budget(allowance=60, first_warning=10, second_warning=20, block_at=50, reset_day=12)
    with practising(tmp_path) as practice:
        crossing(practice, 30)
        elements = outline(tools(practice).text)
        assert is_disabled(start_button(elements))
        assert_reason_above_the_button(elements, blocked_message())

        for budget, blocked in ((raised, False), (None, False), (BUDGET, True), (None, False)):
            change_budget(practice, budget)
            response = tools(practice, duration="slow", outcome="network", tier="thorough")

            assert response.status_code == 200, response.status_code
            assert "<html" not in response.text
            assert "hx-trigger" not in response.headers, "a refresh announces nothing"
            elements = outline(response.text)
            wrapper = required(elements, TOOLS_ID)
            assert [e for e in elements if e.parent is None] == [wrapper], "the section alone"
            assert "hx-swap-oob" not in wrapper.attrs
            assert is_disabled(start_button(elements)) is blocked, budget
            assert (blocked_message() in wrapper.text) is blocked, wrapper.text
            assert bool(limits_links(elements, wrapper)) is blocked
            assert [e for e in elements if "data-autofocus" in e.attrs] == [], "no focus moves"
            assert [e for e in elements if "autofocus" in e.attrs] == []
            assert dialogs(elements) == []
            assert by_id(elements, STATUS_ID) is None and by_id(elements, NOTE_ID) is None
            # What the user had chosen stays chosen.
            assert selected_values(elements) == {
                "duration": "slow",
                "outcome": "network",
                "tier": "thorough",
            }
        assert_nothing_started(practice, count=30)
        assert acknowledged(practice) == frozenset()

        # Freed: the start goes through (no budget, so no dialog).
        practice.gate.hold = True
        started = practice.start(htmx=True, duration="slow")
        assert required(outline(started.text), STATUS_ID).text == MSG_STARTED


def test_testing_tools_refetch_at_a_warning_shows_no_dialog_and_stores_nothing(
    tmp_path: Path,
) -> None:
    with practising(tmp_path) as practice:
        crossing(practice, 9)
        elements = outline(tools(practice).text)
        assert dialogs(elements) == []
        assert not is_disabled(start_button(elements))
        assert acknowledged(practice) == frozenset()
        assert_nothing_started(practice, count=9)


@pytest.mark.parametrize(
    "query",
    [
        {"duration": "fictional", "outcome": "ok", "tier": "fast"},
        {"duration": "slow", "outcome": "network"},
        {"duration": "slow", "outcome": "network", "tier": "Thorough"},
        {"tier": "<b>fictional</b>"},
    ],
)
def test_testing_tools_refetch_never_shows_back_a_refused_value(
    tmp_path: Path, query: dict[str, str]
) -> None:
    """As with a start: unless every field holds a listed value, nothing is selected and
    nothing that was sent is in the answer."""
    with practising(tmp_path) as practice:
        response = tools(practice, **query)
        assert response.status_code == 200
        assert "fictional" not in response.text
        elements = outline(response.text)
        assert selected_values(elements) == {}
        assert not is_disabled(start_button(elements))


def test_testing_tools_fragment_needs_the_token_and_is_get_only(tmp_path: Path) -> None:
    """``GET /settings/testing-tools`` is not a page: without the launch token it is refused
    and shows nothing; the token alone is enough (a GET needs no origin)."""
    with practising(tmp_path) as practice:
        crossing(practice, 30)
        run = practice.run
        for headers in (
            {},
            {"HX-Request": "true"},
            {"origin": ORIGIN},
            {TOKEN_HEADER: "fictional-wrong-token"},
        ):
            refused = run.client.get(TOOLS_PATH, headers=headers)
            assert refused.status_code == 403, (headers, refused.status_code)
            assert "location" not in refused.headers
            assert "Monthly limit reached" not in refused.text
            assert TOOLS_ID not in refused.text
        assert run.client.get(f"{TOOLS_PATH}?token={run.token}").status_code == 403

        answers = [run.client.get(TOOLS_PATH, headers=h) for h in (run.with_token(), run.htmx())]
        for allowed in answers:
            assert allowed.status_code == 200, allowed.status_code
            assert "<html" not in allowed.text, "the section alone, with or without HX-Request"
            assert is_disabled(start_button(outline(allowed.text)))
        assert answers[0].text == answers[1].text
        assert run.client.post(TOOLS_PATH, headers=run.action()).status_code == 405
        assert_nothing_started(practice, count=30)


# --- no workspace open -------------------------------------------------------------------------


def test_gate_without_an_open_workspace_starts_and_blocks_nothing() -> None:
    """While no workspace is open there is no usage to check: nothing is counted, so the
    gate neither refuses a start nor marks an action as blocked."""
    gate = BudgetGate(lambda: None)
    assert gate.check(FAKE_SERVICE, tier="thorough") == Start()
    assert gate.blocked(FAKE_SERVICE) is None


def test_run_once_on_fast_carries_the_chosen_tier_only_for_an_action_with_a_tier() -> None:
    """The dialog posts the action's own values again. With a tier among them it also says
    which one the form showed; an action without a tier field gets no such field."""
    cap = ShowTierCap("Thorough limit reached for Fake provider", "fictional reason", True)

    def posted(fields: dict[str, str]) -> dict[str, str]:
        context = dialog_context(
            cap,
            dialog_id="fictional-dialog",
            action="/fictional",
            target="#fictional",
            fields=fields,
            return_focus="#fictional-start",
        )
        assert context is not None
        return dict(context["fields"])

    assert posted({"tier": "thorough", "outcome": "ok"}) == {
        "tier": "fast",
        "outcome": "ok",
        "budget_chosen_tier": "thorough",
    }
    assert posted({"outcome": "ok"}) == {"tier": "fast", "outcome": "ok"}
