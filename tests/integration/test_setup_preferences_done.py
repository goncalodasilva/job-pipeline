"""Step 5 (job preferences) and step 6 (Done) of the setup guide (M1.19; AC-M1.6, AC-M1.7).

Step 5 (``POST /setup/preferences``) takes target roles, locations, must-haves and deal-breakers
(textareas, one item per line), work modes (checkboxes ``remote``, ``hybrid``, ``on_site``), a
minimum salary (whole number, thousands separators allowed) with a currency from a static ISO
4217 list, and the CV country from a static ISO 3166-1 list (``app/domain/locales.py``). A
valid submit saves ``JobPreferences`` (``JOB_PREFERENCES``), marks the step done, refreshes the
cached decision and moves on to Done (303; htmx: the guide fragment, ``HX-Push-Url`` and the
"Step 4 of 4: Done" announcement). A refused submit answers 422 with each error in
``<p id="<field>-error" class="field-error">``, tied to its control(s) with
``aria-describedby`` and marked ``aria-invalid="true"``; the first invalid field gets
``data-autofocus``, the title starts with "Error: ", the submitted values are shown back and
nothing is saved. Revisiting pre-fills every field from the stored preferences.

Step 6 (``/setup/done``) lists every registered step except Done, in order, each with its title,
its state in text ("Done", "Skipped" or "Not done yet") and a link to ``/setup/<id>``; it shows
no personal details. Its form posts ``then=add_job`` ("Add a job", the primary action, to
``/inbox``) or ``then=today`` ("Go to Today", to ``/``); anything else goes to ``/``. Finishing
marks the Done step done and refreshes the decision; it is refused (422, a form error naming and
linking the unfinished required steps) while a required step is not done. ``create_app`` takes
``setup_registry`` so a test can register an extra optional step (AC-M1.7, skipped steps).

Every folder lives under ``tmp_path``; every value is fictional. Providers are the fake (the
harness saves it; ``JOBPIPELINE_ALLOW_FAKE_PROVIDERS=1`` comes from ``tests/conftest.py``).
"""

import json
import re
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from httpx import Response

from app import __version__
from app.config import RuntimeConfig, write_bootstrap
from app.domain.locales import COUNTRIES, CURRENCIES
from app.domain.settings import JOB_PREFERENCES, JobPreferences, Salary
from app.main import create_app
from app.modules.storage import Database
from app.onboarding.progress import SetupProgress
from app.onboarding.registry import (
    DONE,
    PREFERENCES,
    PROVIDER,
    WELCOME,
    StepDefinition,
    StepRegistry,
)
from app.services.settings import SettingsService
from app.services.setup_state import SetupStateService
from tests.fakes.clock import FrozenClock
from tests.integration.test_setup_redirect import (
    BASE_URL,
    PORT,
    START,
    Element,
    Running,
    assert_not_redirected,
    assert_see_other,
    outline,
)
from tests.setup_harness import HARNESS_PROVIDER, seed_workspace

pytestmark = pytest.mark.integration

PREFERENCES_PATH = "/setup/preferences"
DONE_PATH = "/setup/done"
DONE_ANNOUNCEMENT = "Step 4 of 4: Done"
LIST_FIELDS = ("target_roles", "locations", "must_haves", "deal_breakers")
SAMPLE_OPTIONAL = StepDefinition(
    "sample_optional", "Sample optional step", False, 45, "fragments/setup/_done.html"
)
PYDANTIC_MARKERS = (
    "pydantic",
    "validation error",
    "string should",
    "input should",
    "value error",
    "value_error",
    "errors.pydantic.dev",
    "tuple should",
)
FICTIONAL_ROLE = "Fictional Lighthouse Keeper"
SAVED = JobPreferences(
    target_roles=(FICTIONAL_ROLE, "Fictional Archivist"),
    locations=("Fictionville", "Remote"),
    work_modes=("remote", "on_site"),
    minimum_salary=Salary(amount=45000, currency="EUR"),
    must_haves=("Four-day week",),
    deal_breakers=("Night shifts", "Unpaid trials"),
    cv_country="PT",
)


# --- helpers -------------------------------------------------------------------------------


def make_config(tmp_path: Path, source: str = "bootstrap") -> RuntimeConfig:
    return RuntimeConfig(
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "cfg",
        port=PORT,
        log_level="INFO",
        demo=False,
        setup=False,
        no_browser=False,
        data_dir_source=source,  # type: ignore[arg-type]
    )


