"""Build the shipped fictional demo (M9.5 part C; ADR-131 item 10).

Run ``python -m tests.demo_builder``. It writes a workspace through the app's own services where
they exist (roles, facts, skills,
applications, triage, events) and through the storage layer the AI tasks use for job summaries
(made by the practice provider, labelled as such), then exports it with the M9.1 exporter to
``app/demo_data/workspace.json``. No providers and no network. Everything happens before
``ANCHOR`` (the day the dataset calls today), so after the date shift nothing was made in the
future; interviews and snoozes fall on and after it. Every person, company, address and number
is fictional, and every address is under ``example.test``.
"""

import json
import shutil
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from app.domain.job_normalization import company_key
from app.domain.settings import PERSONAL_DETAILS, PersonalDetails
from app.modules.storage import Database, job_summary_repo, jobs_repo
from app.onboarding.registry import m1_registry
from app.services.applications import ApplicationService
from app.services.data_portability import DataPortabilityService
from app.services.demo import ANCHOR
from app.services.facts import DraftInput, DraftOrigin, FactInput, FactService
from app.services.job_triage import JobTriageService
from app.services.roles import RoleService
from app.services.settings import SettingsService
from app.services.skills import SkillService
from tests.fakes.clock import FrozenClock
from tests.fakes.ids import SequentialIdFactory
from tests.integration.test_schema_0004 import insert
from tests.integration.test_schema_0006 import template_row
from tests.setup_harness import HARNESS_PROVIDER, seed_workspace

TARGET = Path(__file__).resolve().parents[1] / "app" / "demo_data" / "workspace.json"
START = datetime.combine(ANCHOR - timedelta(days=20), time(9), tzinfo=UTC)
UTC_ZONE = ZoneInfo("UTC")
SKILLS = ("Python", "Irrigation", "Data logging", "Team training", "Scheduling", "Spreadsheets")


@dataclass(frozen=True)
class Company:
    name: str
    slug: str
    industry: str
    size: str
    town: str


COMPANIES = {
    "brightwater": Company(
        "Brightwater Fictional Greenhouses",
        "brightwater",
        "Agriculture",
        "51-200",
        "Fictional Harbor",
    ),
    "fernhill": Company(
        "Fernhill Fictional Growers", "fernhill", "Agriculture", "11-50", "Fernhill"
    ),
    "juniper": Company(
        "Juniper Fictional Agritech", "juniper-agritech", "Technology", "51-200", "Fictional Harbor"
    ),
    "saltmarsh": Company(
        "Saltmarsh Fictional Water Co", "saltmarsh-water", "Energy", "201-500", "Saltmarsh"
    ),
    "copperleaf": Company(
        "Copperleaf Fictional Foods", "copperleaf", "Manufacturing", "501-1000", "Copperleaf Bay"
    ),
    "mistral": Company(
        "Mistral Fictional Vertical Farms",
        "mistral-farms",
        "Agriculture",
        "11-50",
        "Fictional Harbor",
    ),
    "harborkite": Company(
        "Harbor Kite Fictional Logistics", "harbor-kite", "Logistics", "201-500", "Fictional Harbor"
    ),
    "quillwort": Company(
        "Quillwort Fictional Labs", "quillwort", "Education", "11-50", "Quillwort"
    ),
    "thistle": Company(
        "Thistle Fictional Cooperative", "thistle-coop", "Agriculture", "11-50", "Thistle Vale"
    ),
}


