"""Package metadata and the empty package tree from spec 2.13 (M0.1)."""

import importlib
import importlib.metadata

import pytest

SPEC_LAYOUT_PACKAGES = [
    "app.api",
    "app.onboarding",
    "app.services",
    "app.domain",
    "app.modules",
    "app.modules.ai",
    "app.modules.ai.providers",
    "app.modules.search",
    "app.modules.search.providers",
    "app.modules.calendar",
    "app.modules.calendar.providers",
    "app.modules.storage",
    "app.modules.secrets",
    "app.modules.secrets.providers",
    "app.modules.fetch",
    "app.render",
    "app.tasks",
]


def test_package_exposes_version_from_metadata() -> None:
    import app

    assert app.__version__ == importlib.metadata.version("jobpipeline")


@pytest.mark.parametrize("package", SPEC_LAYOUT_PACKAGES)
def test_package_tree_matches_spec_layout(package: str) -> None:
    module = importlib.import_module(package)

    assert hasattr(module, "__path__"), f"{package} must be a package, not a module"
