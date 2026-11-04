"""The fixed list of removal reasons (M9.2 part D; ADR-131 item 6; spec 3.6, the
``group_removal_reasons`` row; M9.4 reuses it).

A job removed from the inbox can carry one of these reasons (``jobs.removal_reason``, whose CHECK
list in migration 0004 holds exactly these keys, in this order; a test reads the real schema and
compares). ``maps_to`` names the job preference fields the reason speaks about, which is what
M9.4 uses to suggest a preference change. A reason that speaks about none has an empty tuple.
Pure Python.
"""

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True)
class RemovalReason:
    key: str
    label: str  # plain words, as the chips show them
    maps_to: tuple[str, ...]  # preference fields: seniority, location, work_mode, ...


REMOVAL_REASONS: Final[tuple[RemovalReason, ...]] = (
    RemovalReason("too_junior", "Too junior", ("seniority",)),
    RemovalReason("too_senior", "Too senior", ("seniority",)),
    RemovalReason("location", "Location", ("location", "work_mode")),
    RemovalReason("salary", "Salary", ("minimum_salary",)),
    RemovalReason("industry", "Industry", ("industry",)),
    RemovalReason("company", "Company", ("company",)),
    RemovalReason("not_interested", "Not interested", ()),
    RemovalReason("other", "Other", ()),
)

# The preference fields in words, for the Settings list.
PREFERENCE_FIELD_LABELS: Final[dict[str, str]] = {
    "seniority": "Seniority",
    "location": "Location",
    "work_mode": "Work mode",
    "minimum_salary": "Minimum salary",
    "industry": "Industry",
    "company": "Company",
}

NO_MAPPING: Final = "No preference field"


def mapping_words(reason: RemovalReason) -> str:
    """The preference fields a reason speaks about, in words ("Location and work mode"), or
    ``NO_MAPPING`` when it speaks about none."""
    names = [PREFERENCE_FIELD_LABELS[field] for field in reason.maps_to]
    if not names:
        return NO_MAPPING
    return " and ".join([names[0], *(name.lower() for name in names[1:])])
