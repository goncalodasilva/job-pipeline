"""M9.2 part C: the per-task tier list in Settings > Providers and AI (AC-M9.2; ADR-089 item 9;
ADR-069 item 6; ADR-125 item 5; detailed design "How tiers work" item 2; TD-165).

Inside the Providers section (``#providers``, so it is swapped with it), after ``#tiers`` and not
inside it, a native disclosure ``<details id="task-tiers">`` (collapsed on a plain load, so the
whole list is one Tab stop; it is open in every answer of its form, as the Sessions and Your
details disclosures are, ADR-125 item 1) holds ``<summary id="task-tiers-summary">`` "Tier per
task" and ``<form id="task-tiers-form">`` posting to ``/settings/tiers/tasks`` (``hx-post``, target
``#providers``, swap ``outerHTML``). The form carries a hidden ``version`` (the ``ai_provider``
setting's version) and one row per ``TASK_SPECS`` task (the fake-only ``practice`` task is not
listed): an element with ``data-task="<name>"`` holding a plain-language ``<label for>`` (no
underscores, never the bare task name, one per task), a short description, the text "Default:
Fast" or "Default: Thorough" and ``<select name="tier_<name>">`` with the options ``fast``
"Fast" and ``thorough`` "Thorough", the effective tier selected (the override, else the default).
One ``<button type="submit">`` named "Save task tiers" submits.

``POST /settings/tiers/tasks`` (launch token and origin) takes ``version`` and the ``tier_<task>``
fields. The version is checked before the values (ADR-125 item 5):

- stale version -> 409, nothing saved, the Providers section with ``<p id="task-tiers-conflict"
  role="alert" data-autofocus>`` (the conflict message and a "Reload" link to ``/settings``) and
  no form that could carry the version;
- a task that is not listed (``tier_practice``, ``tier_fictional_task``) or a tier that is not
  ``fast`` or ``thorough`` -> 422, nothing saved, the value never shown back; a refused tier marks
  its select ``aria-invalid="true"`` tied by ``aria-describedby`` to a message, an unknown task
  shows ``<p id="task-tiers-error" role="alert">`` in the list;
- success -> the effective tiers are saved into ``task_tiers`` of the ``ai_provider`` setting
  (``expected_version=version``): a row equal to its default stores NO override, every other key
  of the setting (kind, models, saved sign-ins, an override of a task that is not listed) is kept.
  htmx gets 200 with the Providers section, ``<p id="task-tiers-done">`` "Task tiers saved." and
  ``summary`` as the one ``data-autofocus`` element of the list, and ``HX-Trigger`` ``announce``
  with the same words; a plain request gets 303 to ``/settings``. A version that is not a whole
  number is a plain-text 400.

``POST /settings/tiers`` (the model map) is not touched. Every value is fictional.
"""

import json
from pathlib import Path
from typing import Any

import pytest
from httpx import Response

from app.domain.settings import AI_PROVIDER, AIProviderSettings, TierMap
from app.modules.ai.providers.fake import FAKE_THOROUGH_MODEL
from app.modules.ai.tasks import PRACTICE_SPEC, TASK_SPECS
from tests.integration.test_settings_providers import (
    SETTINGS,
    assert_section_fragment,
    credential_status,
    section,
    settings_page,
    settings_version,
)
from tests.integration.test_setup_provider import described_by, only, required, stored
from tests.integration.test_setup_redirect import (
    BASE_URL,
    Element,
    Running,
    assert_see_other,
    outline,
)
from tests.integration.test_setup_reopen_banner import finished_guide, started

pytestmark = pytest.mark.integration

TASKS_PATH = "/settings/tiers/tasks"
FAST_TASKS = frozenset(
    {
        "summarize_job",
        "assess_fit",
        "enrichment_question",
        "structure_answer",
        "refine_fact",
        "group_removal_reasons",
    }
)
DEFAULT = {name: "fast" if name in FAST_TASKS else "thorough" for name in TASK_SPECS}
SAVE = "Save task tiers"
SAVED = "Task tiers saved."
HOSTILE = '"><b id="injected">x</b>'
PROVIDER_STEP = "/setup/provider"


# --- readers -----------------------------------------------------------------------------------


def list_box(elements: list[Element]) -> Element:
    box = required(elements, "task-tiers")
    assert box.tag == "details", box.tag
    assert box.within(section(elements)), "the list is in the Providers section"
    assert not box.within(required(elements, "tiers")), "and not inside the model map"
    summary = required(elements, "task-tiers-summary")
    assert summary.tag == "summary" and summary.within(box)
    assert summary.text == "Tier per task", summary.text
    return box