def seed(
    tmp_path: Path,
    *,
    done: Sequence[str] = ("welcome", "provider"),
    skipped: Sequence[str] = (),
    preferences: JobPreferences | None = None,
    registry: StepRegistry | None = None,
) -> None:
    """``config.toml`` naming ``tmp_path/data``, whose database has the fake provider saved
    and ``done`` steps done (by default the guide is at the preferences step)."""
    write_bootstrap(tmp_path / "cfg", data_dir=tmp_path / "data", version=__version__)
    seed_workspace(
        tmp_path / "data",
        done=done,
        skipped=skipped,
        clock=FrozenClock(START),
        provider=HARNESS_PROVIDER,
        preferences=preferences,
        registry=registry,
    )


@contextmanager
def started(
    tmp_path: Path, *, registry: StepRegistry | None = None, source: str = "bootstrap"
) -> Iterator[Running]:
    """The app on the seeded folders (nothing is written here). ``registry`` is passed to
    ``create_app(setup_registry=...)`` only when given."""
    extra = {} if registry is None else {"setup_registry": registry}
    app = create_app(make_config(tmp_path, source), clock=FrozenClock(START), **extra)
    with TestClient(app, base_url=BASE_URL, follow_redirects=False) as client:
        yield Running(app=app, client=client, hits=Counter())


@contextmanager
def running(tmp_path: Path, **seeding: object) -> Iterator[Running]:
    seed(tmp_path, **seeding)  # type: ignore[arg-type]
    with started(tmp_path, registry=seeding.get("registry")) as run:  # type: ignore[arg-type]
        yield run


def valid_form(**overrides: str | list[str]) -> dict[str, str | list[str]]:
    form: dict[str, str | list[str]] = {
        "target_roles": f"{FICTIONAL_ROLE}\nFictional Archivist",
        "locations": "Fictionville\nRemote",
        "work_modes": ["remote", "on_site"],
        "salary_amount": "45,000",
        "salary_currency": "EUR",
        "must_haves": "Four-day week",
        "deal_breakers": "Night shifts\nUnpaid trials",
        "cv_country": "PT",
    }
    form.update(overrides)
    return form


def post_preferences(
    run: Running, form: Mapping[str, str | list[str]], *, htmx: bool = False
) -> Response:
    headers = run.action(**({"HX-Request": "true"} if htmx else {}))
    return run.client.post(PREFERENCES_PATH, data=dict(form), headers=headers)


def finish(run: Running, then: str | None, *, htmx: bool = False) -> Response:
    headers = run.action(**({"HX-Request": "true"} if htmx else {}))
    data = {} if then is None else {"then": then}
    return run.client.post(DONE_PATH, data=data, headers=headers)


def get_ok(run: Running, path: str) -> Response:
    response = run.client.get(path)
    assert response.status_code == 200, (path, response.status_code, response.text[:300])
    return response


def stored(run: Running) -> JobPreferences | None:
    db: Database = run.app.state.workspace.db
    return SettingsService(db, FrozenClock(START)).load(JOB_PREFERENCES).value


def live_statuses(run: Running) -> dict[str, str]:
    db: Database = run.app.state.workspace.db
    progress = SetupProgress(
        run.app.state.setup_registry, SetupStateService(db, FrozenClock(START))
    )
    return dict(progress.statuses())


def only(elements: list[Element], description: str) -> Element:
    assert len(elements) == 1, f"expected one {description}, found {len(elements)}"
    return elements[0]


def by_id(elements: list[Element], element_id: str) -> Element | None:
    return next((e for e in elements if e.attrs.get("id") == element_id), None)


def required(elements: list[Element], element_id: str) -> Element:
    element = by_id(elements, element_id)
    assert element is not None, f"#{element_id} is missing"
    return element


def controls(elements: list[Element], name: str) -> list[Element]:
    return [
        e
        for e in elements
        if e.tag in ("input", "textarea", "select") and e.attrs.get("name") == name
    ]


def control(elements: list[Element], name: str) -> Element:
    return only(controls(elements, name), f"control named {name}")


def checkboxes(elements: list[Element]) -> dict[str, Element]:
    found = {
        str(e.attrs.get("value")): e
        for e in controls(elements, "work_modes")
        if e.attrs.get("type") == "checkbox"
    }
    assert set(found) == {"remote", "hybrid", "on_site"}, sorted(found)
    return found


def raw_text(element: Element) -> str:
    return "".join(element.parts)