@dataclass(frozen=True)
class Posting:
    """One fictional job: what its posting says and what its summary found."""

    number: int
    title: str
    company: str
    team: str
    work_mode: str
    seniority: str
    salary: tuple[int, int] | None
    duties: tuple[str, ...]
    needs: tuple[tuple[str, str], ...]  # (kind, text): must_have, key or nice
    summarized: bool = True

    @property
    def id(self) -> str:
        return f"demo-job-{self.number:02d}"

    def text(self) -> str:
        where = COMPANIES[self.company]
        mode = {"remote": "Remote", "hybrid": "Hybrid", "on_site": "On site"}[self.work_mode]
        pay = (
            f"Salary {self.salary[0]:,} to {self.salary[1]:,} EUR a year."
            if self.salary
            else "Salary to be discussed."
        )
        lines = [
            f"{where.name} is hiring a {self.title} for its {self.team} team in {where.town}.",
            "",
            "What you will do:",
            *(f"- {duty}" for duty in self.duties),
            "",
            "What we look for:",
            *(f"- {text}" for _, text in self.needs),
            "",
            f"{mode}, full time. {pay}",
            "This posting is fictional: it belongs to the Job Search Pipeline demo.",
        ]
        return "\n".join(lines)

    def summary(self) -> dict[str, object]:
        where = COMPANIES[self.company]
        return {
            "title": self.title,
            "company": where.name,
            "industry": where.industry,
            "department": self.team,
            "work_mode": self.work_mode,
            "employment_type": "full_time",
            "seniority": self.seniority,
            "experience_text": None,
            "salary_currency": "EUR" if self.salary else None,
            "salary_period": "year" if self.salary else None,
            "posted_date": None,
            "summary": f"{self.title} in the {self.team} team at {where.name}: "
            + "; ".join(duty[0].lower() + duty[1:] for duty in self.duties[:2])
            + ".",
            "locations": [where.town],
            "experience_min": None,
            "experience_max": None,
            "salary_min": self.salary[0] if self.salary else None,
            "salary_max": self.salary[1] if self.salary else None,
            "requirements": [{"kind": kind, "text": text} for kind, text in self.needs],
        }


