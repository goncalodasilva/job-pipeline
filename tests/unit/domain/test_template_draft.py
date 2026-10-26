"""M6.6 ``resolve_template_draft``: the model's template answer checked with the closed
``TemplateSpec`` (ADR-106 items 3 and 6). Pure; nothing here is real data."""

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from app.domain.cv_templates import DateFormat, DetailId, PageSize, SectionId, TemplateSpec
from app.domain.errors import InvalidModelOutput
from app.domain.template_draft import (
    MAX_NOTE,
    NOTE_FIELDS,
    PhotoConvention,
    TemplateDraft,
    TemplateStyle,
    resolve_template_draft,
)

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "ai" / "draft_cv_template"


def fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))["data"]


def changed(**template: Any) -> dict[str, Any]:
    data = copy.deepcopy(fixture("ok"))
    data["template"].update(template)
    return data


def test_ok_output_resolves_to_a_spec_and_notes() -> None:
    draft = resolve_template_draft(fixture("ok"))

    assert isinstance(draft, TemplateDraft)
    assert isinstance(draft.spec, TemplateSpec)
    assert [section.id for section in draft.spec.sections] == [
        SectionId.SUMMARY,
        SectionId.EXPERIENCE,
        SectionId.EDUCATION,
        SectionId.SKILLS,
        SectionId.LANGUAGES,
    ]
    assert draft.spec.headings == ("Profile", "Experience", "Education", "Skills", "Languages")
    assert draft.spec.max_pages == 2
    assert draft.spec.photo_slot is True
    assert draft.spec.details_shown == (
        DetailId.EMAIL,
        DetailId.PHONE,
        DetailId.LOCATION,
        DetailId.LINKS,
    )
    assert draft.spec.date_format is DateFormat.MONTH_YEAR
    assert draft.spec.page_size is PageSize.A4
    assert tuple(draft.notes) == NOTE_FIELDS
    assert all(draft.notes[field] for field in NOTE_FIELDS)


def test_draft_has_no_status_or_approval_field() -> None:
    draft = resolve_template_draft(fixture("ok"))

    assert set(TemplateDraft.__dataclass_fields__) == {"spec", "notes"}
    assert set(draft.to_data()) == {"template", "notes"}
    assert "status" not in draft.to_data()["template"]


def test_to_data_round_trips_through_the_spec() -> None:
    data = resolve_template_draft(fixture("photo_expected")).to_data()

    assert TemplateSpec.model_validate(data["template"]).page_size is PageSize.LETTER
    assert data["template"]["photo_slot"] is True
    assert list(data["notes"]) == list(NOTE_FIELDS)
    json.dumps(data)  # plain data


def test_blank_heading_means_the_default_one() -> None:
    data = changed(sections=[{"id": "experience", "heading": "   "}, {"id": "skills"}])

    assert resolve_template_draft(data).spec.headings == ("Experience", "Skills")


@pytest.mark.parametrize(
    "template",
    [
        {"max_pages": 4},
        {"max_pages": 0},
        {"max_pages": "2"},
        {"max_pages": True},
        {"photo_slot": "yes"},
        {"photo_slot": None},
        {"page_size": "A3"},
        {"date_format": "dd.mm.yyyy"},
        {"date_format": "%Y-%m"},
        {"details_shown": ["email", "date_of_birth"]},
        {"details_shown": ["nationality"]},
        {"details_shown": ["email", "email"]},
        {"sections": []},
        {"sections": [{"id": "references"}]},
        {"sections": [{"id": "summary"}, {"id": "summary"}]},
        {"sections": [{"id": "summary", "approved": True}]},
        {"sections": [{"id": s.value} for s in SectionId] + [{"id": "summary"}]},
        {"sections": [{"id": "summary", "heading": "x" * 41}]},
        {"sections": [{"id": "summary", "heading": "Two\nlines"}]},
        {"sections": [{"id": "summary", "heading": "Work ‮evil‬"}]},
        {"sections": [{"id": "summary", "heading": "Tab\there"}]},
        {"sections": ["summary"]},
        {"status": "approved"},
    ],
)
def test_invalid_values_are_refused(template: dict[str, Any]) -> None:
    with pytest.raises(InvalidModelOutput):
        resolve_template_draft(changed(**template))


def test_a_missing_template_field_is_refused() -> None:
    data = fixture("ok")
    del data["template"]["page_size"]

    with pytest.raises(InvalidModelOutput):
        resolve_template_draft(data)


@pytest.mark.parametrize("name", ["invalid_value", "duplicate_section", "hostile_heading"])
def test_adversarial_fixtures_are_refused(name: str) -> None:
    with pytest.raises(InvalidModelOutput):
        resolve_template_draft(fixture(name))


def test_refusal_does_not_echo_the_model_values() -> None:
    with pytest.raises(InvalidModelOutput) as caught:
        resolve_template_draft(fixture("hostile_heading"))

    assert "evil" not in str(caught.value)
    assert "‮" not in str(caught.value)
    assert "sections" in str(caught.value)  # it does say which field


@pytest.mark.parametrize(
    "garbage",
    [
        {},
        [],
        "x",
        None,
        {"template": "x", "notes": {}},
        {"template": [], "notes": {}},
        {"notes": {}},
        {"template": {}},
    ],
)
def test_malformed_shapes_are_refused(garbage: Any) -> None:
    with pytest.raises(InvalidModelOutput):
        resolve_template_draft(garbage)