def lines(element: Element) -> list[str]:
    return [line.strip() for line in raw_text(element).splitlines() if line.strip()]


def options(select: Element, elements: list[Element]) -> list[Element]:
    return [e for e in elements if e.tag == "option" and e.within(select)]


def selected_values(select: Element, elements: list[Element]) -> list[str]:
    return [str(e.attrs.get("value")) for e in options(select, elements) if "selected" in e.attrs]


def describes(element: Element) -> list[str]:
    return (element.attrs.get("aria-describedby") or "").split()


def title_of(response: Response) -> str:
    return only([e for e in outline(response.text) if e.tag == "title"], "<title>").text


def nearest(element: Element, tag: str) -> Element | None:
    node = element.parent
    while node is not None and node.tag != tag:
        node = node.parent
    return node


def assert_plain(message: str) -> None:
    assert message
    for marker in PYDANTIC_MARKERS:
        assert marker not in message.lower(), message


def assert_field_error(response: Response, field: str) -> None:
    """422; ``#<field>-error`` is a visible plain message tied to every control of the field,
    which is marked invalid; the field's first control has the only ``data-autofocus``."""
    assert response.status_code == 422, (response.status_code, response.text[:300])
    elements = outline(response.text)
    error = required(elements, f"{field}-error")
    assert error.tag == "p"
    assert "field-error" in (error.attrs.get("class") or "").split()
    assert_plain(error.text)
    fields = controls(elements, field)
    assert fields, f"no control named {field}"
    for item in fields:
        assert f"{field}-error" in describes(item), (field, item.attrs)
    if field == "work_modes":
        group = nearest(fields[0], "fieldset")
        assert group is not None, "the checkboxes are grouped in a fieldset"
        assert group.attrs.get("aria-invalid") == "true" or all(
            item.attrs.get("aria-invalid") == "true" for item in fields
        ), "the group or every checkbox is marked invalid"
    else:
        for item in fields:
            assert item.attrs.get("aria-invalid") == "true", (field, item.attrs)
    focused = [e for e in elements if "data-autofocus" in e.attrs]
    assert len(focused) == 1 and focused[0] is fields[0], [
        (e.tag, e.attrs.get("name"), e.attrs.get("id")) for e in focused
    ]
    assert title_of(response).startswith("Error: Job preferences"), title_of(response)


def assert_nothing_saved(run: Running) -> None:
    assert stored(run) is None
    assert live_statuses(run).get("preferences") != "done"
    assert run.decision.kind == "guide"
    assert run.decision.step == "preferences"


def summary(response: Response) -> list[tuple[str, str]]:
    """The Done step's list: ``(href, row text)`` for every ``/setup/<id>`` link in the step
    section, in page order; each link sits in a list item."""
    elements = outline(response.text)
    section = required(elements, "setup-step")
    rows: list[tuple[str, str]] = []
    for link in elements:
        href = link.attrs.get("href") or ""
        if link.tag == "a" and link.within(section) and href.startswith("/setup/"):
            item = nearest(link, "li")
            assert item is not None, f"the link to {href} is in a list item"
            rows.append((href, item.text))
    return rows


def status_word(text: str) -> str:
    """The one state word in a row: "Not done yet", "Skipped" or "Done"."""
    if "Not done yet" in text:
        rest = text.replace("Not done yet", "")
        assert "Skipped" not in rest and "Done" not in rest, text
        return "Not done yet"
    found = [word for word in ("Skipped", "Done") if word in text]
    assert len(found) == 1, (text, found)
    return found[0]


def step_list_item(response: Response, title: str) -> Element:
    elements = outline(response.text)
    step_list = only(
        [e for e in elements if e.tag == "ol" and "setup-steps" in (e.attrs.get("class") or "")],
        "step list",
    )
    return only(
        [e for e in elements if e.tag == "li" and e.within(step_list) and e.text.startswith(title)],
        f"step list item {title}",
    )


def forced_colors_blocks(css: str) -> str:
    """The bodies of every ``@media (forced-colors: active)`` block, joined."""
    bodies: list[str] = []
    for match in re.finditer(r"@media\s*\(\s*forced-colors\s*:\s*active\s*\)\s*\{", css):
        depth, index = 1, match.end()
        while depth and index < len(css):
            depth += {"{": 1, "}": -1}.get(css[index], 0)
            index += 1
        bodies.append(css[match.end() : index - 1])
    return "\n".join(bodies)


# --- step 5: saving --------------------------------------------------------------------------


