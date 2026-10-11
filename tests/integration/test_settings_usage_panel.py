"""Settings > the usage panel and its breakdown by tier and task (M2.23; AC-M2.6, AC-M2.8;
spec 3.7, 3.9; ADR-076 items 4, 5, 6, 12; ADR-078; ADR-079; ADR-084; detailed design
"Settings and usage budgets").

Assumed interface (chosen here, next to M2.22's ``#usage-budgets``):

- The Settings page and its htmx page fragment hold ``<section id="usage-panel">`` labelled
  (``aria-labelledby``) by a heading inside it whose text names "usage".
- ``GET /settings/usage`` (launch token; fragment only, the same with or without
  ``HX-Request``; no side effects) answers that section alone: ``#usage-panel`` is the root.
- One block per offered service, ``#usage-<service>``, in the order of ``SERVICE_NAMES``
  (``fake`` only while fakes are allowed), naming the service. Inside it:
  - ``#usage-<service>-count``: with a budget "<count> / <allowance>" and the word
    "requests"; without one the count alone. The count is the effective one (the higher of
    the local and a comparable provider count).
  - ``#usage-<service>-reset`` (budget only): "reset" and the reset date as ``format_day``
    writes it ("15 January 2026").
  - ``#usage-<service>-status`` (budget only), in words: "Within budget", "First warning
    reached", "Second warning reached" or "Blocking point reached" (the forms' word for
    it). Exactly one ``<progress>``/``<meter>`` (or those roles) with an accessible name,
    ``max`` the allowance and ``value`` the count that is used, at most the allowance.
  - ``#usage-<service>-block`` (only when the blocking point isn't the allowance): "The app
    stops at <n> requests (your blocking point)."
  - ``#usage-<service>-stopped`` (only once the blocking point is reached): "The app won't
    use this service until <reset day>."
  - ``#usage-<service>-cap-fast`` / ``-cap-thorough`` (only for a cap that is set):
    "Thorough tasks: 1 / 1." and, once reached (the rule ``reserve`` refuses by), "Cap
    reached; Thorough tasks won't run until <reset day>."
  - ``#usage-<service>-unfinished`` (only when calls are unfinished): "<n> call(s) is/are
    still running or didn't finish. It is/They are counted."
  - with a comparable provider count (ADR-079): ``#usage-<service>-local`` ("Counted on this
    computer" and the number), ``#usage-<service>-provider`` ("Reported by the provider" and
    the number) and ``#usage-<service>-used`` ("The provider's count is used" or "The count
    on this computer is used"). Without one there is no ``-provider`` element.
  - ``#usage-<service>-info``: every snapshot that can't be compared and hasn't reset (a
    fraction such as the Claude plan's utilization, written as a percentage; a request count
    for another window), each with the local time it was observed, under a sentence saying
    it is shown for information. It never changes the count. The compared snapshot is never
    listed there.
  - ``#usage-<service>-table-note`` (only when the provider's count is the one used): the
    table shows only requests made from this computer.
  - a service without a budget says "No limit" and has no allowance, reset or status; while
    the stored budgets can't be read every service says it is paused, never that it has no
    limit (ADR-084).
  - ``#usage-<service>-breakdown``: ``role="region"``, ``tabindex="0"`` and an accessible
    name, holding one ``<table>`` with a ``<caption>`` (or a label): a row per task type
    (``<th scope="row">``; a type no handler is registered for is shown by its id), columns
    ``<th scope="col">`` "Fast", "Thorough", "Total", "Input tokens" and "Output tokens", and
    a "Total" row. A zero is written "0". A service with no request this cycle has no table
    and says so in ``#usage-<service>-breakdown-empty``.

A real migrated SQLite database in a temp folder, the frozen clock (3 January 2026, so a
budget with reset day 15 resets on 15 January 2026 in every time zone), usage written through
the app's own ``UsageService``, no network, no provider call; every value is fictional.
"""

import re
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from httpx import Response

from app.api.security import TOKEN_HEADER
from app.domain.budgets import Budget
from app.domain.errors import TierCapReached, format_day
from app.domain.settings import AIProviderSettings
from app.domain.usage import SERVICE_NAMES
from app.modules.ai import ProviderUsageSnapshot, TokenUsage
from app.services.usage_meter import ServiceUsageMeter
from app.services.usage_panel import UsagePanelView
from tests.conftest import ALLOW_FAKES_VARIABLE
from tests.fakes.sdk_install import install_fake_sdk
from tests.integration.test_settings_usage_budgets import (
    ALL_SERVICES,
    FAKE_SERVICE,
    NO_LIMIT,
    REAL_SERVICES,
    VALID,
    VALID_BUDGET,
    post_budget,
    post_concurrency,
    settings_page,
    store_unreadable_budgets,
    usage,
    usage_section,
)
from tests.integration.test_setup_provider import EXISTING_KIND, by_id, only, required
from tests.integration.test_setup_redirect import (
    ORIGIN,
    START,
    Element,
    Running,
    outline,
)
from tests.integration.test_setup_reopen_banner import finished_guide, started
from tests.integration.test_task_routes import assert_fragment_only, database_state

pytestmark = pytest.mark.integration

PANEL_PATH = "/settings/usage"
PANEL_ID = "usage-panel"
SUBSCRIPTION = "claude_subscription"
WEB_SEARCH = "web_search"

ECHO = "fictional_echo"  # task types no handler is registered for: shown by their id
DRAFT = "fictional_draft"
TOKENS = TokenUsage(input_tokens=120, output_tokens=45)
ALLOWANCE = VALID_BUDGET.allowance  # 100; first warning 50, second 80, block 100, reset day 15
RESET_DATE = "15 January 2026"

# Copy the panel pins. Compared case-insensitively where a sentence may continue.
WITHIN = "Within budget"
FIRST_REACHED = "First warning reached"
SECOND_REACHED = "Second warning reached"
LIMIT_REACHED = "Blocking point reached"
STATUS_WORDS = (WITHIN, FIRST_REACHED, SECOND_REACHED, LIMIT_REACHED)
LOCAL_LABEL = "Counted on this computer"
PROVIDER_LABEL = "Reported by the provider"
PROVIDER_USED = "The provider's count is used"
LOCAL_USED = "The count on this computer is used"
INFO_INTRO = (
    "Your plan's own limits, as reported by the provider. Shown for information; your "
    "budget above counts requests only."
)
AS_OF = re.compile(r"\(as of \d{1,2} [A-Z][a-z]+ 202[56], \d{2}:\d{2}\)")
PAUSED = "Paused: the saved budgets couldn't be read. Save a budget again below."
TABLE_NOTE = "The table shows only requests made from this computer."
TASKS_UPDATED_EVENT = "tasks:updated"
RESYNC_EVENT = "tasks:resync"
# The panel's whole trigger: a saved or removed budget at once; task changes debounced.
PANEL_TRIGGER = "usage:changed from:body, tasks:updated from:body delay:1s"
COLUMNS = ("Fast", "Thorough", "Total", "Input tokens", "Output tokens")
TOTAL = "Total"
METER_TAGS = ("progress", "meter")
METER_ROLES = ("progressbar", "meter")
# The panel fetches itself again when the page fires this event; the htmx answer to a saved
# or removed budget fires it once it has settled.
CHANGED_EVENT = "usage:changed"
AFTER_SETTLE = "hx-trigger-after-settle"


