"""TD-342: the setup guide's job preferences step keeps stored values off its token-free page
(spec 2.9 rule 13; the M9.2 summary pattern, ``tests/integration/test_settings_areas.py``).

Contract the tests rely on (the route, the step template and the new fragment implement it)

- ``GET /setup/preferences`` (page route, no token needed; ``HX-Request`` makes it answer the guide
  fragment, as for every step) is a SHELL for the step: the heading ``<h2 id="step-heading">Job
  preferences</h2>`` (the only ``data-autofocus``, as before), the intro text, and in place of the
  form one placeholder ``<div id="preferences-form-slot" hx-get="/setup/preferences/form"
  hx-trigger="load" hx-swap="outerHTML">`` with a visible "Loading ..." line. The shell holds no
  ``form``, ``input``, ``textarea``, ``select`` or ``option`` and no stored value anywhere, in text
  or in an attribute (the ``hx-get`` has no query string).
- ``GET /setup/preferences/form`` (NOT a page route: the launch token is required, 403 without it,
  with or without ``HX-Request``; read only, GET writes nothing) answers the FORM only: the same
  ``<form class="step-form" method="post" action="/setup/preferences" hx-post="/setup/preferences"
  hx-target="#setup-guide" hx-swap="outerHTML">`` the step had, with every control filled from the
  stored preferences (or empty when none are stored), autoescaped. It is a fragment (no ``<html>``,
  no ``step-heading``, no ``hx-get``, no ``data-autofocus``, no ``HX-Trigger`` announcement and no
  ``HX-Push-Url``), so loading it neither moves focus nor says anything.
- Without a workspace (step 1 not finished) the page shows the "Finish step 1 first" notice itself
  (no placeholder), and the fragment answers 200 with the same notice (no form).
- The ANSWERS to ``POST /setup/preferences`` (refused submits) hold the form itself, filled from
  what was typed, never the placeholder: they already came with the token.

Real SQLite in a temp folder; the app through ``TestClient``; every value is fictional.
"""

from collections.abc import Sequence
from pathlib import Path

import pytest
from httpx import Response

from app.api.security import TOKEN_HEADER
from app.domain.settings import JobPreferences, Salary
from tests.data_pages import state_of
from tests.integration.test_settings_areas import token_only_in_body_attribute
from tests.integration.test_setup_preferences_done import (
    PREFERENCES_PATH,
    SAVED,
    assert_prefilled,
    checkboxes,
    control,
    controls,
    lines,
    only,
    post_preferences,
    required,
    running,
    selected_values,
    started,
    valid_form,
)
from tests.integration.test_setup_redirect import Element, Running, outline

pytestmark = pytest.mark.integration

FORM_PATH = "/setup/preferences/form"
SLOT_ID = "preferences-form-slot"
HTMX_ONLY = {"HX-Request": "true"}
SHELL_TAGS = ("form", "input", "textarea", "select", "option")

ROLE_MARKER = "Fictional Glacier Surveyor 5521"
LOCATION_MARKER = "Fictional-Fjordtown-4417"
MUST_MARKER = "fictional-must-have-6633"
BREAKER_MARKER = "fictional-deal-breaker-7745"
SALARY_MARKER = 91_377
PLANTED = JobPreferences(
    target_roles=(ROLE_MARKER,),
    locations=(LOCATION_MARKER,),
    work_modes=("remote",),
    minimum_salary=Salary(amount=SALARY_MARKER, currency="SEK"),
    must_haves=(MUST_MARKER,),
    deal_breakers=(BREAKER_MARKER,),
    cv_country="NO",
)
NEEDLES = (
    ROLE_MARKER,
    LOCATION_MARKER,
    MUST_MARKER,
    BREAKER_MARKER,
    str(SALARY_MARKER),
    f"{SALARY_MARKER:,}",
)
HOSTILE_ROLE = "</textarea><script>alert(1)</script> <b>Fictional</b> role"
HOSTILE_LOCATION = '"><img src=x onerror=alert(1)> & Fictional Quay'
HOSTILE = JobPreferences(
    target_roles=(HOSTILE_ROLE,),
    locations=(HOSTILE_LOCATION,),
    must_haves=("<i>Fictional</i> must-have",),
    deal_breakers=("</textarea><u>Fictional</u> breaker",),
    cv_country="PT",
)
ALL_STEPS = ("welcome", "provider", "web_search", "cv_import", "preferences")


def assert_no_needles(response: Response, label: str, needles: Sequence[str] = NEEDLES) -> None:
    for needle in needles:
        assert needle not in response.text, f"{label} holds {needle!r}"
    for name, value in response.headers.items():
        assert not any(n in value for n in needles), f"{label} header {name} holds a value"


