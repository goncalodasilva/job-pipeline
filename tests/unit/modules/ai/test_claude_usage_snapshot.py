"""Claude provider: rate-limit frames become usage snapshots (M2.2; groundwork for AC-M2.8;
ADR-076 items 4 and 6).

The CLI emits ``rate_limit_event`` frames from the subscription's rate-limit headers
(token and existing sign-in); the SDK parses them into ``RateLimitEvent``. The provider
turns each well-formed one with a known window into a ``ProviderUsageSnapshot``
(``unit="fraction"``) and hands it to ``AIRequest.on_usage``. Malformed frames are ignored,
the SDK's ``raw`` dict is never kept or logged, and nothing about usage reporting can change
the call's result.

Frames replay through the SDK's real client and parser (``tests/fakes/claude_query.py``);
nothing starts the CLI or touches the network. Every value is fictional; token-shaped
strings are built at runtime. New names are imported inside each test so each test fails
on its own while they are missing.
"""

import dataclasses
import logging
import traceback
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import anyio
import pytest
from claude_agent_sdk import ClaudeSDKClient, RateLimitEvent, ResultMessage

# Not re-exported by the SDK's top level (0.2.163).
from claude_agent_sdk._errors import MessageParseError

from app.domain.errors import AppError, InvalidModelOutput, UsageLimitReached
from app.logging_config import SdkRecordFilter
from app.modules.ai import AIRequest, AIResponse, CallCancelled
from app.modules.ai.providers.claude_sdk import (
    SECRET_KEY_BY_MODE,
    AuthMode,
    ClaudeSdkProvider,
    build_options,
)
from app.modules.secrets.providers.memory import MemorySecretStore
from tests.fakes.ai import NotCancelled, make_request
from tests.fakes.claude_query import replaying, success_frames
from tests.fakes.claude_transport import ReplayTransport, load_frames
from tests.fakes.clock import FrozenClock

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)  # 1791547200
FIVE_HOUR_RESETS = 1791558000  # NOW + 3 hours, as in rate_limit_five_hour.jsonl
SEVEN_DAY_RESETS = 1791892800  # NOW + 4 days, as in rate_limit_seven_day.jsonl
MODEL = "haiku"
ANSWER = {"answer": "ok"}
FAKE_TOKEN = "fake-" + "token-" + "u" * 10
FAKE_API_KEY = "fake-" + "key-" + "v" * 10
CREDENTIAL: dict[AuthMode, str] = {"subscription_token": FAKE_TOKEN, "api_key": FAKE_API_KEY}
SERVICE_BY_MODE: dict[AuthMode, str] = {
    "subscription_token": "claude_subscription",
    "existing_login": "claude_subscription",
    "api_key": "claude_api_key",
}
# Adversarial extras a future CLI could put in rate_limit_info (the SDK keeps them in raw).
RAW_TOKEN = "fake-" + "oauth-" + "w" * 24
RAW_LONG = "Fictional-long-raw-" + "x" * 20_000
RAW_NESTED_MARKER = "fictional-nested-" + "y" * 12


@pytest.fixture
def temp_root(tmp_path: Path) -> Path:
    folder = tmp_path / "temp-root"
    folder.mkdir()
    return folder


@pytest.fixture
def cli(tmp_path: Path) -> Path:
    return tmp_path / "fictional-cli" / "claude"  # never run: the query is fake


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(NOW)


def _provider(
    frames: Sequence[Mapping[str, Any]],
    temp_root: Path,
    cli: Path,
    clock: FrozenClock,
    *,
    mode: AuthMode = "subscription_token",
) -> ClaudeSdkProvider:
    secrets = MemorySecretStore()
    key = SECRET_KEY_BY_MODE[mode]
    if key is not None:
        secrets.set(key, CREDENTIAL[mode])
    return ClaudeSdkProvider(
        secrets=secrets,
        mode=mode,
        client_factory=replaying(frames),
        cli_path=lambda: cli,
        environ={},
        temp_root=temp_root,
        clock=clock,
    )


