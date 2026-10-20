"""AI tasks known to the app (spec 3.6). Defined in code, not in prompt files, so a user
override can never change a task's kind or enable web search (ADR-055)."""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from app.modules.ai import TaskKind


@dataclass(frozen=True)
class TaskSpec:
    name: str
    kind: TaskKind
    allow_web_search: bool = False


# The practice task (M2.17; ADR-076 item 2) drives the runner, the meter and the tasks UI
# before M3. It is not in TASK_SPECS: wiring adds it only for the fake provider while fakes
# are allowed. It generates no user-facing content and cites no facts.
PRACTICE_SPEC: Final = TaskSpec("practice", kind="summary")

# Real tasks arrive with their features. Each has a row in spec 3.6 and a folder in prompts/
# (tests/unit/modules/ai/test_task_library.py keeps the three in step).
_SPECS: Final = (
    # M3.4: lists what a CV states; every item is stored as a draft (AC-M3.1).
    TaskSpec("extract_cv", kind="extraction"),
    # M3.8: sharpening questions and suggested wording for one fact; it never adds a claim
    # and never changes the fact (spec 3.1 item 5; AC-M3.5).
    TaskSpec("refine_fact", kind="feedback"),
    TaskSpec("summarize_job", kind="summary"),
    TaskSpec("assess_fit", kind="feedback"),
)
TASK_SPECS: Final[Mapping[str, TaskSpec]] = MappingProxyType({spec.name: spec for spec in _SPECS})