POSTINGS = (
    Posting(
        1,
        "Irrigation Technician",
        "brightwater",
        "Growing",
        "on_site",
        "mid",
        (34000, 39000),
        (
            "Keep the drip irrigation of six glasshouses running",
            "Log water use and fix leaks before they spread",
            "Train seasonal staff to check pumps safely",
        ),
        (
            ("must_have", "Two years of hands-on irrigation work"),
            ("key", "Comfortable logging readings in a spreadsheet"),
            ("nice", "Some Python for small tools"),
        ),
    ),
    Posting(
        2,
        "Greenhouse Data Assistant",
        "juniper",
        "Field Data",
        "hybrid",
        "junior",
        (31000, 35000),
        (
            "Collect sensor readings from partner greenhouses",
            "Clean the weekly data and share short reports",
            "Help growers read their dashboards",
        ),
        (
            ("must_have", "Experience with data logging in the field"),
            ("key", "Python for cleaning data"),
            ("nice", "Knowledge of irrigation systems"),
        ),
    ),
    Posting(
        3,
        "Horticulture Technician",
        "fernhill",
        "Nursery",
        "on_site",
        "mid",
        (30000, 34000),
        (
            "Plan planting for three crop cycles a year",
            "Keep the nursery's irrigation and climate in check",
            "Record growth and losses for every batch",
        ),
        (
            ("must_have", "Experience planning planting schedules"),
            ("key", "Practical irrigation skills"),
            ("nice", "Training new nursery staff"),
        ),
    ),
    Posting(
        4,
        "Water Systems Technician",
        "saltmarsh",
        "Network Maintenance",
        "on_site",
        "mid",
        (38000, 44000),
        (
            "Inspect and repair pumps across the rural network",
            "Plan maintenance rounds with two colleagues",
            "Keep the asset log up to date",
        ),
        (
            ("must_have", "Pump maintenance experience"),
            ("key", "Able to plan your own rounds"),
            ("nice", "Data logging with sensors"),
        ),
    ),
    Posting(
        5,
        "Crop Planning Coordinator",
        "copperleaf",
        "Supply",
        "hybrid",
        "mid",
        (36000, 41000),
        (
            "Agree planting and harvest dates with twelve partner farms",
            "Turn the forecast into weekly plans",
            "Report on what was planted and picked",
        ),
        (
            ("must_have", "Scheduling across several teams"),
            ("key", "Strong spreadsheet skills"),
            ("nice", "Experience on a farm or in a nursery"),
        ),
    ),
    Posting(
        6,
        "Vertical Farm Operator",
        "mistral",
        "Operations",
        "on_site",
        "junior",
        (29000, 32000),
        (
            "Run the daily seeding and harvest routine",
            "Watch nutrient and water levels on the racks",
            "Report faults to the engineering team",
        ),
        (
            ("must_have", "Experience with irrigation or hydroponics"),
            ("key", "Careful record keeping"),
        ),
    ),
    Posting(
        7,
        "Irrigation Planner",
        "thistle",
        "Member Services",
        "hybrid",
        "mid",
        (35000, 40000),
        (
            "Design irrigation plans for member farms",
            "Estimate water use per field and season",
            "Run workshops on saving water",
        ),
        (
            ("must_have", "Irrigation design or maintenance experience"),
            ("key", "Explaining technical topics to growers"),
            ("nice", "Python or another scripting language"),
        ),
    ),
    Posting(
        8,
        "Agritech Field Support Specialist",
        "juniper",
        "Customer Support",
        "remote",
        "mid",
        (37000, 42000),
        (
            "Help growers set up soil and water sensors",
            "Solve support questions by video and phone",
            "Write short how-to guides",
        ),
        (
            ("must_have", "Experience with field sensors"),
            ("key", "Clear written English"),
        ),
        summarized=False,
    ),
    Posting(
        9,
        "Greenhouse Supervisor",
        "brightwater",
        "Growing",
        "on_site",
        "senior",
        (42000, 48000),
        (
            "Lead a team of eight growers and technicians",
            "Plan the weekly work and the holiday rota",
            "Own safety checks for the glasshouses",
        ),
        (
            ("must_have", "Experience leading a team"),
            ("key", "Greenhouse growing experience"),
        ),
        summarized=False,
    ),
    Posting(
        10,
        "Plant Research Assistant",
        "quillwort",
        "Trials",
        "on_site",
        "junior",
        (28000, 31000),
        (
            "Set up and water small plant trials",
            "Measure and record growth twice a week",
            "Keep the trial greenhouse tidy and safe",
        ),
        (
            ("must_have", "Careful measurement and record keeping"),
            ("nice", "Spreadsheet skills"),
        ),
    ),
    Posting(
        11,
        "Fresh Produce Logistics Planner",
        "harborkite",
        "Planning",
        "hybrid",
        "mid",
        (36000, 40000),
        (
            "Plan daily pick-ups from farms to the depot",
            "Agree delivery slots with markets",
            "Track late loads and their causes",
        ),
        (
            ("must_have", "Scheduling deliveries or rounds"),
            ("key", "Spreadsheets"),
        ),
    ),
    Posting(
        12,
        "Senior Water Engineer",
        "saltmarsh",
        "Engineering",
        "hybrid",
        "senior",
        (55000, 63000),
        (
            "Design upgrades to pumping stations",
            "Lead technicians on site",
            "Sign off maintenance plans",
        ),
        (
            ("must_have", "An engineering degree"),
            ("must_have", "Five years designing water systems"),
            ("key", "Leading technicians"),
        ),
    ),
    Posting(
        13,
        "Head of Cultivation",
        "mistral",
        "Leadership",
        "on_site",
        "lead",
        (65000, 72000),
        (
            "Set the cultivation strategy for two farms",
            "Lead a team of twenty",
            "Own the yield and quality targets",
        ),
        (
            ("must_have", "Ten years in commercial growing"),
            ("must_have", "Experience managing managers"),
        ),
    ),
    Posting(
        14,
        "Warehouse Operative",
        "harborkite",
        "Depot",
        "on_site",
        "entry",
        (24000, 26000),
        (
            "Load and unload chilled produce",
            "Pick orders at night",
            "Keep the cold store clean",
        ),
        (("must_have", "Available for night shifts"),),
    ),
    Posting(
        15,
        "Junior Gardener",
        "thistle",
        "Grounds",
        "on_site",
        "entry",
        None,
        (
            "Mow and weed the cooperative's grounds",
            "Water the shared beds in summer",
        ),
        (("nice", "Interest in gardening"),),
    ),
)

