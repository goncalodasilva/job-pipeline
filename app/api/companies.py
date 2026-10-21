"""Companies (M4.10): the sortable list and one company's page, with versioned edits.

``GET /companies`` is a page route (``PAGE_TAG``): it loads without the launch token and, with
``HX-Request: true`` (token needed), answers the ``#companies`` block. Query: ``sort``
(``name``, ``open_roles``, ``to_apply`` or ``last_activity``), ``dir`` (``asc`` or ``desc``)
and ``page`` (50 companies a page); anything else is the default. ``focus=sort`` asks for focus
on the activated sort link and a spoken sort; ``focus=page`` for focus on the table and a
spoken "Page 2 of 3." (the page links ask for it).

``GET /companies/{id}`` is a page route too, by safe id (the block is ``#company-detail``).
``POST /companies/{id}/edit`` (token and origin) saves the posted fields with ``version`` and
answers the ``#company-edit`` block: 200 saved, 422 with errors tied to their fields, 409 with
the reload prompt. An unknown or unsafe id is always the fixed plain-text 404, never echoed.
"""

import json
from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from starlette.concurrency import run_in_threadpool

from app.api.errors import INVALID_REQUEST
from app.api.forms import posted_version, single_value_form
from app.api.pages import AREAS, area
from app.api.security import PAGE_TAG
from app.api.templating import render, renders_fragment, templates
from app.domain.companies import (
    COMPANY_FIELDS,
    SORTS,
    CompanyEditInvalid,
    other_direction,
    page_number,
    sort_choice,
    sort_message,
)
from app.domain.errors import VersionConflict
from app.services.companies import CompaniesService

NOT_FOUND = "This company wasn't found."
LIST_FRAGMENT = "fragments/companies/_list.html"
DETAIL_FRAGMENT = "fragments/companies/_detail.html"
EDIT_FRAGMENT = "fragments/companies/_edit.html"


def register_company_routes(app: FastAPI) -> None:
    app.add_api_route(
        "/companies",
        companies_page,
        methods=["GET", "HEAD"],
        tags=[PAGE_TAG],
        response_class=HTMLResponse,
        name="page_companies",
    )
    app.add_api_route(
        "/companies/{company_id}",
        company_page,
        methods=["GET", "HEAD"],
        tags=[PAGE_TAG],
        response_class=HTMLResponse,
        name="page_company",
    )
    app.add_api_route("/companies/{company_id}/edit", edit_company, methods=["POST"])


def _service(request: Request) -> CompaniesService:
    service: CompaniesService = request.app.state.companies
    return service


def _not_found() -> Response:
    return PlainTextResponse(NOT_FOUND, status_code=404)


def _address(sort: str, direction: str, page: int = 1) -> str:
    values = {"sort": sort, "dir": direction}
    if page > 1:
        values["page"] = str(page)
    return "/companies?" + urlencode(values)


def _list_context(request: Request) -> dict[str, Any]:
    query = request.query_params
    sort, direction = sort_choice(query.get("sort", ""), query.get("dir", ""))
    shown = _service(request).listing(sort, direction, page_number(query.get("page", "")))
    page, pages = shown["page"], shown["pages"]
    return {
        **shown,
        "area": area("companies"),
        "areas": AREAS,
        "sort": sort,
        "dir": direction,
        "focus_sort": query.get("focus") == "sort",
        "focus_page": query.get("focus") == "page",
        "sort_text": sort_message(sort, direction),
        "columns": tuple(
            {
                "key": key,
                "label": label,
                "active": key == sort,
                "aria_sort": ("ascending" if direction == "asc" else "descending")
                if key == sort
                else "none",
                "href": _address(
                    key, other_direction(direction) if key == sort else sort_choice(key, "")[1]
                ),
            }
            for key, label in SORTS.items()
        ),
        "previous_href": _address(sort, direction, page - 1) if page > 1 else "",
        "next_href": _address(sort, direction, page + 1) if page < pages else "",
    }


def companies_page(request: Request) -> Response:
    ctx = _list_context(request)
    response = render(request, page="pages/companies.html", fragment=LIST_FRAGMENT, ctx=ctx)
    if renders_fragment(request):
        said = (
            ctx["sort_text"]
            if ctx["focus_sort"]
            else f"Page {ctx['page']} of {ctx['pages']}."
            if ctx["focus_page"]
            else ""
        )
        if said:
            response.headers["HX-Trigger-After-Settle"] = json.dumps({"announce": said})
    return response


def _detail_context(request: Request, company_id: str) -> dict[str, Any]:
    return {
        **_service(request).view(company_id),
        "area": area("companies"),
        "areas": AREAS,
        "saved": False,
        "errors": {},
        "submitted": {},
        "conflict": False,
    }


def company_page(request: Request, company_id: str) -> Response:
    try:
        ctx = _detail_context(request, company_id)
    except LookupError:
        return _not_found()
    return render(request, page="pages/company.html", fragment=DETAIL_FRAGMENT, ctx=ctx)


async def edit_company(request: Request, company_id: str) -> Response:
    if not await run_in_threadpool(_service(request).exists, company_id):
        return _not_found()
    form = await single_value_form(request, {"version", *COMPANY_FIELDS})
    if isinstance(form, Response):
        return form
    version = posted_version(form, minimum=1)
    if version is None:
        return PlainTextResponse(INVALID_REQUEST, status_code=400)
    return await run_in_threadpool(_save, request, company_id, form, version)


def _save(request: Request, company_id: str, form: dict[str, str], version: int) -> Response:
    errors: dict[str, str] = {}
    conflict = saved = False
    try:
        _service(request).edit(company_id, form, expected_version=version)
        saved = True
    except CompanyEditInvalid as error:
        errors = error.errors
    except VersionConflict:
        conflict = True
    except LookupError:
        return _not_found()
    try:
        ctx = _detail_context(request, company_id)
    except LookupError:
        return _not_found()
    ctx.update(
        saved=saved,
        errors=errors,
        conflict=conflict,
        submitted={k: v.replace("\x00", "") for k, v in form.items()} if errors or conflict else {},
    )
    response = templates.TemplateResponse(
        request, EDIT_FRAGMENT, ctx, status_code=409 if conflict else 422 if errors else 200
    )
    if saved:
        response.headers["HX-Trigger-After-Settle"] = json.dumps({"announce": "Saved."})
    return response