def test_ac_m1_6_preferences_saved_when_valid(tmp_path: Path) -> None:
    with running(tmp_path) as run:
        assert_see_other(run.client.get("/"), PREFERENCES_PATH)

        response = post_preferences(run, valid_form())

        assert_see_other(response, DONE_PATH)
        assert stored(run) == SAVED
        assert live_statuses(run)["preferences"] == "done"
        assert run.decision.kind == "guide"
        assert run.decision.step == "done", "the cached decision was refreshed"
        assert_see_other(run.client.get("/"), DONE_PATH)
        # The step list shows the step as done, in text.
        item = step_list_item(get_ok(run, DONE_PATH), "Job preferences")
        assert "Done" in item.text


def test_preferences_htmx_moves_to_done_with_push_url_and_announcement(tmp_path: Path) -> None:
    with running(tmp_path) as run:
        response = post_preferences(run, valid_form(), htmx=True)
        assert stored(run) == SAVED

    assert response.status_code == 200, response.text[:300]
    assert "location" not in response.headers
    assert response.headers.get("hx-push-url") == DONE_PATH
    assert json.loads(response.headers["hx-trigger"]) == {"announce": DONE_ANNOUNCEMENT}
    for marker in ("<html", "<body", 'id="announcer"'):
        assert marker not in response.text, marker
    assert title_of(response).startswith("Done"), title_of(response)
    elements = outline(response.text)
    current = only([e for e in elements if e.attrs.get("aria-current") == "step"], "current")
    assert current.text.startswith("Done")
    assert "Done" in step_list_item(response, "Job preferences").text


def test_preferences_form_controls_labelled_and_selects_list_names(tmp_path: Path) -> None:
    with running(tmp_path) as run:
        response = get_ok(run, PREFERENCES_PATH)
    elements = outline(response.text)
    heading = required(elements, "step-heading")
    assert heading.text == PREFERENCES.title

    form = only(
        [
            e
            for e in elements
            if e.tag == "form"
            and PREFERENCES_PATH in (e.attrs.get("action"), e.attrs.get("hx-post"))
        ],
        "preferences form",
    )
    assert (form.attrs.get("method") or "").lower() == "post"
    for name in (*LIST_FIELDS, "salary_amount", "salary_currency", "cv_country"):
        field = control(elements, name)
        assert field.within(form), name
        field_id = field.attrs.get("id")
        assert field_id, f"{name} has an id"
        only(
            [e for e in elements if e.tag == "label" and e.attrs.get("for") == field_id],
            f"label for {name}",
        )
    for name in LIST_FIELDS:
        assert control(elements, name).tag == "textarea", name
    for value, box in checkboxes(elements).items():
        assert box.within(form)
        assert box.attrs.get("id"), value
        only(
            [e for e in elements if e.tag == "label" and e.attrs.get("for") == box.attrs["id"]],
            f"label for work mode {value}",
        )
    group = nearest(checkboxes(elements)["remote"], "fieldset")
    assert group is not None
    only([e for e in elements if e.tag == "legend" and e.within(group)], "work modes legend")

    for name, table in (("cv_country", COUNTRIES), ("salary_currency", CURRENCIES)):
        select = control(elements, name)
        assert select.tag == "select", name
        listed = options(select, elements)
        assert listed[0].attrs.get("value") == "", "an empty first option"
        assert listed[0].text.startswith("Choose"), listed[0].text
        assert [str(o.attrs.get("value")) for o in listed[1:]] == [code for code, _ in table]
        names = dict(table)
        for option in listed[1:]:
            assert names[str(option.attrs.get("value"))] in option.text, option.text
        assert selected_values(select, elements) in ([], [""]), "nothing chosen yet"
    portugal = next(
        o for o in options(control(elements, "cv_country"), elements) if o.attrs["value"] == "PT"
    )
    assert "Portugal" in portugal.text


# --- step 5: refused submits -----------------------------------------------------------------


