"""The ``company_brief`` wrapper caps what it sends (M8.3 part B review fix).

The stored company fields are untrusted and can be long; the wrapper sends at most a fixed
number of characters of each, still as one JSON object. A stub runner captures the input, so no
provider and no network are involved. Every value is fictional.
"""

import json
from collections.abc import Mapping
from typing import Any

from app.modules.ai import TaskResult
from app.services.company_brief_ai import (
    MAX_INDUSTRY,
    MAX_NAME,
    MAX_WEBSITE,
    BriefContext,
    company_brief,
)
from tests.fakes.ai import NotCancelled
from tests.fakes.usage import RecordingMeter


class CapturingRunner:
    def __init__(self) -> None:
        self.inputs: Mapping[str, object] = {}

    async def run_task(self, task: str, inputs: Mapping[str, object], cancel: Any, **_: Any):
        self.inputs = inputs
        return TaskResult({"sections": []}, "company_brief", "2", "fake", "thorough", 1, ())


async def _sent(context: BriefContext) -> dict[str, Any]:
    runner = CapturingRunner()
    await company_brief(runner, context, NotCancelled(), meter=RecordingMeter())  # type: ignore[arg-type]
    loaded: dict[str, Any] = json.loads(str(runner.inputs["context"]))
    return loaded


async def test_overlong_name_website_and_industry_are_cut_to_their_caps():
    sent = await _sent(BriefContext("c1", "n" * 5000, "https://" + "w" * 5000, "i" * 5000))
    assert (len(sent["company"]), len(sent["website"]), len(sent["industry"])) == (
        MAX_NAME,
        MAX_WEBSITE,
        MAX_INDUSTRY,
    )
    assert set(sent) == {"company", "website", "industry"}


async def test_values_within_the_caps_are_sent_unchanged():
    sent = await _sent(BriefContext("c1", "Fictional Orchard", "https://o.example.test", "Farming"))
    assert sent == {
        "company": "Fictional Orchard",
        "website": "https://o.example.test",
        "industry": "Farming",
    }


async def test_empty_website_and_industry_are_sent_as_null():
    sent = await _sent(BriefContext("c1", "Fictional Orchard", "", ""))
    assert sent["website"] is None and sent["industry"] is None