def list_form(elements: list[Element]) -> Element:
    box = list_box(elements)
    form = required(elements, "task-tiers-form")
    assert form.tag == "form" and form.within(box)
    assert form.attrs.get("action") == TASKS_PATH and form.attrs.get("hx-post") == TASKS_PATH
    assert (form.attrs.get("method") or "").lower() == "post"
    assert form.attrs.get("hx-target") == "#providers"
    return form


def task_rows(elements: list[Element]) -> dict[str, Element]:
    form = list_form(elements)
    rows = [e for e in elements if "data-task" in e.attrs]
    assert all(row.within(form) for row in rows)
    names = [str(row.attrs["data-task"]) for row in rows]
    assert len(names) == len(set(names)), "one row per task"
    return dict(zip(names, rows, strict=True))


def row_select(elements: list[Element], row: Element, task: str) -> Element:
    return only(
        [
            e
            for e in elements
            if e.tag == "select" and e.within(row) and e.attrs.get("name") == f"tier_{task}"
        ],
        f"select for {task}",
    )


def row_label(elements: list[Element], row: Element, select: Element) -> str:
    labels = [
        e
        for e in elements
        if e.tag == "label" and e.within(row) and e.attrs.get("for") == select.attrs.get("id")
    ]
    assert select.attrs.get("id") and len(labels) == 1, "one visible label tied by for"
    return labels[0].text


def row_default(row: Element) -> str:
    import re

    found = re.findall(
        r"\bdefault\b\W*(?:tier\W*)?(?:is\W*)?(fast|thorough)\b", row.text, re.IGNORECASE
    )
    assert len(found) == 1, f"one default tier text in {row.text!r}"
    return found[0].lower()


def chosen(elements: list[Element], select: Element) -> list[str]:
    return [
        str(e.attrs.get("value"))
        for e in elements
        if e.tag == "option" and e.within(select) and "selected" in e.attrs
    ]


def shown_tiers(elements: list[Element]) -> dict[str, str]:
    rows = task_rows(elements)
    result: dict[str, str] = {}
    for task, row in rows.items():
        selected = chosen(elements, row_select(elements, row, task))
        assert len(selected) == 1, (task, selected)
        result[task] = selected[0]
    return result


def hidden_version(elements: list[Element]) -> str:
    form = list_form(elements)
    versions = [
        e
        for e in elements
        if e.tag == "input" and e.within(form) and e.attrs.get("name") == "version"
    ]
    version = only(versions, "hidden version")
    assert version.attrs.get("type") == "hidden"
    return str(version.attrs.get("value"))


def form_for(effective: dict[str, str]) -> dict[str, str]:
    return {f"tier_{task}": tier for task, tier in effective.items()}


def post_tasks(run: Running, data: dict[str, Any], *, htmx: bool = True) -> Response:
    headers = run.action(**({"HX-Request": "true"} if htmx else {}))
    return run.client.post(TASKS_PATH, data=data, headers=headers)


def announced(response: Response) -> str:
    trigger = json.loads(response.headers["hx-trigger"])
    assert isinstance(trigger, dict) and isinstance(trigger.get("announce"), str)
    return str(trigger["announce"])


def seeded(tmp_path: Path, **settings: Any) -> Path:
    finished_guide(tmp_path, provider=AIProviderSettings(kind="fake", **settings))
    return tmp_path


# --- the list ----------------------------------------------------------------------------------


