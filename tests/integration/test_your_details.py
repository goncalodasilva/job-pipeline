"""M6.6 Settings > Your details: name, email, phone, location and up to three links, as a versioned
setting with field-tied 422s and the 409 reload prompt; never sent to the AI; never echoed
(AC-M6.6 groundwork; ADR-106 item 3; ADR-110 item 7; spec 2.9 rules 2, 3 and 13; TD-280).

The routes, markup and messages are described in ``tests/details_pages.py``. In short:
``GET /settings/details`` (token) is the section; ``POST /settings/details`` saves the form
(``version``, ``name``, ``email``, ``phone``, ``location``, ``link_1`` to ``link_3``); the
token-free ``GET /settings`` page carries only a lazy placeholder, never a personal value. A
refused form keeps the typed values in its inputs but never in error text or logs (TD-280). A
real migrated SQLite
database in a temporary folder, the frozen clock, the fake provider, no network; every value is
fictional.
"""

import json
import logging
from typing import Any

import pytest

from app.domain.errors import VersionConflict
from tests.board_pages import elements
from tests.details_pages import (
    DETAILS,
    ENTERED_MARKERS,
    GOOD,
    INVALID,
    PHOTO,
    PHOTO_REMOVE,
    SETTINGS,
    Desk,
    accessible_name,
    announced,
    autofocus_in_section,
    details_app,
    details_version,
    disclosure,
    disclosure_is_open,
    done_text,
    element,
    errors_in,
    form_controls,
    has_section,
    invalid_names,
    section,
    shown_values,
)
from tests.image_support import jpeg_image
from tests.integration.test_app_shell import Element
from tests.leak_scan import RawLogRecords
from tests.tailoring_pages import tailoring_app
from tests.upload_bodies import CONTENT_TYPE, FieldPart, FilePart, multipart_body

pytestmark = pytest.mark.integration

FIELDS = ("name", "email", "phone", "location", "link_1", "link_2", "link_3")
LABELS = {
    "name": "Name",
    "email": "Email",
    "phone": "Phone",
    "location": "Location",
    "link_1": "Link 1",
    "link_2": "Link 2",
    "link_3": "Link 3",
}
SAVED = {
    "name": "Ines Sample",
    "email": "ines.sample@example.org",
    "phone": "555 010 0199",
    "location": "Portoville",
    "link_1": "https://example.com/in/ines-sample",
    "link_2": "",
    "link_3": "",
}


@pytest.fixture
def desk(tmp_path):
    with details_app(tmp_path) as made:
        yield made


def section_elements(html: str) -> list[Element]:
    box = section(html)
    return [e for e in elements(html) if e.within(box)]


def stored_fields(desk: Desk) -> dict[str, object]:
    value = desk.stored()
    assert value is not None
    return {k: v for k, v in value.items() if k != "photo"}


# --- the section, saving, versions -----------------------------------------------------------