APPLICATIONS = (
    (1, "preparing"),
    (2, "applied"),
    (3, "screening"),
    (4, "interviewing"),
    (5, "rejected"),
    (6, "withdrawn"),
)
SEEN = (10, 11)
SNOOZED = (12,)
REMOVED = (
    (13, "too_senior", None),
    (14, "not_interested", "Night shifts only."),
    (15, "too_junior", None),
)
# The order the jobs were found in: the applied-for ones first, the three New ones last.
FOUND_ORDER = (1, 2, 3, 4, 5, 6, 10, 11, 12, 13, 14, 15, 7, 8, 9)
FOUND_EVERY = timedelta(hours=24)  # with the applications' hours: twenty days in all

ROLES = (
    ("Larkspur Fictional Orchards", "Greenhouse Technician", "2023-01", "2026-06"),
    ("Juniper Fictional Farms", "Field Assistant", "2020-03", "2022-12"),
)
FACTS = (
    (
        0,
        "Maintained the drip irrigation of 4 greenhouses and cut their water use by 18% "
        "over one season.",
        ("Irrigation",),
    ),
    (
        0,
        "Built a Python script that logs weekly water readings from 32 sensors into a shared "
        "spreadsheet.",
        ("Python", "Data logging", "Spreadsheets"),
    ),
    (0, "Trained 6 new colleagues to inspect and service irrigation pumps.", ("Team training",)),
    (0, "Planned planting for 3 crop cycles a year with a team of 3.", ("Scheduling",)),
    (
        0,
        "Replaced 120 faulty emitters during a two-week shutdown without losing a harvest day.",
        ("Irrigation",),
    ),
    (
        0,
        "Wrote the greenhouse's first pump maintenance checklist, now used on every shift.",
        ("Team training",),
    ),
    (
        1,
        "Recorded soil moisture at 40 field points every morning for two seasons.",
        ("Data logging",),
    ),
    (1, "Moved the farm's harvest log from paper to a spreadsheet shared by 5 people.", ()),
    (1, "Coordinated delivery slots with 3 local markets during the summer harvest.", ()),
    (1, "Repaired 2 km of field irrigation hose after a storm, with one other assistant.", ()),
)
DRAFT_FACT = "Helped set up a rainwater tank for a community garden."


def _job(conn, posting: Posting, now: str) -> None:
    where = COMPANIES[posting.company]
    key = company_key(where.name)
    row = conn.execute("SELECT id FROM companies WHERE name_key = ?", (key,)).fetchone()
    company_id = row[0] if row else f"demo-company-{posting.company}"
    if row is None:
        jobs_repo.insert_company(conn, id=company_id, name=where.name, name_key=key, now=now)
        conn.execute(
            "UPDATE companies SET website = ?, industry = ?, size = ?, locations = ? WHERE id = ?",
            (
                f"https://{where.slug}.example.test",
                where.industry,
                where.size,
                json.dumps([where.town]),
                company_id,
            ),
        )
    jobs_repo.insert_job(
        conn,
        id=posting.id,
        title=posting.title,
        snapshot_text=posting.text(),
        company_id=company_id,
        captured_at=now,
        found_at=now,
        now=now,
    )


def _version(db: Database, job_id: str) -> int:
    with db.read() as conn:
        stored = jobs_repo.get_job(conn, job_id)
    assert stored is not None
    return stored.version