def _request(on_usage: Callable[[Any], None] | None) -> AIRequest:
    return dataclasses.replace(make_request(model=MODEL), on_usage=on_usage)


def _rate_limit_frame(
    info: Mapping[str, Any], *, uuid: str = "uuid-fictional-0150"
) -> dict[str, Any]:
    return {
        "type": "rate_limit_event",
        "rate_limit_info": dict(info),
        "uuid": uuid,
        "session_id": "session-fictional-0100",
    }


def _info(**overrides: Any) -> dict[str, Any]:
    info: dict[str, Any] = {
        "status": "allowed",
        "resetsAt": FIVE_HOUR_RESETS,
        "rateLimitType": "five_hour",
        "utilization": 0.42,
    }
    info.update(overrides)
    return {name: value for name, value in info.items() if value is not _DROP}


_DROP = object()


def _with_rate_limits(*rate_frames: Mapping[str, Any]) -> list[dict[str, Any]]:
    """A successful run with ``rate_frames`` inserted before the result frame."""
    frames = success_frames(dict(ANSWER), usage={"input_tokens": 12, "output_tokens": 3})
    return [*frames[:-1], *(dict(frame) for frame in rate_frames), frames[-1]]


def _same_result(response: AIResponse, baseline: AIResponse) -> None:
    assert dict(response.data) == dict(baseline.data)
    assert response.raw_text == baseline.raw_text
    assert response.model == baseline.model
    assert response.usage == baseline.usage


async def _baseline(temp_root: Path, cli: Path, clock: FrozenClock) -> AIResponse:
    provider = _provider(_with_rate_limits(), temp_root, cli, clock)
    return await provider.run(make_request(model=MODEL), NotCancelled())


# --- fixtures are real SDK shapes (pass today: they check the fixtures, not the app) ---------


@pytest.mark.parametrize("name", ["rate_limit_five_hour", "rate_limit_seven_day"])
async def test_rate_limit_fixtures_parse_as_sdk_rate_limit_events(
    name: str, tmp_path: Path
) -> None:
    transport = ReplayTransport(load_frames(name))
    options = build_options(
        mode="existing_login",
        model="claude-haiku-fictional",
        system="Fictional system prompt.",
        output_schema={"type": "object"},
        workdir=tmp_path,
        credential=None,
        environ={},
    )
    client = ClaudeSDKClient(options=options, transport=transport)
    messages: list[Any] = []
    with anyio.fail_after(10):
        await client.connect("fictional prompt")
        try:
            async for message in client.receive_response():
                messages.append(message)
        finally:
            await client.disconnect()

    events = [message for message in messages if isinstance(message, RateLimitEvent)]
    assert len(events) == 1
    assert events[0].rate_limit_info.rate_limit_type == name.removeprefix("rate_limit_")
    assert isinstance(messages[-1], ResultMessage)


async def test_sdk_raises_parse_error_for_rate_limit_frame_without_status(
    tmp_path: Path,
) -> None:
    # Why the provider must survive a MessageParseError mid-stream (missing_status case).
    frames = _with_rate_limits(_rate_limit_frame(_info(status=_DROP)))
    options = build_options(
        mode="existing_login",
        model="claude-haiku-fictional",
        system="Fictional system prompt.",
        output_schema={"type": "object"},
        workdir=tmp_path,
        credential=None,
        environ={},
    )
    client = ClaudeSDKClient(options=options, transport=ReplayTransport(frames))
    with anyio.fail_after(10):
        await client.connect("fictional prompt")
        try:
            with pytest.raises(MessageParseError):
                async for _message in client.receive_response():
                    pass
        finally:
            await client.disconnect()


# --- frames become snapshots ------------------------------------------------------------

FRAME_CASES: dict[str, tuple[str, float, int]] = {
    "five_hour": ("rate_limit_five_hour", 0.42, FIVE_HOUR_RESETS),
    "seven_day": ("rate_limit_seven_day", 0.81, SEVEN_DAY_RESETS),
}