def test_personal_details_saved_versioned_and_validated(desk: Desk) -> None:
    first = desk.show()
    assert first.status_code == 200
    assert first.headers["cache-control"] == "no-store"
    shown = first.text
    section(shown)
    assert details_version(shown) == "0" and shown_values(shown) == dict.fromkeys(FIELDS, "")
    assert autofocus_in_section(shown) == [], "loading the section must not take focus"
    controls = form_controls(shown)
    for name, label in LABELS.items():
        assert accessible_name(shown, controls[name]).startswith(label), name
    hint_ids = str(controls["name"].attrs.get("aria-describedby")).split()
    hints = [element(shown, hint_id) for hint_id in hint_ids]
    assert any(found is not None and "export" in found.text for found in hints), "name hint"
    assert desk.row() is None, "showing the section writes nothing"

    # Save: stored in the versioned setting, shown back, announced, one focus target.
    saved = desk.save(0)
    assert saved.status_code == 200, saved.text[:300]
    assert desk.stored() == {
        "name": GOOD["name"],
        "email": GOOD["email"],
        "phone": GOOD["phone"],
        "location": GOOD["location"],
        "links": [GOOD["link_1"], GOOD["link_2"]],
        "photo": None,
    }
    assert desk.version() == 1 and details_version(saved.text) == "1"
    assert shown_values(saved.text) == GOOD
    assert done_text(saved.text) == "Details saved." == announced(saved)
    assert len(autofocus_in_section(saved.text)) == 1
    assert desk.run.spy.requests == [], "saving contact details makes no AI request"

    # Edit: a blank clears a field (None), a blank link is dropped and the rest keep their order.
    again = desk.save(1, phone="  ", link_1="", link_2=GOOD["link_2"], link_3=SAVED["link_1"])
    assert again.status_code == 200 and desk.version() == 2
    assert stored_fields(desk)["phone"] is None
    assert stored_fields(desk)["links"] == [GOOD["link_2"], SAVED["link_1"]]
    assert shown_values(again.text)["link_1"] == GOOD["link_2"], "links shift up when one is blank"

    # Only the name is needed to export, but any field may stay empty.
    assert desk.save(2, name="Only Name", email="", phone="", location="", link_1="", link_2="",
                     link_3="").status_code == 200  # fmt: skip
    assert stored_fields(desk) == {
        "name": "Only Name", "email": None, "phone": None, "location": None, "links": [],
    }  # fmt: skip

    # Plain (no htmx) success: 303 back to the page.
    plain = desk.save(3, htmx=False, location="Elsewhere")
    assert plain.status_code == 303 and plain.headers["location"] == SETTINGS
    assert desk.version() == 4

    # A stale version is the reload prompt and changes nothing.
    before = desk.snapshot()
    stale = desk.save(2, name="Lost change")
    assert stale.status_code == 409 and VersionConflict.message in stale.text
    assert "Reload" in stale.text and f'href="{SETTINGS}"' in stale.text
    assert desk.snapshot() == before
    assert "Lost change" not in stale.text


REFUSED = {
    "name_newline": ("name", "Ana\nSample"),
    "name_tab": ("name", "Ana\tSample"),
    "name_nul": ("name", "Ana\x00Sample"),
    "name_bidi": ("name", "Ana‮Sample"),
    "name_too_long": ("name", "n" * 101),
    "location_newline": ("location", "Porto\nBcc: x"),
    "location_too_long": ("location", "l" * 101),
    "email_no_at": ("email", "not-an-email-marker"),
    "email_two_at": ("email", "a@@example.org"),
    "email_no_dot": ("email", "ana@example"),
    "email_space": ("email", "ana sample@example.org"),
    "email_header_injection": ("email", "ana@example.org\nBcc: other@example.org"),
    "email_too_long": ("email", "a" * 250 + "@b.co"),
    "phone_words": ("phone", "call me maybe"),
    "phone_short": ("phone", "12345"),
    "phone_long": ("phone", "1234567890123456"),
    "phone_markup": ("phone", "<b>5550100199</b>"),
    "link_javascript": ("link_1", "javascript:alert(1)"),
    "link_data": ("link_2", "data:text/html,<script>alert(1)</script>"),
    "link_file": ("link_3", "file:///etc/passwd"),
    "link_ftp": ("link_1", "ftp://example.org/cv.pdf"),
    "link_no_scheme": ("link_2", "example.org/portfolio"),
    "link_credentials": ("link_3", "https://user:secret@example.org/"),
    "link_space": ("link_1", "https://example.org/has space"),
    "link_too_long": ("link_2", "https://example.org/" + "a" * 290),
}


@pytest.mark.parametrize("case", sorted(REFUSED))
def test_personal_details_refused_value_is_tied_to_its_field(desk: Desk, case: str) -> None:
    field, value = REFUSED[case]
    assert desk.save(0, name="Saved Person", **{}).status_code == 200  # something is stored
    before = desk.snapshot()

    refused = desk.save(1, **{field: value})

    assert refused.status_code == 422, refused.status_code
    assert invalid_names(refused.text) == [field]
    message = errors_in(refused.text)[field]
    assert 0 < len(message) < 200 and value.strip()[:12] not in message
    assert announced(refused) == "Details not saved. 1 field needs a change."
    focused = autofocus_in_section(refused.text)
    assert len(focused) == 1 and focused[0].attrs.get("name") == field
    if "\x00" not in value:
        assert shown_values(refused.text)[field] == value, "what was typed stays in the input"
    assert desk.snapshot() == before, "nothing is saved"
    assert details_version(refused.text) == "1", "the section still carries the stored version"


