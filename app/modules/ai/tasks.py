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
    # Existing generic web callers keep the global ceiling; M8 web tasks set five.
    max_searches: int = 20


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
    # M6.5: which requirements the confirmed facts show, plus honest framing ideas; it never
    # adds a claim, and code decides what the user is told about the role (spec 3.1 item 5).
    TaskSpec("gap_check", kind="feedback"),
    # M6.5: two or three positioning angles built from the confirmed facts; never a new claim.
    TaskSpec("propose_angles", kind="feedback"),
    # M6.6: a country's CV layout from the country, style choices and a short note only; no
    # facts and no personal details reach it, so it is a plain summary (no honesty notice).
    TaskSpec("draft_cv_template", kind="summary"),
    # M6.7: the content lines of a tailored CV from the confirmed facts, each citing fact ids.
    # Kind generation: the executor appends the honesty notice last (ADR-106 item 7, ADR-109)
    # and code checks every unit against its cited facts (spec 3.1).
    TaskSpec("tailor_cv", kind="generation"),
    # M6.9: the body paragraphs of a cover letter from the confirmed facts, each citing fact ids;
    # the greeting, opening line, closing and name come from code. Kind generation, as tailor_cv.
    TaskSpec("draft_cover_letter", kind="generation"),
    TaskSpec("enrichment_question", kind="feedback"),
    TaskSpec("structure_answer", kind="feedback"),
    TaskSpec("interview_questions", kind="feedback"),
    TaskSpec("interview_web_questions", kind="summary", allow_web_search=True, max_searches=5),
    # M8.2: feedback on one practice answer from the confirmed facts and the CV sent; the length
    # and the match with the CV are decided in code, and nothing it says is applied for the user.
    TaskSpec("answer_feedback", kind="feedback"),
    # M8.2: the paragraphs of a narrative card (tell me about yourself, why this role and so on)
    # from the confirmed facts, each citing fact ids. Kind generation, as tailor_cv; the salary
    # card is built in code and has no task.
    TaskSpec("draft_narrative", kind="generation"),
    # M8.2 part C: a practice answer from another application rewritten from the confirmed facts
    # for this job, each paragraph citing fact ids. Kind generation, as draft_narrative; the old
    # answer is untrusted data and proves nothing.
    TaskSpec("adapt_answer", kind="generation"),
)
TASK_SPECS: Final[Mapping[str, TaskSpec]] = MappingProxyType({spec.name: spec for spec in _SPECS})
