"""The CV template specification (M6.4; AC-M6.5 groundwork; ADR-106 items 3 and 6; detailed
design "Formats and country templates"): ``app/domain/cv_templates.py``. Pure; every value is
fictional.

Assumed interface (chosen here where the sources left it open; the implementer follows it):

- ``TemplateSpec``: a frozen pydantic model that refuses unknown fields (like the models in
  ``app.domain.settings``), validated from JSON-like data, with these fields and no others:
    * ``sections``: tuple of ``SectionSpec`` in the order they are printed; 1 to 12 entries,
      each ``id`` at most once;
    * ``max_pages``: int 1 to 3;
    * ``photo_slot``: bool, whether the layout has a photo slot;
    * ``details_shown``: tuple of ``DetailId`` values, each at most once (the name is always
      shown);
    * ``date_format``: ``DateFormat``;
    * ``page_size``: ``PageSize``.
- ``SectionSpec``: frozen pydantic model ``(id: SectionId, heading: str | None = None)``.
  ``heading`` is the optional heading label: stripped, 1 to 40 characters, no control
  characters; a blank one is the same as none.
- ``SectionId`` (``StrEnum``), a closed set that includes ``summary``, ``experience``,
  ``education`` and ``skills``. ``DetailId`` (``StrEnum``): ``email``, ``phone``, ``location``,
  ``links`` and nothing else, so a template can never ask for a date of birth, a nationality or
  a marital status (ADR-106 item 3). ``DateFormat`` (``StrEnum``) includes ``month_year``
  (Mar 2024), ``numeric`` (03/2024), ``iso`` (2024-03) and ``year`` (2024); a free pattern is
  refused. ``PageSize`` (``StrEnum``): ``A4`` and ``Letter``.
- ``TemplateSpec.headings`` (property): the heading printed for each section, in order: its
  ``heading`` label, else the default for its id (the id with underscores as spaces, first
  letter capitalised: ``Summary``, ``Experience``, ``Education``, ``Skills``). These are the
  ``template_headings`` the honesty check accepts as structure lines.
- ``TemplateStatus`` (``StrEnum``): ``DRAFT = "draft"``, ``APPROVED = "approved"`` (the values
  0006's CHECK on ``cv_templates.status`` accepts).
- A spec round trips: ``TemplateSpec.model_validate(spec.model_dump(mode="json")) == spec``.
"""

import importlib
from types import ModuleType
from typing import Any

import pytest
from pydantic import ValidationError


def templates() -> ModuleType:
    return importlib.import_module("app.domain.cv_templates")


def valid_spec(**overrides: Any) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "sections": [
            {"id": "summary"},
            {"id": "experience", "heading": "Work history"},
            {"id": "education"},
            {"id": "skills"},
        ],
        "max_pages": 2,
        "photo_slot": False,
        "details_shown": ["email", "phone", "location", "links"],
        "date_format": "month_year",
        "page_size": "A4",
    }
    spec.update(overrides)
    return spec


def build(**overrides: Any) -> Any:
    return templates().TemplateSpec.model_validate(valid_spec(**overrides))


def refused(**overrides: Any) -> set[str]:
    """The fields a refused spec names (the first part of each error's location)."""
    with pytest.raises(ValidationError) as caught:
        build(**overrides)
    return {str(error["loc"][0]) for error in caught.value.errors()}