def assert_shell(response: Response, label: str) -> None:
    """The token-free step: heading, intro, one loading placeholder, no control, no value."""
    assert response.status_code == 200, (label, response.status_code)
    elements = outline(response.text)
    heading = required(elements, "step-heading")
    assert heading.text == "Job preferences", label
    slot = required(elements, SLOT_ID)
    assert slot.attrs.get("hx-get") == FORM_PATH, (label, slot.attrs)
    assert "load" in str(slot.attrs.get("hx-trigger")), label
    assert slot.attrs.get("hx-swap") == "outerHTML", label
    assert "loading" in slot.text.lower(), f"{label}: a visible loading line, got {slot.text!r}"
    # The guide's own "Back to the app" form (setup finished, M1.20) is not the step's form.
    controls_held = [
        e for e in elements if e.tag in SHELL_TAGS and e.attrs.get("action") != "/setup/leave"
    ]
    assert not controls_held, f"{label}: the shell holds no form control"
    assert not [e for e in elements if "selected" in e.attrs or "checked" in e.attrs], label
    focus = [e for e in elements if "data-autofocus" in e.attrs]
    assert focus == [heading], f"{label}: only the heading takes focus, got {focus}"
    others = [e for e in elements if e.attrs.get("hx-get") == FORM_PATH and e is not slot]
    assert others == [], label


def get_form(run: Running, *, htmx: bool = False, path: str = FORM_PATH) -> Response:
    return run.client.get(path, headers=run.htmx() if htmx else run.with_token())


# --- the leak test (spec 2.9 rule 13) --------------------------------------------------------


@pytest.mark.parametrize(
    "done",
    [("welcome", "provider"), ("welcome", "provider", "preferences", "done")],
    ids=["at-the-step", "setup-finished"],
)
def test_td342_token_free_step_page_holds_no_stored_preference_value(
    tmp_path: Path, done: tuple[str, ...]
) -> None:
    """Plant distinctive values; the step's page, plain and htmx, with and without the token,
    holds none of them anywhere (body, attributes, headers) and is the shell."""
    with running(tmp_path, done=done, preferences=PLANTED) as run:
        data = tmp_path / "data"
        before = state_of(run, data)
        variants = {
            "plain": run.client.get(PREFERENCES_PATH),
            "plain with token": run.client.get(PREFERENCES_PATH, headers=run.with_token()),
            "htmx with token": run.client.get(PREFERENCES_PATH, headers=run.htmx()),
            "htmx without token": run.client.get(PREFERENCES_PATH, headers=HTMX_ONLY),
            "query string": run.client.get(
                PREFERENCES_PATH + f"?target_roles={ROLE_MARKER}&cv_country=NO"
            ),
        }
        for label, response in variants.items():
            assert_no_needles(response, label)
            # A full page carries the launch token only in <body hx-headers> (spec 2.9 rule 2, how
            # htmx sends it); the guide fragment holds none.
            if "<html" in response.text:
                assert token_only_in_body_attribute(response.text, run.token), label
            else:
                assert run.token not in response.text, label
        # The token-free variants answer 200 with the shell; the htmx one may also be refused.
        for label in ("plain", "plain with token", "htmx with token", "query string"):
            assert_shell(variants[label], label)
        if variants["htmx without token"].status_code == 200:
            assert_shell(variants["htmx without token"], "htmx without token")
        else:
            assert variants["htmx without token"].status_code == 403
        assert state_of(run, data) == before, "reading the step writes nothing"


def test_td342_no_token_free_setup_step_holds_a_stored_preference_value(tmp_path: Path) -> None:
    """Every step of the guide (and the Done list), as a token-free page, plain and htmx."""
    with running(tmp_path, done=ALL_STEPS, preferences=PLANTED) as run:
        steps = [step.id for step in run.app.state.setup_registry.steps]
        assert "preferences" in steps and "done" in steps, steps
        for step in steps:
            for label, headers in (("plain", {}), ("htmx", HTMX_ONLY)):
                response = run.client.get(f"/setup/{step}", headers=headers)
                assert response.status_code in (200, 403), (step, label)
                assert_no_needles(response, f"/setup/{step} ({label})")


def test_td342_form_fragment_needs_the_token(tmp_path: Path) -> None:
    with running(tmp_path, preferences=PLANTED) as run:
        data = tmp_path / "data"
        before = state_of(run, data)
        for label, headers in (
            ("no token", {}),
            ("htmx, no token", HTMX_ONLY),
            ("wrong token", {TOKEN_HEADER: "not-the-launch-token"}),
            ("htmx, wrong token", {**HTMX_ONLY, TOKEN_HEADER: "not-the-launch-token"}),
        ):
            refused = run.client.get(FORM_PATH, headers=headers)
            assert refused.status_code == 403, label
            assert_no_needles(refused, label)
            assert "step-form" not in refused.text, label
        assert state_of(run, data) == before