@pytest.mark.parametrize("window", list(FRAME_CASES), ids=list(FRAME_CASES))
async def test_rate_limit_frame_becomes_snapshot(
    window: str, temp_root: Path, cli: Path, clock: FrozenClock
) -> None:
    from app.modules.ai import ProviderUsageSnapshot

    fixture, utilization, resets_at = FRAME_CASES[window]
    reported: list[Any] = []
    provider = _provider(load_frames(fixture), temp_root, cli, clock)

    response = await provider.run(_request(reported.append), NotCancelled())

    assert dict(response.data) == ANSWER
    assert reported == [
        ProviderUsageSnapshot(
            service="claude_subscription",
            window=window,
            unit="fraction",
            used=None,
            limit=None,
            utilization=utilization,
            resets_at=datetime.fromtimestamp(resets_at, UTC),
            observed_at=NOW,
        )
    ]
    assert reported[0].resets_at.utcoffset() == timedelta(0)
    assert reported[0].observed_at.utcoffset() == timedelta(0)


@pytest.mark.parametrize("mode", list(SERVICE_BY_MODE), ids=list(SERVICE_BY_MODE))
async def test_snapshot_service_follows_sign_in_mode(
    mode: AuthMode, temp_root: Path, cli: Path, clock: FrozenClock
) -> None:
    reported: list[Any] = []
    provider = _provider(load_frames("rate_limit_five_hour"), temp_root, cli, clock, mode=mode)

    await provider.run(_request(reported.append), NotCancelled())

    assert [snapshot.service for snapshot in reported] == [SERVICE_BY_MODE[mode]]


@pytest.mark.parametrize("window", ["seven_day_opus", "seven_day_sonnet"])
async def test_per_model_weekly_windows_become_snapshots(
    window: str, temp_root: Path, cli: Path, clock: FrozenClock
) -> None:
    frame = _rate_limit_frame(
        _info(rateLimitType=window, resetsAt=SEVEN_DAY_RESETS, utilization=0.25)
    )
    reported: list[Any] = []
    provider = _provider(_with_rate_limits(frame), temp_root, cli, clock)

    await provider.run(_request(reported.append), NotCancelled())

    assert [(snapshot.window, snapshot.utilization) for snapshot in reported] == [(window, 0.25)]


async def test_every_valid_frame_is_reported_in_order(
    temp_root: Path, cli: Path, clock: FrozenClock
) -> None:
    frames = _with_rate_limits(
        _rate_limit_frame(_info(utilization=0.5), uuid="uuid-fictional-0151"),
        _rate_limit_frame(
            _info(rateLimitType="seven_day", resetsAt=SEVEN_DAY_RESETS, utilization=0.6),
            uuid="uuid-fictional-0152",
        ),
    )
    reported: list[Any] = []
    provider = _provider(frames, temp_root, cli, clock)

    await provider.run(_request(reported.append), NotCancelled())

    assert [(snapshot.window, snapshot.utilization) for snapshot in reported] == [
        ("five_hour", 0.5),
        ("seven_day", 0.6),
    ]


async def test_without_on_usage_frames_are_harmless(
    temp_root: Path, cli: Path, clock: FrozenClock
) -> None:
    baseline = await _baseline(temp_root, cli, clock)
    provider = _provider(load_frames("rate_limit_five_hour"), temp_root, cli, clock)

    response = await provider.run(_request(None), NotCancelled())

    assert dict(response.data) == dict(baseline.data)


# --- malformed frames are ignored -------------------------------------------------------

