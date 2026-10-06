"""Full-page routes (spec 2.9 rule 12): the app shell's six areas.

Page routes are tagged ``PAGE_TAG`` and registered on the app itself by ``register_pages``,
so the security middleware can see them (ADR-039): they load without the launch token, and
the middleware marks their responses ``Cache-Control: no-store``. With ``HX-Request: true``
they return only the main-content fragment, which needs the token.

Until each area is built, its page is a placeholder with an empty state (ADR-029). Settings
has a page of its own (``app/api/settings.py``, M1.21), with a small empty state for its parts
still to come.
"""

from collections.abc import Callable
from dataclasses import dataclass

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

from app.api.security import PAGE_TAG
from app.api.templating import render

PLACEHOLDER_FRAGMENT = "fragments/shell/_placeholder.html"


@dataclass(frozen=True)
class Area:
    slug: str
    path: str
    name: str
    explanation: str
    pinned: bool = False
    coming: str = "This part of the app is coming in a later version."
    placeholder: bool = True  # False: the area registers its own page route


# Sidebar order from the detailed design; Settings is pinned at the bottom.
AREAS: tuple[Area, ...] = (
    Area(
        "today",
        "/",
        "Today",
        "Your day at a glance will appear here: follow-ups due, upcoming interviews and reminders.",
    ),
    Area(
        "inbox",
        "/inbox",
        "Job inbox",
        "New jobs to review will appear here, one at a time, so you can decide what to apply for.",
        # The Done step's "Add a job" lands here until intake arrives in M4 (ADR-064).
        coming="Adding jobs, by link or by pasting the ad, arrives in a later version.",
    ),
    Area(
        "board",
        "/board",
        "Board",
        "Your applications will appear here, arranged by where each one stands.",
    ),
    Area(
        "facts",
        "/facts",
        "Fact bank",
        "The facts you confirm about your experience will live here. Every CV and letter "
        "is written only from them.",
    ),
    Area(
        "companies",
        "/companies",
        "Companies",
        "Companies you are interested in will appear here, with your contacts, notes and "
        "history with each.",
    ),
    Area(
        "settings",
        "/settings",
        "Settings",
        "Settings for your job search, usage budgets, lists, prompts and data will appear here.",
        pinned=True,
        coming="They arrive in a later version.",
        placeholder=False,
    ),
)


def register_pages(app: FastAPI) -> None:
    """Add every page route directly to the app (not through an included router)."""
    for area in AREAS:
        if not area.placeholder:
            continue
        app.add_api_route(
            area.path,
            _placeholder_page(area),
            methods=["GET", "HEAD"],
            tags=[PAGE_TAG],
            response_class=HTMLResponse,
            name=f"page_{area.slug}",
        )


def _placeholder_page(area: Area) -> Callable[[Request], HTMLResponse]:
    def page(request: Request) -> HTMLResponse:
        return render(
            request,
            page=f"pages/{area.slug}.html",
            fragment=PLACEHOLDER_FRAGMENT,
            ctx={"area": area, "areas": AREAS},
        )

    return page


def area(slug: str) -> Area:
    """The area with this slug."""
    return next(item for item in AREAS if item.slug == slug)
