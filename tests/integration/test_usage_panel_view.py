"""The usage panel's view on its own (M2.23 review): what ``UsagePanel`` makes of the usage
service's answers when no workspace is open, when task types share a label, and how it writes
what the provider reports for information (the window in words, the local time it was
observed).

A real migrated SQLite database in a temp folder, a frozen clock, an injected time zone (two
hours ahead of UTC, so local times are checked) and a stub ``label_for``. No network; every
value is fictional.
"""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.modules.ai import ProviderUsageSnapshot
from app.modules.storage import MIGRATIONS_DIR, Database, apply_migrations
from app.services.usage import UsageService
from app.services.usage_panel import ServiceUsage, UsagePanel
from tests.fakes.clock import FrozenClock
from tests.fakes.ids import SequentialIdFactory

pytestmark = pytest.mark.integration

START = datetime(2026, 10, 10, 12, 5, 0, tzinfo=UTC)
TZ = timezone(timedelta(hours=2))  # 12:05 UTC is 14:05 here
AS_OF = "(as of 10 October 2026, 14:05)"
FAKE = "fake"
LABELS = {
    "fictional_draft_a": "Draft",
    "fictional_draft_b": "Draft",
    "fictional_sum": "Total",
    "fictional_echo": "Echo",
}


@pytest.fixture
def usage(tmp_path: Path) -> Iterator[UsageService]:
    clock = FrozenClock(START)
    db = Database.open(tmp_path / "data" / "app.db")
    try:
        apply_migrations(db, MIGRATIONS_DIR, tmp_path / "backups", clock)
        yield UsageService(db, clock, SequentialIdFactory("panel"), TZ)
    finally:
        db.close()


def panel(usage: UsageService | None) -> UsagePanel:
    return UsagePanel(lambda: usage, offered=lambda: (FAKE,), label_for=LABELS.get, tz=TZ)


def fake_block(usage: UsageService) -> ServiceUsage:
    (shown,) = panel(usage).view().services
    assert shown.service == FAKE
    return shown


def snapshot(
    window: str,
    *,
    used: int | None = None,
    utilization: float | None = None,
    observed_at: datetime = START,
) -> ProviderUsageSnapshot:
    return ProviderUsageSnapshot(
        service=FAKE,
        window=window,
        unit="fraction" if used is None else "requests",
        used=used,
        limit=None,
        utilization=utilization,
        resets_at=None,
        observed_at=observed_at,
    )


def test_no_open_workspace_shows_no_service() -> None:
    """Before a workspace is open there is no usage service: the panel has no block (the
    template then says usage isn't available yet) and nothing is read."""
    assert panel(None).view().services == ()


def test_task_types_sharing_a_label_or_named_total_get_their_id_added(
    usage: UsageService,
) -> None:
    """Every row header is its own: two types with one label, and a type whose label is the
    total row's, are told apart by their id; a label of its own stays as it is."""
    for task_type in LABELS:
        usage.reserve(FAKE, tier="fast", task_type=task_type)
    usage.reserve(FAKE, tier="thorough", task_type="fictional_sum")

    shown = fake_block(usage)

    assert [row.label for row in shown.rows] == [
        "Draft (fictional_draft_a)",
        "Draft (fictional_draft_b)",
        "Echo",
        "Total (fictional_sum)",
    ]
    by_label = {row.label: row for row in shown.rows}
    summed = by_label["Total (fictional_sum)"]
    assert (summed.fast, summed.thorough, summed.total) == (1, 1, 2)
    assert shown.total.label == "Total"
    assert (shown.total.fast, shown.total.thorough, shown.total.total) == (4, 1, 5)
    assert shown.total.label not in by_label


def test_information_lines_name_the_window_and_the_local_time_observed(
    usage: UsageService,
) -> None:
    """A known window in words, an unknown one as "Other window (<id>)", a fraction as a
    whole percentage, a request count that can't be compared as a count: each with the time
    it was observed, in the local zone."""
    earlier = START - timedelta(hours=13)  # 23:05 on the 9th in UTC, 01:05 on the 10th here
    assert usage.save_snapshot(snapshot("five_hour", utilization=0.25))
    assert usage.save_snapshot(snapshot("week", used=1))
    assert usage.save_snapshot(snapshot("fictional_window", used=9, observed_at=earlier))

    shown = fake_block(usage)

    assert set(shown.information) == {
        f"5-hour window: 25% used {AS_OF}",
        f"Week: 1 request {AS_OF}",
        "Other window (fictional_window): 9 requests (as of 10 October 2026, 01:05)",
    }
    assert shown.provider_count is None and shown.count == 0