def test_personal_details_several_refusals_are_all_tied_and_counted(desk: Desk) -> None:
    refused = desk.save(
        0, email="nope-marker", phone="call me", link_2="javascript:x", name="n" * 101
    )

    assert refused.status_code == 422
    assert set(errors_in(refused.text)) == {"name", "email", "phone", "link_2"}
    assert sorted(invalid_names(refused.text)) == ["email", "link_2", "name", "phone"]
    assert announced(refused) == "Details not saved. 4 fields need a change."
    ids = [e.attrs["id"] for e in section_elements(refused.text) if e.attrs.get("id")]
    assert len(ids) == len(set(ids)), "ids are unique"
    first_invalid = next(
        e for e in section_elements(refused.text) if e.attrs.get("aria-invalid") == "true"
    )
    assert autofocus_in_section(refused.text) == [first_invalid]
    assert desk.row() is None


def test_personal_details_unknown_repeated_and_unversioned_requests_are_400(desk: Desk) -> None:
    assert desk.save(0).status_code == 200
    before = desk.snapshot()
    good = {**GOOD, "version": "1"}
    bad = [
        {k: v for k, v in good.items() if k != "version"},
        {**good, "version": "abc"},
        {**good, "version": "-1"},
        {**good, "version": ["1", "1"]},
        {**good, "name": ["Ana", "Bea"]},
        {**good, "date_of_birth": "1990-01-01"},
        {**good, "nationality": "Fictionland"},
        {**good, "marital_status": "single"},
        {**good, "photo": "photo-evil-0001.png"},  # the reference is never a form field
        {**good, "photo_name": "photo-evil-0001.png"},
        {**good, "links": "https://example.org/x"},
    ]
    for data in bad:
        response = desk.post(DETAILS, data)
        assert response.status_code == 400 and response.text == INVALID, data
    assert desk.snapshot() == before


def test_personal_details_markup_is_stored_as_data_and_escaped(desk: Desk) -> None:
    name = "<script>alert(1)</script> & {{ 7 * 7 }}"
    location = '"><img src=x onerror=alert(1)>'

    saved = desk.save(0, name=name, location=location)

    assert saved.status_code == 200
    assert stored_fields(desk)["name"] == name and stored_fields(desk)["location"] == location
    for page in (saved.text, desk.show().text):
        assert "<script>alert(1)</script>" not in page and "<img src=x" not in page
        assert "&lt;script&gt;" in page
        assert shown_values(page)["name"] == name, "the browser gets the exact text back"
        assert "49" not in section(page).text


def details_tag(html: str) -> Element:
    """The section's ``<details>`` without the content checks (a reload prompt may omit forms)."""
    found = [e for e in section_elements(html) if e.tag == "details"]
    assert len(found) == 1, f"expected one <details> in the section, found {len(found)}"
    return found[0]


def open_in(response: Any) -> bool:
    return "open" in details_tag(response.text).attrs


def test_your_details_disclosure_is_closed_on_load_and_open_after_every_post(desk: Desk) -> None:
    loaded = desk.show().text
    assert not disclosure_is_open(loaded), "the initial lazy load renders it collapsed"
    summary = element(loaded, "details-summary")
    assert summary is not None and summary.text == "Show your details form"
    assert disclosure(loaded).tag == "details"  # form, photo block and heading are inside

    saved = desk.save(0)
    assert saved.status_code == 200 and open_in(saved) and disclosure_is_open(saved.text)

    refused = desk.save(1, email="nope-marker")
    assert refused.status_code == 422 and open_in(refused) and disclosure_is_open(refused.text)

    stale = desk.save(0, name="Lost change")
    assert stale.status_code == 409 and open_in(stale), "the reload prompt is visible"

    uploaded = desk.upload(jpeg_image(120, 150))
    assert uploaded.status_code == 200 and open_in(uploaded)
    bad_upload = desk.upload(b"GIF89a" + bytes(100))
    assert bad_upload.status_code == 422 and open_in(bad_upload)
    stale_upload = desk.upload(jpeg_image(120, 150), version=0)
    assert stale_upload.status_code == 409 and open_in(stale_upload)
    removed = desk.remove()
    assert removed.status_code == 200 and open_in(removed)
    stale_remove = desk.remove(1)
    assert stale_remove.status_code == 409 and open_in(stale_remove)

    # Only the initial load is collapsed: the next plain GET is closed again.
    assert not disclosure_is_open(desk.show().text)