def build_demo(target: Path = TARGET) -> None:
    clock = FrozenClock(START)
    ids = SequentialIdFactory("demo")
    with tempfile.TemporaryDirectory(prefix="jobpipeline-demo-build-") as temporary:
        folder = Path(temporary)
        seed_workspace(
            folder, clock=clock, provider=HARNESS_PROVIDER, done=[s.id for s in m1_registry().steps]
        )
        with Database.open(folder / "app.db") as db:
            SettingsService(db, clock).save(
                PERSONAL_DETAILS,
                PersonalDetails(
                    name="Alex Meadow (Fictional)",
                    email="alex@meadow.example.test",
                    location="Fictional Harbor",
                    links=("https://meadow.example.test",),
                ),
                expected_version=0,
            )
            skills = SkillService(db, clock, ids)
            for name in SKILLS:
                skills.add(name)
            roles = [
                RoleService(db, clock, ids).create(
                    employer=employer, title=title, start_date=start, end_date=end
                )
                for employer, title, start, end in ROLES
            ]
            facts = FactService(db, clock, ids)
            for role, text, fact_skills in FACTS:
                clock.advance(timedelta(minutes=7))
                facts.create(FactInput(text=text, role_id=roles[role].id, skills=fact_skills))
            facts.create_drafts(
                DraftOrigin(source="cv_import", provider="fake", model="fake-thorough"),
                [DraftInput(text=DRAFT_FACT, original_answer=DRAFT_FACT, needs_check=True)],
            )

            # Jobs arrive over the twenty days before the anchor day; most are summarized. A job
            # applied for moves along the board in the hours after it was found.
            applications = ApplicationService(lambda: db, clock, ids)
            statuses = dict(APPLICATIONS)
            for number in FOUND_ORDER:
                posting = POSTINGS[number - 1]
                clock.advance(FOUND_EVERY)
                now = clock.now().isoformat()
                with db.transaction() as conn:
                    _job(conn, posting, now)
                    if posting.summarized:
                        job_summary_repo.store(
                            conn,
                            job_id=posting.id,
                            task_id=None,  # type: ignore[arg-type] - made here, not by a task
                            data=posting.summary(),
                            summary_id=f"demo-summary-{posting.number:02d}",
                            provider="fake",
                            model="fake-thorough",
                            prompt_id="summarize_job",
                            prompt_version="1",
                            ids=ids,
                            now=now,
                        )
                if number in statuses:
                    _apply(applications, db, clock, posting.id, statuses[number])
            assert clock.now().date() < ANCHOR, "everything is made before the anchor day"

            triage = JobTriageService(lambda: db, clock, UTC_ZONE)
            for number in SEEN + SNOOZED:
                clock.advance(timedelta(minutes=3))
                job_id = POSTINGS[number - 1].id
                triage.mark_seen(job_id, expected_version=_version(db, job_id))
            for number in SNOOZED:
                job_id = POSTINGS[number - 1].id
                triage.snooze(
                    job_id,
                    expected_version=_version(db, job_id),
                    choice="date",
                    picked=(ANCHOR + timedelta(days=3)).isoformat(),
                )
            for number, reason, text in REMOVED:
                clock.advance(timedelta(minutes=3))
                job_id = POSTINGS[number - 1].id
                triage.mark_seen(job_id, expected_version=_version(db, job_id))
                triage.remove(
                    job_id, expected_version=_version(db, job_id), reason=reason, text=text
                )

            with db.transaction() as conn:
                insert(
                    conn,
                    "cv_templates",
                    template_row("demo-template", provider="fake", model="fake"),
                )
            assert clock.now().date() < ANCHOR, "everything is made before the anchor day"
            source = DataPortabilityService(lambda: db, lambda: folder, clock).export()
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)


def _apply(
    applications: ApplicationService, db: Database, clock: FrozenClock, job_id: str, status: str
) -> None:
    """Apply for the job, then move the application to ``status`` over the next hours."""
    clock.advance(timedelta(hours=2))
    made = applications.apply_to_job(job_id, expected_job_version=_version(db, job_id)).application
    for step in _path_to(status):
        clock.advance(timedelta(hours=6))
        made = applications.move(
            made.id, step, expected_version=made.version, source="board"
        ).application
    if status == "screening":
        applications.add_interview_date(
            made.id,
            starts_at=datetime.combine(ANCHOR, time(15, 30)),
            time_zone="UTC",
            duration_minutes=30,
        )
    if status == "interviewing":
        applications.add_interview_date(
            made.id,
            starts_at=datetime.combine(ANCHOR + timedelta(days=2), time(10)),
            time_zone="UTC",
        )


def _path_to(status: str) -> tuple[str, ...]:
    """The moves from Preparing to ``status``, as a person would make them on the board."""
    return {
        "preparing": (),
        "applied": ("applied",),
        "screening": ("applied", "screening"),
        "interviewing": ("applied", "screening", "interviewing"),
        "rejected": ("applied", "screening", "rejected"),
        "withdrawn": ("applied", "withdrawn"),
    }[status]


if __name__ == "__main__":
    build_demo()
