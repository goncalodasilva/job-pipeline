"""The command palette's two read-only fragments (M9.5 part A; ADR-098 item 7; spec 2.9 rules 2,
3 and 12).

- ``GET /palette/search?q=``: what the typed text finds, and the actions it names.
- ``GET /palette/recent?path=...``: the titles of the recent item pages the browser remembers.

Both need the launch token (neither is a page), write nothing, and take their input from the
query only. Nothing here logs the query: uvicorn's access records drop the query string whole
(``AccessQueryFilter``, ADR-093). A bad value is bounded or dropped, never refused with a 4xx:
the dialog swaps these answers in, and an error body would be swapped away.
"""

from fastapi import FastAPI, Request
from fastapi.responses import Response

from app.api.templating import templates
from app.services.palette import PaletteAnswer, PaletteService


def register_palette_routes(app: FastAPI) -> None:
    app.add_api_route("/palette/search", search, methods=["GET"])
    app.add_api_route("/palette/recent", recent, methods=["GET"])


def _answer(request: Request, answer: PaletteAnswer) -> Response:
    return templates.TemplateResponse(request, "fragments/palette/_answer.html", {"answer": answer})


def search(request: Request) -> Response:
    service: PaletteService = request.app.state.palette
    return _answer(request, service.search(request.query_params.get("q", "")))


def recent(request: Request) -> Response:
    service: PaletteService = request.app.state.palette
    return _answer(request, service.recent(request.query_params.getlist("path")))