def test_td342_form_fragment_is_not_a_page_route(tmp_path: Path) -> None:
    """Only the step's page is token-free; the fragment route is a plain, protected GET."""
    from fastapi.routing import APIRoute

    from app.api.security import PAGE_TAG

    with running(tmp_path, preferences=PLANTED) as run:
        routes = [
            route
            for route in run.app.routes
            if isinstance(route, APIRoute) and route.path == FORM_PATH
        ]
        assert routes, f"no route for {FORM_PATH}"
        for route in routes:
            assert PAGE_TAG not in (route.tags or []), "the fragment must need the token"
            assert "GET" in route.methods
        # Only reads: no verb that changes data.
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            answer = run.client.request(method, FORM_PATH, headers=run.action())
            assert answer.status_code in (404, 405), method


def test_td342_form_fragment_holds_the_stored_values_with_the_token(tmp_path: Path) -> None:
    with running(tmp_path, preferences=PLANTED) as run:
        data = tmp_path / "data"
        before = state_of(run, data)
        for htmx in (False, True):
            response = get_form(run, htmx=htmx)
            label = f"htmx={htmx}"
            assert response.status_code == 200, (label, response.status_code)
            for marker in ("<html", "<body", 'id="announcer"', "step-heading"):
                assert marker not in response.text, (label, marker)
            assert run.token not in response.text, label
            elements = outline(response.text)
            assert lines(control(elements, "target_roles")) == [ROLE_MARKER], label
            assert lines(control(elements, "locations")) == [LOCATION_MARKER], label
            assert lines(control(elements, "must_haves")) == [MUST_MARKER], label
            assert lines(control(elements, "deal_breakers")) == [BREAKER_MARKER], label
            boxes = checkboxes(elements)
            assert {v for v, box in boxes.items() if "checked" in box.attrs} == {"remote"}, label
            assert control(elements, "salary_amount").attrs.get("value") == str(SALARY_MARKER)
            assert selected_values(control(elements, "salary_currency"), elements) == ["SEK"]
            assert selected_values(control(elements, "cv_country"), elements) == ["NO"]
            assert not [e for e in elements if (e.attrs.get("id") or "").endswith("-error")]
        assert state_of(run, data) == before, "reading the fragment writes nothing"


def test_td342_form_fragment_keeps_the_form_contract(tmp_path: Path) -> None:
    """The form is the one the step always had (it posts with htmx, targets the guide), and a
    fragment that loads on its own: no second heading, no reload, no focus, no announcement."""
    with running(tmp_path, preferences=SAVED) as run:
        for htmx in (False, True):
            response = get_form(run, htmx=htmx)
            assert response.status_code == 200
            elements = outline(response.text)
            form = only([e for e in elements if e.tag == "form"], "form")
            assert (form.attrs.get("method") or "").lower() == "post"
            assert form.attrs.get("action") == PREFERENCES_PATH
            assert form.attrs.get("hx-post") == PREFERENCES_PATH
            assert form.attrs.get("hx-target") == "#setup-guide"
            assert form.attrs.get("hx-swap") == "outerHTML"
            assert "step-form" in (form.attrs.get("class") or "").split()
            assert [e for e in elements if "hx-get" in e.attrs] == [], "it does not reload itself"
            assert [e for e in elements if "data-autofocus" in e.attrs] == [], "no focus moves"
            assert [e for e in elements if e.tag in ("h1", "h2")] == [], "the heading stays"
            assert "hx-trigger" not in response.headers, "loading announces nothing"
            assert "hx-push-url" not in response.headers
            assert "hx-redirect" not in response.headers
            assert_prefilled(response)


def test_td342_form_fragment_escapes_stored_text(tmp_path: Path) -> None:
    """Stored text is data: hostile markup in a preference stays text in the fragment."""
    with running(tmp_path, preferences=HOSTILE) as run:
        for htmx in (False, True):
            response = get_form(run, htmx=htmx)
            assert response.status_code == 200
            for raw in (
                "<script>alert(1)</script>",
                "<img src=x",
                "<b>Fictional</b>",
                "<i>Fictional</i>",
                "<u>Fictional</u>",
            ):
                assert raw not in response.text, f"{raw!r} must be escaped"
            elements = outline(response.text)
            assert lines(control(elements, "target_roles")) == [HOSTILE_ROLE]
            assert lines(control(elements, "locations")) == [HOSTILE_LOCATION]
            assert lines(control(elements, "must_haves")) == ["<i>Fictional</i> must-have"]
            assert lines(control(elements, "deal_breakers")) == [
                "</textarea><u>Fictional</u> breaker"
            ]
            assert not [e for e in elements if e.tag in ("script", "img", "u", "i", "b")]
            assert len(controls(elements, "target_roles")) == 1, "the textarea was not closed early"


