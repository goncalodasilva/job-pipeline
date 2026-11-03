"""The running app, routes, markup readers and fictional data for the M6.6 Your details and photo
tests (ADR-106 items 3 and 4; ADR-110 item 7; spec 2.9 rules 2, 3, 5 and 13; TD-280).

The application modules are not imported here (only test helpers are), so a missing service or
route fails the test that needs it. Every value is fictional (example.org, 555 numbers,
generated pictures).

Assumed interface (the implementer matches it; the integration and e2e tests share these names).

Service: ``app/services/personal_details.py``, ``PersonalDetailsService`` on
``app.state.personal_details`` (built like ``SettingsService``: workspace db, clock, ids, data
folder), over the versioned setting ``PERSONAL_DETAILS``. Photo files live in ``<data>/assets/``.

Privacy: ``GET /settings/your-details`` (the Your details and CV templates area, M9.2) is a
token-free page (spec 2.9 rule 13), so the full page carries NO
personal value and no photo. Its "Your details" block is a lazy placeholder
``<section id="details-settings" class="settings-section" aria-labelledby="details-heading"
hx-get="/settings/details" hx-trigger="load">`` (with ``<h2 id="details-heading">Your
details</h2>`` and a loading line) that htmx replaces with the protected fragment.

Routes (none is a page: all need the launch token; every POST also needs the origin):

- ``GET /settings/details`` (token; read only; same with or without ``HX-Request``): the
  ``#details-settings`` section (outer HTML) with the saved values. No ``data-autofocus``.
- ``POST /settings/details``: URL-encoded ``version`` (the setting's version as last seen, ``0``
  while nothing is stored), ``name``, ``email``, ``phone``, ``location``, ``link_1``,
  ``link_2``, ``link_3``. Each field appears at most once (a blank one is None; blank links are
  dropped, the others keep their order). The photo is not a field: saving keeps the stored
  photo. An unknown field (``date_of_birth``, ``nationality``...), a repeated field, a missing
  or non-numeric ``version`` is a plain-text 400 ``INVALID_REQUEST`` and changes nothing.
- ``POST /settings/details/photo``: multipart; the part ``file`` (the picture) and the field
  ``version``. The upload is read with the 5 MB cap in mind; the client's file name and
  declared type are never used.
- ``POST /settings/details/photo/remove``: URL-encoded ``version``.

Markup of the section (``GET /settings/details`` and every answer):

- ``<section id="details-settings" class="settings-section" aria-labelledby="details-heading">``
  holding ONE native disclosure ``<details>`` (lead decision: a collapsed disclosure is one tab
  stop instead of ten, so the Settings Tab count stays within ``MAX_TABS``). Its first child is
  ``<summary id="details-summary">`` with its own short text ("Show your details form", or "Edit
  your details" once a name is saved). The heading ``<h2 id="details-heading" tabindex="-1">Your
  details</h2>`` is OUTSIDE the ``<details>``, above it, so it stays a heading (it takes focus
  after a details answer). The form, the photo block and the messages are inside the
  ``<details>``, so they are in the DOM either way.
  The ``<details>`` has NO ``open`` attribute in the answer to the initial lazy
  ``GET /settings/details`` and HAS ``open`` in every answer to a POST in it (save, upload,
  remove, a 409 reload prompt, a 422), so a result or an error is visible and focus can move
  inside it. A later plain GET renders it closed again.
- ``<form id="details-form" method="post" action="/settings/details">`` with a hidden
  ``version`` and the text inputs ``name``, ``email``, ``phone``, ``location``, ``link_1``,
  ``link_2``, ``link_3`` (accessible names "Name", "Email", "Phone", "Location", "Link 1",
  "Link 2", "Link 3"; each prefilled with the SAVED value) and a submit button "Save details".
  The name's ``aria-describedby`` names a visible hint that says the name is needed to export.
- ``<div id="photo-block">``: with a photo, ``<img id="photo-preview" alt="..." src="data:image/
  jpeg;base64,..."> (or ``image/png``): the stored file inline, so the protected fragment
  shows it and no unprotected image URL exists; without one, ``<p id="photo-empty">`` and no
  ``#photo-preview``. ``<form id="photo-form" method="post" action="/settings/details/photo"
  enctype="multipart/form-data">`` with a hidden ``version``, ``<input type="file" name="file"
  accept="image/jpeg,image/png">`` (accessible name starts "Photo") and a submit button
  ("Upload photo", or "Replace photo" when one is stored). With a photo, also ``<form
  id="photo-remove-form" method="post" action="/settings/details/photo/remove">`` with a hidden
  ``version`` and a button "Remove photo".
- ``<p id="details-done">`` after a success, with the message below.

Answers (htmx; without ``HX-Request`` a success is a 303 to ``/settings/your-details``):

- 200: the whole section again with fresh versions, ``#details-done`` and ``HX-Trigger``
  ``{"announce": "<message>"}``; exactly one ``data-autofocus`` element in the section.
  Messages: "Details saved.", "Photo saved.", "Photo removed.".
- 422: the section again, keeping what the user typed in the input ``value`` attributes (escaped;
  hostile markup stays data; ui-htmx.md, lead decision). Each refused control is marked
  ``aria-invalid="true"`` with ``aria-describedby`` naming a visible error element (ids unique);
  exactly one ``data-autofocus`` sits on the first invalid control; the announcement is
  "Details not saved. 1 field needs a change." ("N fields need a change.") or, for the photo,
  "Photo not saved. 1 field needs a change.". Error texts (the ``aria-describedby`` targets,
  ``role="alert"`` elements, the announcement) are fixed copy per field and never include an
  entered value, a file name or pydantic's text (TD-280); nothing is logged with a value. Detail
  fields are tied by their name (a bad link to the ``link_<n>`` it came from); a photo refusal is
  tied to ``file``.
- 409: the reload prompt (``VersionConflict.message`` and a "Reload" link to
  ``/settings/your-details``); nothing changes.
"""