# --- helpers -------------------------------------------------------------------------------


@pytest.fixture
def bundled_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The SDK looks installed with its bundled CLI (a placeholder that is never run), so a
    Claude kind's provider health is decided by its sign-in alone."""
    return install_fake_sdk(monkeypatch, tmp_path / "sdk", with_cli=True)


def call(
    run: Running,
    service: str,
    *,
    tier: str = "fast",
    task_type: str = ECHO,
    finished: bool | None = True,
    tokens: TokenUsage | None = TOKENS,
) -> str:
    """One counted request. ``finished``: True succeeded, False failed, None never completed
    (what a crash during the provider call leaves behind, AC-M2.6)."""
    record_id = usage(run).reserve(service, tier=tier, task_type=task_type)
    if finished is not None:
        usage(run).complete(record_id, success=finished, tokens=tokens if finished else None)
    return record_id


def calls(
    run: Running, service: str, count: int, *, tier: str = "fast", task_type: str = ECHO
) -> None:
    """``count`` finished requests."""
    for _ in range(count):
        call(run, service, tier=tier, task_type=task_type)


def set_budget(run: Running, service: str, budget: Budget = VALID_BUDGET) -> None:
    version = usage(run).load_budgets().version
    usage(run).save_budget(service, budget, expected_version=version)


def provider_reports(
    run: Running,
    service: str,
    used: int,
    *,
    window: str = "month",
    minutes: int = 0,
    resets_in_days: int | None = None,
) -> None:
    """A request count as the fake provider reports it during a call (ADR-078), saved the
    way the executor saves it: through the service's usage meter. ``minutes`` makes a later
    observation, which replaces the stored one for the same window."""
    cycle = usage(run).status(service).cycle
    snapshot = ProviderUsageSnapshot(
        service=service,
        window=window,
        unit="requests",
        used=used,
        limit=None,
        utilization=None,
        resets_at=None if resets_in_days is None else cycle.end + timedelta(days=resets_in_days),
        observed_at=START + timedelta(minutes=minutes),
    )
    ServiceUsageMeter(usage(run), service).snapshot(snapshot)


def provider_reports_fraction(run: Running, service: str, window: str, utilization: float) -> None:
    """A share of one of the provider's own windows, as the Claude plan reports it."""
    snapshot = ProviderUsageSnapshot(
        service=service,
        window=window,
        unit="fraction",
        used=None,
        limit=None,
        utilization=utilization,
        resets_at=START + timedelta(hours=3),
        observed_at=START,
    )
    ServiceUsageMeter(usage(run), service).snapshot(snapshot)


def panel_fragment(run: Running, *, htmx: bool = True) -> Response:
    headers = run.htmx() if htmx else run.with_token()
    return run.client.get(PANEL_PATH, headers=headers)


def views(run: Running) -> list[tuple[str, list[Element]]]:
    """The panel everywhere it is shown: the Settings page, its htmx page fragment and the
    panel's own fragment route."""
    shown = [
        ("page", outline(settings_page(run).text)),
        ("page fragment", outline(settings_page(run, htmx=True).text)),
    ]
    for _, elements in shown:
        panel(elements)
    fragment = panel_fragment(run)
    assert fragment.status_code == 200, (PANEL_PATH, fragment.status_code)
    return [*shown, ("panel fragment", outline(fragment.text))]


def panel(elements: list[Element]) -> Element:
    """``#usage-panel``: a section labelled by a heading inside it that names usage."""
    found = required(elements, PANEL_ID)
    assert found.tag == "section", found.tag
    heading = required(elements, str(found.attrs.get("aria-labelledby")))
    assert heading.tag in ("h2", "h3"), heading.tag
    assert heading.within(found)
    assert "usage" in heading.text.lower(), heading.text
    return found


def block(elements: list[Element], service: str) -> Element:
    """``#usage-<service>``, inside the panel, naming the service."""
    found = required(elements, f"usage-{service}")
    assert found.within(panel(elements)), service
    assert SERVICE_NAMES[service] in found.text, (service, found.text[:200])
    return found


def shown_services(elements: list[Element]) -> list[str]:
    ids = [str(e.attrs.get("id") or "") for e in elements]
    return [i.removeprefix("usage-") for i in ids if i.removeprefix("usage-") in SERVICE_NAMES]


def part(elements: list[Element], service: str, name: str) -> Element | None:
    """``#usage-<service>-<name>`` when the block has it."""
    found = by_id(elements, f"usage-{service}-{name}")
    if found is not None:
        assert found.within(block(elements, service)), (service, name)
    return found


def part_text(elements: list[Element], service: str, name: str) -> str:
    found = part(elements, service, name)
    assert found is not None, f"#usage-{service}-{name} is missing"
    assert found.text, f"#usage-{service}-{name} is empty"
    return found.text


def info_lines(elements: list[Element], service: str) -> list[str]:
    """The list items in ``#usage-<service>-info``: one per snapshot shown for information."""
    found = part(elements, service, "info")
    assert found is not None, f"#usage-{service}-info is missing"
    return [e.text for e in elements if e.tag == "li" and e.within(found)]


def meters(elements: list[Element], service: str) -> list[Element]:
    """The progress bars (by tag or role) in the service's block."""
    found = block(elements, service)
    return [
        e
        for e in elements
        if e.within(found) and (e.tag in METER_TAGS or e.attrs.get("role") in METER_ROLES)
    ]


def numbers(text: str) -> list[int]:
    return [int(digits) for digits in re.findall(r"\d+", text)]


def assert_count(elements: list[Element], service: str, count: int, allowance: int | None) -> None:
    """The count in text: "<count> / <allowance>" with a budget, the count alone without."""
    text = part_text(elements, service, "count")
    assert "request" in text.lower(), text
    if allowance is None:
        assert "/" not in text, text
        assert numbers(text) == [count], text
    else:
        assert f"{count} / {allowance}" in text, text
        assert numbers(text) == [count, allowance], text


def status_words(elements: list[Element], service: str) -> list[str]:
    """Which of the four statuses the status line says."""
    text = part_text(elements, service, "status").lower()
    return [words for words in STATUS_WORDS if words.lower() in text]


