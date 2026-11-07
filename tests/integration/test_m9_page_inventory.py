"""M9.6 part A: the page inventory covers every page route (AC-M9.5).

Every route registered as a page (``PAGE_TAG``) must have at least one screen in
``tests/page_inventory.py``, which the e2e audit runs axe on (light and dark, desktop and phone)
and walks by keyboard. A new page without a screen there fails here.
"""

import re
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.api.security import PAGE_TAG
from tests.integration.test_app_shell import BASE_URL
from tests.page_inventory import OVERLAY_CASES, PAGE_CASES

pytestmark = pytest.mark.integration


def page_routes(app: object) -> set[str]:
    found: set[str] = set()
    for route in app.router.routes:  # type: ignore[attr-defined]
        for inner in getattr(route, "routes", None) or [route]:
            if isinstance(inner, APIRoute) and PAGE_TAG in (inner.tags or []):
                found.add(inner.path)
    return found


def test_ac_m9_5_every_page_route_has_an_axe_case(tmp_path: Path) -> None:
    from app.config import RuntimeConfig
    from app.main import create_app

    config = RuntimeConfig(
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "config",
        port=8000,
        log_level="INFO",
        demo=False,
        setup=False,
        no_browser=True,
        data_dir_source="cli",
    )
    app = create_app(config)
    with TestClient(app, base_url=BASE_URL):
        routes = page_routes(app)
    assert routes, "the app registers page routes"
    covered = {case.route for case in PAGE_CASES}
    assert routes - covered == set(), "page routes with no screen in tests/page_inventory.py"
    assert covered - routes == set(), "screens naming a route the app does not have"


def test_page_cases_are_unique_and_reach_their_route() -> None:
    names = [case.name for case in PAGE_CASES]
    assert len(names) == len(set(names))
    for case in PAGE_CASES:
        if case.via is not None:
            assert case.arrives is not None, case.name
            continue
        pattern = re.sub(r"\{[^}]+\}", "[^/?]+", case.route)
        path = case.url.split("?", 1)[0]
        assert re.fullmatch(pattern, path), (case.name, case.url)


def test_overlay_cases_are_unique_and_open_on_a_page_with_a_case() -> None:
    names = [case.name for case in OVERLAY_CASES]
    assert len(names) == len(set(names))
    page_urls = {case.url.split("?", 1)[0] for case in PAGE_CASES}
    for case in OVERLAY_CASES:
        assert case.url in page_urls, (case.name, case.url)
    assert OVERLAY_CASES[-1].name == "notifications-panel", "it marks rows seen: keep it last"