ERROR_CASES = [
    pytest.param("target_roles", {"target_roles": "  \n \n"}, id="target_roles-empty"),
    pytest.param(
        "target_roles",
        {"target_roles": "\n".join(f"Fictional role {i}" for i in range(21))},
        id="target_roles-21",
    ),
    pytest.param("locations", {"locations": "Fictionville\n" + "L" * 201}, id="locations-201"),
    pytest.param("locations", {"locations": "Fiction\tville"}, id="locations-tab"),
    pytest.param("work_modes", {"work_modes": ["remote", "teleport"]}, id="work_modes-unknown"),
    pytest.param("salary_amount", {"salary_amount": "lots"}, id="salary_amount-text"),
    pytest.param("salary_amount", {"salary_amount": "4.5"}, id="salary_amount-decimal"),
    pytest.param("salary_amount", {"salary_amount": "1000000001"}, id="salary_amount-range"),
    pytest.param("salary_currency", {"salary_currency": ""}, id="salary_currency-missing"),
    pytest.param("salary_currency", {"salary_currency": "ZZZ"}, id="salary_currency-unknown"),
    pytest.param("must_haves", {"must_haves": "Bell\x07ringer"}, id="must_haves-control"),
    pytest.param(
        "deal_breakers",
        {"deal_breakers": "\n".join(f"Fictional no {i}" for i in range(21))},
        id="deal_breakers-21",
    ),
    pytest.param("cv_country", {"cv_country": ""}, id="cv_country-missing"),
    pytest.param("cv_country", {"cv_country": "ZZ"}, id="cv_country-unassigned"),
]


@pytest.mark.parametrize(("field", "overrides"), ERROR_CASES)
def test_preferences_errors_tied_to_fields(
    tmp_path: Path, field: str, overrides: dict[str, str | list[str]]
) -> None:
    with running(tmp_path) as run:
        page = post_preferences(run, valid_form(**overrides))
        fragment = post_preferences(run, valid_form(**overrides), htmx=True)
        assert_field_error(page, field)
        assert_field_error(fragment, field)
        assert "hx-push-url" not in fragment.headers
        # Only the refused field has an error.
        for response in (page, fragment):
            errors = [
                e
                for e in outline(response.text)
                if (e.attrs.get("id") or "").endswith("-error")
                and e.attrs.get("id") != f"{field}-error"
            ]
            assert errors == [], [e.attrs.get("id") for e in errors]
        assert_nothing_saved(run)


def test_preferences_first_invalid_field_gets_focus(tmp_path: Path) -> None:
    form = valid_form(cv_country="ZZ", salary_amount="lots", locations="Fiction\tville")
    with running(tmp_path) as run:
        response = post_preferences(run, form)
        assert_nothing_saved(run)
    assert response.status_code == 422
    elements = outline(response.text)
    for field in ("locations", "salary_amount", "cv_country"):
        required(elements, f"{field}-error")
        assert control(elements, field).attrs.get("aria-invalid") == "true"
    focused = [e for e in elements if "data-autofocus" in e.attrs]
    assert len(focused) == 1, len(focused)
    assert focused[0] is control(elements, "locations"), "the first invalid field, in form order"


def test_preferences_refused_submit_echoes_values(tmp_path: Path) -> None:
    roles = "<b>Fictional</b> Editor\nFictional Archivist & Co"
    form = valid_form(
        target_roles=roles,
        work_modes=["hybrid"],
        salary_amount="lots",
        salary_currency="GBP",
        cv_country="DE",
    )
    with running(tmp_path) as run:
        responses = [post_preferences(run, form), post_preferences(run, form, htmx=True)]
        assert_nothing_saved(run)

    for response in responses:
        assert response.status_code == 422
        assert "<b>Fictional</b>" not in response.text, "shown back escaped"
        elements = outline(response.text)
        assert lines(control(elements, "target_roles")) == [
            "<b>Fictional</b> Editor",
            "Fictional Archivist & Co",
        ]
        assert lines(control(elements, "locations")) == ["Fictionville", "Remote"]
        assert lines(control(elements, "must_haves")) == ["Four-day week"]
        assert lines(control(elements, "deal_breakers")) == ["Night shifts", "Unpaid trials"]
        boxes = checkboxes(elements)
        assert "checked" in boxes["hybrid"].attrs
        assert "checked" not in boxes["remote"].attrs
        assert "checked" not in boxes["on_site"].attrs
        assert control(elements, "salary_amount").attrs.get("value") == "lots"
        currency = control(elements, "salary_currency")
        assert selected_values(currency, elements) == ["GBP"]
        country = control(elements, "cv_country")
        assert selected_values(country, elements) == ["DE"]


