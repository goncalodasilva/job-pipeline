"""Fictional inputs shared by the gap check and angle tests (M6.5). Nothing here is real.

The requirement ids and fact ids are the ones the fixtures of ``tests/fixtures/ai/gap_check/``
and ``tests/fixtures/ai/propose_angles/`` talk about.
"""

from pathlib import Path

from app.domain.cv_templates import TemplateSpec
from app.domain.documents import DocumentUnit, UnitOrigin
from app.domain.fact_selection import FactRecord, facts_for_ai
from app.domain.facts import FactContent, FactStatus, Privacy
from app.domain.fit import Requirement
from app.domain.settings import PersonalDetails

ROOT = Path(__file__).resolve().parents[1]
JOB_ID = "job-fictional"
HIDDEN = "Confidential fictional Brightwater audit statement."
ROLE = "Operations Analyst at Example Widgets Ltd"

REQUIREMENTS = (
    Requirement("req-1", "must_have", "Five years of route planning"),
    Requirement("req-2", "must_have", "Experience with Terraform"),
    Requirement("req-3", "key", "Fluent English"),
    Requirement("req-4", "nice", "Knowledge of SAP"),
)
POSTING = "Fictional Harbor Kite is hiring a route planner. Five years of route planning."


def record(
    number: int,
    text: str,
    *,
    status: FactStatus = FactStatus.CONFIRMED,
    privacy: Privacy = Privacy.NORMAL,
    current: bool = True,
    role: str | None = None,
    skills: tuple[str, ...] = (),
) -> FactRecord:
    return FactRecord(number, 1, current, status, privacy, FactContent(text=text), role, skills)


FACT_1 = "Planned 60 delivery routes a week with a team of 3."
FACT_2 = "Cut invoice processing time by 35% across 4 warehouses."
FACT_3 = "Speaks English at work every day."
# F1..F3 are sent; the never-use, draft and superseded facts must not be.
RECORDS = (
    record(1, FACT_1, role=ROLE),
    record(2, FACT_2, role=ROLE),
    record(3, FACT_3),
    record(4, HIDDEN, privacy=Privacy.NEVER_USE),
    record(5, "An unconfirmed draft about Kubernetes.", status=FactStatus.DRAFT),
    record(6, "A superseded wording.", current=False),
)
SENT = facts_for_ai(RECORDS)

# What "other applications" earlier chose: the ids the model may reuse.
EARLIER_ANGLE_ID = "angle-earlier-1"

# Every personal value M6.4 can store, made fictional and distinctive.
DETAILS = PersonalDetails(
    name="Zephyrine Quillfeather",
    email="zephyrine.quillfeather@example.test",
    phone="+351 912 345 678",
    location="Nowhereton Fictional Quay",
    links=("https://example.test/zq-profile", "https://example.test/zq-portfolio"),
    photo={"stored_name": "photo-zqphoto01.jpg"},  # type: ignore[arg-type]
)


def personal_values() -> list[str]:
    """Every string a request must never hold, plus the photo's file name."""
    values = [DETAILS.name, DETAILS.email, DETAILS.phone, DETAILS.location, *DETAILS.links]
    assert DETAILS.photo is not None
    values.append(DETAILS.photo.stored_name)
    return [value for value in values if value]


# --- M6.7 tailor_cv ---------------------------------------------------------------------------

OTHER_ROLE = "Junior Planner at Sample Logistics SA"
FACT_7 = "Scheduled 12 vans for the night shift."
FACT_8 = "Audited the accounts of Lakeshore Bank over 2 years."
# F1..F3, F7 and F8 are sent (F8 asks for its name to be withheld); F4..F6 never are.
CV_RECORDS = (
    *RECORDS,
    record(7, FACT_7, role=OTHER_ROLE),
    record(8, FACT_8, privacy=Privacy.GENERALIZE, role=OTHER_ROLE),
)
CV_SENT = facts_for_ai(CV_RECORDS)
CV_REFS = ("F1@1", "F2@1", "F3@1", "F7@1", "F8@1")
TEMPLATE = TemplateSpec.model_validate(
    {
        "sections": [
            {"id": "summary"},
            {"id": "experience", "heading": "Work history"},
            {"id": "education"},
            {"id": "skills"},
            {"id": "languages"},
        ],
        "max_pages": 2,
        "photo_slot": True,
        "details_shown": ["email", "phone", "location", "links"],
        "date_format": "month_year",
        "page_size": "A4",
    }
)
HEADINGS = TEMPLATE.headings  # ("Summary", "Work history", "Education", "Skills", "Languages")
KEPT_TEXT = "Planned 60 delivery routes a week with a team of 3, mostly at night."
KEPT_UNIT = DocumentUnit(
    "unit-kept-1", "content", "experience", ROLE, KEPT_TEXT, UnitOrigin.USER, ("F1@1",)
)