def test_task_tier_list_lists_every_task_spec_with_default(tmp_path: Path) -> None:
    """One row per task of ``TASK_SPECS`` (no ``practice``) with a label, a description, its
    default tier in words and a Fast or Thorough select showing the effective tier; collapsed on
    a plain load; on the full page and the htmx content; reading saves nothing."""
    seeded(
        tmp_path,
        task_tiers={"summarize_job": "thorough", "tailor_cv": "fast", PRACTICE_SPEC.name: "fast"},
    )
    effective = {**DEFAULT, "summarize_job": "thorough", "tailor_cv": "fast"}
    with started(tmp_path) as run:
        version = settings_version(run)
        page = settings_page(run)
        fragment = run.client.get(SETTINGS, headers=run.htmx())
        assert fragment.status_code == 200, fragment.text[:300]
        guide = run.client.get(PROVIDER_STEP)
        assert settings_version(run) == version, "GET saves nothing"

    assert len(TASK_SPECS) >= 17 and PRACTICE_SPEC.name not in TASK_SPECS
    for label, response in (("page", page), ("htmx", fragment)):
        elements = outline(response.text)
        box = list_box(elements)
        assert "open" not in box.attrs, f"{label}: collapsed on a plain load (one tab stop)"
        rows = task_rows(elements)
        assert set(rows) == set(TASK_SPECS), f"{label}: a row for every task, none for practice"
        labels: dict[str, str] = {}
        for task, row in rows.items():
            select = row_select(elements, row, task)
            offered = [
                (str(e.attrs.get("value")), e.text)
                for e in elements
                if e.tag == "option" and e.within(select)
            ]
            assert offered == [("fast", "Fast"), ("thorough", "Thorough")], (task, offered)
            assert row_default(row) == DEFAULT[task], (task, row.text)
            labels[task] = row_label(elements, row, select)
        assert shown_tiers(elements) == effective, label
        assert hidden_version(elements) == str(version)
        form = list_form(elements)
        buttons = [e for e in elements if e.tag == "button" and e.within(form)]
        assert [(b.text, b.attrs.get("type", "submit")) for b in buttons] == [(SAVE, "submit")]

        # No missing copy: a plain-language label and a description for every task.
        assert len(set(labels.values())) == len(labels), f"{label}: labels are unique"
        for task, text in labels.items():
            assert text and "_" not in text and text != task, (task, text)
            assert text.lower() != task.replace("_", " "), f"{task}: a label, not the task name"
            row = rows[task]
            leftover = row.text
            for part in (text, "Default", "Fast", "Thorough"):
                leftover = leftover.replace(part, " ")
            assert len(leftover.split()) >= 4, f"{task}: a short description ({row.text!r})"

    guide_elements = outline(guide.text)
    assert [e for e in guide_elements if e.attrs.get("id") == "task-tiers"] == []
    assert TASKS_PATH not in guide.text, "the setup guide never shows the task tiers"


def test_task_tier_list_follows_the_defaults_when_nothing_is_stored(tmp_path: Path) -> None:
    seeded(tmp_path)
    with started(tmp_path) as run:
        elements = outline(settings_page(run).text)
        assert shown_tiers(elements) == DEFAULT


# --- save --------------------------------------------------------------------------------------


def test_task_tiers_save_versioned_409(tmp_path: Path) -> None:
    from app.domain.errors import VersionConflict

    original = AIProviderSettings(
        kind="fake",
        tiers=TierMap(fast=FAKE_THOROUGH_MODEL),
        task_tiers={"assess_fit": "thorough", PRACTICE_SPEC.name: "thorough"},
    )
    finished_guide(tmp_path, provider=original)
    with started(tmp_path) as run:
        version = settings_version(run)
        before_status = credential_status(settings_page(run))
        elements = outline(settings_page(run).text)
        assert hidden_version(elements) == str(version)
        assert shown_tiers(elements)["assess_fit"] == "thorough"

        # --- a save: only the rows that differ from their default are stored ------------------
        wanted = {**DEFAULT, "summarize_job": "thorough", "tailor_cv": "fast"}  # assess_fit back
        saved = post_tasks(run, {"version": str(version), **form_for(wanted)})
        assert saved.status_code == 200, saved.text[:300]
        settings = stored(run)
        assert dict(settings.task_tiers) == {
            "summarize_job": "thorough",
            "tailor_cv": "fast",
            PRACTICE_SPEC.name: "thorough",
        }, "defaults store nothing; a key the list does not show is kept"
        assert settings == original.model_copy(update={"task_tiers": settings.task_tiers})
        assert settings.kind == "fake" and settings.tiers == original.tiers
        new_version = settings_version(run)
        assert new_version == version + 1

        section_elements = assert_section_fragment(saved)
        assert shown_tiers(section_elements) == wanted
        assert hidden_version(section_elements) == str(new_version)
        assert "open" in list_box(section_elements).attrs, "open so the message is visible"
        done = required(section_elements, "task-tiers-done")
        assert done.text == SAVED
        assert announced(saved) == SAVED
        focus = [e for e in section_elements if "data-autofocus" in e.attrs]
        assert [e.attrs.get("id") for e in focus] == ["task-tiers-summary"]
        assert credential_status(settings_page(run)) == before_status, "the sign-in state stays"
        assert shown_tiers(outline(settings_page(run).text)) == wanted

        # --- everything back to its default clears the overrides it shows --------------------
        cleared = post_tasks(run, {"version": str(new_version), **form_for(DEFAULT)})
        assert cleared.status_code == 200
        assert dict(stored(run).task_tiers) == {PRACTICE_SPEC.name: "thorough"}
        current = settings_version(run)

        # --- a stale version: 409 with the reload prompt, whatever the values are --------------
        for stale in ("0", str(current - 1), str(current + 1), "99"):
            for values in (form_for(wanted), {**form_for(DEFAULT), "tier_assess_fit": "medium"}):
                refused = post_tasks(run, {"version": stale, **values})
                assert refused.status_code == 409, (stale, refused.status_code)
                assert VersionConflict.message in refused.text
                refused_elements = assert_section_fragment(refused)
                prompt = required(refused_elements, "task-tiers-conflict")
                assert prompt.attrs.get("role") == "alert"
                links = [e for e in refused_elements if e.tag == "a" and e.within(prompt)]
                assert [(e.text, e.attrs.get("href")) for e in links] == [("Reload", SETTINGS)]
                focus = [e for e in refused_elements if "data-autofocus" in e.attrs]
                assert [e.attrs.get("id") for e in focus] == ["task-tiers-conflict"]
                assert [e for e in refused_elements if e.attrs.get("id") == "task-tiers-done"] == []
                assert not [
                    e
                    for e in refused_elements
                    if e.tag == "input"
                    and e.attrs.get("name") == "version"
                    and e.within(required(refused_elements, "task-tiers"))
                ], "no form that could carry the version"
                assert SAVED not in refused.headers.get("hx-trigger", "")
        assert settings_version(run) == current
        assert dict(stored(run).task_tiers) == {PRACTICE_SPEC.name: "thorough"}

        # --- a plain form goes back to Settings; a stale plain form shows the whole page ------
        plain = post_tasks(
            run, {"version": str(current), **form_for({**DEFAULT, "refine_fact": "thorough"})},
            htmx=False,
        )  # fmt: skip
        assert_see_other(plain, SETTINGS)
        assert stored(run).task_tiers["refine_fact"] == "thorough"
        stale_plain = post_tasks(run, {"version": "0", **form_for(DEFAULT)}, htmx=False)
        assert stale_plain.status_code == 409 and "<html" in stale_plain.text
        assert required(outline(stale_plain.text), "task-tiers-conflict")
        assert stored(run).task_tiers["refine_fact"] == "thorough"


