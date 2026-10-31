"""The Fact bank's Coverage tab (M7.7; AC-M7.5): the sorted table of requirement clusters and
the facts behind one Thin cluster.

The tab itself is ``GET /facts?tab=coverage`` (``app/api/facts.py``): a page, token-free and
read-only. These two are fragments, so each needs the launch token (spec 2.9) and neither
changes anything (rule 12):

``GET /facts/coverage`` answers ``#coverage``; the query field ``page`` picks the page of 50
(past the last is the last). An answer to a paging link moves focus to the count and announces
it. ``GET /facts/coverage/{cluster_id}`` answers ``#coverage-detail``: the related confirmed
facts, each with a link to its editor's "Refine with AI". A cluster id that is not of the
exact shape, or names no cluster now, gets one fixed 404 text that repeats nothing from the
request. Start session posts to the existing ``POST /sessions``. Rendering reads the database,
so these are plain functions that FastAPI runs in its thread pool.
"""

import json
from typing import Any, Final

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response

from app.api.pages import area
from app.api.templating import templates
from app.services.coverage_views import NOT_FOUND_TEXT, CoverageViews, coverage_page

COVERAGE_PATH: Final = "/facts/coverage"
COVERAGE_FRAGMENT: Final = "fragments/facts/_coverage.html"
DETAIL_FRAGMENT: Final = "fragments/facts/_coverage_detail.html"
# The paging links: an answer to one of them moves focus to the count (the link is gone).
PAGING_LINKS: Final = ("coverage-previous", "coverage-next")


def register_fact_coverage_routes(app: FastAPI) -> None:
    """Not pages: each needs the launch token.

    Route order (harmless): the fact editor's ``/facts/{id:path}/history`` is registered first, so
    ``/facts/coverage/history`` goes there, looks for a fact "coverage" and gets a 404; this
    route would answer the same 404 for "history", which is not a cluster id."""
    app.add_api_route(
        COVERAGE_PATH,
        coverage_fragment,
        methods=["GET"],
        response_class=HTMLResponse,
        name="facts_coverage",
    )
    app.add_api_route(
        COVERAGE_PATH + "/{cluster_id}",
        coverage_detail,
        methods=["GET"],
        response_class=HTMLResponse,
        name="facts_coverage_detail",
    )


def _views(request: Request) -> CoverageViews:
    views: CoverageViews = request.app.state.coverage_views
    return views


def coverage_context(request: Request, *, count_focus: bool = False) -> dict[str, Any]:
    """The ``coverage`` template value: the page asked for in the query. Blocking."""
    return {
        "view": _views(request).page(coverage_page(request.query_params)),
        "count_focus": count_focus,
    }


def coverage_fragment(request: Request) -> Response:
    """``#coverage`` alone. Blocking (the database)."""
    paging = request.headers.get("HX-Trigger") in PAGING_LINKS
    coverage = coverage_context(request, count_focus=paging)
    response = templates.TemplateResponse(
        request, COVERAGE_FRAGMENT, {"area": area("facts"), "coverage": coverage}
    )
    if paging:
        response.headers["HX-Trigger"] = json.dumps({"announce": coverage["view"].count_text})
    return response


def coverage_detail(request: Request, cluster_id: str) -> Response:
    """``#coverage-detail`` for one cluster, announced. Blocking (the database). A 404 is shown
    by app.js's banner for any failed htmx request; the slot keeps what it held."""
    detail = _views(request).detail(cluster_id)
    if detail is None:
        return PlainTextResponse(NOT_FOUND_TEXT, status_code=404)
    response = templates.TemplateResponse(request, DETAIL_FRAGMENT, {"detail": detail})
    # json.dumps escapes quotes, backslashes and non-ASCII, so a hostile label stays one string.
    response.headers["HX-Trigger"] = json.dumps(
        {"announce": f"Showing the facts behind {detail.label}."}
    )
    return response
