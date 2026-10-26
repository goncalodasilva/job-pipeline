"""The AI draft of a country CV template (M6.6; ADR-106 items 3 and 6; spec 3.6). Pure.

``draft_cv_template`` gets a country, the person's style choices and an optional note, and
nothing about the person. Its answer is the closed ``TemplateSpec`` of
``app/domain/cv_templates.py`` plus one short note per field. ``resolve_template_draft``
validates the spec with that model, so an unknown section, detail, date format or page size,
a fourth page or a heading with control or bidi characters is refused (``InvalidModelOutput``),
and checks the notes. The draft carries no status: only the person's approval, in a later
step, makes a template usable.

The person's explicit style choices win: when the style is given, ``max_pages`` and
``photo_slot`` are set from it whatever the model returned (a note in the model's output cannot
turn a choice into another), and a note for a value that changed says so. Everything else is
the model's proposal, which the person reviews.

Refusals never echo the model's values, which can be hostile text.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Final

from pydantic import ValidationError

from app.domain.cv_templates import TemplateSpec
from app.domain.errors import InvalidModelOutput
from app.domain.settings import has_control_characters

# The fields of the spec, in order; each gets one note.
NOTE_FIELDS: Final = (
    "sections",
    "max_pages",
    "photo_slot",
    "details_shown",
    "date_format",
    "page_size",
)
MAX_NOTE: Final = 200
MIN_PAGES: Final = 1
MAX_PAGES: Final = 3


_PHOTO_WORDS: Final = {
    "none": "no photo slot",
    "optional": "a photo slot, filled only if you add a photo",
    "expected": "a photo slot, filled only if you add a photo; photos are customary",
}


class PhotoConvention(StrEnum):
    """What the person wants about a photo: none, a slot used only if they add a photo, or a
    slot the country's CVs customarily have. ``none`` gives ``photo_slot`` false."""

    NONE = "none"
    OPTIONAL = "optional"
    EXPECTED = "expected"


@dataclass(frozen=True)
class TemplateStyle:
    """The style choices sent to the model: the length wanted and the photo convention."""

    pages: int
    photo: PhotoConvention

    def __post_init__(self) -> None:
        if (
            isinstance(self.pages, bool)
            or not isinstance(self.pages, int)
            or not MIN_PAGES <= self.pages <= MAX_PAGES
        ):
            raise ValueError("The style's pages must be a whole number from 1 to 3.")
        try:
            object.__setattr__(self, "photo", PhotoConvention(self.photo))
        except ValueError:
            raise ValueError("The style's photo must be none, optional or expected.") from None

    def to_data(self) -> dict[str, Any]:
        return {"pages": self.pages, "photo": self.photo.value}


@dataclass(frozen=True)
class TemplateDraft:
    """A validated draft: the closed spec and a note per field (in ``NOTE_FIELDS`` order)."""

    spec: TemplateSpec
    notes: Mapping[str, str]

    def to_data(self) -> dict[str, Any]:
        return {"template": self.spec.model_dump(mode="json"), "notes": dict(self.notes)}


def resolve_template_draft(output: object, style: TemplateStyle | None = None) -> TemplateDraft:
    """Check the model's answer (``{"template": {...}, "notes": {...}}``). Raises
    ``InvalidModelOutput`` for a spec the closed model refuses, a missing or extra field or a
    bad note. With ``style``, the person's choices are pinned: ``max_pages`` is ``style.pages``
    and ``photo_slot`` is true unless the photo choice is none; where that changed the model's
    value, the field's note becomes "Set from your choice (...)"."""
    if not isinstance(output, Mapping) or set(output) != {"template", "notes"}:
        raise InvalidModelOutput(detail="template draft: expected a template and notes")
    raw = output["template"]
    if not isinstance(raw, Mapping):
        raise InvalidModelOutput(detail="template draft: the template must be an object")
    try:
        spec = TemplateSpec.model_validate(dict(raw))
    except ValidationError as error:
        raise InvalidModelOutput(detail=_refusal(error)) from None
    notes = _notes(output["notes"])
    if style is not None:
        spec, notes = _pin_style(spec, notes, style)
    return TemplateDraft(spec, MappingProxyType(notes))


def _pin_style(
    spec: TemplateSpec, notes: dict[str, str], style: TemplateStyle
) -> tuple[TemplateSpec, dict[str, str]]:
    """The person's choices win over the model's proposal (and over the person's free-text
    note, which only refines sections, headings and details)."""
    slot = style.photo is not PhotoConvention.NONE
    if spec.max_pages != style.pages:
        word = "page" if style.pages == 1 else "pages"
        notes["max_pages"] = f"Set from your choice ({style.pages} {word})."
    if spec.photo_slot != slot:
        notes["photo_slot"] = f"Set from your choice ({_PHOTO_WORDS[style.photo]})."
    return spec.model_copy(update={"max_pages": style.pages, "photo_slot": slot}), notes


def _refusal(error: ValidationError) -> str:
    fields = sorted(
        {
            str(item["loc"][0])
            for item in error.errors()
            if item["loc"] and str(item["loc"][0]) in TemplateSpec.model_fields
        }
    )
    return "template draft: invalid " + (", ".join(fields) if fields else "template")


def _notes(raw: object) -> dict[str, str]:
    if not isinstance(raw, Mapping) or set(raw) != set(NOTE_FIELDS):
        raise InvalidModelOutput(detail="template draft: one note for each field is required")
    notes: dict[str, str] = {}
    for field in NOTE_FIELDS:
        value = raw[field]
        text = value.strip() if isinstance(value, str) else ""
        if not text or len(text) > MAX_NOTE or has_control_characters(text):
            raise InvalidModelOutput(
                detail=f"template draft: the {field} note must be one short line of text"
            )
        notes[field] = text
    return notes
