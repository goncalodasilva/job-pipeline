"""M9.2 part D: Settings > Lists and statuses, the lists editor (AC-M9.2; ADR-131 items 6-7;
ADR-125 item 5): controlled values for industry, seniority and employment type (add, replace the
other spellings, delete only when unused), work mode shown read only, skill aliases, and the
fixed removal reasons with their preference-field mapping. No migration.

The routes, the markup, the copy and the status codes are described in ``tests/lists_pages.py``;
the SQL in ``tests/integration/test_controlled_values_repo.py``. Real SQLite in a temp folder,
the app through ``TestClient``, no network. Every value is fictional. Application names are
imported inside the tests that need them, so each test fails on its own while they are missing.
"""

import json
from pathlib import Path
from typing import Any, get_args

import pytest
from fastapi.routing import APIRoute

from app.api.security import PAGE_TAG
from app.domain.errors import VersionConflict
from app.domain.ids import is_safe_id
from app.domain.job_normalization import value_key
from tests.board_pages import by_id, elements
from tests.data_pages import get_token, state_of
from tests.integration.test_settings_areas import area_app
from tests.integration.test_setup_redirect import BASE_URL, Running
from tests.job_fixtures import generate_jobs, seed_jobs
from tests.lists_pages import (
    CONFLICT_ID,
    DONE_ID,
    EDITABLE,
    GROUPS,
    HOSTILE,
    IN_USE_STATUSES,
    INVALID,
    OVER_LENGTH,
    PATH,
    REASONS,
    REFUSED_FIELD_STATUSES,
    REFUSED_ID_STATUSES,
    SECTION_ID,
    VALUES_PATH,
    VALUES_TO_MAP,
    WORK_MODES,
    add_refused,
    added_message,
    alias_lines,
    aliases_message,
    aliases_refused,
    button_named,
    controls_in,
    deleted_message,
    done_text,
    error_text,
    focus_targets,
    group,
    group_open,
    hidden_version,
    ids_are_unique,
    inside,
    invalid_controls,
    invalid_names,
    jobs_phrase,
    label,
    label_text,
    row_of,
    said,
    section,
    seed_id,
    skill_path,
    skill_rows,
    textarea_of,
    value_path,
    value_rows,
)

pytestmark = pytest.mark.integration

HTMX = {"HX-Request": "true"}
RELOAD = "Reload"


# --- helpers ------------------------------


def post(run: Running, path: str, *, htmx: bool = True, **values: Any) -> Any:
    return run.client.post(path, data=values, headers=run.action(**(HTMX if htmx else {})))


def post_data(run: Running, path: str, data: Any) -> Any:
    return run.client.post(path, data=data, headers=run.action(**HTMX))


def page(run: Running) -> str:
    response = run.client.get(PATH)
    assert response.status_code == 200, response.status_code
    return str(response.text)