import base64
import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from httpx import Response

from tests.board_pages import by_id as _find
from tests.board_pages import elements, said_headers
from tests.cv_import_harness import Importing, importing
from tests.integration.test_app_shell import Element
from tests.settings_board_pages import accessible_name, controls_in, errors_in, invalid_names
from tests.upload_bodies import CONTENT_TYPE, FieldPart, FilePart, multipart_body

SETTINGS = "/settings/your-details"  # M9.2: the Your details area (was /settings)
DETAILS = "/settings/details"
PHOTO = "/settings/details/photo"
PHOTO_REMOVE = "/settings/details/photo/remove"
SECTION_ID = "details-settings"
DONE_ID = "details-done"
HTMX = {"HX-Request": "true"}
INVALID = "This request isn't valid."
KEY = "personal_details"

# Fictional contact details; the markers make a leak easy to find in a response or a log.
NAME = "Zara Quillfeather-Marker"
EMAIL = "zara.marker@example.org"
PHONE = "+44 20 7946 0555"
LOCATION = "Quillhaven, Fictionland"
LINKS = ("https://example.org/in/zara-marker", "https://example.net/zara-portfolio")
GOOD = {
    "name": NAME,
    "email": EMAIL,
    "phone": PHONE,
    "location": LOCATION,
    "link_1": LINKS[0],
    "link_2": LINKS[1],
    "link_3": "",
}
ENTERED_MARKERS = (NAME, EMAIL, PHONE, LOCATION, *LINKS, "zara-marker", "Quillfeather")


def by_id(html: str, element_id: str) -> Element:
    found = _find(html, element_id)
    assert found is not None, f"the answer has no #{element_id}"
    return found


def section(html: str) -> Element:
    found = by_id(html, SECTION_ID)
    assert found.tag == "section", "#details-settings is a <section>"
    heading = by_id(html, str(found.attrs.get("aria-labelledby")))
    assert heading.tag == "h2" and heading.text == "Your details" and heading.within(found)
    return found


