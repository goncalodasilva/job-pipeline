"""Settings > Providers, model per tier: review follow-ups (M1.22 review).

- A body over the form limit answers 413 on ``POST /settings/tiers`` and changes nothing.
- A remembered "Connected" is forgotten by a saved tier map (the announcement asks for a new
  test) and kept after a refused one.
- One focus target: the tier heading after a save, the first invalid select after a refusal;
  never the page heading as well.

Providers are the app-selectable fake; every value is fictional.
"""

from pathlib import Path

import pytest
from httpx import Response

from app.api.forms import MAX_FORM_BYTES
from app.domain.settings import TierMap
from app.modules.ai.providers.fake import FAKE_FAST_MODEL, FAKE_THOROUGH_MODEL
from tests.integration.test_settings_providers import (
    assert_section_fragment,
    post_settings_test,
    settings_page,
    settings_version,
)
from tests.integration.test_setup_provider import FAKE_KIND, stored
from tests.integration.test_setup_provider_test import FAKE_CONNECTED, announced
from tests.integration.test_setup_redirect import Element, Running, outline
from tests.integration.test_setup_reopen_banner import finished_guide, started

pytestmark = pytest.mark.integration

SETTINGS_TIERS = "/settings/tiers"
SWAPPED = {"fast": FAKE_THOROUGH_MODEL, "thorough": FAKE_FAST_MODEL}
UNKNOWN_MODEL = "fictional-model-9"


def post_tiers(run: Running, form: dict[str, str], *, htmx: bool = True) -> Response:
    headers = run.action(**({"HX-Request": "true"} if htmx else {}))
    return run.client.post(SETTINGS_TIERS, data=form, headers=headers)


def focused(elements: list[Element]) -> list[str | None]:
    return [e.attrs.get("id") for e in elements if "data-autofocus" in e.attrs]


def connection_results(elements: list[Element]) -> list[str]:
    return [e.text for e in elements if e.attrs.get("id") == "connection-result"]


def connected(run: Running) -> None:
    tested = post_settings_test(run, FAKE_KIND, FAKE_CONNECTED)
    assert tested.status_code == 200, tested.text[:300]
    shown = connection_results(outline(settings_page(run).text))
    assert len(shown) == 1 and shown[0].startswith("Connected"), shown


def test_tiers_route_refuses_an_oversized_body(tmp_path: Path) -> None:
    finished_guide(tmp_path)
    with started(tmp_path) as run:
        version = settings_version(run)
        body = {"fast": "x" * (MAX_FORM_BYTES + 1), "thorough": FAKE_FAST_MODEL}
        response = post_tiers(run, body)
        assert response.status_code == 413, response.status_code
        assert response.text == ""
        assert settings_version(run) == version
        assert stored(run).tiers == TierMap()


def test_saved_tier_map_forgets_connected_and_asks_for_a_test(tmp_path: Path) -> None:
    finished_guide(tmp_path)
    with started(tmp_path) as run:
        connected(run)
        saved = post_tiers(run, SWAPPED)
        assert saved.status_code == 200, saved.text[:300]
        assert connection_results(assert_section_fragment(saved)) == []
        assert announced(saved) == ("Tier models saved. Test the connection again to check them.")
        assert connection_results(outline(settings_page(run).text)) == []

        # Nothing remembered any more: a second save just says it was saved.
        again = post_tiers(run, {"fast": FAKE_FAST_MODEL, "thorough": FAKE_FAST_MODEL})
        assert announced(again) == "Tier models saved."


def test_refused_tier_map_keeps_connected(tmp_path: Path) -> None:
    finished_guide(tmp_path)
    with started(tmp_path) as run:
        connected(run)
        refused = post_tiers(run, {"fast": UNKNOWN_MODEL, "thorough": FAKE_FAST_MODEL})
        assert refused.status_code == 422, refused.status_code
        shown = connection_results(assert_section_fragment(refused))
        assert len(shown) == 1 and shown[0].startswith("Connected"), shown
        assert connection_results(outline(settings_page(run).text)) == shown


@pytest.mark.parametrize("htmx", [True, False], ids=["htmx", "plain"])
def test_tier_focus_has_one_target(tmp_path: Path, htmx: bool) -> None:
    finished_guide(tmp_path)
    with started(tmp_path) as run:
        refused = post_tiers(run, {"fast": UNKNOWN_MODEL, "thorough": ""}, htmx=htmx)
        assert refused.status_code == 422
        assert focused(outline(refused.text)) == ["tier-fast"], "the first invalid select only"

        saved = post_tiers(run, SWAPPED)
        assert saved.status_code == 200
        elements = outline(saved.text)
        assert focused(elements) == ["tiers-heading"]
        heading = next(e for e in elements if e.attrs.get("id") == "tiers-heading")
        assert heading.attrs.get("tabindex") == "-1"
        assert "Tier models saved." in saved.text
        assert 'role="alert"' not in saved.text
