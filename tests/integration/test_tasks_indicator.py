"""Which pages carry the top-bar task indicator (M2.20).

Every app page renders, in the top bar's ``data-slot="tasks"``, the disclosure button
``#tasks-toggle`` (closed, with the running count in words in ``#tasks-count``) and the
element it controls, ``#tasks-popover`` (hidden until opened), which holds or loads the M2.19
panel fragment from ``GET /tasks/panel``. The setup guide, with its own top bar, has none of
it and never names the panel URL. What the button does is browser behaviour:
``tests/e2e/test_tasks_panel.py``. All folders live under ``tmp_path``; values are fictional.
"""

from pathlib import Path

import pytest

from tests.integration.test_events_page_wiring import APP_PAGES, GUIDE_STEPS, client_for
from tests.integration.test_practice_task import by_id
from tests.integration.test_setup_redirect import Element, outline

pytestmark = pytest.mark.integration

SLOT = "tasks"
TOGGLE_ID = "tasks-toggle"
COUNT_ID = "tasks-count"
POPOVER_ID = "tasks-popover"
PANEL_URL = "/tasks/panel"
TRIGGERS = ("tasks:resync from:body", "tasks:updated from:body")
NONE_RUNNING = "No tasks running"


def slot_of(elements: list[Element]) -> Element | None:
    found = [e for e in elements if e.attrs.get("data-slot") == SLOT]
    assert len(found) <= 1, "one tasks slot at most"
    return found[0] if found else None


@pytest.mark.parametrize("path", APP_PAGES)
def test_app_pages_render_indicator_with_panel_url(tmp_path: Path, path: str) -> None:
    with client_for(tmp_path, complete=True) as client:
        response = client.get(path)

    assert response.status_code == 200, path
    elements = outline(response.text)
    slot = slot_of(elements)
    assert slot is not None, "the top bar keeps its tasks slot"

    toggle = by_id(elements, TOGGLE_ID)
    assert toggle is not None, "the indicator button is on the page"
    assert toggle.tag == "button" and toggle.attrs.get("type") == "button"
    assert toggle.within(slot), "inside the top bar's tasks slot"
    assert toggle.attrs.get("aria-expanded") == "false", "closed on a fresh page"
    assert toggle.attrs.get("aria-controls") == POPOVER_ID
    assert "Tasks" in toggle.text

    count = by_id(elements, COUNT_ID)
    assert count is not None and count.within(toggle), "the count is part of the button's name"
    assert count.text == NONE_RUNNING, "in words, never colour alone"

    popover = by_id(elements, POPOVER_ID)
    assert popover is not None, "aria-controls names an element on the page"
    assert "hidden" in popover.attrs, "closed on a fresh page"
    assert popover.attrs.get("aria-modal") != "true" and popover.tag != "dialog"

    loaders = [e for e in elements if e.attrs.get("hx-get") == PANEL_URL]
    assert loaders, "the panel fragment URL is on the page"
    for loader in loaders:
        assert loader is popover or loader.within(popover) or loader.within(slot), loader.attrs
    assert any(loader is popover or loader.within(popover) for loader in loaders)
    wired = " ".join(loader.attrs.get("hx-trigger") or "" for loader in loaders)
    for trigger in TRIGGERS:
        assert trigger in wired, f"refreshes on {trigger}"


@pytest.mark.parametrize("step", GUIDE_STEPS)
def test_guide_pages_have_no_indicator_or_panel(tmp_path: Path, step: str) -> None:
    with client_for(tmp_path, complete=True) as client:
        app_page = outline(client.get("/").text)  # the positive control
        assert by_id(app_page, TOGGLE_ID) is not None, "app pages carry the indicator"
        response = client.get(f"/setup/{step}")

    assert response.status_code == 200, step
    elements = outline(response.text)
    assert slot_of(elements) is None
    for element_id in (TOGGLE_ID, COUNT_ID, POPOVER_ID, "tasks-panel"):
        assert by_id(elements, element_id) is None, element_id
    assert PANEL_URL not in response.text


def test_guide_page_has_no_indicator_while_setup_is_incomplete(tmp_path: Path) -> None:
    with client_for(tmp_path, complete=False) as client:
        response = client.get("/setup/welcome")

    assert response.status_code == 200
    elements = outline(response.text)
    assert by_id(elements, TOGGLE_ID) is None and by_id(elements, POPOVER_ID) is None
    assert PANEL_URL not in response.text
