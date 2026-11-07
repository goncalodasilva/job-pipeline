"""Rebuild the shipped fictional demo: python -m tests.demo_builder. No providers or network."""

import shutil
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.domain.settings import PERSONAL_DETAILS, PersonalDetails
from app.modules.storage import Database
from app.onboarding.registry import m1_registry
from app.services.applications import ApplicationService, ApplicationStatuses
from app.services.data_portability import DataPortabilityService
from app.services.facts import FactInput, FactService
from app.services.roles import RoleService
from app.services.settings import SettingsService
from tests.board_pages import Seeder, stamp_after, ticker
from tests.fakes.clock import FrozenClock
from tests.fakes.ids import SequentialIdFactory
from tests.integration.test_schema_0004 import insert
from tests.integration.test_schema_0006 import template_row
from tests.setup_harness import HARNESS_PROVIDER, seed_workspace


def build_demo(target: Path) -> None:
    clock = FrozenClock(datetime(2026, 11, 7, 9, tzinfo=UTC))
    ids = SequentialIdFactory("demo")
    with tempfile.TemporaryDirectory(prefix="jobpipeline-demo-build-") as temporary:
        folder = Path(temporary)
        seed_workspace(
            folder, clock=clock, provider=HARNESS_PROVIDER, done=[s.id for s in m1_registry().steps]
        )
        with Database.open(folder / "app.db") as db:
            settings = SettingsService(db, clock)
            settings.save(
                PERSONAL_DETAILS,
                PersonalDetails(
                    name="Alex Meadow (Fictional)",
                    email="alex@meadow.example.test",
                    location="Fictional Harbor",
                    links=("https://meadow.example.test",),
                ),
                expected_version=0,
            )
            role = RoleService(db, clock, ids).create(
                employer="Larkspur Fictional Orchards",
                title="Greenhouse Technician",
                start_date="2023-01",
                end_date="2026-06",
            )
            facts = FactService(db, clock, ids)
            for text in (
                "Maintained irrigation systems in 4 fictional greenhouses.",
                "Trained 6 colleagues to inspect irrigation pumps.",
                "Built a Python tool to record weekly water usage.",
                "Coordinated planting schedules with a team of 3.",
            ):
                facts.create(FactInput(text=text, role_id=role.id))
            from app.services.skills import SkillService

            skills = SkillService(db, clock, ids)
            skills.add("Python")
            skills.add("Irrigation")
            apps = ApplicationService(lambda: db, clock, ids)
            statuses = ApplicationStatuses(lambda: db, clock, ids)
            seed = Seeder.make(db, apps, statuses, ticker(clock), stamp_after(clock))
            for i, state in enumerate(
                ("preparing", "applied", "screening", "interviewing", "rejected", "withdrawn")
            ):
                application = seed.application(
                    f"demo-job-{i + 1:02}",
                    f"Greenhouse Technician {i + 1}",
                    f"Fictional Orchard {i + 1}",
                    state,
                )
                if state in ("screening", "interviewing"):
                    apps.add_interview_date(
                        application.id,
                        starts_at=datetime(2026, 11, 7 + (state == "interviewing"), 14),  # noqa: DTZ001 - local wall time API
                        time_zone="UTC",
                    )
            for i, state in enumerate(
                ("new", "seen", "snoozed", "removed", "new", "seen", "removed", "new", "snoozed"), 7
            ):
                seed.job(f"demo-job-{i:02}", f"Irrigation Planner {i}", f"Fictional Orchard {i}")
                with db.transaction() as conn:
                    conn.execute(
                        "UPDATE jobs SET triage_state=?,snooze_until=? WHERE id=?",
                        (
                            state,
                            (clock.now() + timedelta(days=3)).date().isoformat()
                            if state == "snoozed"
                            else None,
                            f"demo-job-{i:02}",
                        ),
                    )
            with db.transaction() as conn:
                insert(
                    conn,
                    "cv_templates",
                    template_row("demo-template", provider="fake", model="fake"),
                )
            source = DataPortabilityService(lambda: db, lambda: folder, clock).export()
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)


if __name__ == "__main__":
    build_demo(Path(__file__).resolve().parents[1] / "app/demo_data/workspace.json")
