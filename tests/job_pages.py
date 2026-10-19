"""The fictional job page fixtures and their manifest (M4.1; used again by M4.3).

``tests/fixtures/jobs/manifest.json`` lists every page under ``pages``: the file, the content
type it is served with, the ``*.example.test`` URL it lives at, a short description, the
``markers`` that ``page_text`` must keep and the ``absent`` strings it must drop.
``relations`` says which pairs are the same ad (``duplicates``) and which are not
(``distinct``). Every company, board and host is made up.

The fake fetcher's page type is imported lazily, so this helper loads even before the fetch
providers exist.
"""

import importlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
JOBS_DIR = REPO_ROOT / "tests" / "fixtures" / "jobs"
MANIFEST_NAME = "manifest.json"


def load_manifest() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((JOBS_DIR / MANIFEST_NAME).read_text(encoding="utf-8"))
    return loaded


def page_entries() -> dict[str, dict[str, Any]]:
    """Page id to its manifest entry."""
    pages: dict[str, dict[str, Any]] = load_manifest()["pages"]
    return pages


def page_entry(page_id: str) -> dict[str, Any]:
    return page_entries()[page_id]


def page_bytes(page_id: str) -> bytes:
    path = JOBS_DIR / page_entry(page_id)["file"]
    assert path.is_file(), f"missing job page fixture: {path.name}"
    return path.read_bytes()


def page_url(page_id: str) -> str:
    url: str = page_entry(page_id)["url"]
    return url


def relations() -> dict[str, list[list[str]]]:
    found: dict[str, list[list[str]]] = load_manifest()["relations"]
    return found


def fake_pages(page_ids: list[str] | None = None) -> Mapping[str, Any]:
    """``{url: FakePage}`` for ``FakeJobPageFetcher``, for every page or the ones named."""
    fake = importlib.import_module("app.modules.fetch.providers.fake")
    chosen = page_ids if page_ids is not None else sorted(page_entries())
    return {
        page_url(page_id): fake.FakePage(
            content_type=page_entry(page_id)["content_type"], body=page_bytes(page_id)
        )
        for page_id in chosen
    }
