"""AIRequest carries the prompt version as text, so overrides read "<v>+override" (M1.11).

Spec 3.6: every generated record stores ``prompt_version``; with a user override it is
recorded as ``"<default version>+override"``.
"""

from typing import get_type_hints

from app.modules.ai import AIRequest
from tests.fakes.ai import make_request


def test_ai_request_prompt_version_is_text_to_carry_override() -> None:
    assert get_type_hints(AIRequest)["prompt_version"] is str
    assert make_request().prompt_version == "1"
    assert make_request(prompt_version="3+override").prompt_version == "3+override"
