"""Root test configuration shared by every test layer.

Enables the ``pytester`` plugin so tests can run inner pytest sessions
against the project's real pytest configuration, and gates live tests
behind ``JOBPIPELINE_LIVE_TESTS=1`` (spec 7.2).
"""

import os

import pytest

pytest_plugins = ["pytester"]

LIVE_ENV = "JOBPIPELINE_LIVE_TESTS"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get(LIVE_ENV) == "1":
        return
    skip_live = pytest.mark.skip(reason=f"live test: set {LIVE_ENV}=1 to run it")
    for item in items:
        if item.get_closest_marker("live") is not None:
            item.add_marker(skip_live)
