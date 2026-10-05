"""The cached start-up decision behind the setup redirect (AC-M1.2; ADR-050, ADR-052).

``OnboardingState`` holds one ``StartupDecision``. It starts from the start-up inputs with no
database, and is recomputed only by ``refresh``: at start-up, after a workspace opens and after
state-changing requests, never on GET. The redirect middleware reads the cached value, so a
request does no database, keychain or file reads to decide where it goes.
"""

import threading
from collections.abc import Mapping
from dataclasses import dataclass, replace

from app.onboarding.detection import StartupDecision, StartupFacts, StepStatus, detect
from app.onboarding.progress import SetupProgress
from app.onboarding.registry import StepRegistry


@dataclass(frozen=True)
class StartupInputs:
    bootstrap_exists: bool  # config.toml present
    data_dir_explicit: bool  # folder given by --data-dir, JOBPIPELINE_DATA_DIR or --demo
    demo: bool
    force_setup: bool  # --setup


class OnboardingState:
    def __init__(self, registry: StepRegistry, inputs: StartupInputs) -> None:
        self._registry = registry
        self._inputs = inputs
        self._provider_healthy = True
        self._lock = threading.Lock()
        self._statuses: dict[str, StepStatus] = {}
        self._decision = self._detect(None)

    @property
    def decision(self) -> StartupDecision:
        return self._decision

    @property
    def statuses(self) -> Mapping[str, StepStatus]:
        """Step statuses as of the last refresh (for the guide's step list; no I/O)."""
        return self._statuses

    def guide_step(self) -> str | None:
        """The step requests are sent to while setup is incomplete, else None.

        A folder that moved or was deleted (``locate_or_fresh``) is handled by the first step.
        """
        decision = self._decision
        if decision.kind == "guide":
            return decision.step or self._registry.first.id
        if decision.kind == "locate_or_fresh":
            return self._registry.first.id
        return None

    def refresh(
        self,
        progress: SetupProgress | None,
        *,
        provider_healthy: bool | None = None,
        bootstrap_exists: bool | None = None,
    ) -> StartupDecision:
        """Recompute and cache the decision. ``progress`` is None while no workspace is open.

        ``provider_healthy`` (healthy until told otherwise) and ``bootstrap_exists`` update the
        stored values; None keeps them.
        """
        with self._lock:
            if bootstrap_exists is not None:
                self._inputs = replace(self._inputs, bootstrap_exists=bootstrap_exists)
            if provider_healthy is not None:
                self._provider_healthy = provider_healthy
            self._decision = self._detect(progress)
            return self._decision

    def _detect(self, progress: SetupProgress | None) -> StartupDecision:
        inputs = self._inputs
        self._statuses = dict(progress.statuses()) if progress is not None else {}
        return detect(
            StartupFacts(
                bootstrap_exists=inputs.bootstrap_exists,
                data_dir_explicit=inputs.data_dir_explicit,
                demo=inputs.demo,
                database_exists=progress is not None,
                steps=self._registry.infos(),
                statuses=self._statuses,
                provider_healthy=self._provider_healthy,
                force_setup=inputs.force_setup,
                done_step=self._registry.last.id,
            )
        )