MALFORMED: dict[str, list[dict[str, Any]]] = {
    # The SDK raises MessageParseError for this frame; the provider must carry on.
    "missing_status": [_rate_limit_frame(_info(status=_DROP))],
    "utilization_above_1": [_rate_limit_frame(_info(utilization=1.5))],
    "negative": [
        _rate_limit_frame(_info(utilization=-0.2), uuid="uuid-fictional-0160"),
        _rate_limit_frame(_info(resetsAt=-60), uuid="uuid-fictional-0161"),
    ],
    "non_numeric": [
        _rate_limit_frame(_info(utilization="half"), uuid="uuid-fictional-0162"),
        _rate_limit_frame(_info(resetsAt="soon"), uuid="uuid-fictional-0163"),
    ],
    "resets_at_in_past": [_rate_limit_frame(_info(resetsAt=1791547200 - 60))],
    "unknown_type": [
        _rate_limit_frame(_info(rateLimitType="overage"), uuid="uuid-fictional-0164"),
        # A CLI window type SDK 0.2.163 doesn't model.
        _rate_limit_frame(
            _info(rateLimitType="seven_day_overage_included"), uuid="uuid-fictional-0165"
        ),
        _rate_limit_frame(_info(rateLimitType="fictional_window"), uuid="uuid-fictional-0166"),
    ],
}


@pytest.mark.parametrize("case", list(MALFORMED), ids=list(MALFORMED))
async def test_malformed_rate_limit_frames_ignored(
    case: str,
    temp_root: Path,
    cli: Path,
    clock: FrozenClock,
) -> None:
    baseline = await _baseline(temp_root, cli, clock)
    reported: list[Any] = []
    provider = _provider(_with_rate_limits(*MALFORMED[case]), temp_root, cli, clock)

    response = await provider.run(_request(reported.append), NotCancelled())

    _same_result(response, baseline)
    assert reported == []


INCOMPLETE: dict[str, dict[str, Any]] = {
    "no_utilization": _info(utilization=_DROP),
    "no_type": _info(rateLimitType=_DROP),
    "utilization_null": _info(utilization=None),
    "utilization_bool": _info(utilization=True),
    "utilization_nan": _info(utilization=float("nan")),
    "resets_at_float_text": _info(resetsAt="1791558000"),
    "resets_at_huge": _info(resetsAt=10**20),
    "no_resets_at": _info(resetsAt=_DROP),
    # Just past the 31-day bound on how far ahead a window may reset.
    "resets_at_past_31_days": _info(resetsAt=int((NOW + timedelta(days=31)).timestamp()) + 1),
    # JSON allows integers too large for a float: converting them raises OverflowError.
    "utilization_huge_int": _info(utilization=10**400),
    "resets_at_huge_int": _info(resetsAt=10**400),
}


@pytest.mark.parametrize("case", list(INCOMPLETE), ids=list(INCOMPLETE))
async def test_incomplete_rate_limit_frames_ignored(
    case: str, temp_root: Path, cli: Path, clock: FrozenClock
) -> None:
    frame = _rate_limit_frame(INCOMPLETE[case])
    baseline = await _baseline(temp_root, cli, clock)
    reported: list[Any] = []
    provider = _provider(_with_rate_limits(frame), temp_root, cli, clock)

    response = await provider.run(_request(reported.append), NotCancelled())

    _same_result(response, baseline)
    assert reported == []


@pytest.mark.parametrize("bad", ["missing_status", "utilization_above_1", "unknown_type"])
async def test_valid_frame_after_malformed_frame_still_reported(
    bad: str, temp_root: Path, cli: Path, clock: FrozenClock
) -> None:
    good = _rate_limit_frame(
        _info(rateLimitType="seven_day", resetsAt=SEVEN_DAY_RESETS, utilization=0.3),
        uuid="uuid-fictional-0170",
    )
    reported: list[Any] = []
    provider = _provider(_with_rate_limits(*MALFORMED[bad], good), temp_root, cli, clock)

    response = await provider.run(_request(reported.append), NotCancelled())

    assert dict(response.data) == ANSWER
    assert [(snapshot.window, snapshot.utilization) for snapshot in reported] == [
        ("seven_day", 0.3)
    ]


# --- raw is never kept or logged --------------------------------------------------------