def test_an_extra_top_level_field_is_refused() -> None:
    with pytest.raises(InvalidModelOutput):
        resolve_template_draft(fixture("injection_note"))


def test_notes_must_cover_every_field_with_short_one_line_text() -> None:
    def with_notes(**notes: Any) -> dict[str, Any]:
        data = fixture("ok")
        data["notes"].update(notes)
        return data

    for bad in (
        {"page_size": ""},
        {"page_size": "  "},
        {"page_size": "x" * (MAX_NOTE + 1)},
        {"page_size": "two\nlines"},
        {"page_size": "bidi ‮ text"},
        {"page_size": 3},
        {"unknown_field": "text"},
    ):
        with pytest.raises(InvalidModelOutput):
            resolve_template_draft(with_notes(**bad))
    data = fixture("ok")
    del data["notes"]["photo_slot"]
    with pytest.raises(InvalidModelOutput):
        resolve_template_draft(data)
    data["notes"] = "not an object"
    with pytest.raises(InvalidModelOutput):
        resolve_template_draft(data)


def test_notes_are_trimmed() -> None:
    data = fixture("ok")
    data["notes"]["page_size"] = "  A4 here.  "

    assert resolve_template_draft(data).notes["page_size"] == "A4 here."


def test_input_is_not_changed_and_the_draft_is_frozen() -> None:
    data = fixture("ok")
    before = copy.deepcopy(data)

    draft = resolve_template_draft(data)

    assert data == before
    with pytest.raises(AttributeError):
        draft.spec = draft.spec  # type: ignore[misc]
    with pytest.raises(TypeError):
        draft.notes["page_size"] = "other"  # type: ignore[index]


# --- the style the person chooses -------------------------------------------------------------


def test_style_is_a_small_closed_set() -> None:
    assert [item.value for item in PhotoConvention] == ["none", "optional", "expected"]
    style = TemplateStyle(pages=2, photo=PhotoConvention.OPTIONAL)
    assert style.to_data() == {"pages": 2, "photo": "optional"}


@pytest.mark.parametrize("pages", [0, 4, -1, True, 2.0, "2", None])
def test_style_pages_are_one_to_three_whole_numbers(pages: Any) -> None:
    with pytest.raises(ValueError, match="pages"):
        TemplateStyle(pages=pages, photo=PhotoConvention.NONE)


def test_style_photo_must_be_a_convention() -> None:
    with pytest.raises(ValueError, match="photo"):
        TemplateStyle(pages=1, photo="mandatory")  # type: ignore[arg-type]
    assert TemplateStyle(pages=1, photo="expected").photo is PhotoConvention.EXPECTED  # type: ignore[arg-type]


# --- the person's choices are pinned ----------------------------------------------------------


def test_without_a_style_the_models_pages_and_photo_are_kept() -> None:
    draft = resolve_template_draft(fixture("injection_obeyed"))

    assert draft.spec.max_pages == 3 and draft.spec.photo_slot is False


@pytest.mark.parametrize("photo", list(PhotoConvention))
@pytest.mark.parametrize("pages", [1, 2, 3])
def test_style_pins_pages_and_photo_slot_whatever_the_model_returned(
    pages: int, photo: PhotoConvention
) -> None:
    style = TemplateStyle(pages=pages, photo=photo)

    for name in ("ok", "photo_expected", "injection_obeyed"):
        draft = resolve_template_draft(fixture(name), style)
        assert draft.spec.max_pages == pages, name
        assert draft.spec.photo_slot is (photo is not PhotoConvention.NONE), name
        assert tuple(draft.notes) == NOTE_FIELDS


def test_a_changed_value_gets_a_deterministic_note_and_an_unchanged_one_keeps_the_models() -> None:
    data = fixture("injection_obeyed")  # three pages, no slot

    changed_both = resolve_template_draft(data, TemplateStyle(1, PhotoConvention.EXPECTED))
    assert changed_both.notes["max_pages"] == "Set from your choice (1 page)."
    assert changed_both.notes["photo_slot"].startswith("Set from your choice (a photo slot")
    assert changed_both.notes["sections"] == data["notes"]["sections"]

    same = resolve_template_draft(data, TemplateStyle(3, PhotoConvention.NONE))
    assert same.notes == resolve_template_draft(data).notes
    assert all(len(note) <= MAX_NOTE for note in changed_both.notes.values())


def test_pinning_does_not_change_the_other_fields_or_the_input() -> None:
    data = fixture("photo_expected")
    before = copy.deepcopy(data)
    plain = resolve_template_draft(data)

    pinned = resolve_template_draft(data, TemplateStyle(3, PhotoConvention.NONE))

    assert data == before
    assert pinned.spec.sections == plain.spec.sections
    assert pinned.spec.details_shown == plain.spec.details_shown
    assert pinned.spec.date_format is plain.spec.date_format
    assert pinned.spec.page_size is plain.spec.page_size
    assert pinned.spec.photo_slot is False and plain.spec.photo_slot is True


def test_photo_expected_fixture_note_agrees_with_its_slot() -> None:
    notes = fixture("photo_expected")["notes"]

    assert fixture("photo_expected")["template"]["photo_slot"] is True
    assert "No photo slot" not in notes["photo_slot"]