# --- protection ------------------------------------------------------------------------------


def photo_body(desk: Desk) -> bytes:
    return multipart_body(
        FieldPart(str(desk.version()), name="version"),
        FilePart("me.jpg", jpeg_image(120, 150), content_type="image/jpeg"),
    )


def test_personal_details_routes_need_token_and_origin(desk: Desk) -> None:
    run = desk.run.run
    assert desk.save(0).status_code == 200
    before = desk.snapshot()
    token, origin = run.token, run.action()["origin"]
    posts = [
        (DETAILS, {"data": {**GOOD, "version": "1", "name": "Intruder"}}),
        (PHOTO_REMOVE, {"data": {"version": "1"}}),
        (PHOTO, {"content": photo_body(desk), "headers": {"content-type": CONTENT_TYPE}}),
    ]
    untrusted = {
        "no token, no origin": {},
        "origin only": {"origin": origin},
        "token only": {"X-JP-Token": token},
        "wrong token": {"X-JP-Token": "x" * len(token), "origin": origin},
        "another site's origin": {"X-JP-Token": token, "origin": "http://evil.example"},
        "origin null": {"X-JP-Token": token, "origin": "null"},
    }
    for path, kwargs in posts:
        for label, extra in untrusted.items():
            headers = {**kwargs.get("headers", {}), **extra, "HX-Request": "true"}
            response = desk.client.post(
                path, **{k: v for k, v in kwargs.items() if k != "headers"}, headers=headers
            )
            assert response.status_code == 403, (path, label)
            assert token not in response.text and "Intruder" not in response.text, (path, label)
    assert desk.snapshot() == before and desk.files() == [], "nothing reached the routes"

    # The section itself is a protected fragment: contact details never load without the token.
    for label, headers in {"none": {}, "wrong token": {"X-JP-Token": "x" * len(token)}}.items():
        response = desk.client.get(DETAILS, headers=headers)
        assert response.status_code == 403, label
        assert GOOD["name"] not in response.text and GOOD["email"] not in response.text, label
    assert desk.client.get(DETAILS, headers=run.htmx()).status_code == 200

    # With both, the same requests are accepted.
    assert desk.save(1, name="Trusted").status_code == 200


def test_settings_page_carries_no_personal_value_and_loads_the_section_protected(
    desk: Desk,
) -> None:
    assert desk.save(0).status_code == 200
    assert desk.upload(jpeg_image(120, 150)).status_code == 200
    stored_photo = desk.photo_name()
    assert stored_photo is not None

    for page in (
        desk.client.get(SETTINGS).text,  # the token-free page
        desk.client.get(SETTINGS, headers=desk.run.run.htmx()).text,  # its fragment
    ):
        assert has_section(page)
        placeholder = section(page)
        assert placeholder.attrs.get("hx-get") == DETAILS
        assert "load" in str(placeholder.attrs.get("hx-trigger"))
        for secret in (*ENTERED_MARKERS, stored_photo, "data:image", "photo-preview"):
            assert secret not in page, secret
        assert not [e for e in section_elements(page) if e.tag == "form"], "form comes protected"

    loaded = desk.show().text
    assert shown_values(loaded)["name"] == GOOD["name"]
    assert "hx-trigger" not in str(section(loaded).attrs), "the answer must not reload itself"


# --- TD-280: contact details are never echoed or logged ----------------------------------------


def error_texts(response: Any) -> list[str]:
    """Every piece of text that reports a problem: the described-by targets of invalid controls,
    role=alert elements, the done line, the announcement and the response headers."""
    html = response.text
    texts = list(errors_in(html).values())
    texts += [e.text for e in section_elements(html) if e.attrs.get("role") in ("alert", "status")]
    texts += [done_text(html) or "", announced(response), json.dumps(dict(response.headers))]
    return texts