def test_preferences_list_limits(tmp_path: Path) -> None:
    longest = [f"{i:02d}" + "w" * 198 for i in range(20)]
    with running(tmp_path) as run:
        too_many = post_preferences(
            run, valid_form(must_haves="\n".join([*longest, "Fictional extra"]))
        )
        assert_field_error(too_many, "must_haves")
        too_long = post_preferences(run, valid_form(locations="x" * 201))
        assert_field_error(too_long, "locations")
        assert_nothing_saved(run)

        accepted = post_preferences(
            run, valid_form(**{name: "\n".join(longest) for name in LIST_FIELDS})
        )
        assert_see_other(accepted, DONE_PATH)
        saved = stored(run)
    assert saved is not None
    for name in LIST_FIELDS:
        assert getattr(saved, name) == tuple(longest), name


def test_preferences_unassigned_country_refused(tmp_path: Path) -> None:
    with running(tmp_path) as run:
        response = post_preferences(run, valid_form(cv_country="ZZ"))
        assert_field_error(response, "cv_country")
        assert_nothing_saved(run)


def test_preferences_amount_without_currency_refused(tmp_path: Path) -> None:
    with running(tmp_path) as run:
        response = post_preferences(run, valid_form(salary_amount="45,000", salary_currency=""))
        assert_field_error(response, "salary_currency")
        assert_nothing_saved(run)

        # A currency without an amount means no minimum salary.
        accepted = post_preferences(run, valid_form(salary_amount="", salary_currency="USD"))
        assert_see_other(accepted, DONE_PATH)
        saved = stored(run)
    assert saved is not None
    assert saved.minimum_salary is None


def test_preferences_without_workspace_asks_for_step_1(tmp_path: Path) -> None:
    with started(tmp_path, source="default") as run:
        assert run.app.state.workspace is None
        response = post_preferences(run, valid_form())
        assert run.app.state.workspace is None
    assert response.status_code == 422, response.text[:300]
    error = required(outline(response.text), "form-error")
    assert "step 1" in error.text.lower()
    assert not (tmp_path / "data").exists(), "nothing is created"


def test_preferences_post_needs_token_and_origin(tmp_path: Path) -> None:
    with running(tmp_path) as run:
        no_token = run.client.post(
            PREFERENCES_PATH, data=valid_form(), headers={"origin": BASE_URL}
        )
        bad_origin = run.client.post(
            PREFERENCES_PATH, data=valid_form(), headers=run.action(origin="http://evil.example")
        )
        assert no_token.status_code == 403
        assert bad_origin.status_code == 403
        assert_nothing_saved(run)


def test_preferences_get_never_saves(tmp_path: Path) -> None:
    with running(tmp_path) as run:
        get_ok(run, PREFERENCES_PATH + "?target_roles=Fictional+Spy&cv_country=PT")
        assert_nothing_saved(run)


# --- step 5: revisiting ----------------------------------------------------------------------


def assert_prefilled(response: Response) -> None:
    elements = outline(response.text)
    assert lines(control(elements, "target_roles")) == [FICTIONAL_ROLE, "Fictional Archivist"]
    assert lines(control(elements, "locations")) == ["Fictionville", "Remote"]
    assert lines(control(elements, "must_haves")) == ["Four-day week"]
    assert lines(control(elements, "deal_breakers")) == ["Night shifts", "Unpaid trials"]
    boxes = checkboxes(elements)
    assert {value for value, box in boxes.items() if "checked" in box.attrs} == {
        "remote",
        "on_site",
    }
    amount = str(control(elements, "salary_amount").attrs.get("value"))
    assert re.sub(r"[ ,]", "", amount) == "45000", amount
    assert selected_values(control(elements, "salary_currency"), elements) == ["EUR"]
    assert selected_values(control(elements, "cv_country"), elements) == ["PT"]
    assert not [e for e in elements if (e.attrs.get("id") or "").endswith("-error")]


def test_preferences_prefilled_on_revisit(tmp_path: Path) -> None:
    with running(tmp_path) as run:
        assert_see_other(post_preferences(run, valid_form()), DONE_PATH)
        assert_prefilled(get_ok(run, PREFERENCES_PATH))
        assert_prefilled(run.client.get(PREFERENCES_PATH, headers=run.htmx()))

    # After a restart, from the database.
    with started(tmp_path) as run:
        assert_prefilled(get_ok(run, PREFERENCES_PATH))


def test_preferences_prefilled_from_seeded_preferences(tmp_path: Path) -> None:
    with running(tmp_path, done=("welcome", "provider", "preferences"), preferences=SAVED) as run:
        assert_prefilled(get_ok(run, PREFERENCES_PATH))


# --- AC-M1.6: resume after a restart ---------------------------------------------------------