def _adversarial_info(**overrides: Any) -> dict[str, Any]:
    return _info(
        fictionalToken=RAW_TOKEN,
        fictionalLongText=RAW_LONG,
        nested={"deeper": {"token": RAW_TOKEN, "marker": RAW_NESTED_MARKER, "list": [RAW_TOKEN]}},
        unifiedWindows={"five_hour": {"utilization": 0.42, "resetsAt": FIVE_HOUR_RESETS}},
        **overrides,
    )


ADVERSARIAL: dict[str, tuple[list[dict[str, Any]], bool]] = {
    # (frames, whether the call itself fails)
    "valid_frame": (_with_rate_limits(_rate_limit_frame(_adversarial_info())), False),
    "missing_status": (
        _with_rate_limits(_rate_limit_frame(_adversarial_info(status=_DROP))),
        False,
    ),
    "out_of_range": (
        _with_rate_limits(_rate_limit_frame(_adversarial_info(utilization=7))),
        False,
    ),
    "rejected_call": (
        [
            *load_frames("rate_limit")[:-1],
            _rate_limit_frame(_adversarial_info(status="rejected", utilization=1.0)),
            load_frames("rate_limit")[-1],
        ],
        True,
    ),
}
FORBIDDEN = (RAW_TOKEN, RAW_LONG[:60], RAW_NESTED_MARKER, "fictionalToken", "fictionalLongText")


def _texts_of(value: object) -> list[str]:
    texts = [repr(value), str(value)]
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        texts.extend(repr(getattr(value, field.name)) for field in dataclasses.fields(value))
        texts.append(repr(vars(value)) if hasattr(value, "__dict__") else "")
    return texts