def sql(run: Running, query: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    with run.app.state.workspace.db.transaction() as conn:
        return [tuple(r) for r in conn.execute(query, params)]


def write(run: Running, query: str, params: tuple[Any, ...] = ()) -> None:
    with run.app.state.workspace.db.transaction() as conn:
        conn.execute(query, params)


def stored(run: Running, field: str, value: str) -> dict[str, Any] | None:
    found = sql(
        run,
        "SELECT id, aliases, version FROM controlled_values WHERE field=? AND value=?",
        (field, value),
    )
    if not found:
        return None
    value_id, aliases, version = found[0]
    return {"id": value_id, "aliases": json.loads(aliases), "version": version}


def tables(run: Running) -> dict[str, list[tuple[Any, ...]]]:
    return {
        name: sql(run, f"SELECT * FROM {name} ORDER BY 1")  # noqa: S608 - fixed names
        for name in ("controlled_values", "skills", "jobs", "field_value_queue")
    }


def hold(run: Running, field: str, value: str, count: int) -> None:
    """``count`` fictional jobs hold ``value`` in ``field``: the first is user-corrected there,
    the second removed (all of them count)."""
    seed_jobs(run.app.state.workspace.db, generate_jobs(count))
    write(run, f"UPDATE jobs SET {field}=?", (value,))  # noqa: S608 - fixed test fields
    write(run, "UPDATE jobs SET user_corrected=? WHERE id='job-0001'", (json.dumps([field]),))
    if count > 1:
        write(
            run,
            "UPDATE jobs SET triage_state='removed', removal_reason='other' WHERE id='job-0002'",
        )


def version_of(markup: str, field: str, value: str) -> str:
    return hidden_version(markup, row_of(markup, field, value))


# --- the page: token free, read only, collapsed groups -----------------------------------------


def test_lists_editor_page_is_token_free_read_only_and_collapsed(tmp_path: Path) -> None:
    root = tmp_path / "root"
    with area_app(root) as run:
        before = state_of(run, root / "data")
        rows = tables(run)
        full = run.client.get(PATH)
        part = get_token(run, PATH, htmx=True)
        head = run.client.head(PATH)
        assert full.status_code == 200 and "<html" in full.text
        assert part.status_code == 200 and "<html" not in part.text
        assert head.status_code == 200
        assert run.client.get(PATH, headers=HTMX).status_code == 403, "htmx needs the token"
        for markup in (full.text, part.text):
            box = section(markup)
            assert "settings-section" in str(box.attrs.get("class"))
            heading = by_id(markup, "lists-editor-heading")
            assert heading is not None and heading.tag == "h2" and heading.within(box)
            assert (
                heading.text == "Lists" and box.attrs.get("aria-labelledby") == heading.attrs["id"]
            )
            assert heading.attrs.get("tabindex") == "-1"
            board = by_id(markup, "board-settings")
            assert board is not None and not box.within(board) and not board.within(box)
            order = [
                e.attrs["id"]
                for e in inside(markup, box)
                if e.tag == "details" and str(e.attrs.get("id", "")).startswith("list-")
            ]
            assert order == [f"list-{key}" for key in GROUPS]
            for key in GROUPS:
                assert not group_open(markup, key), f"{key} is collapsed on a page load"
            assert not focus_targets(markup) and by_id(markup, DONE_ID) is None
            assert by_id(markup, CONFLICT_ID) is None
            ids_are_unique(markup)
        # The page says where each unknown text goes next.
        link = by_id(full.text, "values-to-map-link")
        assert link is not None and link.tag == "a" and link.within(section(full.text))
        assert link.attrs.get("href") == VALUES_TO_MAP and link.text == "Values to map"
        assert state_of(run, root / "data") == before and tables(run) == rows


def test_lists_editor_lists_each_editable_field_with_values_aliases_and_use(tmp_path: Path) -> None:
    with area_app(tmp_path / "root") as run:
        hold(run, "industry", "energy", 2)
        for markup in (page(run), get_token(run, PATH, htmx=True).text):
            for field, name in EDITABLE.items():
                box = group(markup, field)
                summary = by_id(markup, f"list-{field}-summary")
                assert summary is not None and summary.tag == "summary" and summary.text == name
                assert summary.within(box)
                stored_rows = sql(
                    run,
                    "SELECT id, value, aliases, version FROM controlled_values "
                    "WHERE field=? ORDER BY value",
                    (field,),
                )
                shown = value_rows(markup, field)
                assert list(shown) == [r[1] for r in stored_rows], "ordered by value"
                for value_id, value, aliases, version in stored_rows:
                    li = shown[value]
                    assert (
                        li.attrs.get("id") == f"value-{value_id}"
                        and li.attrs.get("tabindex") == "-1"
                    )
                    assert li.attrs.get("data-value-id") == value_id
                    assert li.attrs.get("data-field") == field
                    assert li.text.startswith(label(value))
                    assert hidden_version(markup, li) == str(version)
                    assert alias_lines(markup, li) == json.loads(aliases)
                    area = textarea_of(markup, li)
                    assert area.attrs.get("name") == "aliases"
                    assert label_text(markup, area) == f"Other spellings of {label(value)}"
                    forms = [e for e in inside(markup, li) if e.tag == "form"]
                    assert {f.attrs.get("action") for f in forms} == {
                        value_path(value_id, "aliases"),
                        value_path(value_id, "delete"),
                    }
                    for f in forms:
                        assert (f.attrs.get("method") or "").lower() == "post"
                        assert f.attrs.get("hx-post") == f.attrs.get("action")
                    save = button_named(markup, li, f"Save aliases for {label(value)}")
                    assert (
                        save.text == "Save aliases" and save.attrs.get("type", "submit") == "submit"
                    )
                    gone = button_named(markup, li, f"Delete {label(value)}")
                    assert gone.text == "Delete"
                add = by_id(markup, f"value-add-form-{field}")
                assert add is not None and add.tag == "form" and add.within(box)
                assert add.attrs.get("action") == VALUES_PATH == add.attrs.get("hx-post")
                assert (add.attrs.get("method") or "").lower() == "post"
                fields = {
                    str(e.attrs.get("name")): e for e in inside(markup, add) if e.tag == "input"
                }
                assert set(fields) == {"field", "value"}, "no version: adding changes no row"
                assert fields["field"].attrs.get("type") == "hidden"
                assert fields["field"].attrs.get("value") == field
                assert fields["value"].attrs.get("id") == f"value-add-{field}"
                assert label_text(markup, fields["value"]) == f"New {name.lower()} value"
                button_named(markup, add, f"Add {name.lower()} value")
            assert "Used by 2 jobs" in row_of(markup, "industry", "energy").text
            assert "Not used" in row_of(markup, "industry", "retail").text


# --- add, replace aliases, delete unused ------------------------------


def test_controlled_value_add_alias_and_delete_unused(tmp_path: Path) -> None:
    with area_app(tmp_path / "root") as run:
        before_rows = len(sql(run, "SELECT 1 FROM controlled_values WHERE field='industry'"))

        # --- add: trimmed and lower-cased, version 1, announced, focus on the new row ----------
        added = post(run, VALUES_PATH, field="industry", value="  Aerospace ")
        assert added.status_code == 200, added.text[:300]
        assert "<html" not in added.text
        row = stored(run, "industry", "aerospace")
        assert row is not None and row["aliases"] == [] and row["version"] == 1
        assert is_safe_id(row["id"])
        assert (
            len(sql(run, "SELECT 1 FROM controlled_values WHERE field='industry'"))
            == before_rows + 1
        )
        assert done_text(added.text) == added_message("industry", "aerospace")
        assert said(added.headers) == added_message("industry", "aerospace")
        [target] = focus_targets(added.text)
        assert target.attrs.get("id") == f"value-{row['id']}"
        assert group_open(added.text, "industry")
        assert not any(group_open(added.text, key) for key in GROUPS if key != "industry")
        listed = list(value_rows(added.text, "industry"))
        assert "aerospace" in listed and listed == sorted(listed)
        assert hidden_version(added.text, row_of(added.text, "industry", "aerospace")) == "1"
        assert "Not used" in row_of(added.text, "industry", "aerospace").text

        # --- the same text with spaces: the key rule is value_key, the stored text is lower ----
        post(run, VALUES_PATH, field="industry", value="Quantum Farming")
        [(stored_text,)] = sql(
            run,
            "SELECT value FROM controlled_values WHERE field='industry' "
            "AND lower(value) LIKE 'quantum%'",
        )
        assert stored_text == stored_text.strip() == stored_text.lower()
        assert value_key(stored_text) == value_key("Quantum Farming")

        # --- replace the other spellings: lower-cased, blank lines dropped, order kept ---------
        saved = post(
            run,
            value_path(row["id"], "aliases"),
            version="1",
            aliases="Aviation Tech\r\n\r\n  SPACE \r\n",
        )
        assert saved.status_code == 200, saved.text[:300]
        assert "<html" not in saved.text
        assert stored(run, "industry", "aerospace") == {
            "id": row["id"],
            "aliases": ["aviation tech", "space"],
            "version": 2,
        }
        assert done_text(saved.text) == aliases_message("aerospace")
        assert said(saved.headers) == aliases_message("aerospace")
        [target] = focus_targets(saved.text)
        assert target.attrs.get("id") == f"value-{row['id']}"
        li = row_of(saved.text, "industry", "aerospace")
        assert alias_lines(saved.text, li) == ["aviation tech", "space"]
        assert hidden_version(saved.text, li) == "2"
        # Replace, not append; and an empty box empties the list.
        assert (
            post(run, value_path(row["id"], "aliases"), version="2", aliases="space").status_code
            == 200
        )
        found = stored(run, "industry", "aerospace")
        assert found is not None and found["aliases"] == ["space"] and found["version"] == 3
        assert (
            post(run, value_path(row["id"], "aliases"), version="3", aliases="").status_code == 200
        )
        found = stored(run, "industry", "aerospace")
        assert found is not None and found["aliases"] == [] and found["version"] == 4

        # --- delete an unused value ------------------------------------------------------------
        gone = post(run, value_path(row["id"], "delete"), version="4")
        assert gone.status_code == 200, gone.text[:300]
        assert "<html" not in gone.text
        assert stored(run, "industry", "aerospace") is None
        assert done_text(gone.text) == deleted_message("industry", "aerospace")
        assert said(gone.headers) == deleted_message("industry", "aerospace")
        assert "aerospace" not in value_rows(gone.text, "industry")
        [target] = focus_targets(gone.text)
        assert target.attrs.get("id") == "list-industry-summary"
        assert group_open(gone.text, "industry")
        # Seeded values can be deleted too while nothing uses them; others are untouched.
        assert (
            post(run, value_path(seed_id("industry", "retail"), "delete"), version="1").status_code
            == 200
        )
        assert stored(run, "industry", "retail") is None
        assert stored(run, "industry", "energy") is not None


@pytest.mark.parametrize("field", list(EDITABLE))
def test_a_value_can_be_added_to_each_editable_field(tmp_path: Path, field: str) -> None:
    with area_app(tmp_path / "root") as run:
        answer = post(run, VALUES_PATH, field=field, value="Fictional Kind")
        assert answer.status_code == 200
        [(stored_text,)] = sql(
            run,
            "SELECT value FROM controlled_values WHERE field=? AND lower(value) LIKE 'fictional%'",
            (field,),
        )
        assert value_key(stored_text) == value_key("Fictional Kind")
        assert stored_text == stored_text.lower() == stored_text.strip()
        assert group_open(answer.text, field)


@pytest.mark.parametrize(
    ("field", "typed"),
    [
        ("industry", ""),
        ("industry", "   "),
        ("industry", "!!!"),
        ("industry", "​"),
        ("industry", "energy"),
        ("industry", "ENERGY"),
        ("industry", "  Energy "),
        ("industry", "telecom"),
        ("industry", "Tele-com"),
        ("industry", "real estate"),
        ("industry", "Real-Estate"),
        ("seniority", "S.R."),
        ("seniority", "Entry Level"),
        ("employment_type", "full-time"),
        ("employment_type", "Full Time"),
        ("industry", "x" * (OVER_LENGTH + 1)),
        ("industry", HOSTILE + "x" * OVER_LENGTH),
    ],
    ids=lambda v: str(v)[:24],
)
def test_adding_a_blank_duplicate_or_over_long_value_is_a_422_tied_to_its_field(
    tmp_path: Path, field: str, typed: str
) -> None:
    with area_app(tmp_path / "root") as run:
        before = tables(run)
        refused = post(run, VALUES_PATH, field=field, value=typed)
        assert refused.status_code == 422, refused.text[:300]
        assert "<html" not in refused.text
        assert invalid_names(refused.text) == ["value"]
        [bad] = invalid_controls(refused.text)
        assert bad.attrs.get("id") == f"value-add-{field}"
        assert error_text(refused.text, bad)
        [target] = focus_targets(refused.text)
        assert target is bad, "focus goes to the refused field"
        assert group_open(refused.text, field)
        assert said(refused.headers) == add_refused(1)
        assert done_text(refused.text) is None
        assert bad.attrs.get("value") == typed, "what was typed is kept (escaped)"
        assert '<script id="injected"' not in refused.text
        ids_are_unique(refused.text)
        assert tables(run) == before, "nothing is written"


def test_a_value_may_repeat_in_another_field(tmp_path: Path) -> None:
    with area_app(tmp_path / "root") as run:
        assert post(run, VALUES_PATH, field="seniority", value="Remote").status_code == 200
        assert post(run, VALUES_PATH, field="industry", value="Senior").status_code == 200
        assert post(run, VALUES_PATH, field="employment_type", value="Energy").status_code == 200
        assert stored(run, "seniority", "remote") is not None
        assert stored(run, "industry", "senior") is not None


def test_an_alias_of_another_value_cannot_be_added_as_a_value_and_the_inbox_agrees(
    tmp_path: Path,
) -> None:
    """Same key rule as "Values to map": a text that already belongs to a value is refused."""
    with area_app(tmp_path / "root") as run:
        assert post(run, VALUES_PATH, field="industry", value="Aero-space").status_code == 200
        assert post(run, VALUES_PATH, field="industry", value="aero space").status_code == 422
        assert len(sql(run, "SELECT 1 FROM controlled_values WHERE value LIKE 'aero%'")) == 1


# --- replacing aliases: key rule, version first ----------------------------------------------


@pytest.mark.parametrize(
    ("field", "value", "aliases"),
    [
        ("industry", "agriculture", "farming\nenergy"),  # another value
        ("industry", "agriculture", "ENERGY"),
        ("industry", "agriculture", "e n e r g y"),  # same key
        ("industry", "agriculture", "utilities"),  # another value's alias
        ("industry", "agriculture", "Utili-ties"),
        ("seniority", "junior", "senior"),
        ("employment_type", "contract", "Full-Time"),
        ("industry", "agriculture", "x" * (OVER_LENGTH + 1)),
    ],
    ids=lambda v: str(v)[:20].replace("\n", "|"),
)
def test_an_alias_that_means_another_value_of_the_field_is_a_422(
    tmp_path: Path, field: str, value: str, aliases: str
) -> None:
    with area_app(tmp_path / "root") as run:
        before = tables(run)
        value_id = seed_id(field, value)
        refused = post(run, value_path(value_id, "aliases"), version="1", aliases=aliases)
        assert refused.status_code == 422, refused.text[:300]
        assert "<html" not in refused.text
        assert invalid_names(refused.text) == ["aliases"]
        [bad] = invalid_controls(refused.text)
        assert bad.tag == "textarea" and bad.attrs.get("id") == f"value-{value_id}-aliases"
        assert error_text(refused.text, bad)
        [target] = focus_targets(refused.text)
        assert target is bad
        assert said(refused.headers) == aliases_refused(1)
        assert alias_lines(refused.text, row_of(refused.text, field, value)) == [
            line.strip() for line in aliases.split("\n") if line.strip()
        ], "what was typed is kept"
        assert hidden_version(refused.text, row_of(refused.text, field, value)) == "1"
        assert group_open(refused.text, field)
        assert tables(run) == before


def test_an_alias_may_be_the_text_of_a_value_in_another_field(tmp_path: Path) -> None:
    with area_app(tmp_path / "root") as run:
        answer = post(
            run,
            value_path(seed_id("industry", "agriculture"), "aliases"),
            version="1",
            aliases="senior\nremote",
        )
        assert answer.status_code == 200
        found = stored(run, "industry", "agriculture")
        assert found is not None and found["aliases"] == ["senior", "remote"]


def test_aliases_check_the_version_before_the_values(tmp_path: Path) -> None:
    """A stale version with a refused alias is the 409 reload prompt, not a 422 whose form
    carries the current version (ADR-125 item 5)."""
    with area_app(tmp_path / "root") as run:
        value_id = seed_id("industry", "agriculture")
        assert (
            post(run, value_path(value_id, "aliases"), version="1", aliases="farm").status_code
            == 200
        )
        before = tables(run)
        for stale in ("1", "3", "99"):
            bad = post(run, value_path(value_id, "aliases"), version=stale, aliases="energy")
            assert bad.status_code == 409, stale
            assert by_id(bad.text, CONFLICT_ID) is not None
            assert "aria-invalid" not in bad.text, "a stale save is not judged on its values"
            assert "<html" not in bad.text
        assert tables(run) == before
        assert (
            post(run, value_path(value_id, "aliases"), version="2", aliases="energy").status_code
            == 422
        )


# --- delete only when unused ------------------------------


@pytest.mark.parametrize(
    ("field", "value"),
    [("industry", "agriculture"), ("seniority", "senior"), ("employment_type", "contract")],
)
def test_controlled_value_in_use_cannot_be_deleted(tmp_path: Path, field: str, value: str) -> None:
    with area_app(tmp_path / "root") as run:
        hold(run, field, value, 3)  # one user-corrected, one removed: all three count
        # The same text in another column does not count.
        other = "seniority" if field != "seniority" else "industry"
        write(run, f"UPDATE jobs SET {other}=? WHERE id='job-0003'", (value,))  # noqa: S608
        value_id = seed_id(field, value)
        markup = page(run)
        assert f"Used by {jobs_phrase(3)}" in row_of(markup, field, value).text
        before = tables(run)

        refused = post(run, value_path(value_id, "delete"), version="1")
        assert refused.status_code in IN_USE_STATUSES, refused.status_code
        assert "<html" not in refused.text
        error = by_id(refused.text, f"value-{value_id}-delete-error")
        assert error is not None and jobs_phrase(3) in error.text
        assert by_id(refused.text, CONFLICT_ID) is None, "in use is not a stale version"
        [target] = focus_targets(refused.text)
        assert target is error, "focus goes to the message"
        button = button_named(
            refused.text, row_of(refused.text, field, value), f"Delete {label(value)}"
        )
        # The refusal first, then the group's "can't be undone" hint (M9.2 fix pass, item 5e).
        assert str(button.attrs.get("aria-describedby")).split()[0] == error.attrs.get("id")
        assert jobs_phrase(3) in said(refused.headers)
        assert done_text(refused.text) is None
        assert group_open(refused.text, field)
        ids_are_unique(refused.text)
        assert tables(run) == before, "nothing is deleted or changed"
        assert stored(run, field, value) is not None

        # A stale version is the reload prompt first, in use or not.
        stale = post(run, value_path(value_id, "delete"), version="7")
        assert stale.status_code == 409 and by_id(stale.text, CONFLICT_ID) is not None
        assert by_id(stale.text, f"value-{value_id}-delete-error") is None
        assert tables(run) == before

        # Once no job holds it, the same delete works.
        write(run, f"UPDATE jobs SET {field}=NULL")  # noqa: S608 - fixed test fields
        assert post(run, value_path(value_id, "delete"), version="1").status_code == 200
        assert stored(run, field, value) is None


def test_the_in_use_message_names_one_job_in_the_singular(tmp_path: Path) -> None:
    with area_app(tmp_path / "root") as run:
        hold(run, "industry", "retail", 1)
        refused = post(run, value_path(seed_id("industry", "retail"), "delete"), version="1")
        assert refused.status_code in IN_USE_STATUSES
        error = by_id(refused.text, f"value-{seed_id('industry', 'retail')}-delete-error")
        assert error is not None and "1 job" in error.text and "1 jobs" not in error.text
        assert stored(run, "industry", "retail") is not None


def test_a_job_that_takes_the_value_after_the_page_loaded_blocks_the_delete(tmp_path: Path) -> None:
    with area_app(tmp_path / "root") as run:
        markup = page(run)
        assert "Not used" in row_of(markup, "industry", "retail").text
        version = version_of(markup, "industry", "retail")
        hold(run, "industry", "retail", 2)
        refused = post(run, value_path(seed_id("industry", "retail"), "delete"), version=version)
        assert refused.status_code in IN_USE_STATUSES and jobs_phrase(2) in refused.text
        assert stored(run, "industry", "retail") is not None


# --- work mode is fixed ------------------------------


def test_work_mode_list_fixed(tmp_path: Path) -> None:
    from app.domain.settings import WorkMode

    assert tuple(get_args(WorkMode)) == WORK_MODES, "control: the domain's work modes"
    with area_app(tmp_path / "root") as run:
        stored_rows = sql(
            run,
            "SELECT id, value, aliases FROM controlled_values "
            "WHERE field='work_mode' ORDER BY value",
        )
        assert [r[1] for r in stored_rows] == sorted(WORK_MODES)
        for markup in (page(run), get_token(run, PATH, htmx=True).text):
            box = group(markup, "work_mode")
            summary = by_id(markup, "list-work_mode-summary")
            assert summary is not None and summary.text == "Work mode"
            rows = value_rows(markup, "work_mode")
            assert list(rows) == sorted(WORK_MODES)
            for _, value, aliases in stored_rows:
                text = rows[value].text
                assert label(value) in text
                for alias in json.loads(aliases):
                    assert alias in text, f"{alias!r} is shown for {value}"
                assert rows[value].attrs.get("data-field") == "work_mode"
            note = by_id(markup, "work-mode-fixed-note")
            assert note is not None and note.within(box) and len(note.text.split()) >= 4
            assert controls_in(markup, box) == [], "nothing in work mode can be operated"
            assert by_id(markup, "value-add-form-work_mode") is None

        # No route can change it either.
        before = tables(run)
        for field in ("work_mode", "Work_Mode", "department", "", "jobs"):
            answer = post(run, VALUES_PATH, field=field, value="seasonal")
            assert answer.status_code in REFUSED_FIELD_STATUSES, (field, answer.status_code)
        for _, value, _aliases in stored_rows:
            work_id = seed_id("work_mode", value)
            changed = post(run, value_path(work_id, "aliases"), version="1", aliases="anywhere")
            assert changed.status_code in REFUSED_ID_STATUSES, value
            removed = post(run, value_path(work_id, "delete"), version="1")
            assert removed.status_code in REFUSED_ID_STATUSES, value
        assert tables(run) == before


# --- skills ------------------------------


def test_skill_aliases_edit_versioned(tmp_path: Path) -> None:
    with area_app(tmp_path / "root") as run:
        skills = run.app.state.skills
        python = skills.add("Python", aliases=("py",))
        rust = skills.add("Rust")
        skills.add_alias(rust.id, "rs", expected_version=1)
        markup = page(run)
        box = group(markup, "skills")
        rows = skill_rows(markup)
        assert set(rows) == {python.id, rust.id}
        assert by_id(markup, "skill-list-empty") is None
        assert [r.attrs.get("id") for r in rows.values()] == [
            f"skill-{python.id}",
            f"skill-{rust.id}",
        ]
        assert (
            rows[python.id].text.startswith("Python")
            and rows[python.id].attrs.get("tabindex") == "-1"
        )
        assert alias_lines(markup, rows[python.id]) == ["py"]
        assert alias_lines(markup, rows[rust.id]) == ["rs"]
        assert hidden_version(markup, rows[python.id]) == "1"
        assert hidden_version(markup, rows[rust.id]) == "2"
        form = by_id(markup, f"skill-{python.id}-aliases-form")
        assert form is not None and form.within(rows[python.id])
        assert form.attrs.get("action") == skill_path(python.id) == form.attrs.get("hx-post")
        area = textarea_of(markup, rows[python.id])
        assert area.attrs.get("id") == f"skill-{python.id}-aliases"
        assert label_text(markup, area) == "Other spellings of Python"
        button_named(markup, rows[python.id], "Save aliases for Python")
        # Skills have aliases only: no add and no delete.
        actions = {str(e.attrs.get("action")) for e in inside(markup, box) if e.tag == "form"}
        assert actions == {skill_path(python.id), skill_path(rust.id)}

        # --- replace: the spelling is kept as typed, blank lines dropped, version moves on -----
        saved = post(run, skill_path(python.id), version="1", aliases="py\r\nCPython\r\n\r\n")
        assert saved.status_code == 200, saved.text[:300]
        assert "<html" not in saved.text
        assert [(s.name, s.aliases, s.version) for s in skills.list() if s.id == python.id] == [
            ("Python", ("py", "CPython"), 2)
        ]
        assert done_text(saved.text) == aliases_message("Python")
        assert said(saved.headers) == aliases_message("Python")
        [target] = focus_targets(saved.text)
        assert target.attrs.get("id") == f"skill-{python.id}"
        assert group_open(saved.text, "skills") and not group_open(saved.text, "industry")
        assert hidden_version(saved.text, skill_rows(saved.text)[python.id]) == "2"
        assert post(run, skill_path(python.id), version="2", aliases="CPython").status_code == 200
        assert [s.aliases for s in skills.list() if s.id == python.id] == [("CPython",)]
        assert post(run, skill_path(python.id), version="3", aliases="").status_code == 200
        assert [s.aliases for s in skills.list() if s.id == python.id] == [()]

        # --- stale: 409 with the reload prompt, nothing changed --------------------------------
        before = tables(run)
        stale = post(run, skill_path(python.id), version="1", aliases="snake")
        assert stale.status_code == 409 and "<html" not in stale.text
        assert VersionConflict.message in stale.text
        prompt = by_id(stale.text, CONFLICT_ID)
        assert prompt is not None and prompt.attrs.get("role") == "alert"
        links = [e for e in inside(stale.text, prompt) if e.tag == "a"]
        assert [(e.text, e.attrs.get("href")) for e in links] == [(RELOAD, PATH)]
        assert [e.attrs.get("id") for e in focus_targets(stale.text)] == [CONFLICT_ID]
        assert tables(run) == before

        # --- an alias that means another skill is refused (skill_matches) ----------------------
        for aliases in ("RUST", "rs", " Rs ", "ok\nrust"):
            refused = post(run, skill_path(python.id), version="4", aliases=aliases)
            assert refused.status_code == 422, aliases
            assert invalid_names(refused.text) == ["aliases"]
            [bad] = invalid_controls(refused.text)
            assert bad.tag == "textarea" and error_text(refused.text, bad)
            assert focus_targets(refused.text)[0] is bad
            assert said(refused.headers) == aliases_refused(1)
            assert [e for e in alias_lines(refused.text, skill_rows(refused.text)[python.id])]
            assert tables(run) == before
        # The version is checked first: stale with a collision is the reload prompt.
        stale_clash = post(run, skill_path(python.id), version="1", aliases="rust")
        assert stale_clash.status_code == 409 and "aria-invalid" not in stale_clash.text

        # --- unknown skill: 404; there is no delete ------------------------------------------
        for unknown in ("no-such-skill", "x" * 80):
            missing = post(run, skill_path(unknown), version="1", aliases="a")
            assert missing.status_code == 404 and unknown not in missing.text
        gone = run.client.post(
            f"/settings/lists/skills/{python.id}/delete",
            data={"version": "4"},
            headers=run.action(**HTMX),
        )
        assert gone.status_code in (404, 405)
        assert any(s.id == python.id for s in skills.list())


def test_skills_group_says_so_when_there_are_none(tmp_path: Path) -> None:
    with area_app(tmp_path / "root") as run:
        markup = page(run)
        box = group(markup, "skills")
        empty = by_id(markup, "skill-list-empty")
        assert empty is not None and empty.within(box) and empty.text
        assert skill_rows(markup) == {}


def test_skill_service_set_aliases_replaces_and_checks_version_then_meaning(tmp_path: Path) -> None:
    """``SkillService.set_aliases(skill_id, aliases, *, expected_version)`` replaces the whole
    list through ``skills_repo.set_aliases``: spellings are trimmed and single-spaced as
    ``display_skill`` does, blank and repeated ones are dropped, and it returns the new
    ``SkillView``. Stale version -> ``VersionConflict`` (first); an alias that means another
    skill -> ``SkillAliasTaken``; unknown id -> ``FactNotFound``."""
    from app.domain.errors import FactNotFound, SkillAliasTaken

    with area_app(tmp_path / "root") as run:
        skills = run.app.state.skills
        python = skills.add("Python", aliases=("py",))
        rust = skills.add("Rust", aliases=("rs",))
        view = skills.set_aliases(
            python.id, ["  CPython ", "", "cpython", "py  thon"], expected_version=1
        )
        assert (view.id, view.name, view.version) == (python.id, "Python", 2)
        assert view.aliases == ("CPython", "py thon")
        assert skills.find("py thon") is not None and skills.find("py") is None, "replaced"
        before = tables(run)
        with pytest.raises(VersionConflict):
            skills.set_aliases(python.id, ["snake"], expected_version=1)
        with pytest.raises(VersionConflict):
            skills.set_aliases(python.id, ["rust"], expected_version=1)  # stale wins
        with pytest.raises(SkillAliasTaken):
            skills.set_aliases(python.id, ["snake", "RS"], expected_version=2)
        with pytest.raises(SkillAliasTaken):
            skills.set_aliases(python.id, ["rust"], expected_version=2)
        with pytest.raises(FactNotFound):
            skills.set_aliases("no-such-skill", ["x"], expected_version=1)
        assert tables(run) == before
        assert skills.set_aliases(rust.id, [], expected_version=1).aliases == ()


# --- removal reasons are fixed ------------------------------


def test_removal_reasons_listed_fixed_with_mapping(tmp_path: Path) -> None:
    from app.domain.removal_reasons import REMOVAL_REASONS

    assert {r.key: (r.label, tuple(r.maps_to)) for r in REMOVAL_REASONS} == {
        key: (name, fields) for key, (name, fields, _) in REASONS.items()
    }, "control: the domain list is the one written out in the tests"
    with area_app(tmp_path / "root") as run:
        for markup in (page(run), get_token(run, PATH, htmx=True).text):
            box = by_id(markup, "removal-reasons")
            assert box is not None and box.tag == "section" and box.within(section(markup))
            heading = by_id(markup, "removal-reasons-heading")
            assert heading is not None and heading.tag == "h3" and heading.text == "Removal reasons"
            assert box.attrs.get("aria-labelledby") == "removal-reasons-heading"
            items = [e for e in inside(markup, box) if e.tag == "li" and "data-reason" in e.attrs]
            assert [e.attrs["data-reason"] for e in items] == list(REASONS)
            for item in items:
                key = str(item.attrs["data-reason"])
                name, fields, words = REASONS[key]
                assert item.attrs.get("id") == f"removal-reason-{key}"
                assert item.attrs.get("data-maps-to") == " ".join(fields)
                assert name in item.text and words in item.text, item.text
                if not fields:
                    assert "No preference field" in item.text
                else:
                    assert "No preference field" not in item.text
            assert controls_in(markup, box) == [], "a fixed list: nothing to operate"
            ids_are_unique(markup)
            link = by_id(markup, "values-to-map-link")
            assert link is not None and link.attrs.get("href") == VALUES_TO_MAP
        assert run.client.get(VALUES_TO_MAP).status_code == 200, "the link resolves"

        # No route changes the list.
        before = tables(run)
        for path in ("/settings/lists/removal-reasons", "/settings/lists/removal-reasons/other"):
            answer = run.client.post(
                path, data={"label": "Mine", "version": "1"}, headers=run.action(**HTMX)
            )
            assert answer.status_code in (404, 405), path
        assert tables(run) == before


# --- guards, malformed requests, unknown ids ------------------------------


def guarded_routes(run: Running) -> list[tuple[str, dict[str, str]]]:
    skill = run.app.state.skills.add("Guarded", aliases=())
    return [
        (VALUES_PATH, {"field": "industry", "value": "aerospace"}),
        (value_path(seed_id("seniority", "lead"), "aliases"), {"version": "1", "aliases": "boss"}),
        (value_path(seed_id("industry", "retail"), "delete"), {"version": "1"}),
        (skill_path(skill.id), {"version": "1", "aliases": "shield"}),
    ]


def test_lists_routes_need_token_and_origin_and_only_the_page_reads(tmp_path: Path) -> None:
    with area_app(tmp_path / "root") as run:
        routes = guarded_routes(run)
        before = tables(run)
        for path, data in routes:
            for headers in (
                {},
                run.with_token(),
                {"origin": BASE_URL},
                {"origin": "http://evil.example", **run.with_token()},
            ):
                assert run.client.post(path, data=data, headers=headers).status_code == 403, (
                    path,
                    headers,
                )
            assert run.client.get(path, headers=run.htmx()).status_code == 405, path
            assert run.client.get(path).status_code in (403, 405), path
            for method in ("PUT", "PATCH", "DELETE"):
                answer = run.client.request(method, path, headers=run.action())
                assert answer.status_code in (404, 405), (method, path)
        assert tables(run) == before, "a refused request changes nothing"
        # Control: with both, each is accepted.
        for path, data in routes:
            assert post(run, path, **data).status_code == 200, path
        pages = {
            route.path
            for route in run.app.routes
            if isinstance(route, APIRoute) and PAGE_TAG in (route.tags or [])
        }
        assert PATH in pages and not any(path.startswith(PATH + "/") for path in pages)


@pytest.mark.parametrize(
    "data",
    [
        {"value": "aerospace"},
        {"field": "industry"},
        {},
        {"field": "industry", "value": "aerospace", "version": "1"},
        {"field": "industry", "value": "aerospace", "aliases": "x"},
        {"field": ["industry", "seniority"], "value": "aerospace"},
        {"field": "industry", "value": ["aerospace", "space"]},
    ],
    ids=["no-field", "no-value", "empty", "version", "extra", "repeated-field", "repeated-value"],
)
def test_adding_a_value_with_a_malformed_form_is_a_400(
    tmp_path: Path, data: dict[str, Any]
) -> None:
    with area_app(tmp_path / "root") as run:
        before = tables(run)
        answer = post_data(run, VALUES_PATH, data)
        assert answer.status_code == 400 and answer.text == INVALID
        assert tables(run) == before


@pytest.mark.parametrize("action", ["aliases", "delete"])
@pytest.mark.parametrize(
    "version",
    [None, "", "0", "-1", "abc", "1.5", ["1", "1"]],
    ids=["missing", "empty", "zero", "negative", "text", "fraction", "repeated"],
)
def test_a_value_form_needs_one_whole_version(tmp_path: Path, action: str, version: Any) -> None:
    with area_app(tmp_path / "root") as run:
        before = tables(run)
        data: dict[str, Any] = {"aliases": "x"} if action == "aliases" else {}
        if version is not None:
            data["version"] = version
        answer = post_data(run, value_path(seed_id("industry", "retail"), action), data)
        assert answer.status_code == 400 and answer.text == INVALID
        assert tables(run) == before


@pytest.mark.parametrize(
    ("action", "data"),
    [
        ("aliases", {"version": "1"}),
        ("aliases", {"version": "1", "aliases": ["a", "b"]}),
        ("aliases", {"version": "1", "aliases": "a", "value": "x"}),
        ("delete", {"version": "1", "aliases": "a"}),
        ("delete", {"version": "1", "extra": "x"}),
    ],
    ids=[
        "no-aliases",
        "repeated-aliases",
        "extra-on-aliases",
        "aliases-on-delete",
        "extra-on-delete",
    ],
)
def test_a_value_form_with_missing_repeated_or_extra_fields_is_a_400(
    tmp_path: Path, action: str, data: dict[str, Any]
) -> None:
    with area_app(tmp_path / "root") as run:
        before = tables(run)
        answer = post_data(run, value_path(seed_id("industry", "retail"), action), data)
        assert answer.status_code == 400 and answer.text == INVALID
        assert tables(run) == before


@pytest.mark.parametrize(
    "data",
    [
        {"aliases": "a"},
        {"version": "abc", "aliases": "a"},
        {"version": "1"},
        {"version": "1", "aliases": "a", "x": "1"},
    ],
    ids=["no-version", "text-version", "no-aliases", "extra"],
)
def test_a_skill_form_with_a_malformed_body_is_a_400(tmp_path: Path, data: dict[str, Any]) -> None:
    with area_app(tmp_path / "root") as run:
        skill = run.app.state.skills.add("Guarded")
        before = tables(run)
        answer = post_data(run, skill_path(skill.id), data)
        assert answer.status_code == 400 and answer.text == INVALID
        assert tables(run) == before


@pytest.mark.parametrize("unknown", ["no-such-value", "cv-industry-nothing", "x" * 80, "a%20b"])
def test_an_unknown_or_unsafe_value_id_is_a_404_that_does_not_echo_it(
    tmp_path: Path, unknown: str
) -> None:
    with area_app(tmp_path / "root") as run:
        # Control: the routes exist, so the 404 below is about the id and not a missing route.
        known = value_path(seed_id("industry", "retail"), "aliases")
        assert post(run, known, version="1", aliases="shop").status_code == 200
        before = tables(run)
        for path, data in (
            (value_path(unknown, "aliases"), {"version": "1", "aliases": "a"}),
            (value_path(unknown, "delete"), {"version": "1"}),
        ):
            answer = post(run, path, **data)
            assert answer.status_code == 404, (path, answer.status_code)
            assert "<html" not in answer.text and unknown not in answer.text
        assert tables(run) == before


# --- two tabs ------------------------------


def test_two_tabs_the_second_save_gets_the_reload_prompt_and_nothing_of_it_is_applied(
    tmp_path: Path,
) -> None:
    with area_app(tmp_path / "root") as run:
        tab_a, tab_b = page(run), page(run)
        version = version_of(tab_a, "industry", "energy")
        assert version == version_of(tab_b, "industry", "energy") == "1"
        value_id = seed_id("industry", "energy")

        assert (
            post(run, value_path(value_id, "aliases"), version=version, aliases="power").status_code
            == 200
        )
        before = tables(run)
        late = post(run, value_path(value_id, "aliases"), version=version, aliases="grid")
        assert late.status_code == 409 and by_id(late.text, CONFLICT_ID) is not None
        assert VersionConflict.message in late.text
        late_delete = post(run, value_path(value_id, "delete"), version=version)
        assert late_delete.status_code == 409 and stored(run, "industry", "energy") is not None
        assert tables(run) == before
        found = stored(run, "industry", "energy")
        assert found is not None and found["aliases"] == ["power"] and found["version"] == 2

        # After a reload tab B carries the current version and its save goes through.
        fresh_version = version_of(page(run), "industry", "energy")
        assert fresh_version == "2"
        assert (
            post(
                run, value_path(value_id, "aliases"), version=fresh_version, aliases="grid"
            ).status_code
            == 200
        )
        found = stored(run, "industry", "energy")
        assert found is not None and found["aliases"] == ["grid"] and found["version"] == 3


def test_two_tabs_a_value_deleted_elsewhere_is_a_404_and_added_twice_a_422(tmp_path: Path) -> None:
    with area_app(tmp_path / "root") as run:
        value_id = seed_id("industry", "retail")
        assert post(run, value_path(value_id, "delete"), version="1").status_code == 200
        before = tables(run)
        again = post(run, value_path(value_id, "delete"), version="1")
        assert again.status_code == 404
        gone = post(run, value_path(value_id, "aliases"), version="1", aliases="shop")
        assert gone.status_code == 404
        assert tables(run) == before
        assert post(run, VALUES_PATH, field="industry", value="Aerospace").status_code == 200
        second = post(run, VALUES_PATH, field="industry", value="aerospace")
        assert second.status_code == 422 and invalid_names(second.text) == ["value"]
        assert len(sql(run, "SELECT 1 FROM controlled_values WHERE value='aerospace'")) == 1


# --- plain forms ------------------------------


def test_plain_forms_redirect_on_success_and_show_the_whole_page_when_refused(
    tmp_path: Path,
) -> None:
    with area_app(tmp_path / "root") as run:
        ok = post(run, VALUES_PATH, htmx=False, field="industry", value="Aerospace")
        assert ok.status_code == 303 and ok.headers["location"] == PATH
        assert stored(run, "industry", "aerospace") is not None
        row = stored(run, "industry", "aerospace")
        assert row is not None
        saved = post(
            run, value_path(row["id"], "aliases"), htmx=False, version="1", aliases="space"
        )
        assert saved.status_code == 303 and saved.headers["location"] == PATH
        assert "HX-Trigger" not in saved.headers

        refused = post(run, VALUES_PATH, htmx=False, field="industry", value="energy")
        assert refused.status_code == 422 and "<html" in refused.text
        assert invalid_names(refused.text) == ["value"]
        heading = by_id(refused.text, "page-heading")
        assert heading is not None and "data-autofocus" not in heading.attrs
        assert len(focus_targets(refused.text)) == 1, "one focus target on a whole page"
        assert by_id(refused.text, "board-settings") is not None, "the whole area page"
        ids_are_unique(refused.text)

        stale = post(run, value_path(row["id"], "aliases"), htmx=False, version="1", aliases="x")
        assert stale.status_code == 409 and "<html" in stale.text
        assert by_id(stale.text, CONFLICT_ID) is not None

        hold(run, "industry", "energy", 2)
        in_use = post(
            run, value_path(seed_id("industry", "energy"), "delete"), htmx=False, version="1"
        )
        assert in_use.status_code in IN_USE_STATUSES and "<html" in in_use.text
        assert jobs_phrase(2) in in_use.text

        deleted = post(run, value_path(row["id"], "delete"), htmx=False, version="2")
        assert deleted.status_code == 303 and deleted.headers["location"] == PATH
        assert stored(run, "industry", "aerospace") is None

        skill = run.app.state.skills.add("Plain")
        assert (
            post(run, skill_path(skill.id), htmx=False, version="1", aliases="simple").status_code
            == 303
        )


# --- escaping and the rest of the page ------------------------------


def test_stored_and_typed_text_is_escaped(tmp_path: Path) -> None:
    with area_app(tmp_path / "root") as run:
        now = "2026-11-04T09:00:00+00:00"
        write(
            run,
            "INSERT INTO controlled_values (id, field, value, aliases, created_at, updated_at) "
            "VALUES ('cv-hostile', 'industry', ?, ?, ?, ?)",
            (HOSTILE, json.dumps([HOSTILE + "a", "</textarea>" + HOSTILE]), now, now),
        )
        run.app.state.skills.add(HOSTILE, aliases=("</textarea>" + HOSTILE,))
        for markup in (page(run), get_token(run, PATH, htmx=True).text):
            assert '<script id="injected"' not in markup
            assert "</textarea><script" not in markup
            shown = value_rows(markup, "industry")
            assert HOSTILE in shown, "the hostile value is listed as text"
            assert HOSTILE in alias_lines(markup, shown[HOSTILE])[0]
            ids_are_unique(markup)
        # What is typed and refused comes back escaped.
        refused = post(
            run,
            value_path("cv-hostile", "aliases"),
            version="1",
            aliases="</textarea>" + HOSTILE + "\nenergy",
        )
        assert refused.status_code == 422
        assert (
            '<script id="injected"' not in refused.text and "</textarea><script" not in refused.text
        )
        assert "</textarea>" + HOSTILE in "\n".join(
            alias_lines(refused.text, row_of(refused.text, "industry", HOSTILE))
        )
        added = post(run, VALUES_PATH, field="industry", value=HOSTILE + " " + "y" * OVER_LENGTH)
        assert added.status_code == 422 and '<script id="injected"' not in added.text
        assert [e for e in elements(added.text) if e.attrs.get("id") == "injected"] == []


def test_the_board_statuses_and_reminders_are_still_on_the_page(tmp_path: Path) -> None:
    with area_app(tmp_path / "root") as run:
        for markup in (page(run), get_token(run, PATH, htmx=True).text):
            board = by_id(markup, "board-settings")
            assert board is not None
            rows = [
                e for e in inside(markup, board) if e.tag == "li" and "data-status-key" in e.attrs
            ]
            assert len(rows) == 9
            assert by_id(markup, "status-add-form") is not None
            assert by_id(markup, SECTION_ID) is not None
        # The editor's answers leave the board's section out; the board's answers leave the
        # editor out (each is its own fragment).
        added = post(run, VALUES_PATH, field="industry", value="Aerospace")
        assert by_id(added.text, "board-settings") is None
        status = post(run, "/settings/board/statuses", label="Waiting", phase="active")
        assert status.status_code == 200 and by_id(status.text, SECTION_ID) is None


def test_the_values_to_map_page_is_unaffected_by_the_editor(tmp_path: Path) -> None:
    with area_app(tmp_path / "root") as run:
        write(
            run,
            "INSERT INTO field_value_queue (id, field, value, created_at, updated_at) "
            "VALUES ('unknown-1', 'industry', 'Fictional cultivation', '2026-11-04', '2026-11-04')",
        )
        assert post(run, VALUES_PATH, field="industry", value="Aerospace").status_code == 200
        shown = run.client.get(VALUES_TO_MAP)
        assert shown.status_code == 200 and "Fictional cultivation" in shown.text
        assert "aerospace" in shown.text.lower(), "a value added here can be a target there"
        assert sql(run, "SELECT status FROM field_value_queue") == [("pending",)]


# --- M9.2 fix pass: seeded spellings survive, caps, control characters, hints ---------------------

MANY = "Too many other spellings. Keep up to 50."
NO_CONTROL_COPY = "Use plain text, without control characters."
BELL = chr(7)
BIDI_OVERRIDE = chr(0x202E)


def test_saving_every_seeded_value_unchanged_keeps_its_other_spellings(tmp_path: Path) -> None:
    """Open the form of each seeded industry, seniority and employment-type value, save it as
    shown, and the stored spellings are identical: nothing the migration seeded vanishes on the
    first save ("full-time" and "full time" differ from ``full_time`` only in punctuation)."""
    with area_app(tmp_path / "root") as run:
        seeded = sql(
            run,
            "SELECT field, value, id, aliases, version FROM controlled_values "
            "WHERE field IN ('industry', 'seniority', 'employment_type') ORDER BY field, value",
        )
        assert len(seeded) > 20 and any(json.loads(row[3]) for row in seeded)
        assert ("employment_type", "full_time") in {(r[0], r[1]) for r in seeded}
        for field, value, value_id, aliases, version in seeded:
            markup = page(run)
            li = row_of(markup, field, value)
            shown = alias_lines(markup, li)
            assert shown == json.loads(aliases), (field, value, "the form shows what is stored")
            saved = post(
                run,
                value_path(value_id, "aliases"),
                version=hidden_version(markup, li),
                aliases="\n".join(shown),
            )
            assert saved.status_code == 200, (field, value, saved.status_code)
            found = stored(run, field, value)
            assert found is not None and found["aliases"] == json.loads(aliases), (field, value)
            assert found["version"] == version + 1


def test_a_typed_spelling_that_differs_only_in_punctuation_is_kept(tmp_path: Path) -> None:
    with area_app(tmp_path / "root") as run:
        value_id = seed_id("employment_type", "full_time")
        answer = post(
            run,
            value_path(value_id, "aliases"),
            version="1",
            aliases="Full-Time\nfull time\nFULL-TIME",
        )
        assert answer.status_code == 200
        found = stored(run, "employment_type", "full_time")
        assert found is not None and found["aliases"] == ["full-time", "full time"]
        # Another value's spelling is still refused whatever its punctuation.
        refused = post(
            run,
            value_path(seed_id("employment_type", "contract"), "aliases"),
            version="1",
            aliases="Part Time",
        )
        assert refused.status_code == 422


def test_aliases_of_a_value_are_capped_at_fifty_lines(tmp_path: Path) -> None:
    with area_app(tmp_path / "root") as run:
        value_id = seed_id("industry", "construction")
        fifty = "\n".join(f"fictional spelling {n}" for n in range(50))
        before = tables(run)
        refused = post(run, value_path(value_id, "aliases"), version="1", aliases=fifty + "\nmore")
        assert refused.status_code == 422, refused.text[:300]
        assert invalid_names(refused.text) == ["aliases"]
        [bad] = invalid_controls(refused.text)
        assert error_text(refused.text, bad) == MANY
        assert focus_targets(refused.text)[0] is bad
        assert said(refused.headers) == aliases_refused(1)
        shown = alias_lines(refused.text, row_of(refused.text, "industry", "construction"))
        assert len(shown) == 51, "what was typed is kept"
        assert tables(run) == before
        # Fifty is allowed.
        allowed = post(run, value_path(value_id, "aliases"), version="1", aliases=fifty)
        assert allowed.status_code == 200
        found = stored(run, "industry", "construction")
        assert found is not None and len(found["aliases"]) == 50


def test_skill_aliases_are_capped_at_fifty_lines(tmp_path: Path) -> None:
    with area_app(tmp_path / "root") as run:
        python = run.app.state.skills.add("Python")
        fifty = "\n".join(f"snake {n}" for n in range(50))
        before = tables(run)
        refused = post(run, skill_path(python.id), version="1", aliases=fifty + "\nmore")
        assert refused.status_code == 422
        [bad] = invalid_controls(refused.text)
        assert error_text(refused.text, bad) == MANY
        assert tables(run) == before
        assert post(run, skill_path(python.id), version="1", aliases=fifty).status_code == 200


@pytest.mark.parametrize("bad", [f"x{BELL}y", f"x{BIDI_OVERRIDE}y", "x\x1by", "x\ty"])
def test_control_characters_are_refused_in_values_and_spellings_with_fixed_copy(
    tmp_path: Path, bad: str
) -> None:
    with area_app(tmp_path / "root") as run:
        python = run.app.state.skills.add("Python")
        construction = seed_id("industry", "construction")
        before = tables(run)
        cases = [
            (VALUES_PATH, {"field": "industry", "value": bad}, "value-add-industry"),
            (
                value_path(construction, "aliases"),
                {"version": "1", "aliases": f"fine\n{bad}"},
                f"value-{construction}-aliases",
            ),
            (
                skill_path(python.id),
                {"version": "1", "aliases": f"fine\n{bad}"},
                f"skill-{python.id}-aliases",
            ),
        ]
        for path, data, control_id in cases:
            refused = post(run, path, **data)
            assert refused.status_code == 422, (path, refused.text[:200])
            [control] = invalid_controls(refused.text)
            assert control.attrs.get("id") == control_id
            assert error_text(refused.text, control) == NO_CONTROL_COPY
            assert focus_targets(refused.text)[0] is control
            assert tables(run) == before, "nothing is written"


def test_the_first_alias_box_of_each_group_says_one_spelling_per_line(tmp_path: Path) -> None:
    """Each group's first other-spellings box is described by a hint, so the one-per-line rule is
    heard where it is needed (also in a refused answer, next to the error)."""
    with area_app(tmp_path / "root") as run:
        run.app.state.skills.add("Python", aliases=("py",))
        markup = page(run)
        for key in (*EDITABLE, "skills"):
            areas = [e for e in inside(markup, group(markup, key)) if e.tag == "textarea"]
            assert areas, key
            hints = [
                by_id(markup, i) for i in str(areas[0].attrs.get("aria-describedby") or "").split()
            ]
            assert [h.text for h in hints if h is not None] == ["One spelling per line."], key
        refused = post(
            run,
            value_path(seed_id("industry", "agriculture"), "aliases"),
            version="1",
            aliases="energy",
        )
        [bad] = invalid_controls(refused.text)
        described = [by_id(refused.text, i) for i in str(bad.attrs["aria-describedby"]).split()]
        texts = [e.text for e in described if e is not None]
        assert "One spelling per line." in texts and len(texts) == 2


def test_each_editable_group_says_delete_cannot_be_undone_and_ties_it_to_delete(
    tmp_path: Path,
) -> None:
    with area_app(tmp_path / "root") as run:
        markup = page(run)
        for key in EDITABLE:
            hint = by_id(markup, f"list-{key}-delete-hint")
            assert hint is not None and hint.within(group(markup, key)), key
            assert "This can't be undone." in hint.text
            deletes = [
                e
                for e in inside(markup, group(markup, key))
                if e.tag == "button" and str(e.attrs.get("aria-label", "")).startswith("Delete ")
            ]
            assert deletes, key
            for button in deletes:
                assert hint.attrs["id"] in str(button.attrs.get("aria-describedby")).split()