def assert_which_is_used(
    elements: list[Element], service: str, *, local: int, provider: int, used: str
) -> None:
    """Both counts in text, each with its label, and a sentence saying which one counts."""
    local_text = part_text(elements, service, "local")
    assert LOCAL_LABEL in local_text and numbers(local_text) == [local], local_text
    provider_text = part_text(elements, service, "provider")
    assert PROVIDER_LABEL in provider_text and numbers(provider_text) == [provider], provider_text
    said = part_text(elements, service, "used")
    other = LOCAL_USED if used == PROVIDER_USED else PROVIDER_USED
    assert used in said and other not in said, said


def assert_no_provider_count(elements: list[Element], service: str) -> None:
    assert part(elements, service, "provider") is None, "no comparable provider count"
    text = block(elements, service).text
    assert PROVIDER_USED not in text, text
    assert PROVIDER_LABEL not in text, text


def breakdown_region(elements: list[Element], service: str) -> Element:
    found = part(elements, service, "breakdown")
    assert found is not None, f"#usage-{service}-breakdown is missing"
    return found


def accessible_name(elements: list[Element], element: Element) -> str:
    """``aria-labelledby`` (the texts it points to) or ``aria-label``."""
    ids = (element.attrs.get("aria-labelledby") or "").split()
    named = " ".join(e.text for e in (by_id(elements, i) for i in ids) if e is not None)
    return named.strip() or (element.attrs.get("aria-label") or "").strip()


def breakdown_table(elements: list[Element], service: str) -> Element:
    region = breakdown_region(elements, service)
    return only(
        [e for e in elements if e.tag == "table" and e.within(region)],
        f"table in #usage-{service}-breakdown",
    )


def read_table(elements: list[Element], table: Element) -> dict[str, dict[str, str]]:
    """The table as ``{row header: {column header: cell text}}``. The first row holds the
    column headers (``<th scope="col">``); every other row starts with ``<th scope="row">``."""

    def cells(row: Element) -> list[Element]:
        return [e for e in elements if e.parent is row and e.tag in ("th", "td")]

    rows = [e for e in elements if e.tag == "tr" and e.within(table)]
    assert len(rows) >= 2, "a header row and at least one row"
    head, *body = rows
    columns = cells(head)
    for column in columns[1:]:
        assert column.tag == "th" and column.attrs.get("scope") == "col", column.attrs
    names = [column.text for column in columns]
    assert len(set(names[1:])) == len(names[1:]), names
    read: dict[str, dict[str, str]] = {}
    for row in body:
        row_cells = cells(row)
        assert len(row_cells) == len(names), (names, [c.text for c in row_cells])
        header = row_cells[0]
        assert header.tag == "th" and header.attrs.get("scope") == "row", header.attrs
        assert header.text and header.text not in read, header.text
        assert all(cell.tag == "td" for cell in row_cells[1:]), header.text
        read[header.text] = {
            name: cell.text for name, cell in zip(names[1:], row_cells[1:], strict=True)
        }
    return read


def row_for(table: dict[str, dict[str, str]], words: str) -> dict[str, str]:
    """The one row whose header holds ``words`` (case-insensitive)."""
    found = [cells for header, cells in table.items() if words.lower() in header.lower()]
    assert len(found) == 1, (words, list(table))
    return found[0]


def shown_numbers(row: dict[str, str], columns: tuple[str, ...] = COLUMNS) -> list[int]:
    for column in columns:
        assert column in row, (column, list(row))
        assert row[column].isdigit(), f"{column}: a number in text, a zero as 0 ({row[column]!r})"
    return [int(row[column]) for column in columns]


# --- where the panel is shown ------------------------------------------------------------------


def test_panel_on_the_settings_page_and_as_a_fragment(tmp_path: Path) -> None:
    """ADR-076 item 12: the panel comes with the page and is its own fragment route under
    ``/settings/usage``; every offered service has a block, in order, next to (never in
    place of) the budget forms."""
    finished_guide(tmp_path)
    with started(tmp_path) as run:
        for name, elements in views(run):
            found = panel(elements)
            assert shown_services(elements) == list(ALL_SERVICES), name
            for service in ALL_SERVICES:
                assert block(elements, service).within(found), (name, service)
                assert_count(elements, service, 0, None)
            assert not [e for e in elements if e.tag == "form" and e.within(found)], (
                f"{name}: the panel only shows; the budget forms stay in #usage-budgets"
            )
        usage_section(outline(settings_page(run).text))  # M2.22's section is still there

        htmx = panel_fragment(run)
        plain = panel_fragment(run, htmx=False)
        restore = run.client.get(
            PANEL_PATH, headers=run.htmx(**{"HX-History-Restore-Request": "true"})
        )
        for response in (htmx, plain, restore):
            assert response.status_code == 200, response.status_code
            assert response.headers["content-type"].startswith("text/html")
            assert_fragment_only(response.text)
            elements = outline(response.text)
            assert elements[0].attrs.get("id") == PANEL_ID, "the fragment's root comes first"
            assert all(e.within(elements[0]) for e in elements[1:]), "one root element"
            assert run.token not in response.text
            assert "hx-headers" not in response.text
            assert "hx-push-url" not in response.headers
        assert plain.text == htmx.text == restore.text