@pytest.mark.parametrize("case", list(ADVERSARIAL), ids=list(ADVERSARIAL))
async def test_adversarial_raw_fields_never_kept_or_logged(
    case: str,
    temp_root: Path,
    cli: Path,
    clock: FrozenClock,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from app.modules.ai import ProviderUsageSnapshot

    caplog.set_level(logging.DEBUG)
    # The app's handlers reduce SDK records with SdkRecordFilter; the capture does too.
    caplog.handler.addFilter(SdkRecordFilter())
    frames, call_fails = ADVERSARIAL[case]
    reported: list[Any] = []
    provider = _provider(frames, temp_root, cli, clock)

    error: BaseException | None = None
    try:
        response = await provider.run(_request(reported.append), NotCancelled())
    except AppError as raised:
        error = raised

    if call_fails:
        assert isinstance(error, UsageLimitReached)
        texts = [
            str(error),
            repr(error),
            repr(error.args),
            "".join(traceback.format_exception(error)),
        ]
        assert error.__cause__ is None
        assert error.__context__ is None
    else:
        assert error is None
        assert dict(response.data) == ANSWER
        texts = []
    if case == "valid_frame":
        assert len(reported) == 1
    if case == "rejected_call":
        # The frame is well formed, so it is reported even though the call fails.
        assert [snapshot.utilization for snapshot in reported] == [1.0]
    for snapshot in reported:
        assert isinstance(snapshot, ProviderUsageSnapshot)
        assert not hasattr(snapshot, "raw")
        texts.extend(_texts_of(snapshot))
    captured = capsys.readouterr()
    texts.extend([caplog.text, captured.out, captured.err])
    texts.extend(repr(record.args) for record in caplog.records)
    texts.extend(repr(getattr(record, "exc_info", None)) for record in caplog.records)
    for text in texts:
        for value in FORBIDDEN:
            assert value not in text, (case, value)


# --- API-key mode -----------------------------------------------------------------------


async def test_api_key_mode_without_frames_reports_none(
    temp_root: Path, cli: Path, clock: FrozenClock
) -> None:
    # API-key sessions get no subscription rate-limit headers, so the CLI sends no frames.
    reported: list[Any] = []
    provider = _provider(_with_rate_limits(), temp_root, cli, clock, mode="api_key")

    response = await provider.run(_request(reported.append), NotCancelled())

    assert dict(response.data) == ANSWER
    assert reported == []


# --- the callback can't change the call -------------------------------------------------


class _CallbackFailure(Exception):
    pass


async def test_snapshot_never_changes_call_result(
    temp_root: Path,
    cli: Path,
    clock: FrozenClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    callback_text = "Fictional callback failure " + FAKE_TOKEN
    calls: list[Any] = []

    def raising(snapshot: Any) -> None:
        calls.append(snapshot)
        raise _CallbackFailure(callback_text)

    frames = _with_rate_limits(
        *MALFORMED["missing_status"],
        *MALFORMED["utilization_above_1"],
        _rate_limit_frame(_info(), uuid="uuid-fictional-0180"),
        *MALFORMED["non_numeric"],
        _rate_limit_frame(
            _info(rateLimitType="seven_day", resetsAt=SEVEN_DAY_RESETS, utilization=0.7),
            uuid="uuid-fictional-0181",
        ),
    )
    baseline = await _baseline(temp_root, cli, clock)
    provider = _provider(frames, temp_root, cli, clock)

    response = await provider.run(_request(raising), NotCancelled())

    _same_result(response, baseline)
    assert calls, "the callback ran and raised"
    # Only the exception's type may be logged, never its text.
    assert callback_text not in caplog.text
    assert FAKE_TOKEN not in caplog.text
    for record in caplog.records:
        assert callback_text not in repr(record.args)
        assert record.exc_info is None or callback_text not in repr(record.exc_info)


# --- the receive loop -------------------------------------------------------------------


class _CancelAfterFirstCheck:
    """Not cancelled when the call starts; cancelled at every later check."""

    def __init__(self) -> None:
        self.checks = 0

    @property
    def cancelled(self) -> bool:
        self.checks += 1
        return self.checks > 1


async def test_cancel_noticed_between_unparseable_rate_limit_frames(
    temp_root: Path, cli: Path, clock: FrozenClock
) -> None:
    # Only frames the SDK can't parse arrive, so no message ever reaches the loop body:
    # the cancel must be checked after each dropped frame too.
    frames = [
        _rate_limit_frame(_info(status=_DROP), uuid=f"uuid-fictional-02{index:02d}")
        for index in range(3)
    ]
    reported: list[Any] = []
    provider = _provider(frames, temp_root, cli, clock)

    with pytest.raises(CallCancelled):
        await provider.run(_request(reported.append), _CancelAfterFirstCheck())

    assert reported == []


async def test_other_parse_errors_still_fail_the_call(
    temp_root: Path, cli: Path, clock: FrozenClock
) -> None:
    # Only rate-limit frames may be dropped; any other unparseable frame fails the call.
    broken = {
        "type": "assistant",
        "uuid": "uuid-fictional-0210",
        "session_id": "session-fictional-0100",
    }
    provider = _provider(_with_rate_limits(broken), temp_root, cli, clock)

    with pytest.raises(InvalidModelOutput) as raised:
        await provider.run(_request(None), NotCancelled())

    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


class _FailingClock:
    def now(self) -> datetime:
        raise RuntimeError("Fictional clock failure " + RAW_TOKEN)


async def test_unexpected_error_reading_usage_never_changes_call_result(
    temp_root: Path, cli: Path, clock: FrozenClock, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    baseline = await _baseline(temp_root, cli, clock)
    reported: list[Any] = []
    secrets = MemorySecretStore()
    secrets.set(SECRET_KEY_BY_MODE["subscription_token"] or "", FAKE_TOKEN)
    provider = ClaudeSdkProvider(
        secrets=secrets,
        mode="subscription_token",
        client_factory=replaying(load_frames("rate_limit_five_hour")),
        cli_path=lambda: cli,
        environ={},
        temp_root=temp_root,
        clock=_FailingClock(),
    )

    response = await provider.run(_request(reported.append), NotCancelled())

    _same_result(response, baseline)
    assert reported == []
    # Only the exception's type may be logged, never its text.
    assert RAW_TOKEN not in caplog.text
    for record in caplog.records:
        assert RAW_TOKEN not in repr(record.args)
