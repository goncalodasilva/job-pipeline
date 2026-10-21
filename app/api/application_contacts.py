"""Contacts and interactions in the application drawer and page (M5.6).

All routes need the launch token and the origin and answer the ``#application-detail`` block
(200) like the notes route, announcing the result. A refusal (422) answers only the submitting
form (retargeted to it), keeping what was typed and tying each error to its field; nothing is
changed. Only a contact of the application's company can be linked or logged (anything else is
a 422 on ``contact_id``); unlinking removes the link, never the contact. An unknown or unsafe
application id is the fixed 404; an unknown field is 400.
"""

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, Response
from starlette.concurrency import run_in_threadpool

from app.api.application_detail import answer, context
from app.api.forms import single_value_form
from app.api.templating import templates
from app.domain.contacts import CONTACT_FIELDS, INTERACTION_FIELDS, ContactInvalid
from app.domain.errors import ApplicationNotFound
from app.services.contacts import ContactsService

CONTACT_NOT_FOUND = "This contact wasn't found."
LINK_FORM = "fragments/applications/_contact_link_form.html"
NEW_FORM = "fragments/contacts/_form.html"
INTERACTION_FORM = "fragments/applications/_interaction_form.html"


def register_application_contact_routes(app: FastAPI) -> None:
    base = "/applications/{application_id}"
    app.add_api_route(f"{base}/contacts", link_contact, methods=["POST"])
    app.add_api_route(f"{base}/contacts/new", add_contact, methods=["POST"])
    app.add_api_route(f"{base}/contacts/{{contact_id}}/unlink", unlink_contact, methods=["POST"])
    app.add_api_route(f"{base}/interactions", log_interaction, methods=["POST"])


def _not_found() -> Response:
    return PlainTextResponse(ApplicationNotFound.message, status_code=404)


def _service(request: Request) -> ContactsService:
    service: ContactsService = request.app.state.contacts
    return service


def _refused(
    request: Request,
    aid: str,
    form: dict[str, str],
    *,
    template: str,
    form_id: str,
    errors: dict[str, str],
    extra: dict[str, Any] | None = None,
) -> Response:
    ctx = context(request, aid)
    ctx.update(extra or {})
    ctx.update(
        cf_typed={k: v.replace("\x00", "") for k, v in form.items()},
        cf_errors=errors,
        cf_conflict=False,
    )
    response = templates.TemplateResponse(request, template, ctx, 422)
    response.headers["HX-Retarget"] = f"#{form_id}"
    response.headers["HX-Reswap"] = "outerHTML"
    return response


def _run(request: Request, aid: str, form: dict[str, str], *, kind: str) -> Response:
    service = _service(request)
    try:
        if kind == "link":
            service.link(aid, form.get("contact_id", ""))
            return answer(request, aid, message="Contact linked.", focus="contacts")
        if kind == "new":
            service.add_and_link(aid, form)
            return answer(request, aid, message="Contact added and linked.", focus="contacts")
        service.log_interaction(aid, form)
        return answer(request, aid, message="Interaction logged.")
    except ApplicationNotFound:
        return _not_found()
    except ContactInvalid as error:
        if kind == "link":
            return _refused(
                request,
                aid,
                form,
                template=LINK_FORM,
                form_id="contact-link-form",
                errors=error.errors,
            )
        if kind == "new":
            return _refused(
                request,
                aid,
                form,
                template=NEW_FORM,
                form_id="application-contact-form",
                errors=error.errors,
                extra={"cf_scope": "application", "cf_key": ""},
            )
        return _refused(
            request,
            aid,
            form,
            template=INTERACTION_FORM,
            form_id="interaction-form",
            errors=error.errors,
        )


async def link_contact(request: Request, application_id: str) -> Response:
    form = await single_value_form(request, {"contact_id"})
    if isinstance(form, Response):
        return form
    return await run_in_threadpool(_run, request, application_id, form, kind="link")


async def add_contact(request: Request, application_id: str) -> Response:
    form = await single_value_form(request, set(CONTACT_FIELDS))
    if isinstance(form, Response):
        return form
    return await run_in_threadpool(_run, request, application_id, form, kind="new")


async def log_interaction(request: Request, application_id: str) -> Response:
    form = await single_value_form(request, set(INTERACTION_FIELDS))
    if isinstance(form, Response):
        return form
    return await run_in_threadpool(_run, request, application_id, form, kind="interaction")


async def unlink_contact(request: Request, application_id: str, contact_id: str) -> Response:
    form = await single_value_form(request, set())
    if isinstance(form, Response):
        return form

    def unlink() -> Response:
        try:
            if not _service(request).unlink(application_id, contact_id):
                return PlainTextResponse(CONTACT_NOT_FOUND, status_code=404)
            return answer(request, application_id, message="Contact unlinked.", focus="contacts")
        except ApplicationNotFound:
            return _not_found()

    return await run_in_threadpool(unlink)
