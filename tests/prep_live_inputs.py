"""The requests the app builds for the six M8 interview-preparation tasks, from fictional data
(M8.3 part C). Shared by the opt-in live test (``tests/live/test_m8_live.py``) and by the offline
control that proves these inputs build, hold no personal data and carry no tools
(``tests/integration/test_m8_live_inputs.py``).

``built_prep_inputs`` drives the app's own services against the fake provider, one temporary
folder per task, with a person's contact details and photo saved first (so a request that leaked
them would be caught), and keeps the request the fake provider was sent for that task. The tasks
are ``interview_questions``, ``interview_web_questions``, ``answer_feedback``, ``draft_narrative``,
``adapt_answer`` and ``company_brief``. Nothing here touches the network, a real provider, a
keychain or a real data folder.

``live_request`` turns one built request into the one the live test sends: the real model for its
tier, no usage or tool callbacks, and web search switched off, so six calls make zero searches.
"""

from dataclasses import dataclass, replace
from pathlib import Path

from app.modules.ai import AIRequest

PREP_TASKS = (
    "interview_questions",
    "interview_web_questions",
    "answer_feedback",
    "draft_narrative",
    "adapt_answer",
    "company_brief",
)


@dataclass(frozen=True)
class PrepInputs:
    requests: tuple[AIRequest, ...]  # one per task in ``PREP_TASKS``, in that order
    personal: tuple[str, ...]  # the personal values that were saved and must not be in a request
    photo: bytes  # the saved photo's bytes


def live_request(request: AIRequest, model: str) -> AIRequest:
    """The request as the live test sends it: the real ``model``, no callbacks and no search."""
    return replace(
        request,
        model=model,
        on_usage=None,
        on_tool_use=None,
        on_search_usage=None,
        allow_web_search=False,
    )


def built_prep_inputs(folder: Path) -> PrepInputs:
    """Run each task's flow once on fictional data and keep its request."""
    from tests.export_pages import exporting_app
    from tests.integration.test_m6_cross_cutting import DRIVERS, saved_personal

    built: list[AIRequest] = []
    personal: tuple[str, ...] = ()
    photo = b""
    for task in PREP_TASKS:
        with exporting_app(folder / task) as desk:
            strings, photo_bytes = saved_personal(desk)
            DRIVERS[task](desk)
            sent = [request for request in desk.spy.requests if request.task == task]
            assert sent, f"the flow for {task} made no request"
            built.append(sent[-1])
            personal, photo = tuple(strings), photo_bytes
    return PrepInputs(tuple(built), personal, photo)