def test_refused_details_never_echo_entered_values_in_the_response_or_logs(
    desk: Desk, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = RawLogRecords()
    raw.install(monkeypatch)
    logging.getLogger().setLevel(logging.DEBUG)
    assert desk.save(0, **SAVED).status_code == 200  # something else is stored
    before = desk.snapshot()
    hostile_name = "<script>alert(1)</script>-" + GOOD["name"]
    typed = {
        "name": hostile_name,
        "email": "bad-email-marker@@example.org",
        "link_2": "javascript:marker-link",
    }

    refused = desk.save(1, **typed)

    assert refused.status_code == 422
    # (a) the error messages and everything that reports a problem hold no entered value
    entered = (*ENTERED_MARKERS, "bad-email-marker", "marker-link", "alert(1)")
    assert set(errors_in(refused.text)) == {"email", "link_2"}
    for text in error_texts(refused):
        assert not [value for value in entered if value in text], text
    # (c) the typed values come back in the inputs, escaped; hostile markup stays data
    shown = shown_values(refused.text)
    assert {k: shown[k] for k in typed} == typed
    assert shown["phone"] == GOOD["phone"] and shown["location"] == GOOD["location"]
    assert "<script>alert(1)</script>" not in refused.text and "&lt;script&gt;" in refused.text
    # (d) no validation-library text
    for leak in ("ValidationError", "input_value", "pydantic", "Value error", "type=value_error"):
        assert leak not in refused.text and leak not in json.dumps(dict(refused.headers)), leak
    assert desk.snapshot() == before

    # (b) no log record holds a value, for the refusal or for an accepted save
    assert desk.save(1).status_code == 200
    every = (*entered, hostile_name)
    assert raw.sources, "the run produced log records"
    leaked = [where for where, entry in raw.entries if any(value in entry for value in every)]
    assert leaked == [], f"log records hold contact details: {leaked}"


def test_contact_details_are_stored_in_the_settings_row_only(desk: Desk) -> None:
    assert desk.save(0).status_code == 200
    assert desk.upload(jpeg_image(120, 150)).status_code == 200
    assert desk.save(2, location="Quillhaven, Fictionland").status_code == 200
    photo = desk.photo_name()
    assert photo is not None

    holding = [
        table
        for table, text in desk.tables_text().items()
        if any(marker in text for marker in ("Quillfeather", "zara.marker", "Quillhaven", photo))
    ]

    assert holding == ["settings"], f"contact details must live in the setting only, not {holding}"
    assert "Quillfeather" in desk.tables_text()["settings"]


def test_personal_details_never_reach_ai_requests(tmp_path) -> None:
    with tailoring_app(tmp_path) as tw:
        desk = Desk(tw.r.run)
        aid = tw.ready().id
        assert desk.save(0).status_code == 200
        assert desk.upload(jpeg_image(120, 150)).status_code == 200
        photo = desk.photo_name()
        assert photo is not None

        tw.check(aid)
        tw.propose(aid)

        requests = tw.spy.requests
        assert len(requests) >= 2, "the gap check and the angle proposals made requests"
        needles = (
            "Quillfeather", "zara.marker", "zara-marker", "0555", "Quillhaven", "example.org/in",
            photo, "data:image", "base64",
        )  # fmt: skip
        for request in requests:
            sent = " ".join(
                (request.system, request.user, json.dumps(request.output_schema, default=str))
            )
            assert [n for n in needles if n in sent] == [], request.task


# --- structure after review: scoped swaps, a real heading, form hints ---------------------------


def attr(html: str, element_id: str, name: str) -> str:
    found = element(html, element_id)
    assert found is not None, element_id
    return str(found.attrs.get(name) or "")


def test_photo_forms_swap_only_the_photo_block_and_the_details_form_swaps_the_section(
    desk: Desk,
) -> None:
    html = desk.show().text

    assert attr(html, "details-form", "hx-target") == "#details-settings"
    for form_id in ("photo-form",):
        assert attr(html, form_id, "hx-target") == "#photo-block"
        assert attr(html, form_id, "hx-select") == "#photo-block"
        assert attr(html, form_id, "hx-select-oob") == "#details-version,#details-contents"
    assert desk.upload(jpeg_image(120, 150)).status_code == 200
    with_photo = desk.show().text
    assert attr(with_photo, "photo-remove-form", "hx-target") == "#photo-block"
    assert attr(with_photo, "photo-remove-form", "hx-select") == "#photo-block"
    # The out-of-band spots exist in the answer, so the details form's version is refreshed.
    assert element(with_photo, "details-version") is not None
    assert element(with_photo, "details-contents") is not None
    assert details_version(with_photo) == "1"


def test_photo_outcomes_live_inside_the_photo_block_and_details_outcomes_do_not(
    desk: Desk,
) -> None:
    saved = desk.save(0)
    done = element(saved.text, "details-done")
    block = element(saved.text, "photo-block")
    assert done is not None and block is not None and not done.within(block)
    assert [e.attrs.get("id") for e in autofocus_in_section(saved.text)] == ["details-heading"]

    uploaded = desk.upload(jpeg_image(120, 150))
    done, block = element(uploaded.text, "details-done"), element(uploaded.text, "photo-block")
    assert done is not None and block is not None and done.within(block)
    assert [e.attrs.get("id") for e in autofocus_in_section(uploaded.text)] == ["details-done"]

    removed = desk.remove()
    assert done_text(removed.text) == "Photo removed."
    after = element(removed.text, "details-after")
    assert after is not None and after.text == "Upload one again any time."
    assert after.within(element(removed.text, "photo-block"))

    stale = desk.upload(jpeg_image(120, 150), version=0)
    assert stale.status_code == 409
    prompt, block = element(stale.text, "details-conflict"), element(stale.text, "photo-block")
    assert prompt is not None and block is not None and prompt.within(block)
    # A stale version never reaches the details form out of band.
    assert element(stale.text, "details-version") is None
    assert element(stale.text, "details-contents") is None


def test_details_heading_is_a_real_heading_above_the_disclosure_with_what_is_inside(
    desk: Desk,
) -> None:
    empty = desk.show().text
    heading = element(empty, "details-heading")
    assert heading is not None and heading.tag == "h2" and heading.text == "Your details"
    assert not heading.within(disclosure(empty)), "not inside the summary or the disclosure"
    contents = element(empty, "details-contents")
    assert contents is not None
    assert (
        "Name, email, phone, links and photo. A name is needed to export your CV." in contents.text
    )
    assert "Name: Not set yet." in contents.text and "Photo: Not set yet." in contents.text
    summary = element(empty, "details-summary")
    assert summary is not None and summary.text == "Show your details form"

    desk.save(0)
    desk.upload(jpeg_image(120, 150))
    saved = desk.show().text
    contents, summary = element(saved, "details-contents"), element(saved, "details-summary")
    assert contents is not None and summary is not None
    assert "Name: Saved." in contents.text and "Photo: Saved." in contents.text
    assert summary.text == "Edit your details"
    assert GOOD["name"] not in contents.text, "the status never repeats a value"


def test_details_inputs_have_the_right_keyboard_hints(desk: Desk) -> None:
    html = desk.show().text

    for name in ("email", "link_1", "link_2", "link_3"):
        control = form_controls(html)[name]
        assert control.attrs.get("autocapitalize") == "off", name
        assert control.attrs.get("spellcheck") == "false", name
    for name in ("link_1", "link_2", "link_3"):
        assert form_controls(html)[name].attrs.get("autocomplete") == "url", name
    assert form_controls(html)["email"].attrs.get("autocomplete") == "email"


def test_photo_input_is_described_by_its_help_and_by_its_error(desk: Desk) -> None:
    html = desk.show().text
    picker = form_controls(html, "photo-form")["file"]
    assert str(picker.attrs.get("aria-describedby")).split() == ["photo-help"]
    help_text = element(html, "photo-help")
    assert help_text is not None and "5 MB" in help_text.text

    refused = desk.upload(b"GIF89a" + bytes(100))
    picker = form_controls(refused.text, "photo-form")["file"]
    assert str(picker.attrs.get("aria-describedby")).split() == ["photo-help", "photo-file-error"]
    assert element(refused.text, "photo-file-error") is not None


def test_a_failed_write_is_announced_once(desk: Desk, monkeypatch: pytest.MonkeyPatch) -> None:
    import sqlite3

    from app.services.settings import SettingsService

    def fail(*args: Any, **kwargs: Any) -> None:
        raise sqlite3.OperationalError("disk full marker")

    monkeypatch.setattr(SettingsService, "save", fail)

    failed = desk.save(0)

    assert failed.status_code == 422 and announced(failed)
    problem = element(failed.text, "details-problem")
    assert problem is not None and problem.attrs.get("role") is None, "the header announces it"
    assert [e.attrs.get("id") for e in autofocus_in_section(failed.text)] == ["details-problem"]
