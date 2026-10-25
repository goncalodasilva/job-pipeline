"""The CV template specification (ADR-106 items 3 and 6; detailed design "Formats and country
templates"). Pure validation, no I/O.

A spec is closed data, never code: section ids, details, date formats and page sizes come from
fixed sets, so a template can't ask for a date of birth, a nationality or a marital status, and
a free date pattern or style is refused. Models are frozen and refuse unknown fields.
"""

from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.settings import has_control_characters

_MAX_HEADING = 40
_MAX_SECTIONS = 12


class SectionId(StrEnum):
    SUMMARY = "summary"
    EXPERIENCE = "experience"
    EDUCATION = "education"
    SKILLS = "skills"
    PROJECTS = "projects"
    CERTIFICATIONS = "certifications"
    LANGUAGES = "languages"
    VOLUNTEERING = "volunteering"
    PUBLICATIONS = "publications"
    AWARDS = "awards"
    INTERESTS = "interests"


class DetailId(StrEnum):
    """The personal details a CV header may show; the name is always shown."""

    EMAIL = "email"
    PHONE = "phone"
    LOCATION = "location"
    LINKS = "links"


class DateFormat(StrEnum):
    MONTH_YEAR = "month_year"  # Mar 2024
    NUMERIC = "numeric"  # 03/2024
    ISO = "iso"  # 2024-03
    YEAR = "year"  # 2024


class PageSize(StrEnum):
    A4 = "A4"
    LETTER = "Letter"


class TemplateStatus(StrEnum):
    """The values 0006's CHECK on ``cv_templates.status`` accepts."""

    DRAFT = "draft"
    APPROVED = "approved"


class _Closed(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SectionSpec(_Closed):
    id: SectionId
    heading: str | None = None  # the optional heading label

    @field_validator("heading", mode="after")
    @classmethod
    def _clean_heading(cls, value: str | None) -> str | None:
        cleaned = (value or "").strip()
        if not cleaned:
            return None
        if len(cleaned) > _MAX_HEADING or has_control_characters(cleaned):
            raise ValueError(f"A heading is 1 to {_MAX_HEADING} characters on one line.")
        return cleaned


class TemplateSpec(_Closed):
    sections: Annotated[tuple[SectionSpec, ...], Field(min_length=1, max_length=_MAX_SECTIONS)]
    max_pages: Annotated[int, Field(strict=True, ge=1, le=3)]
    photo_slot: Annotated[bool, Field(strict=True)]
    details_shown: tuple[DetailId, ...]
    date_format: DateFormat
    page_size: PageSize

    @field_validator("sections", mode="after")
    @classmethod
    def _sections_once(cls, value: tuple[SectionSpec, ...]) -> tuple[SectionSpec, ...]:
        if len({section.id for section in value}) != len(value):
            raise ValueError("Each section can appear once.")
        return value

    @field_validator("details_shown", mode="after")
    @classmethod
    def _details_once(cls, value: tuple[DetailId, ...]) -> tuple[DetailId, ...]:
        if len(set(value)) != len(value):
            raise ValueError("Each detail can appear once.")
        return value

    @property
    def headings(self) -> tuple[str, ...]:
        """The heading printed for each section, in order: its label, else the id as words."""
        return tuple(
            section.heading or section.id.value.replace("_", " ").capitalize()
            for section in self.sections
        )
