"""What the Settings > Providers and AI task tier list is given (M9.2 part C; ADR-089 item 9;
ADR-069 item 6; ADR-125 item 5; TD-165).

Kept apart from the route (``app/api/settings_task_tiers.py``) so the Providers page
(``app/api/settings.py``) can show the list without importing it, as for Sessions. A read: it
writes nothing. The words of each task (a plain-language label and one line saying what it does)
are the UI's own copy; the defaults are ``app.domain.tiers.DEFAULT_TIERS`` and the effective tier
comes from the provider step's view (the override, else the default).
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Final

from app.domain.errors import VersionConflict
from app.domain.tiers import DEFAULT_TIERS

SAVE_PATH: Final = "/settings/tiers/tasks"
AREA_PATH: Final = "/settings"
FIELD_PREFIX: Final = "tier_"
SAVED: Final = "Task tiers saved."
NOT_SAVED: Final = "Task tiers not saved."
TIER_WORDS: Final[Mapping[str, str]] = MappingProxyType({"fast": "Fast", "thorough": "Thorough"})

# label, one line of what the task does. One entry per task of DEFAULT_TIERS (a test keeps them
# in step); the fake-only practice task is not listed.
TASK_COPY: Final[Mapping[str, tuple[str, str]]] = MappingProxyType(
    {
        "extract_cv": (
            "Reading a CV you import",
            "Lists the roles, skills and achievements a CV states, as drafts for you to review.",
        ),
        "refine_fact": (
            "Sharpening a fact",
            "Suggests questions and wording that make one of your facts more specific.",
        ),
        "summarize_job": (
            "Summarising a job",
            "Boils a job post down to its role, requirements and key details.",
        ),
        "assess_fit": (
            "Judging how well a job fits",
            "Compares a job's requirements with the facts you have confirmed.",
        ),
        "gap_check": (
            "Checking the gaps",
            "Finds requirements your facts do not show yet and suggests honest ways to frame them.",
        ),
        "propose_angles": (
            "Proposing angles",
            "Suggests two or three ways to present yourself for one role.",
        ),
        "draft_cv_template": (
            "Drafting a CV layout",
            "Proposes the layout a country expects from your style choices and a short note.",
        ),
        "tailor_cv": (
            "Tailoring your CV",
            "Writes the lines of a CV for one job from your confirmed facts.",
        ),
        "draft_cover_letter": (
            "Drafting a cover letter",
            "Writes the body paragraphs of a cover letter from your confirmed facts.",
        ),
        "enrichment_question": (
            "Asking session questions",
            "Chooses the next question in a session that helps you add facts.",
        ),
        "structure_answer": (
            "Structuring your answers",
            "Turns an answer from a session or a debrief into a draft fact for you to review.",
        ),
        "interview_questions": (
            "Preparing interview questions",
            "Suggests questions an interviewer may ask for one application.",
        ),
        "interview_web_questions": (
            "Finding interview questions online",
            "Looks up questions reported for the company, only when you press the search button.",
        ),
        "answer_feedback": (
            "Giving feedback on a practice answer",
            "Reads your practice answer against your facts and CV and suggests improvements.",
        ),
        "draft_narrative": (
            "Drafting talking points",
            "Writes answers such as tell me about yourself from your confirmed facts.",
        ),
        "adapt_answer": (
            "Adapting an earlier answer",
            "Rewrites a practice answer from another application for this job.",
        ),
        "company_brief": (
            "Briefing you on a company",
            "Writes a short sourced brief about one company, using a web search.",
        ),
    }
)
_NOTHING: Mapping[str, str] = MappingProxyType({})


def task_label(task: str) -> str:
    copy = TASK_COPY.get(task)
    return copy[0] if copy else task.replace("_", " ").capitalize()


@dataclass(frozen=True)
class TaskTierState:
    """What just happened in the list; the default is a plain page load."""

    errors: Mapping[str, str] = field(default_factory=lambda: _NOTHING)  # message per task name
    list_error: str = ""  # a task that is not listed was sent: shown once for the list
    typed: Mapping[str, str] = field(default_factory=lambda: _NOTHING)  # accepted tiers sent
    saved: bool = False  # done: the line shows and the summary takes focus
    conflict: bool = False  # a stale version: the reload prompt shows and takes focus

    @property
    def attention(self) -> bool:
        return bool(self.errors or self.list_error or self.saved or self.conflict)


def task_tiers_context(
    effective: Mapping[str, str], version: int, state: TaskTierState | None
) -> dict[str, Any]:
    """The ``task_tiers`` template value: one row per task of ``DEFAULT_TIERS``, showing the
    stored tier (an accepted value that was sent stays; a refused one is never shown back)."""
    state = state or TaskTierState()
    rows: list[dict[str, Any]] = []
    for task, default in DEFAULT_TIERS.items():
        label, description = TASK_COPY.get(task, (task_label(task), "Runs one AI task."))
        chosen = effective.get(task, default)
        if task not in state.errors and state.typed.get(task) in TIER_WORDS:
            chosen = state.typed[task]
        rows.append(
            {
                "task": task,
                "field": f"{FIELD_PREFIX}{task}",
                "label": label,
                "description": description,
                "default": TIER_WORDS[default],
                "chosen": chosen,
                "error": state.errors.get(task, ""),
            }
        )
    first_invalid = next((row["field"] for row in rows if row["error"]), "")
    return {
        "version": version,
        "rows": rows,
        "tiers": list(TIER_WORDS.items()),
        "save_url": SAVE_PATH,
        "list_error": state.list_error,
        "first_invalid": first_invalid,
        "saved": SAVED if state.saved else "",
        "conflict": state.conflict,
        "conflict_message": VersionConflict.message,
        "reload_url": AREA_PATH,
        "attention": state.attention,
    }