def test_td342_form_fragment_ignores_the_query_string(tmp_path: Path) -> None:
    """Nothing typed in the address reaches the form; a read still writes nothing."""
    with running(tmp_path, preferences=SAVED) as run:
        data = tmp_path / "data"
        before = state_of(run, data)
        response = get_form(run, path=FORM_PATH + "?target_roles=Fictional+Spy&cv_country=DE")
        assert response.status_code == 200
        assert "Fictional Spy" not in response.text
        assert_prefilled(response)
        assert state_of(run, data) == before


def test_td342_empty_form_shows_when_nothing_is_stored(tmp_path: Path) -> None:
    with running(tmp_path) as run:
        data = tmp_path / "data"
        before = state_of(run, data)
        assert_shell(run.client.get(PREFERENCES_PATH), "page")
        for htmx in (False, True):
            response = get_form(run, htmx=htmx)
            assert response.status_code == 200
            elements = outline(response.text)
            for name in ("target_roles", "locations", "must_haves", "deal_breakers"):
                assert lines(control(elements, name)) == [], name
            assert not [b for b in checkboxes(elements).values() if "checked" in b.attrs]
            assert control(elements, "salary_amount").attrs.get("value") in (None, "")
            assert selected_values(control(elements, "salary_currency"), elements) in ([], [""])
            assert selected_values(control(elements, "cv_country"), elements) in ([], [""])
            assert not [e for e in elements if (e.attrs.get("id") or "").endswith("-error")]
            assert [e for e in elements if "data-autofocus" in e.attrs] == []
        assert state_of(run, data) == before


def test_td342_without_a_workspace_the_page_asks_for_step_1_and_the_fragment_agrees(
    tmp_path: Path,
) -> None:
    with started(tmp_path, source="default") as run:
        assert run.app.state.workspace is None
        page = run.client.get(PREFERENCES_PATH)
        assert page.status_code == 200
        elements = outline(page.text)
        assert (
            "step 1"
            in " ".join(
                e.text for e in elements if "notice" in (e.attrs.get("class") or "")
            ).lower()
        )
        assert by_slot(elements) is None, "nothing to load: no placeholder"
        assert not [e for e in elements if e.tag in SHELL_TAGS]

        fragment = get_form(run)
        assert fragment.status_code == 200
        assert "step 1" in fragment.text.lower()
        assert not [e for e in outline(fragment.text) if e.tag in SHELL_TAGS]
        assert run.app.state.workspace is None
        assert not (tmp_path / "data").exists(), "nothing is created"


def by_slot(elements: list[Element]) -> Element | None:
    return next((e for e in elements if e.attrs.get("id") == SLOT_ID), None)


# --- the form still works: answers to the save come with the form itself ----------------------


@pytest.mark.parametrize("htmx", [False, True], ids=["plain", "htmx"])
def test_td342_refused_submit_answers_with_the_form_not_the_placeholder(
    tmp_path: Path, htmx: bool
) -> None:
    """The answer to a refused save holds the typed values and the errors at once: it does not
    wait for a fragment (which would replace what was typed with what is stored)."""
    with running(tmp_path, preferences=PLANTED) as run:
        response = post_preferences(
            run, valid_form(target_roles="  \n", locations="Typed Fictional Quay"), htmx=htmx
        )
        assert response.status_code == 422
        elements = outline(response.text)
        assert by_slot(elements) is None
        assert [e for e in elements if e.attrs.get("hx-get") == FORM_PATH] == []
        assert lines(control(elements, "locations")) == ["Typed Fictional Quay"]
        required(elements, "target_roles-error")
        assert LOCATION_MARKER not in response.text, "what was typed, not what is stored"
        # Nothing was saved by the refusal.
        assert get_form(run).text.count(ROLE_MARKER) == 1


def test_td342_saving_still_moves_on_and_the_fragment_then_shows_the_new_values(
    tmp_path: Path,
) -> None:
    with running(tmp_path, preferences=PLANTED) as run:
        saved = post_preferences(run, valid_form(), htmx=True)
        assert saved.status_code == 200
        assert saved.headers.get("hx-push-url") == "/setup/done"
        assert ROLE_MARKER not in saved.text
        shown = get_form(run)
        assert ROLE_MARKER not in shown.text
        assert_prefilled(shown)
        assert_no_needles(run.client.get(PREFERENCES_PATH), "the page after saving")
