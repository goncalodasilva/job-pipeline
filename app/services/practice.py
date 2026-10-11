"""The practice task: a fake-gated vehicle for the task runner, the usage meter and the tasks
UI before the first real AI task (M2.17; ADR-076 item 2).

Offered only while fakes are allowed and the saved provider is the fake. A start carries three
values from fixed lists (no free text); a run is ``PRACTICE_STEPS`` steps of one fake call
each, with a heartbeat and a cancel check before every step, counted on the ``fake`` usage
service. The prompt is a fixed text plus the step numbers: nothing from the workspace reaches
it, and its output is never shown to the user.
"""

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Final, Protocol

from app.domain.errors import PracticeNotOffered, TaskFailed
from app.domain.settings import AIProviderSettings, Tier
from app.domain.tasks import TaskRef
from app.domain.usage import SERVICE_BY_PROVIDER_KIND
from app.modules.ai import AIProvider, AIProviderFactory, CancelToken
from app.services.tasks import TaskService
from app.services.usage import UsageService
from app.services.usage_meter import ServiceUsageMeter

PRACTICE: Final = "practice"  # the task type, the AI task and the prompt id
PRACTICE_LABEL: Final = "Practice task"
PRACTICE_TARGET: Final = "settings"  # one practice task at a time
PRACTICE_STEPS: Final = 5
SLOW_STEP_SECONDS: Final = 3.0
FAKE_KIND: Final = "fake"
# The usage service a practice run is counted on, and its start is checked against.
PRACTICE_SERVICE: Final = SERVICE_BY_PROVIDER_KIND[FAKE_KIND]

DURATIONS: Final = ("quick", "slow")
OUTCOMES: Final = ("ok", "usage_limit", "network", "invalid_output")
TIERS: Final[tuple[Tier, ...]] = ("fast", "thorough")
PRACTICE_FIELDS: Final[Mapping[str, tuple[str, ...]]] = MappingProxyType(
    {"duration": DURATIONS, "outcome": OUTCOMES, "tier": TIERS}
)
_SEPARATOR: Final = "/"

ScriptPractice = Callable[..., None]
"""Wiring's ``script_practice(provider, *, outcome, delay_seconds)``: only wiring may touch
the fake provider (spec 2.3 rule 7)."""


@dataclass(frozen=True)
class PracticeChoice:
    duration: str
    outcome: str
    tier: Tier

    @property
    def input_ref(self) -> str:
        return _SEPARATOR.join((self.duration, self.outcome, self.tier))

    def as_form(self) -> Mapping[str, str]:
        """The value chosen for each form field."""
        return {"duration": self.duration, "outcome": self.outcome, "tier": self.tier}


class PracticeContext(Protocol):
    """The part of the runner's ``TaskContext`` a practice run uses."""

    @property
    def task_id(self) -> str: ...

    @property
    def input_ref(self) -> str | None: ...

    @property
    def cancel_flag(self) -> CancelToken: ...

    def heartbeat(self, progress: int | None = None) -> Awaitable[None]: ...

    def check_cancelled(self) -> None: ...


def choice_from_form(form: Mapping[str, Sequence[str]]) -> PracticeChoice | None:
    """The choice a submitted form names, or None unless each field is there exactly once
    with a value from its list (exact match: no trimming, no case folding)."""
    values: list[str] = []
    for name, allowed in PRACTICE_FIELDS.items():
        given = form.get(name, ())
        if len(given) != 1 or given[0] not in allowed:
            return None
        values.append(given[0])
    return _choice(values)


def _choice(values: Sequence[str]) -> PracticeChoice | None:
    if len(values) != len(PRACTICE_FIELDS):
        return None
    duration, outcome, tier = values
    if duration not in DURATIONS or outcome not in OUTCOMES:
        return None
    chosen = next((known for known in TIERS if known == tier), None)
    if chosen is None:
        return None
    return PracticeChoice(duration, outcome, chosen)


def practice_result_ref(steps: int) -> str:
    """Names the number of steps a run finished; no results table until M3."""
    return f"{PRACTICE}:{steps}"


class PracticeService:
    def __init__(
        self,
        *,
        factory: AIProviderFactory,
        allow_fakes: bool,
        script: ScriptPractice,
        registered: Callable[[], bool],
        settings: Callable[[], AIProviderSettings | None],
        tasks: Callable[[], TaskService | None],
        usage: Callable[[], UsageService | None],
        data_dir: Callable[[], Path | None],
    ) -> None:
        """``registered`` says whether the app's task registry knows the practice type.
        ``settings`` (blocking: a database read), ``tasks``, ``usage`` and ``data_dir``
        answer None while no workspace is open."""
        self._factory = factory
        self._allow_fakes = allow_fakes
        self._script = script
        self._registered = registered
        self._settings = settings
        self._tasks = tasks
        self._usage = usage
        self._data_dir = data_dir

    def offered(self) -> bool:
        """Fakes are allowed, the app's task registry knows the practice type and the saved
        provider is the fake. Blocking; writes nothing."""
        return self._registered() and self._fake_settings() is not None

    def start(self, choice: PracticeChoice) -> TaskRef:
        """Queue a practice task; one already queued or running is returned instead (with
        the values it was started with). Blocking. Raises ``PracticeNotOffered``."""
        tasks = self._tasks()
        if tasks is None or not self.offered():
            raise PracticeNotOffered
        return tasks.enqueue(PRACTICE, PRACTICE_TARGET, input_ref=choice.input_ref)

    async def run(self, ctx: PracticeContext) -> int:
        """Run the steps of one practice task and return how many there were. Provider
        errors and the cancel signal propagate; ``TaskFailed`` when its input isn't a
        practice choice and ``PracticeNotOffered`` when the task is no longer offered
        (before any provider call or usage record)."""
        choice = _choice((ctx.input_ref or "").split(_SEPARATOR))
        if choice is None:
            raise TaskFailed(detail="practice: input is not a practice choice")
        settings = await asyncio.to_thread(self._fake_settings)
        usage, data_dir = self._usage(), self._data_dir()
        if settings is None or usage is None or data_dir is None:
            raise PracticeNotOffered
        provider = self._scripted_provider(settings, data_dir, choice)
        runner = self._factory.task_runner(provider, lambda: settings, data_dir=data_dir)
        meter = ServiceUsageMeter(usage, PRACTICE_SERVICE)
        for step in range(PRACTICE_STEPS):
            ctx.check_cancelled()
            await ctx.heartbeat(step * 100 // PRACTICE_STEPS)
            await runner.run_task(
                PRACTICE,
                {"step": step + 1, "steps": PRACTICE_STEPS},
                ctx.cancel_flag,
                meter=meter,
                tier=choice.tier,
                task_id=ctx.task_id,
            )
        return PRACTICE_STEPS

    def _fake_settings(self) -> AIProviderSettings | None:
        if not self._allow_fakes:
            return None
        settings = self._settings()
        return settings if settings is not None and settings.kind == FAKE_KIND else None

    def _scripted_provider(
        self, settings: AIProviderSettings, data_dir: Path, choice: PracticeChoice
    ) -> AIProvider:
        delay = SLOW_STEP_SECONDS if choice.duration == "slow" else 0.0
        try:
            provider = self._factory.ai_provider(settings, data_dir=data_dir)
            self._script(provider, outcome=choice.outcome, delay_seconds=delay)
        except ValueError:  # wiring no longer builds or scripts the fake
            raise PracticeNotOffered from None
        return provider
