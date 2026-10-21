"""A company's contacts (M5.6): add and edit, both needing the launch token and the origin.

``POST /companies/{id}/contacts`` adds a contact; ``POST /companies/{id}/contacts/{contact id}``
edits one with its ``version``. 200 answers the ``#company-contacts`` block; 422 and 409 answer
only the submitting form (retargeted), keeping what was typed, with each error tied to its
field and the reload prompt for a stale version. An unknown company, a contact of another
company or an unsafe id is the fixed 404; a missing or non-numeric version is 400. The page
itself (``GET /companies/{id}``) is a page route and needs no token.
"""

import json
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, Response
from starlette.concurrency import run_in_threadpool

from app.api.companies import NOT_FOUND, detail_context
from app.api.errors import INVALID_REQUEST
from app.api.forms import posted_version, single_value_form
from app.api.templating import templates
from app.domain.contacts import CONTACT_FIELDS, ContactInvalid
from app.domain.errors import VersionConflict
from app.services.contacts import ContactsService

BLOCK = "fragments/contacts/_company_contacts.html"
FORM = "fragments/contacts/_form.html"
SAVED = "Contact saved."


def register_company_contact_routes(app: FastAPI) -> None:
    app.add_api_route("/companies/{company_id}/contacts", save_contact, methods=["POST"])
    app.add_api_route(
        "/companies/{company_id}/contacts/{contact_id}", save_contact, methods=["POST"]
    )


def _service(request: Request) -> ContactsService:
    service: ContactsService = request.app.state.contacts
    return service


def _not_found() -> Response:
    return PlainTextResponse(NOT_FOUND, status_code=404)


def _saved(request: Request, company_id: str) -> Response:
    ctx = detail_context(request, company_id)
    ctx["contact_message"] = SAVED
    response = templates.TemplateResponse(request, BLOCK, ctx)
    response.headers["HX-Trigger-After-Settle"] = json.dumps({"announce": SAVED})
    return response


def _refused(
    request: Request,
    company_id: str,
    contact_id: str,
    form: dict[str, str],
    *,
    errors: dict[str, str],
    conflict: bool,
) -> Response:
    ctx: dict[str, Any] = detail_context(request, company_id)
    stored: dict[str, Any] = next((c for c in ctx["contacts"] if c["id"] == contact_id), {})
    ctx.update(
        cf_scope="company",
        cf_key=contact_id,
        cf_item=stored,
        cf_typed={k: v.replace("\x00", "") for k, v in form.items()},
        cf_errors=errors,
        cf_conflict=conflict,
    )
    response = templates.TemplateResponse(request, FORM, ctx, status_code=409 if conflict else 422)
    response.headers["HX-Retarget"] = "#contact-form-" + (contact_id or "new")
    response.headers["HX-Reswap"] = "outerHTML"
    return response


async def save_contact(
    request: Request, company_id: str, contact_id: str | None = None
) -> Response:
    service = _service(request)
    if not await run_in_threadpool(service.company_exists, company_id):
        return _not_found()
    if contact_id is not None and not await run_in_threadpool(
        service.contact_exists, company_id, contact_id
    ):
        return _not_found()
    form = await single_value_form(request, {*CONTACT_FIELDS, *({"version"} if contact_id else ())})
    if isinstance(form, Response):
        return form
    version = posted_version(form, minimum=1) if contact_id else None
    if contact_id and version is None:
        return PlainTextResponse(INVALID_REQUEST, status_code=400)

    def save() -> Response:
        try:
            if contact_id is None:
                service.add(company_id, form)
            else:
                service.edit(company_id, contact_id, form, expected_version=version or 0)
            return _saved(request, company_id)
        except LookupError:
            return _not_found()
        except ContactInvalid as error:
            return _refused(
                request, company_id, contact_id or "", form, errors=error.errors, conflict=False
            )
        except VersionConflict:
            return _refused(request, company_id, contact_id or "", form, errors={}, conflict=True)

    return await run_in_threadpool(save)