def test_template_spec_closed_values_and_limits() -> None:
    module = templates()

    spec = build()
    assert [(s.id.value, s.heading) for s in spec.sections] == [
        ("summary", None),
        ("experience", "Work history"),
        ("education", None),
        ("skills", None),
    ]
    assert (spec.max_pages, spec.photo_slot, spec.date_format.value, spec.page_size.value) == (
        2,
        False,
        "month_year",
        "A4",
    )
    assert [d.value for d in spec.details_shown] == ["email", "phone", "location", "links"]

    # Closed sets: section ids, details, date formats and page sizes.
    assert {"summary", "experience", "education", "skills"} <= {s.value for s in module.SectionId}
    assert {d.value for d in module.DetailId} == {"email", "phone", "location", "links"}
    assert {"month_year", "numeric", "iso", "year"} <= {f.value for f in module.DateFormat}
    assert {p.value for p in module.PageSize} == {"A4", "Letter"}
    assert build(page_size="Letter").page_size.value == "Letter"
    assert refused(page_size="A3") == {"page_size"}
    assert refused(page_size="a4") == {"page_size"}
    assert refused(date_format="%d.%m.%Y") == {"date_format"}
    assert refused(date_format="{{ 7 * 7 }}") == {"date_format"}
    assert refused(sections=[{"id": "hobbies_and_beliefs"}]) == {"sections"}
    assert refused(sections=[{"id": "date_of_birth"}]) == {"sections"}
    for forbidden in ("date_of_birth", "nationality", "marital_status", "photo", "name"):
        assert refused(details_shown=["email", forbidden]) == {"details_shown"}

    # Limits: 1 to 3 pages, 1 to 12 sections, each section and detail once.
    assert [build(max_pages=n).max_pages for n in (1, 2, 3)] == [1, 2, 3]
    for pages in (0, 4, -1, 2.5, "many"):
        assert refused(max_pages=pages) == {"max_pages"}
    assert refused(sections=[]) == {"sections"}
    twelve = [{"id": "experience"}] * 12
    assert refused(sections=twelve) == {"sections"}  # repeats, not the count, are the problem
    assert refused(sections=[{"id": "experience"}, {"id": "experience"}]) == {"sections"}
    assert refused(details_shown=["email", "email"]) == {"details_shown"}
    assert build(details_shown=[]).details_shown == ()
    assert build(photo_slot=True).photo_slot is True

    # The spec is a value: no unknown fields, nothing to change afterwards.
    assert refused(custom_css="body { display: none }") == {"custom_css"}
    assert refused(date_of_birth_shown=True) == {"date_of_birth_shown"}
    with pytest.raises(ValidationError):
        spec.max_pages = 3


def test_template_spec_section_count_is_limited_to_twelve() -> None:
    module = templates()
    ids = [s.value for s in module.SectionId]

    if len(ids) >= 13:
        assert refused(sections=[{"id": i} for i in ids[:13]]) == {"sections"}
    assert build(sections=[{"id": i} for i in ids[:12]]).sections  # up to twelve


def test_template_section_must_be_a_known_object() -> None:
    assert refused(sections=["experience"]) == {"sections"}
    assert refused(sections=[{"id": "experience", "css": "x"}]) == {"sections"}
    assert refused(sections=[{"heading": "No id"}]) == {"sections"}


def test_template_heading_labels_are_optional_short_and_clean() -> None:
    def heading(value: Any) -> Any:
        return build(sections=[{"id": "experience", "heading": value}]).sections[0].heading

    assert heading(None) is None
    assert heading("  Work history  ") == "Work history"
    assert heading("") is None
    assert heading("   ") is None
    assert heading("Berufserfahrung — São") == "Berufserfahrung — São"
    assert heading("A" * 40) == "A" * 40
    assert heading("<b>Work</b> & {{x}}") == "<b>Work</b> & {{x}}"  # markup is data, escaped later
    for bad in ("A" * 41, "Work\nhistory", "Work\x00history", "Work\thistory"):
        assert refused(sections=[{"id": "experience", "heading": bad}]) == {"sections"}


def test_template_headings_are_labels_or_defaults_in_order() -> None:
    spec = build()

    assert spec.headings == ("Summary", "Work history", "Education", "Skills")
    assert isinstance(spec.headings, tuple)


def test_template_spec_round_trips_through_json() -> None:
    module = templates()
    spec = build(photo_slot=True, page_size="Letter", date_format="iso", max_pages=3)

    dumped = spec.model_dump(mode="json")

    assert dumped["sections"][1] == {"id": "experience", "heading": "Work history"}
    assert dumped["page_size"] == "Letter" and dumped["date_format"] == "iso"
    assert module.TemplateSpec.model_validate(dumped) == spec
    assert module.TemplateSpec.model_validate_json(spec.model_dump_json()) == spec


def test_template_order_of_sections_is_the_order_given() -> None:
    spec = build(sections=[{"id": "skills"}, {"id": "education"}, {"id": "experience"}])

    assert [s.id.value for s in spec.sections] == ["skills", "education", "experience"]
    assert spec.headings == ("Skills", "Education", "Experience")


def test_template_status_values_match_the_schema() -> None:
    assert {s.value for s in templates().TemplateStatus} == {"draft", "approved"}


@pytest.mark.parametrize(
    "mark",
    [
        "‪",
        "‫",
        "‬",
        "‭",
        "‮",
        "⁦",
        "⁧",
        "⁨",
        "⁩",
        "‎",
        "‏",
        "؜",
    ],
    ids=lambda mark: f"U+{ord(mark):04X}",
)
def test_template_heading_refuses_bidi_override_isolate_and_mark_characters(mark: str) -> None:
    assert refused(sections=[{"id": "experience", "heading": f"Work{mark}history"}]) == {"sections"}


def test_template_heading_allows_a_zero_width_space() -> None:
    spec = build(sections=[{"id": "experience", "heading": "Work​history"}])

    assert spec.sections[0].heading == "Work​history"