def disclosure(html: str) -> Element:
    """The one ``<details>`` of the section: ``#details-summary`` is its first child, the
    heading sits above it (outside), and the form and the photo block are inside it."""
    box = section(html)
    found = [e for e in elements(html) if e.tag == "details" and e.within(box)]
    assert len(found) == 1, f"expected one <details> in the section, found {len(found)}"
    summary = by_id(html, "details-summary")
    assert summary.tag == "summary" and summary.parent is found[0], "summary is its first child"
    heading = by_id(html, "details-heading")
    assert not heading.within(found[0]) and not heading.within(summary), "a real heading, outside"
    for inner in ("details-form", "photo-block"):
        assert by_id(html, inner).within(found[0]), f"#{inner} is inside the disclosure"
    return found[0]


def disclosure_is_open(html: str) -> bool:
    return "open" in disclosure(html).attrs


def has_section(html: str) -> bool:
    return any(e.attrs.get("id") == SECTION_ID for e in elements(html))


def element(html: str, element_id: str) -> Element | None:
    found = [e for e in elements(html) if e.attrs.get("id") == element_id]
    assert len(found) <= 1, f"#{element_id} appears {len(found)} times"
    return found[0] if found else None


def details_form(html: str) -> Element:
    return by_id(html, "details-form")


def form_controls(html: str, form_id: str = "details-form") -> dict[str, Element]:
    return controls_in(html, by_id(html, form_id))


def shown_values(html: str) -> dict[str, str]:
    """What the details form shows in its text inputs."""
    controls = form_controls(html)
    return {
        name: str(control.attrs.get("value") or "")
        for name, control in controls.items()
        if name not in ("version",) and control.attrs.get("type") != "hidden"
    }


def details_version(html: str) -> str:
    """The version the details form carries; the photo forms carry the same one."""
    versions = {
        str(e.attrs["value"])
        for e in elements(html)
        if e.tag == "input"
        and e.attrs.get("type") == "hidden"
        and e.attrs.get("name") == "version"
        and e.within(section(html))
    }
    assert len(versions) == 1, f"expected one version in the section, found {sorted(versions)}"
    return versions.pop()


def autofocus_in_section(html: str) -> list[Element]:
    box = section(html)
    return [e for e in elements(html) if "data-autofocus" in e.attrs and e.within(box)]


def done_text(html: str) -> str | None:
    found = element(html, DONE_ID)
    return None if found is None else found.text


def announced(response: Response) -> str:
    """The ``announce`` text of the answer's HX-Trigger headers, or ""."""
    for name, value in response.headers.items():
        if name.lower().startswith("hx-trigger"):
            data = json.loads(value)
            if isinstance(data, dict) and "announce" in data:
                return str(data["announce"])
    return ""


def preview_bytes(html: str) -> tuple[str, bytes] | None:
    """(media type, bytes) of the inline photo preview, or None when there is none."""
    img = element(html, "photo-preview")
    if img is None:
        return None
    assert img.tag == "img" and (img.attrs.get("alt") or "").strip(), "the preview has an alt text"
    source = str(img.attrs.get("src"))
    match = re.fullmatch(r"data:(image/(?:jpeg|png));base64,([A-Za-z0-9+/=]+)", source)
    assert match is not None, "the preview is an inline data URI of a JPEG or PNG"
    return match.group(1), base64.b64decode(match.group(2))


def photo_name_in(response: Response) -> list[str]:
    return re.findall(r"photo-[A-Za-z0-9_-]+\.(?:jpg|png)", response.text)