def test_task_tiers_save_keeps_the_model_map_and_the_other_route_working(tmp_path: Path) -> None:
    """The model-map route is untouched: it keeps the task overrides, and a task-tier save keeps
    the model map."""
    from tests.integration.test_settings_tier_mapping import post_tiers

    seeded(tmp_path, task_tiers={"summarize_job": "thorough"})
    with started(tmp_path) as run:
        version = settings_version(run)
        assert post_tasks(run, {"version": str(version), **form_for(DEFAULT)}).status_code == 200
        assert dict(stored(run).task_tiers) == {}
        mapped = post_tiers(run, {"fast": FAKE_THOROUGH_MODEL, "thorough": FAKE_THOROUGH_MODEL})
        assert_see_other(mapped, SETTINGS)
        assert stored(run).tiers == TierMap(fast=FAKE_THOROUGH_MODEL, thorough=FAKE_THOROUGH_MODEL)
        moved = {**DEFAULT, "gap_check": "fast"}
        assert (
            post_tasks(run, {"version": str(settings_version(run)), **form_for(moved)}).status_code
            == 200
        )
        assert stored(run).tiers == TierMap(fast=FAKE_THOROUGH_MODEL, thorough=FAKE_THOROUGH_MODEL)
        assert dict(stored(run).task_tiers) == {"gap_check": "fast"}


# --- refused -----------------------------------------------------------------------------------


@pytest.mark.parametrize("htmx", [True, False], ids=["htmx", "plain"])
@pytest.mark.parametrize(
    "value", ["medium", "Fast", "THOROUGH", "", " fast", HOSTILE], ids=lambda v: repr(v)[:12]
)
def test_task_tiers_refuse_a_tier_that_is_not_listed(
    tmp_path: Path, value: str, htmx: bool
) -> None:
    original = AIProviderSettings(kind="fake", task_tiers={"assess_fit": "thorough"})
    finished_guide(tmp_path, provider=original)
    with started(tmp_path) as run:
        version = settings_version(run)
        before = shown_tiers(outline(settings_page(run).text))
        form = {"version": str(version), **form_for({**DEFAULT, "assess_fit": "thorough"})}
        form["tier_tailor_cv"] = value
        refused = post_tasks(run, form, htmx=htmx)
        assert refused.status_code == 422, (refused.status_code, refused.text[:300])
        assert settings_version(run) == version and stored(run) == original, "nothing saved"
        elements = assert_section_fragment(refused) if htmx else outline(refused.text)
        if not htmx:
            assert "<html" in refused.text
        rows = task_rows(elements)
        select = row_select(elements, rows["tailor_cv"], "tailor_cv")
        assert select.attrs.get("aria-invalid") == "true"
        messages = described_by(elements, select)
        assert any(e.text for e in messages), "the error is tied to the select"
        assert chosen(elements, select) == [before["tailor_cv"]], "what is saved, not what was sent"
        assert "open" in list_box(elements).attrs, "open so the error is visible"
        others = [
            task
            for task, row in rows.items()
            if task != "tailor_cv"
            and row_select(elements, row, task).attrs.get("aria-invalid") == "true"
        ]
        assert others == []
        assert "injected" not in refused.text, "a refused value is never shown back"
        assert shown_tiers(elements)["assess_fit"] == "thorough"
        if htmx:
            focus = [e for e in elements if "data-autofocus" in e.attrs]
            assert [e.attrs.get("name") for e in focus] == ["tier_tailor_cv"]
            assert "not saved" in announced(refused).lower()