def test_ac_m1_6_restart_resumes_at_preferences_then_done(tmp_path: Path) -> None:
    seed(tmp_path)
    with started(tmp_path) as run:
        assert_see_other(run.client.get("/"), PREFERENCES_PATH)
        assert_see_other(post_preferences(run, valid_form()), DONE_PATH)

    with started(tmp_path) as run:
        assert run.decision.kind == "guide"
        assert run.decision.step == "done"
        assert_see_other(run.client.get("/"), DONE_PATH)
        assert stored(run) == SAVED


# --- step 6: Done ----------------------------------------------------------------------------


def test_ac_m1_7_done_lists_skipped_steps_with_links(tmp_path: Path) -> None:
    registry = StepRegistry((WELCOME, PROVIDER, SAMPLE_OPTIONAL, PREFERENCES, DONE))
    with running(
        tmp_path,
        done=("welcome", "provider", "preferences"),
        skipped=("sample_optional",),
        preferences=SAVED,
        registry=registry,
    ) as run:
        assert run.app.state.setup_registry is registry
        assert_see_other(run.client.get("/"), DONE_PATH)
        response = get_ok(run, DONE_PATH)
        fragment = run.client.get(DONE_PATH, headers=run.htmx())

    for shown in (response, fragment):
        rows = summary(shown)
        assert [href for href, _ in rows] == [
            "/setup/welcome",
            "/setup/provider",
            "/setup/sample_optional",
            "/setup/preferences",
        ]
        texts = dict(rows)
        assert SAMPLE_OPTIONAL.title in texts["/setup/sample_optional"]
        assert status_word(texts["/setup/sample_optional"]) == "Skipped"
        for path in ("/setup/welcome", "/setup/provider", "/setup/preferences"):
            assert status_word(texts[path]) == "Done", texts[path]
        # The step list says "Skipped" in text too.
        assert "Skipped" in step_list_item(shown, SAMPLE_OPTIONAL.title).text


def test_done_lists_configured_items(tmp_path: Path) -> None:
    with running(tmp_path, done=("welcome", "provider", "preferences"), preferences=SAVED) as run:
        response = get_ok(run, DONE_PATH)

    elements = outline(response.text)
    assert required(elements, "step-heading").text == DONE.title
    rows = summary(response)
    assert [href for href, _ in rows] == ["/setup/welcome", "/setup/provider", "/setup/preferences"]
    for (href, text), step in zip(rows, (WELCOME, PROVIDER, PREFERENCES), strict=True):
        assert step.title in text, (href, text)
        assert status_word(text) == "Done", text
    assert DONE_PATH not in [href for href, _ in rows], "Done does not list itself"
    # No personal details: neither the data folder nor the preferences.
    assert str(tmp_path / "data") not in response.text
    assert str(tmp_path) not in response.text
    for value in (FICTIONAL_ROLE, "Fictionville", "Night shifts", "45000", "45,000"):
        assert value not in response.text, value


def test_done_shows_not_done_yet_for_unfinished_steps(tmp_path: Path) -> None:
    with running(tmp_path) as run:  # preferences not done
        rows = dict(summary(get_ok(run, DONE_PATH)))
    assert status_word(rows["/setup/preferences"]) == "Not done yet"
    assert status_word(rows["/setup/welcome"]) == "Done"


def test_done_first_action_add_a_job_link(tmp_path: Path) -> None:
    with running(tmp_path, done=("welcome", "provider", "preferences"), preferences=SAVED) as run:
        elements = outline(get_ok(run, DONE_PATH).text)
        form = only(
            [
                e
                for e in elements
                if e.tag == "form" and DONE_PATH in (e.attrs.get("action"), e.attrs.get("hx-post"))
            ],
            "done form",
        )
        assert (form.attrs.get("method") or "").lower() == "post"
        buttons = [e for e in elements if e.tag == "button" and e.within(form)]
        assert buttons, "the form has buttons"
        first = buttons[0]
        assert (first.attrs.get("name"), first.attrs.get("value")) == ("then", "add_job")
        assert first.text == "Add a job"
        assert "button-primary" in (first.attrs.get("class") or "").split()
        assert (first.attrs.get("type") or "submit") == "submit"
        today = only(
            [b for b in buttons if b.attrs.get("value") == "today"], 'the "Go to Today" button'
        )
        assert today.attrs.get("name") == "then"
        assert today.text == "Go to Today"

        response = finish(run, "add_job")
        assert_see_other(response, "/inbox")
        inbox = get_ok(run, "/inbox")
        assert_not_redirected(inbox)
        assert required(outline(inbox.text), "page-heading").text == "Job inbox"