class Desk:
    """The app with a finished setup (preferences saved, cv_country PT), a spy provider and the
    routes above."""

    def __init__(self, run: Importing) -> None:
        self.run = run

    @property
    def client(self) -> Any:
        return self.run.client

    @property
    def data_dir(self) -> Path:
        return self.run.data_dir

    @property
    def assets(self) -> Path:
        return self.data_dir / "assets"

    # --- requests -----------------------------------------------------------------------------

    def headers(self, htmx: bool = True, **extra: str) -> dict[str, str]:
        return self.run.run.action(**{**(HTMX if htmx else {}), **extra})

    def show(self) -> Response:
        return self.client.get(DETAILS, headers=self.run.run.htmx())

    def post(self, path: str, data: Any, *, htmx: bool = True) -> Response:
        return self.client.post(path, data=data, headers=self.headers(htmx))

    def save(self, version: int | str, htmx: bool = True, **fields: str) -> Response:
        body = {**GOOD, **fields, "version": str(version)}
        return self.post(DETAILS, body, htmx=htmx)

    def upload(
        self,
        data: bytes,
        *,
        version: int | str | None = None,
        filename: str = "me.jpg",
        content_type: str = "image/jpeg",
        htmx: bool = True,
    ) -> Response:
        current = self.version() if version is None else version
        parts: list[FilePart | FieldPart] = [FieldPart(str(current), name="version")]
        parts.append(FilePart(filename, data, content_type=content_type))
        return self.upload_body(multipart_body(*parts), htmx=htmx)

    def upload_body(self, body: bytes, *, htmx: bool = True) -> Response:
        headers = self.headers(htmx, **{"content-type": CONTENT_TYPE})
        return self.client.post(PHOTO, content=body, headers=headers)

    def remove(self, version: int | str | None = None, *, htmx: bool = True) -> Response:
        current = self.version() if version is None else version
        return self.post(PHOTO_REMOVE, {"version": str(current)}, htmx=htmx)

    # --- what is stored -----------------------------------------------------------------------

    def row(self) -> dict[str, Any] | None:
        found = self.run.rows("SELECT value, version FROM settings WHERE key = ?", (KEY,))
        return found[0] if found else None

    def stored(self) -> dict[str, Any] | None:
        row = self.row()
        return None if row is None else json.loads(row["value"])

    def version(self) -> int:
        row = self.row()
        return 0 if row is None else int(row["version"])

    def photo_name(self) -> str | None:
        stored = self.stored()
        photo = None if stored is None else stored.get("photo")
        return None if photo is None else str(photo["stored_name"])

    def files(self) -> list[Path]:
        """Every file under the data folder's ``assets/`` (none when it is missing)."""
        if not self.assets.exists():
            return []
        return sorted(
            path for path in self.assets.rglob("*") if path.is_file() or path.is_symlink()
        )

    def names(self) -> list[str]:
        return [path.name for path in self.files()]

    def snapshot(self) -> tuple[Any, list[str]]:
        """The setting row and the asset file names: what a refused request must leave as it is."""
        return self.row(), self.names()

    def tables_text(self) -> dict[str, str]:
        """Every table's rows as text, to find a value where it must not be."""
        names = [
            row["name"]
            for row in self.run.rows(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        return {name: repr(self.run.rows(f"SELECT * FROM {name}")) for name in names}  # noqa: S608


@contextmanager
def details_app(tmp_path: Path) -> Iterator[Desk]:
    """Each test file wraps this in its own ``desk`` fixture."""
    with importing(tmp_path) as run:
        yield Desk(run)


__all__ = [
    "DETAILS",
    "ENTERED_MARKERS",
    "GOOD",
    "INVALID",
    "PHOTO",
    "PHOTO_REMOVE",
    "SETTINGS",
    "Desk",
    "accessible_name",
    "announced",
    "autofocus_in_section",
    "details_app",
    "details_form",
    "details_version",
    "disclosure",
    "disclosure_is_open",
    "done_text",
    "element",
    "errors_in",
    "form_controls",
    "has_section",
    "invalid_names",
    "photo_name_in",
    "preview_bytes",
    "said_headers",
    "section",
    "shown_values",
]