@pytest.mark.parametrize(
    "task", ["practice", "fictional_task", "Summarize_Job", "summarize_job ", HOSTILE]
)
def test_task_tiers_refuse_a_task_that_is_not_listed(tmp_path: Path, task: str) -> None:
    original = AIProviderSettings(kind="fake", task_tiers={"assess_fit": "thorough"})
    finished_guide(tmp_path, provider=original)
    with started(tmp_path) as run:
        version = settings_version(run)
        form = {"version": str(version), **form_for(DEFAULT), f"tier_{task}": "thorough"}
        refused = post_tasks(run, form)
        assert refused.status_code == 422, (refused.status_code, refused.text[:300])
        assert settings_version(run) == version and stored(run) == original
        elements = assert_section_fragment(refused)
        error = required(elements, "task-tiers-error")
        assert (
            error.attrs.get("role") == "alert" and error.text and error.within(list_box(elements))
        )
        assert "injected" not in refused.text
        assert "open" in list_box(elements).attrs


def test_task_tiers_malformed_version_is_a_400(tmp_path: Path) -> None:
    seeded(tmp_path)
    with started(tmp_path) as run:
        version = settings_version(run)
        for data in (
            form_for(DEFAULT),
            {"version": "abc", **form_for(DEFAULT)},
            {"version": "-1", **form_for(DEFAULT)},
            {"version": "", **form_for(DEFAULT)},
            {"version": ["1", "1"], **form_for(DEFAULT)},
        ):
            answer = post_tasks(run, data)
            assert answer.status_code == 400, (data.get("version"), answer.status_code)
            assert "<html" not in answer.text
        assert settings_version(run) == version and stored(run).task_tiers == {}


def test_task_tiers_route_needs_token_and_origin_and_is_post_only(tmp_path: Path) -> None:
    seeded(tmp_path)
    with started(tmp_path) as run:
        version = settings_version(run)
        form = {"version": str(version), **form_for({**DEFAULT, "assess_fit": "thorough"})}
        for headers in (run.with_token(), run.htmx(), run.action()):
            assert run.client.get(TASKS_PATH, params=form, headers=headers).status_code == 405
        refused = (
            run.client.post(TASKS_PATH, data=form, headers={"origin": BASE_URL}),
            run.client.post(
                TASKS_PATH, data=form, headers=run.action(origin="http://evil.example")
            ),
            run.client.post(TASKS_PATH, data=form, headers=run.with_token()),
            run.client.post(TASKS_PATH, data=form),
        )
        assert [r.status_code for r in refused] == [403, 403, 403, 403]
        assert settings_version(run) == version and stored(run).task_tiers == {}
        # Control: with both, the same request is accepted.
        assert run.client.post(TASKS_PATH, data=form, headers=run.action()).status_code == 303
        assert dict(stored(run).task_tiers) == {"assess_fit": "thorough"}


def test_task_tier_list_is_inside_the_providers_area_only(tmp_path: Path) -> None:
    """The list lives on ``/settings`` and on no other area page."""
    seeded(tmp_path)
    with started(tmp_path) as run:
        assert required(outline(settings_page(run).text), "task-tiers")
        for path in (
            "/settings/budgets",
            "/settings/preferences",
            "/settings/lists",
            "/settings/sessions",
            "/settings/appearance",
            "/settings/keyboard",
        ):
            answer = run.client.get(path)
            assert answer.status_code == 200
            assert "task-tiers" not in answer.text and TASKS_PATH not in answer.text, path
        # The task tier setting is the AI provider's own versioned row.
        assert AI_PROVIDER.key == "ai_provider"