def test_finishing_completes_setup_and_root_shows_today(tmp_path: Path) -> None:
    with running(tmp_path, done=("welcome", "provider", "preferences"), preferences=SAVED) as run:
        assert_see_other(run.client.get("/"), DONE_PATH)
        response = finish(run, "today")
        assert_see_other(response, "/")
        assert live_statuses(run)["done"] == "done"
        assert run.decision.kind == "app"
        today = get_ok(run, "/")
        assert_not_redirected(today)
        assert required(outline(today.text), "page-heading").text == "Today"

    # Still complete after a restart.
    with started(tmp_path) as run:
        assert run.decision.kind == "app"
        assert_not_redirected(get_ok(run, "/"))


@pytest.mark.parametrize("then", [None, "", "board", "https://evil.example/", "//evil.example"])
def test_finishing_with_unknown_target_goes_to_today(tmp_path: Path, then: str | None) -> None:
    with running(tmp_path, done=("welcome", "provider", "preferences"), preferences=SAVED) as run:
        response = finish(run, then)
        assert_see_other(response, "/")
        assert live_statuses(run)["done"] == "done"


@pytest.mark.parametrize(("then", "target"), [("add_job", "/inbox"), ("today", "/")])
def test_finishing_htmx_gets_hx_redirect(tmp_path: Path, then: str, target: str) -> None:
    with running(tmp_path, done=("welcome", "provider", "preferences"), preferences=SAVED) as run:
        response = finish(run, then, htmx=True)
        assert live_statuses(run)["done"] == "done"
    assert response.status_code == 200, (response.status_code, response.text[:300])
    assert response.headers.get("hx-redirect") == target
    assert "location" not in response.headers
    assert "hx-push-url" not in response.headers


def test_finishing_refused_while_preferences_not_done(tmp_path: Path) -> None:
    with running(tmp_path) as run:
        responses = [finish(run, "add_job"), finish(run, "today", htmx=True)]
        assert "done" not in live_statuses(run)
        assert run.decision.kind == "guide"
        assert run.decision.step == "preferences"
        assert_see_other(run.client.get("/"), PREFERENCES_PATH)

    for response in responses:
        assert response.status_code == 422, (response.status_code, response.text[:300])
        assert "location" not in response.headers
        assert "hx-redirect" not in response.headers
        elements = outline(response.text)
        error = required(elements, "form-error")
        assert PREFERENCES.title in error.text
        assert WELCOME.title not in error.text
        links = [e for e in elements if e.tag == "a" and e.within(error)]
        assert [link.attrs.get("href") for link in links] == [PREFERENCES_PATH]
        assert title_of(response).startswith("Error: Done"), title_of(response)


def test_done_post_needs_token_and_origin(tmp_path: Path) -> None:
    with running(tmp_path, done=("welcome", "provider", "preferences"), preferences=SAVED) as run:
        no_token = run.client.post(DONE_PATH, data={"then": "today"}, headers={"origin": BASE_URL})
        assert no_token.status_code == 403
        assert "done" not in live_statuses(run)


# --- the step list: states in text, forced colors --------------------------------------------


def test_step_list_states_in_text(tmp_path: Path) -> None:
    registry = StepRegistry((WELCOME, PROVIDER, SAMPLE_OPTIONAL, PREFERENCES, DONE))
    with running(
        tmp_path, done=("welcome", "provider"), skipped=("sample_optional",), registry=registry
    ) as run:
        page = get_ok(run, PREFERENCES_PATH)

    assert "Done" in step_list_item(page, WELCOME.title).text
    assert "Skipped" in step_list_item(page, SAMPLE_OPTIONAL.title).text
    assert "Done" not in step_list_item(page, PREFERENCES.title).text
    current = step_list_item(page, PREFERENCES.title)
    assert current.attrs.get("aria-current") == "step"


def test_step_list_forced_colors_rule(tmp_path: Path) -> None:
    with running(tmp_path) as run:
        css = get_ok(run, "/static/css/app.css").text

    forced = forced_colors_blocks(css)
    assert forced, "a @media (forced-colors: active) block exists"
    assert ".setup-steps" in forced, "the step list has forced-colors rules"
    assert ".step-status" in forced, "step states stay visible in forced colors"
    assert re.search(r'\[aria-current="step"\]', forced), "the current step is marked"
