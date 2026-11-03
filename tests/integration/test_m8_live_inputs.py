"""The inputs of the opt-in live test ``tests/live/test_m8_live.py`` built offline (M8.3 part C).

The live test cannot run in CI, so what it sends is proved here against the fake provider: the six
M8 tasks are each built once from the app's own flows, with a person's saved contact details and
photo absent from every request, valid schemas that the fake's own answers satisfy, and the tool
flags stripped by the helper the live test uses, so six calls make zero searches.
"""

from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from app.modules.ai.tasks import TASK_SPECS
from tests.integration.test_m6_cross_cutting import find_personal, request_text
from tests.prep_live_inputs import PREP_TASKS, built_prep_inputs, live_request

pytestmark = pytest.mark.integration


def test_m8_live_inputs_build_offline_with_no_personal_data_and_no_tools(tmp_path: Path) -> None:
    built = built_prep_inputs(tmp_path)

    assert PREP_TASKS == (
        "interview_questions",
        "interview_web_questions",
        "answer_feedback",
        "draft_narrative",
        "adapt_answer",
        "company_brief",
    )
    assert tuple(r.task for r in built.requests) == PREP_TASKS, "six tasks, one request each"
    assert set(PREP_TASKS) <= set(TASK_SPECS)
    assert len({r.user for r in built.requests}) == 6, "six different requests"
    assert built.personal and built.photo, "personal details were saved, so a leak could show"

    web_tasks = {t for t in PREP_TASKS if TASK_SPECS[t].allow_web_search}
    assert web_tasks == {"interview_web_questions", "company_brief"}
    for request in built.requests:
        Draft202012Validator.check_schema(request.output_schema)
        found = find_personal(request_text(request), list(built.personal), built.photo)
        assert not found, f"{request.task} request holds personal data: {found}"
        assert request.model, request.task

        live = live_request(request, "real-model-name")
        assert live.model == "real-model-name"
        assert live.allow_web_search is False, "the live call makes no search"
        assert live.on_tool_use is None and live.on_usage is None
        assert live.on_search_usage is None
        assert (live.task, live.system, live.user, live.output_schema) == (
            request.task,
            request.system,
            request.user,
            request.output_schema,
        ), "only the model and the tool flags change"
        if request.task in web_tasks:
            assert request.allow_web_search is True, "stripping changes the web tasks' request"
