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


# Real tasks arrive with their features (extract_cv in M3 and so on).
TASK_SPECS: Final[Mapping[str, TaskSpec]] = MappingProxyType({})
