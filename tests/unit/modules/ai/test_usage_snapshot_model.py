"""ProviderUsageSnapshot and the AIRequest hand-over (M2.2; groundwork for AC-M2.8; ADR-076
item 6).

A snapshot is provider-agnostic usage the provider reported during a call: a request count
against a limit (``unit="requests"``) or a utilization fraction of a time window
(``unit="fraction"``, shown for information only, never compared with a request budget).
It is validated when built, so nothing out of range reaches budgets or storage. New names
are imported inside each test so each test fails on its own while they are missing.
"""

import dataclasses
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest

from tests.fakes.ai import make_request

OBSERVED = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
RESETS = OBSERVED + timedelta(hours=3)


def _fraction(**overrides: Any) -> Any:
    from app.modules.ai import ProviderUsageSnapshot

    values: dict[str, Any] = {
        "service": "claude_subscription",
        "window": "five_hour",
        "unit": "fraction",
        "used": None,
        "limit": None,
        "utilization": 0.42,
        "resets_at": RESETS,
        "observed_at": OBSERVED,
    }
    values.update(overrides)
    return ProviderUsageSnapshot(**values)


def _requests(**overrides: Any) -> Any:
    from app.modules.ai import ProviderUsageSnapshot

    values: dict[str, Any] = {
        "service": "fake",
        "window": "month",
        "unit": "requests",
        "used": 7,
        "limit": 100,
        "utilization": None,
        "resets_at": None,
        "observed_at": OBSERVED,
    }
    values.update(overrides)
    return ProviderUsageSnapshot(**values)


def test_snapshot_has_exactly_the_agreed_fields() -> None:
    from app.modules.ai import ProviderUsageSnapshot

    names = {field.name for field in dataclasses.fields(ProviderUsageSnapshot)}

    # No provider-specific extras, and never the SDK's raw dict.
    assert names == {
        "service",
        "window",
        "unit",
        "used",
        "limit",
        "utilization",
        "resets_at",
        "observed_at",
    }


def test_fraction_snapshot_valid() -> None:
    snapshot = _fraction()

    assert snapshot.unit == "fraction"
    assert snapshot.utilization == 0.42
    assert snapshot.resets_at == RESETS
    assert snapshot.observed_at == OBSERVED


def test_requests_snapshot_valid() -> None:
    snapshot = _requests()

    assert snapshot.unit == "requests"
    assert (snapshot.used, snapshot.limit) == (7, 100)


@pytest.mark.parametrize("utilization", [0.0, 1.0], ids=["zero", "one"])
def test_utilization_bounds_inclusive(utilization: float) -> None:
    assert _fraction(utilization=utilization).utilization == utilization


def test_snapshot_is_frozen() -> None:
    snapshot = _fraction()

    with pytest.raises(dataclasses.FrozenInstanceError):
        snapshot.utilization = 0.9  # type: ignore[misc]


@pytest.mark.parametrize(
    "overrides",
    [
        {"utilization": -0.01},
        {"utilization": 1.01},
        {"utilization": float("nan")},
        {"utilization": float("inf")},
        {"utilization": 10**400},
    ],
    ids=["negative", "above_1", "nan", "inf", "huge_int"],
)
def test_utilization_out_of_range_rejected(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        _fraction(**overrides)


@pytest.mark.parametrize(
    "overrides",
    [{"used": -1}, {"limit": -1}, {"used": 1.5}, {"limit": "100"}, {"used": True}],
    ids=["used_negative", "limit_negative", "used_not_int", "limit_not_int", "used_bool"],
)
def test_counts_must_be_non_negative_ints(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        _requests(**overrides)


def test_requests_unit_requires_used() -> None:
    with pytest.raises(ValueError):
        _requests(used=None)


def test_fraction_unit_requires_utilization() -> None:
    with pytest.raises(ValueError):
        _fraction(utilization=None)


def test_unknown_unit_rejected() -> None:
    with pytest.raises(ValueError):
        _requests(unit="tokens")


@pytest.mark.parametrize(
    "overrides",
    [
        {"observed_at": OBSERVED.replace(tzinfo=None)},
        {"resets_at": RESETS.replace(tzinfo=None)},
    ],
    ids=["observed_at_naive", "resets_at_naive"],
)
def test_naive_datetimes_rejected(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        _fraction(**overrides)


def test_aware_non_utc_datetimes_accepted() -> None:
    plus_one = timezone(timedelta(hours=1))

    snapshot = _fraction(
        observed_at=OBSERVED.astimezone(plus_one), resets_at=RESETS.astimezone(plus_one)
    )

    assert snapshot.observed_at == OBSERVED
    assert snapshot.resets_at == RESETS
    # Kept in UTC, so storage and comparisons never depend on the input's offset.
    assert snapshot.observed_at.utcoffset() == timedelta(0)
    assert snapshot.resets_at.utcoffset() == timedelta(0)


@pytest.mark.parametrize("field", ["service", "window"])
def test_blank_service_or_window_rejected(field: str) -> None:
    with pytest.raises(ValueError):
        _fraction(**{field: "  "})


def test_ai_request_on_usage_defaults_to_none() -> None:
    request = make_request()

    assert request.on_usage is None


def test_ai_request_carries_on_usage_callback() -> None:
    reported: list[Any] = []

    request = dataclasses.replace(make_request(), on_usage=reported.append)

    assert request.on_usage is not None
    snapshot = _fraction()
    request.on_usage(snapshot)
    assert reported == [snapshot]


# --- the window is a short token (M2.26 review) ---------------------------------------------

# The Claude provider's own window ids (``_USAGE_WINDOWS``) and the ones tests use.
REAL_WINDOWS = ["five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet", "month", "week"]
NOT_A_WINDOW = [
    "five hour",  # a space: free text, not an id
    "month\n",
    " month",
    "month;DROP",
    "a/b",
    "<b>month</b>",
    "Fictional provider text with a fictional name in it",
    "m" * 65,  # longer than any id
    "mês",
]


@pytest.mark.parametrize("window", [*REAL_WINDOWS, "a", "A.b:c-d_9", "m" * 64])
def test_snapshot_window_is_a_short_token(window: str) -> None:
    assert _requests(window=window).window == window
    assert _fraction(window=window).window == window


@pytest.mark.parametrize("window", NOT_A_WINDOW)
def test_snapshot_window_refuses_anything_but_a_short_token(window: str) -> None:
    """The window is the provider's own word and is stored: only ``[A-Za-z0-9_.:-]{1,64}``
    gets past the boundary, so no provider text can ride along in it."""
    with pytest.raises(ValueError) as refused:
        _requests(window=window)
    assert window not in str(refused.value), "the refused text is never repeated"
    with pytest.raises(ValueError):
        _fraction(window=window)


def test_claude_provider_windows_are_all_valid_snapshot_windows() -> None:
    from app.modules.ai.providers import claude_sdk

    assert claude_sdk._USAGE_WINDOWS, "control: the provider knows its windows"
    for window in claude_sdk._USAGE_WINDOWS:
        assert _fraction(window=window).window == window