def test_fake_service_usage_shown_only_when_fakes_allowed(
    tmp_path: Path, bundled_cli: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-076 item 4: without the fake gate the panel has no Fake block, even when usage
    rows for it exist."""
    monkeypatch.delenv(ALLOW_FAKES_VARIABLE)
    finished_guide(tmp_path, provider=AIProviderSettings(kind=EXISTING_KIND))
    with started(tmp_path) as run:
        calls(run, FAKE_SERVICE, 2)
        calls(run, SUBSCRIPTION, 1)
        for name, elements in views(run):
            assert shown_services(elements) == list(REAL_SERVICES), name
            assert by_id(elements, f"usage-{FAKE_SERVICE}") is None, name
            assert SERVICE_NAMES[FAKE_SERVICE] not in panel(elements).text, name
            assert_count(elements, SUBSCRIPTION, 1, None)


# --- AC-M2.8: local and provider counts --------------------------------------------------------


def test_ac_m2_8_panel_shows_local_and_provider_counts_and_which_is_used(
    tmp_path: Path,
) -> None:
    """The fake provider reports 9 requests this month while 3 were counted here: the panel
    shows both, says the provider's count is the one used, and "count / allowance" uses it."""
    finished_guide(tmp_path)
    with started(tmp_path) as run:
        set_budget(run, FAKE_SERVICE)
        calls(run, FAKE_SERVICE, 3)
        provider_reports(run, FAKE_SERVICE, 9)
        status = usage(run).status(FAKE_SERVICE)
        assert (status.local_count, status.provider_count) == (3, 9)
        assert (status.effective.value, status.effective.source) == (9, "provider")

        for _, elements in views(run):
            assert_count(elements, FAKE_SERVICE, 9, ALLOWANCE)
            assert_which_is_used(elements, FAKE_SERVICE, local=3, provider=9, used=PROVIDER_USED)
            assert part(elements, FAKE_SERVICE, "info") is None, (
                "the compared snapshot is not information only"
            )
            # Another service's counts are its own.
            assert_count(elements, WEB_SEARCH, 0, None)
            assert_no_provider_count(elements, WEB_SEARCH)


def test_ac_m2_8_provider_count_decides_the_status(tmp_path: Path) -> None:
    """The status in words follows the count that is used, not the local one."""
    finished_guide(tmp_path)
    with started(tmp_path) as run:
        set_budget(run, FAKE_SERVICE)  # first warning at 50, second at 80
        calls(run, FAKE_SERVICE, 3)
        for _, elements in views(run):
            assert status_words(elements, FAKE_SERVICE) == [WITHIN]

        provider_reports(run, FAKE_SERVICE, 60)
        for _, elements in views(run):
            assert_count(elements, FAKE_SERVICE, 60, ALLOWANCE)
            assert status_words(elements, FAKE_SERVICE) == [FIRST_REACHED]


def test_ac_m2_8_equal_or_lower_provider_count_keeps_the_local_one(tmp_path: Path) -> None:
    """A provider count that is equal to or lower than the local one is shown, and the panel
    says the local count is the one used."""
    finished_guide(tmp_path)
    with started(tmp_path) as run:
        set_budget(run, FAKE_SERVICE)
        calls(run, FAKE_SERVICE, 4)
        provider_reports(run, FAKE_SERVICE, 4)
        for _, elements in views(run):
            assert_count(elements, FAKE_SERVICE, 4, ALLOWANCE)
            assert_which_is_used(elements, FAKE_SERVICE, local=4, provider=4, used=LOCAL_USED)

        calls(run, FAKE_SERVICE, 2)
        for _, elements in views(run):
            assert_count(elements, FAKE_SERVICE, 6, ALLOWANCE)
            assert_which_is_used(elements, FAKE_SERVICE, local=6, provider=4, used=LOCAL_USED)

        # Without a budget the two counts are still shown, and which one is used.
        calls(run, WEB_SEARCH, 2)
        provider_reports(run, WEB_SEARCH, 5)
        for _, elements in views(run):
            assert_count(elements, WEB_SEARCH, 5, None)
            assert_which_is_used(elements, WEB_SEARCH, local=2, provider=5, used=PROVIDER_USED)


def test_ac_m2_8_snapshot_with_other_unit_or_window_is_information_only(
    tmp_path: Path,
) -> None:
    """ADR-076 item 6, ADR-079: a request count for another window, a fraction, and a month
    count whose reset isn't the cycle's never change the count, however high they are."""
    finished_guide(tmp_path)
    with started(tmp_path) as run:
        set_budget(run, FAKE_SERVICE)
        calls(run, FAKE_SERVICE, 3)
        provider_reports(run, FAKE_SERVICE, 77, window="week")
        provider_reports_fraction(run, FAKE_SERVICE, "five_hour", 0.25)
        provider_reports(run, FAKE_SERVICE, 66, resets_in_days=1)  # another cycle's month
        status = usage(run).status(FAKE_SERVICE)
        assert status.provider_count is None and status.effective.value == 3
        assert len(status.provider_snapshots) == 3

        for _, elements in views(run):
            assert_count(elements, FAKE_SERVICE, 3, ALLOWANCE)
            assert status_words(elements, FAKE_SERVICE) == [WITHIN]
            assert_no_provider_count(elements, FAKE_SERVICE)
            info = part_text(elements, FAKE_SERVICE, "info")
            assert INFO_INTRO in info, info
            assert "77" in info, "the week's request count is shown"
            assert "25%" in info, "the fraction is shown as a percentage"
            lines = info_lines(elements, FAKE_SERVICE)
            assert len(lines) == 3, lines
            for words in ("Week: 77 requests", "5-hour window: 25% used", "Month: 66 requests"):
                (line,) = [found for found in lines if found.startswith(f"{words} (as of ")]
                assert AS_OF.fullmatch(line.removeprefix(f"{words} ")), line
            assert part(elements, WEB_SEARCH, "info") is None, "nothing reported: no line"


def test_claude_plan_utilization_is_information_only(tmp_path: Path) -> None:
    """ADR-076 item 6: the Claude plan's utilization (a fraction of the plan's own windows)
    is shown as information and never counted against the request budget."""
    finished_guide(tmp_path)
    with started(tmp_path) as run:
        set_budget(run, SUBSCRIPTION)
        calls(run, SUBSCRIPTION, 2)
        provider_reports_fraction(run, SUBSCRIPTION, "five_hour", 0.25)
        provider_reports_fraction(run, SUBSCRIPTION, "seven_day", 0.5)

        for _, elements in views(run):
            assert_count(elements, SUBSCRIPTION, 2, ALLOWANCE)
            assert status_words(elements, SUBSCRIPTION) == [WITHIN]
            assert_no_provider_count(elements, SUBSCRIPTION)
            info = part_text(elements, SUBSCRIPTION, "info")
            assert INFO_INTRO in info, info
            assert "25%" in info and "50%" in info, info
            for other in set(ALL_SERVICES) - {SUBSCRIPTION}:
                assert part(elements, other, "info") is None, other


# --- AC-M2.6: unfinished calls -----------------------------------------------------------------


def test_ac_m2_6_unfinished_call_shown_and_counted(tmp_path: Path) -> None:
    """A reserved call that never completed (what a crash during the provider call leaves)
    is in the count, and the panel says so in text. A failed call is finished: counted, not
    noted."""
    finished_guide(tmp_path)
    with started(tmp_path) as run:
        set_budget(run, FAKE_SERVICE)
        call(run, FAKE_SERVICE)
        call(run, FAKE_SERVICE, finished=False)
        for _, elements in views(run):
            assert_count(elements, FAKE_SERVICE, 2, ALLOWANCE)
            assert part(elements, FAKE_SERVICE, "unfinished") is None, "every call finished"

        call(run, FAKE_SERVICE, finished=None)
        call(run, WEB_SEARCH, finished=None)  # a service without a budget says it too
        assert usage(run).status(FAKE_SERVICE).unfinished_count == 1
        for _, elements in views(run):
            assert_count(elements, FAKE_SERVICE, 3, ALLOWANCE)
            assert_count(elements, WEB_SEARCH, 1, None)
            for service in (FAKE_SERVICE, WEB_SEARCH):
                note = part_text(elements, service, "unfinished")
                assert note.startswith("1 call "), note
                assert "finish" in note and "is counted" in note, note
                assert note == "1 call is still running or didn't finish. It is counted.", note
            assert part(elements, SUBSCRIPTION, "unfinished") is None

        call(run, FAKE_SERVICE, tier="thorough", finished=None)
        for _, elements in views(run):
            assert_count(elements, FAKE_SERVICE, 4, ALLOWANCE)
            note = part_text(elements, FAKE_SERVICE, "unfinished")
            assert note.startswith("2 calls "), note
            assert "finish" in note and "are counted" in note, note
            assert note == "2 calls are still running or didn't finish. They are counted.", note


# --- the meter ---------------------------------------------------------------------------------


def test_meter_status_in_text_not_colour_only(tmp_path: Path) -> None:
    """Spec 3.9: the count, the allowance, the reset date and the status are all text. A bar
    is optional; when there is one it has an accessible name and the numbers stay in text."""
    budget = Budget(allowance=8, first_warning=3, second_warning=5, block_at=7, reset_day=15)
    expected = {1: WITHIN, 3: FIRST_REACHED, 5: SECOND_REACHED, 7: LIMIT_REACHED}
    finished_guide(tmp_path)
    with started(tmp_path) as run:
        set_budget(run, FAKE_SERVICE, budget)
        assert format_day(usage(run).status(FAKE_SERVICE).cycle.reset_date) == RESET_DATE
        count = 0
        for target, words in expected.items():
            calls(run, FAKE_SERVICE, target - count)
            count = target
            for name, elements in views(run):
                assert_count(elements, FAKE_SERVICE, count, 8)
                assert status_words(elements, FAKE_SERVICE) == [words], (name, count)
                reset = part_text(elements, FAKE_SERVICE, "reset")
                assert "reset" in reset.lower() and RESET_DATE in reset, reset

                found = block(elements, FAKE_SERVICE)
                bars = [
                    e
                    for e in elements
                    if e.within(found)
                    and (e.tag in METER_TAGS or e.attrs.get("role") in METER_ROLES)
                ]
                for bar in bars:
                    assert accessible_name(elements, bar), f"a bar without a name: {bar.attrs}"


# --- no budget ---------------------------------------------------------------------------------


def test_service_without_budget_shows_usage_without_limits(tmp_path: Path) -> None:
    """AC-M2.10 on the panel: usage is shown, with no allowance, no reset date, no status
    and no warning, however much the service is used; a service with a budget next to it
    keeps its own."""
    finished_guide(tmp_path)
    with started(tmp_path) as run:
        set_budget(run, FAKE_SERVICE)
        calls(run, FAKE_SERVICE, 1)
        calls(run, WEB_SEARCH, 150)  # more than the other service's allowance
        assert usage(run).status(WEB_SEARCH).budget is None

        for _, elements in views(run):
            assert_count(elements, WEB_SEARCH, 150, None)
            text = block(elements, WEB_SEARCH).text
            assert NO_LIMIT in text, text
            assert part(elements, WEB_SEARCH, "reset") is None
            assert part(elements, WEB_SEARCH, "status") is None
            lowered = text.lower()
            assert "reset" not in lowered and "warning" not in lowered, text
            for words in STATUS_WORDS:
                assert words.lower() not in lowered, (words, text)
            table = read_table(elements, breakdown_table(elements, WEB_SEARCH))
            assert shown_numbers(table[TOTAL], COLUMNS[:3]) == [150, 0, 150]

            assert_count(elements, FAKE_SERVICE, 1, ALLOWANCE)
            assert NO_LIMIT not in block(elements, FAKE_SERVICE).text
            assert status_words(elements, FAKE_SERVICE) == [WITHIN]


def test_unreadable_budgets_are_never_shown_as_no_limit_in_the_panel(tmp_path: Path) -> None:
    """ADR-084: while the stored budgets can't be read, the panel still shows the usage and
    never says a service has no limit."""
    finished_guide(tmp_path)
    with started(tmp_path) as run:
        calls(run, FAKE_SERVICE, 2)
        store_unreadable_budgets(run)
        assert usage(run).status(FAKE_SERVICE).budgets_unreadable

        for name, elements in views(run):
            assert NO_LIMIT not in panel(elements).text, name
            for service in ALL_SERVICES:
                assert_count(elements, service, 2 if service == FAKE_SERVICE else 0, None)
                assert part(elements, service, "status") is None, service
                assert PAUSED in block(elements, service).text, (name, service)


# --- the breakdown -----------------------------------------------------------------------------


def seed_breakdown(run: Running) -> None:
    """Seven requests on the fake service: by tier 4 Fast and 3 Thorough; by task 4 echo,
    2 draft (one never finished) and the connection test."""
    calls(run, FAKE_SERVICE, 3, tier="fast", task_type=ECHO)
    calls(run, FAKE_SERVICE, 1, tier="thorough", task_type=ECHO)
    call(run, FAKE_SERVICE, tier="thorough", task_type=DRAFT)
    call(run, FAKE_SERVICE, tier="thorough", task_type=DRAFT, finished=None)
    tested = usage(run).reserve(FAKE_SERVICE, tier="fast", skip_budget=True)
    usage(run).complete(tested, success=True, tokens=None)


def test_breakdown_totals_by_tier_and_task(tmp_path: Path) -> None:
    """Rows by task type, columns by tier, request counts, and totals that add up to the
    service's count; token counts where they were recorded (ADR-076 item 5)."""
    finished_guide(tmp_path)
    with started(tmp_path) as run:
        set_budget(run, FAKE_SERVICE)
        seed_breakdown(run)
        calls(run, WEB_SEARCH, 2, tier="thorough", task_type=DRAFT)  # never mixed in
        assert usage(run).status(FAKE_SERVICE).local_count == 7

        for name, elements in views(run):
            assert_count(elements, FAKE_SERVICE, 7, ALLOWANCE)
            table = read_table(elements, breakdown_table(elements, FAKE_SERVICE))
            assert len(table) == 4, f"{name}: three task types and the total row: {list(table)}"

            assert shown_numbers(row_for(table, ECHO)) == [3, 1, 4, 480, 180]
            assert shown_numbers(row_for(table, DRAFT)) == [0, 2, 2, 120, 45]
            assert shown_numbers(row_for(table, "connection"), COLUMNS[:3]) == [1, 0, 1]
            assert shown_numbers(table[TOTAL]) == [4, 3, 7, 600, 225]

            # Totals add up, whatever the rows are called.
            tasks = [shown_numbers(row) for header, row in table.items() if header != TOTAL]
            sums = [sum(column) for column in zip(*tasks, strict=True)]
            assert sums == shown_numbers(table[TOTAL])
            for fast, thorough, total, _, _ in (*tasks, shown_numbers(table[TOTAL])):
                assert fast + thorough == total

            other = read_table(elements, breakdown_table(elements, WEB_SEARCH))
            assert len(other) == 2, list(other)
            assert shown_numbers(row_for(other, DRAFT)) == [0, 2, 2, 240, 90]
            assert shown_numbers(other[TOTAL]) == [0, 2, 2, 240, 90]


def test_breakdown_total_is_the_local_count_when_the_provider_count_is_used(
    tmp_path: Path,
) -> None:
    """The table lists what was counted here; a higher provider count changes the count that
    is used, never the table."""
    finished_guide(tmp_path)
    with started(tmp_path) as run:
        set_budget(run, FAKE_SERVICE)
        calls(run, FAKE_SERVICE, 3)
        provider_reports(run, FAKE_SERVICE, 9)
        for _, elements in views(run):
            assert_count(elements, FAKE_SERVICE, 9, ALLOWANCE)
            table = read_table(elements, breakdown_table(elements, FAKE_SERVICE))
            assert shown_numbers(table[TOTAL], COLUMNS[:3]) == [3, 0, 3]
            assert part_text(elements, FAKE_SERVICE, "table-note") == TABLE_NOTE

        calls(run, FAKE_SERVICE, 7)  # 10 counted here: the local count is the one used again
        for _, elements in views(run):
            assert_count(elements, FAKE_SERVICE, 10, ALLOWANCE)
            assert part(elements, FAKE_SERVICE, "table-note") is None
            assert TABLE_NOTE not in panel(elements).text


def test_breakdown_table_is_labelled_and_scrolls_in_its_own_region(tmp_path: Path) -> None:
    """Spec 3.9 and the htmx-ui skill: a real table (caption or label, scoped headers) inside
    its own container, which keyboard users can focus and scroll and which has a name."""
    finished_guide(tmp_path)
    with started(tmp_path) as run:
        seed_breakdown(run)
        calls(run, WEB_SEARCH, 1)
        for name, elements in views(run):
            names = []
            for service in (FAKE_SERVICE, WEB_SEARCH):
                region = breakdown_region(elements, service)
                assert region.attrs.get("role") == "region", region.attrs
                assert region.attrs.get("tabindex") == "0", region.attrs
                names.append(accessible_name(elements, region))
                assert names[-1], f"{name}: the {service} region has no accessible name"

                table = breakdown_table(elements, service)
                assert table.attrs.get("role") in (None, "table"), table.attrs
                captions = [e for e in elements if e.tag == "caption" and e.parent is table]
                caption = captions[0].text if captions else accessible_name(elements, table)
                assert caption, f"{name}: the {service} table has no caption or label"
                headers = [
                    e.text
                    for e in elements
                    if e.tag == "th" and e.within(table) and e.attrs.get("scope") == "col"
                ]
                for column in COLUMNS:
                    assert headers.count(column) == 1, (column, headers)
                unscoped = [
                    e.text
                    for e in elements
                    if e.tag == "th" and e.within(table) and e.attrs.get("scope") is None
                ]
                assert unscoped == [], f"every header cell has a scope: {unscoped}"
            assert len(set(names)) == len(names), f"each region has its own name: {names}"


def test_service_without_usage_shows_zero_and_no_table(tmp_path: Path) -> None:
    """The empty state: a service with no request this cycle shows 0, says there is nothing
    to break down, and has no empty table or scroll region to tab into."""
    finished_guide(tmp_path)
    with started(tmp_path) as run:
        set_budget(run, FAKE_SERVICE)
        calls(run, WEB_SEARCH, 1)
        for name, elements in views(run):
            assert_count(elements, FAKE_SERVICE, 0, ALLOWANCE)
            assert status_words(elements, FAKE_SERVICE) == [WITHIN]
            for service in (FAKE_SERVICE, SUBSCRIPTION):
                found = block(elements, service)
                assert part(elements, service, "breakdown") is None, (name, service)
                assert not [e for e in elements if e.tag == "table" and e.within(found)]
                assert part_text(elements, service, "breakdown-empty")
                assert part(elements, service, "unfinished") is None
            assert part(elements, WEB_SEARCH, "breakdown-empty") is None
            breakdown_table(elements, WEB_SEARCH)


# --- no side effects, token --------------------------------------------------------------------


def test_usage_panel_get_has_no_side_effects(tmp_path: Path) -> None:
    """Spec 2.9 rule 12: showing the panel (page, page fragment, its own route), with a
    budget past its warnings, an unfinished call and provider snapshots, changes no row
    and creates no file; no warning is marked as shown and no unfinished call is completed."""
    finished_guide(tmp_path)
    with started(tmp_path) as run:
        data_dir = run.app.state.workspace.db.path.parent
        near = Budget(allowance=10, first_warning=2, second_warning=3, block_at=10, reset_day=31)
        set_budget(run, FAKE_SERVICE, near)
        seed_breakdown(run)
        provider_reports(run, FAKE_SERVICE, 9)
        provider_reports_fraction(run, SUBSCRIPTION, "five_hour", 0.25)
        before = database_state(data_dir)
        assert ECHO in before[0], "the dump holds the usage rows"

        for _ in range(2):
            for name, elements in views(run):
                assert_count(elements, FAKE_SERVICE, 9, 10)
                assert status_words(elements, FAKE_SERVICE) == [SECOND_REACHED], name
            assert panel_fragment(run, htmx=False).status_code == 200

        assert database_state(data_dir) == before, "showing the panel wrote nothing"
        status = usage(run).status(FAKE_SERVICE)
        assert status.shown_thresholds == frozenset()
        assert status.unfinished_count == 1


def test_usage_panel_fragment_needs_the_token(tmp_path: Path) -> None:
    """``GET /settings/usage`` is not a page: without the launch token it is refused and
    shows nothing; the token alone is enough (a GET needs no origin); it is GET only."""
    finished_guide(tmp_path)
    with started(tmp_path) as run:
        set_budget(run, FAKE_SERVICE)
        calls(run, FAKE_SERVICE, 3)
        for headers in (
            {},
            {"HX-Request": "true"},
            {"origin": ORIGIN},
            {TOKEN_HEADER: "fictional-wrong-token"},
        ):
            refused = run.client.get(PANEL_PATH, headers=headers)
            assert refused.status_code == 403, (headers, refused.status_code)
            assert "location" not in refused.headers
            assert "hx-redirect" not in refused.headers
            assert SERVICE_NAMES[FAKE_SERVICE] not in refused.text
            assert PANEL_ID not in refused.text
        assert run.client.get(f"{PANEL_PATH}?token={run.token}").status_code == 403

        for headers in (run.with_token(), run.htmx()):
            allowed = run.client.get(PANEL_PATH, headers=headers)
            assert allowed.status_code == 200, allowed.status_code
            assert_count(outline(allowed.text), FAKE_SERVICE, 3, ALLOWANCE)

        posted = run.client.post(PANEL_PATH, data={}, headers=run.action())
        assert posted.status_code == 405, posted.status_code


# --- staying current ---------------------------------------------------------------------------


def test_panel_refetches_itself_after_a_budget_is_saved_or_removed(tmp_path: Path) -> None:
    """The panel is the one thing that fetches ``/settings/usage``: wherever it is shown it
    refetches itself on ``usage:changed`` from the body, and only the htmx answer to a saved
    or removed budget fires that event (after it settled, next to the announcement). The
    refetched panel then shows the new budget."""
    finished_guide(tmp_path)
    with started(tmp_path) as run:
        calls(run, FAKE_SERVICE, 3)
        for name, elements in views(run):
            found = panel(elements)
            assert found.attrs.get("hx-get") == PANEL_PATH, name
            assert found.attrs.get("hx-trigger") == PANEL_TRIGGER, name
            assert PANEL_TRIGGER.startswith(f"{CHANGED_EVENT} from:body, "), "at once"
            assert RESYNC_EVENT not in str(found.attrs.get("hx-trigger")), (
                f"{name}: every stream connection fires it, right after the page loads"
            )
            assert not [e for e in elements if e.within(found) and "aria-live" in e.attrs], (
                f"{name}: a refresh is silent"
            )
            assert found.attrs.get("hx-swap") == "outerHTML", name
            fetchers = [e for e in elements if e.attrs.get("hx-get") == PANEL_PATH]
            assert fetchers == [found], f"{name}: one fetcher"
            assert not [a for e in elements if e.within(found) for a in e.attrs if a == "hx-get"]
            assert_count(elements, FAKE_SERVICE, 3, None)

        refused = post_budget(run, FAKE_SERVICE, {**VALID, "reset_day": "0"}, htmx=True)
        assert refused.status_code == 422
        assert AFTER_SETTLE not in refused.headers, "nothing changed: nothing to refetch"
        assert AFTER_SETTLE not in post_budget(run, FAKE_SERVICE, VALID).headers, "plain: a reload"

        saved = post_budget(run, FAKE_SERVICE, VALID, htmx=True)
        assert saved.status_code == 200
        assert saved.headers[AFTER_SETTLE] == CHANGED_EVENT
        assert PANEL_ID not in saved.text, "the answer doesn't carry the panel"
        assert_count(outline(panel_fragment(run).text), FAKE_SERVICE, 3, ALLOWANCE)

        stale = post_budget(run, FAKE_SERVICE, VALID, version=1, htmx=True)
        assert stale.status_code == 409 and AFTER_SETTLE not in stale.headers

        removed = post_budget(run, FAKE_SERVICE, action="remove", htmx=True)
        assert removed.status_code == 200
        assert removed.headers[AFTER_SETTLE] == CHANGED_EVENT
        assert_count(outline(panel_fragment(run).text), FAKE_SERVICE, 3, None)

        # How many tasks run at once isn't shown in the panel.
        concurrency = post_concurrency(run, "2", htmx=True)
        assert concurrency.status_code == 200 and AFTER_SETTLE not in concurrency.headers


# --- review of M2.23 ---------------------------------------------------------------------------


def test_blocking_point_below_the_allowance_is_shown_with_its_consequence(
    tmp_path: Path,
) -> None:
    """With allowance 20 and blocking point 6 the panel says where the app stops; once it is
    reached the status uses the forms' word for it and says what follows, with the reset
    day. A blocking point equal to the allowance needs no line of its own."""
    early = Budget(allowance=20, first_warning=2, second_warning=4, block_at=6, reset_day=15)
    finished_guide(tmp_path)
    with started(tmp_path) as run:
        set_budget(run, FAKE_SERVICE, early)
        set_budget(run, WEB_SEARCH, replace(early, block_at=20))
        calls(run, FAKE_SERVICE, 5)
        calls(run, WEB_SEARCH, 5)
        for name, elements in views(run):
            assert_count(elements, FAKE_SERVICE, 5, 20)
            assert part_text(elements, FAKE_SERVICE, "block") == (
                "The app stops at 6 requests (your blocking point)."
            ), name
            assert status_words(elements, FAKE_SERVICE) == [SECOND_REACHED], name
            assert part(elements, FAKE_SERVICE, "stopped") is None, name
            assert part(elements, WEB_SEARCH, "block") is None, "the allowance is the point"
            assert "blocking point" not in block(elements, WEB_SEARCH).text.lower(), name

        calls(run, FAKE_SERVICE, 1)
        for name, elements in views(run):
            assert_count(elements, FAKE_SERVICE, 6, 20)
            assert part_text(elements, FAKE_SERVICE, "status") == LIMIT_REACHED, name
            assert part_text(elements, FAKE_SERVICE, "stopped") == (
                f"The app won't use this service until {RESET_DATE}."
            ), name
            assert part_text(elements, FAKE_SERVICE, "block").startswith("The app stops at 6 ")
            assert "Limit reached" not in panel(elements).text, name
            assert part(elements, WEB_SEARCH, "stopped") is None, name

        # A service without a budget never stops.
        calls(run, SUBSCRIPTION, 30)
        for _, elements in views(run):
            assert part(elements, SUBSCRIPTION, "block") is None
            assert part(elements, SUBSCRIPTION, "stopped") is None


def test_blocking_point_at_the_allowance_reached_says_the_consequence(tmp_path: Path) -> None:
    budget = Budget(allowance=4, first_warning=1, second_warning=2, block_at=4, reset_day=15)
    finished_guide(tmp_path)
    with started(tmp_path) as run:
        set_budget(run, FAKE_SERVICE, budget)
        calls(run, FAKE_SERVICE, 4)
        for name, elements in views(run):
            assert part(elements, FAKE_SERVICE, "block") is None, name
            assert status_words(elements, FAKE_SERVICE) == [LIMIT_REACHED], name
            assert part_text(elements, FAKE_SERVICE, "stopped") == (
                f"The app won't use this service until {RESET_DATE}."
            ), name


def test_tier_caps_are_shown_with_their_counts_and_when_reached(tmp_path: Path) -> None:
    """A cap that refuses tasks is never hidden behind "Within budget": each set cap has a
    line with the tier's count, and says so once the next task on that tier is refused."""
    capped = Budget(
        allowance=100,
        first_warning=50,
        second_warning=80,
        block_at=100,
        reset_day=15,
        fast_cap=3,
        thorough_cap=1,
    )
    finished_guide(tmp_path)
    with started(tmp_path) as run:
        set_budget(run, FAKE_SERVICE, capped)
        set_budget(run, WEB_SEARCH)  # a budget without caps
        calls(run, FAKE_SERVICE, 1, tier="fast")
        for name, elements in views(run):
            assert part_text(elements, FAKE_SERVICE, "cap-fast") == "Fast tasks: 1 / 3.", name
            assert part_text(elements, FAKE_SERVICE, "cap-thorough") == (
                "Thorough tasks: 0 / 1."
            ), name
            for tier in ("fast", "thorough"):
                assert part(elements, WEB_SEARCH, f"cap-{tier}") is None, "no cap: no line"
                assert part(elements, SUBSCRIPTION, f"cap-{tier}") is None, "no budget"

        calls(run, FAKE_SERVICE, 1, tier="thorough")
        with pytest.raises(TierCapReached):
            usage(run).reserve(FAKE_SERVICE, tier="thorough", task_type=ECHO)
        for name, elements in views(run):
            assert_count(elements, FAKE_SERVICE, 2, ALLOWANCE)
            assert status_words(elements, FAKE_SERVICE) == [WITHIN], name
            assert part_text(elements, FAKE_SERVICE, "cap-thorough") == (
                f"Thorough tasks: 1 / 1. Cap reached; Thorough tasks won't run until {RESET_DATE}."
            ), name
            assert part_text(elements, FAKE_SERVICE, "cap-fast") == "Fast tasks: 1 / 3.", name

        calls(run, FAKE_SERVICE, 2, tier="fast")
        for name, elements in views(run):
            assert part_text(elements, FAKE_SERVICE, "cap-fast") == (
                f"Fast tasks: 3 / 3. Cap reached; Fast tasks won't run until {RESET_DATE}."
            ), name

        # Only the Thorough cap set: no Fast line.
        set_budget(run, FAKE_SERVICE, replace(capped, fast_cap=None))
        for name, elements in views(run):
            assert part(elements, FAKE_SERVICE, "cap-fast") is None, name
            assert part(elements, FAKE_SERVICE, "cap-thorough") is not None, name


def test_one_progress_bar_per_budget_showing_the_count_that_is_used(tmp_path: Path) -> None:
    """Exactly one named bar per budgeted service: ``max`` is the allowance and ``value`` the
    count that is used (the provider's when it is higher), never more than the allowance.
    A service without a budget has none."""
    finished_guide(tmp_path)
    with started(tmp_path) as run:
        set_budget(run, FAKE_SERVICE)
        set_budget(run, WEB_SEARCH)
        calls(run, FAKE_SERVICE, 3)
        provider_reports(run, FAKE_SERVICE, 9)  # the provider's count is used: 9, not 3
        calls(run, WEB_SEARCH, 2)
        provider_reports(run, WEB_SEARCH, ALLOWANCE + 50)  # above the allowance
        calls(run, SUBSCRIPTION, 4)  # no budget
        assert usage(run).status(WEB_SEARCH).effective.value == ALLOWANCE + 50

        for name, elements in views(run):
            for service, value in ((FAKE_SERVICE, 9), (WEB_SEARCH, ALLOWANCE)):
                (bar,) = meters(elements, service)
                assert bar.attrs.get("max") == str(ALLOWANCE), (name, service, bar.attrs)
                assert bar.attrs.get("value") == str(value), (name, service, bar.attrs)
                assert accessible_name(elements, bar), (name, service)
            assert_count(elements, WEB_SEARCH, ALLOWANCE + 50, ALLOWANCE)
            assert meters(elements, SUBSCRIPTION) == [], f"{name}: no budget, no bar"
            for service in set(ALL_SERVICES) - {FAKE_SERVICE, WEB_SEARCH, SUBSCRIPTION}:
                assert meters(elements, service) == [], (name, service)


def test_expired_provider_information_is_not_shown(tmp_path: Path) -> None:
    """A window that has reset says nothing about now: its line isn't shown, and neither is
    the block when it was the only one."""
    finished_guide(tmp_path)
    with started(tmp_path) as run:
        for service, window, used in ((SUBSCRIPTION, "week", 88), (FAKE_SERVICE, "day", 99)):
            reset = ProviderUsageSnapshot(
                service=service,
                window=window,
                unit="requests",
                used=used,
                limit=None,
                utilization=None,
                resets_at=START - timedelta(minutes=1),
                observed_at=START - timedelta(hours=2),
            )
            ServiceUsageMeter(usage(run), service).snapshot(reset)
        provider_reports_fraction(run, FAKE_SERVICE, "five_hour", 0.25)  # resets in 3 hours
        assert len(usage(run).status(FAKE_SERVICE).provider_snapshots) == 2

        for name, elements in views(run):
            assert part(elements, SUBSCRIPTION, "info") is None, name
            (line,) = info_lines(elements, FAKE_SERVICE)
            assert line.startswith("5-hour window: 25% used (as of "), (name, line)
            assert "88" not in panel(elements).text and "99" not in panel(elements).text, name


def test_panel_says_usage_is_not_available_before_a_workspace_is_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The empty state: with no usage service to read, the panel has its heading, no service
    block and one line saying so."""
    finished_guide(tmp_path)
    with started(tmp_path) as run:
        calls(run, FAKE_SERVICE, 2)
        monkeypatch.setattr(run.app.state, "usage", None)
        elements = outline(panel_fragment(run).text)
        found = panel(elements)
        assert shown_services(elements) == []
        assert required(elements, "usage-panel-unavailable").text == "Usage isn't available yet."
        assert required(elements, "usage-panel-unavailable").within(found)


def test_panel_is_read_once_and_only_where_it_is_shown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The panel's database read happens once per answer that shows it (the page, the page
    fragment, its own route) and never for a budget form's htmx answer, which doesn't carry
    the panel."""
    finished_guide(tmp_path)
    with started(tmp_path) as run:
        shown_panel = run.app.state.usage_panel
        real_view = shown_panel.view
        seen: list[int] = []

        def view() -> UsagePanelView:
            seen.append(1)
            found: UsagePanelView = real_view()
            return found

        monkeypatch.setattr(shown_panel, "view", view)

        def reads(response: Response) -> int:
            assert response.status_code in (200, 303, 409, 422), response.status_code
            count = len(seen)
            seen.clear()
            return count

        assert reads(settings_page(run)) == 1
        assert reads(settings_page(run, htmx=True)) == 1
        assert reads(panel_fragment(run)) == 1
        assert reads(post_budget(run, FAKE_SERVICE, VALID)) == 0, "a redirect"
        refused = post_budget(run, FAKE_SERVICE, {**VALID, "reset_day": "0"})
        panel(outline(refused.text))
        assert reads(refused) == 1, "the full page shows the panel"

        version = usage(run).load_budgets().version
        for answer in (
            post_budget(run, FAKE_SERVICE, VALID, version=version, htmx=True),
            post_budget(run, FAKE_SERVICE, {**VALID, "reset_day": "0"}, htmx=True),
            post_budget(run, FAKE_SERVICE, VALID, version=0, htmx=True),
            post_concurrency(run, "2", htmx=True),
        ):
            assert PANEL_ID not in answer.text
            assert reads(answer) == 0, "an answer without the panel doesn't read it"


def test_panel_refresh_is_silent(tmp_path: Path) -> None:
    """Fetching the panel again (a task changed) announces nothing and moves no focus."""
    finished_guide(tmp_path)
    with started(tmp_path) as run:
        calls(run, FAKE_SERVICE, 1)
        response = panel_fragment(run)
        assert "hx-trigger" not in response.headers
        assert AFTER_SETTLE not in response.headers
        assert "data-autofocus" not in response.text
