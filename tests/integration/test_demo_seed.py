"""M9.5 part C: the demo dataset and how the demo is seeded (AC-M9.4; ADR-049; ADR-131 item 10).

The shipped dataset is an M9.1 export file (``app/demo_data/workspace.json``, rebuilt with
``python -m tests.demo_builder``) and is restored through the importer only: once, into the
folder the app chose for ``--demo`` while it has no content, never into a folder named by
``--data-dir`` or ``JOBPIPELINE_DATA_DIR``. Its dates move so that the day it was built becomes
today, from the injected clock. The AC-M9.4 test runs the real command line in a subprocess
with an audit hook on the real data and settings folders. Every value is fictional.
"""

import json
import re
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import RuntimeConfig
from app.main import create_app
from app.modules.storage import MIGRATIONS_DIR, Database
from app.services.data_portability import PortabilityError
from app.services.demo import ANCHOR, DATASET, STAGING_PREFIX, seed_demo, shifted_dataset
from tests.fakes.clock import FrozenClock
from tests.integration.test_portability import fresh
from tests.running_app import PROJECT_ROOT, isolated_env

pytestmark = pytest.mark.integration

BASE_URL = "http://127.0.0.1:8000"
TODAY = datetime(2027, 3, 10, 8, 30, tzinfo=UTC)
TRIAGE_STATES = {"new", "seen", "snoozed", "removed", "to_apply"}
EMAIL = re.compile(r"[\w.+-]+@([\w-]+(?:\.[\w-]+)+)")
URL = re.compile(r"https?://([^/\s\"'<>\\]+)")


def dataset() -> dict[str, Any]:
    value: dict[str, Any] = json.loads(DATASET.read_bytes())
    return value


def rows(payload: dict[str, Any], table: str) -> list[dict[str, Any]]:
    found = payload["tables"][table]
    return [dict(zip(found["columns"], row, strict=True)) for row in found["rows"]]


def demo_config(folder: Path, *, source: str = "demo") -> RuntimeConfig:
    return RuntimeConfig(
        data_dir=folder,
        config_dir=folder.parent / "settings-folder",
        port=8000,
        log_level="INFO",
        demo=True,
        setup=False,
        no_browser=True,
        data_dir_source=source,  # type: ignore[arg-type]
    )


@contextmanager
def demo_app(
    folder: Path, *, source: str = "demo", clock: datetime = TODAY, **options: Any
) -> Iterator[Any]:
    app = create_app(
        demo_config(folder, source=source), clock=FrozenClock(clock), tz=UTC, **options
    )
    with TestClient(app, base_url=BASE_URL, follow_redirects=False) as client:
        yield app, client


def count(app: Any, table: str) -> int:
    value: int = app.state.workspace.db.connection.execute(
        f"SELECT count(*) FROM {table}"  # noqa: S608 - a fixed table name from this file
    ).fetchone()[0]
    return value


# --- AC-M9.4: the real data and settings folders are never read or written -------------------


def test_ac_m9_4_demo_never_reads_or_writes_real_data_folder(tmp_path: Path) -> None:
    env = isolated_env(
        tmp_path / "unused-data", tmp_path / "unused", allow_fakes=False, explicit_data_dir=False
    )
    env.pop("JOBPIPELINE_CONFIG_DIR")  # the settings folder the app finds by itself
    env["JOBPIPELINE_AUDIT_ROOT"] = str(tmp_path)
    (tmp_path / "tmp").mkdir()
    env["TMPDIR"] = str(tmp_path / "tmp")  # so every write the process makes stays in here
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    command = [sys.executable, "-m", "tests.demo_audit_child"]
    result = subprocess.run(  # noqa: S603 - this interpreter and a module of this project
        command, cwd=PROJECT_ROOT, env=env, capture_output=True, text=True, timeout=120, check=False
    )
    assert result.returncode == 0, result.stderr[-3000:]
    shown = json.loads(result.stdout.strip().splitlines()[-1])
    assert shown["refused"] == [], "the demo touched the real folders or wrote outside the test"
    assert shown["demo_folder"] == "jobpipeline-demo"
    assert shown["jobs"] == len(rows(dataset(), "jobs"))
    assert shown["today"] and shown["demo_note"] and shown["palette"]
    assert shown["pages"] and shown["fragments"], "every page and fragment answered"
    assert shown["exported"] and shown["erased"] and shown["after_erase"]
    # Delete all data empties the demo; it is seeded again only at the next start.
    assert shown["jobs_after_erase"] == 0


def test_demo_uses_demo_keychain_service() -> None:
    from app.logging_config import Redactor
    from app.modules.secrets import ANTHROPIC_API_KEY
    from app.wiring import build_wiring
    from tests.fakes.keyring_backends import MemoryKeyring

    backend = MemoryKeyring()
    wiring = build_wiring(
        demo=True, demo_workspace=True, environ={}, redactor=Redactor(), keyring_backend=backend
    )
    wiring.secrets.set(ANTHROPIC_API_KEY, "fictional-demo-value")
    assert backend.get_password("jobpipeline-demo", ANTHROPIC_API_KEY) == "fictional-demo-value"
    assert backend.get_password("jobpipeline", ANTHROPIC_API_KEY) is None


# --- only the designated demo folder, only while it has no content ---------------------------


def test_demo_seed_refuses_non_demo_workspace(tmp_path: Path) -> None:
    clock = FrozenClock(TODAY)
    with fresh(tmp_path) as db, pytest.raises(ValueError, match="demo"):
        seed_demo(db, data_dir=tmp_path, demo_workspace=False, clock=clock, zone=UTC)
    with fresh(tmp_path / "other") as db, pytest.raises(ValueError, match="demo"):
        seed_demo(db, data_dir=tmp_path / "elsewhere", demo_workspace=True, clock=clock, zone=UTC)

    # --demo with --data-dir or JOBPIPELINE_DATA_DIR: the folder is the user's (ADR-025,
    # ADR-049), so it is never seeded, even when it is empty.
    for source in ("cli", "environment"):
        with demo_app(tmp_path / source, source=source) as (app, _):
            assert count(app, "jobs") == 0 and count(app, "facts") == 0, source


def test_demo_seed_only_once_on_empty_demo_folder(tmp_path: Path) -> None:
    with fresh(tmp_path) as db:
        clock = FrozenClock(TODAY)
        assert seed_demo(db, data_dir=tmp_path, demo_workspace=True, clock=clock, zone=UTC)
        before = db.connection.execute("SELECT id, found_at FROM jobs ORDER BY id").fetchall()
        assert len(before) == len(rows(dataset(), "jobs"))
        clock.advance(timedelta(days=5))
        assert not seed_demo(db, data_dir=tmp_path, demo_workspace=True, clock=clock, zone=UTC)
        after = db.connection.execute("SELECT id, found_at FROM jobs ORDER BY id").fetchall()
        assert after == before

    # The app seeds a new demo folder at start-up, and a restart keeps what the user changed.
    folder = tmp_path / "jobpipeline-demo"
    with demo_app(folder) as (app, client):
        assert count(app, "jobs") == len(rows(dataset(), "jobs"))
        assert client.get("/").status_code == 200
        with app.state.workspace.db.transaction() as conn:
            conn.execute("UPDATE jobs SET notes = 'kept by the user' WHERE id = 'demo-job-01'")
    with demo_app(folder, clock=TODAY + timedelta(days=9)) as (app, _):
        assert count(app, "jobs") == len(rows(dataset(), "jobs"))
        note = app.state.workspace.db.connection.execute(
            "SELECT notes FROM jobs WHERE id = 'demo-job-01'"
        ).fetchone()[0]
        assert note == "kept by the user"


def test_demo_seed_leaves_existing_content_alone(tmp_path: Path) -> None:
    with fresh(tmp_path) as db:
        db.connection.execute(
            "INSERT INTO settings(key,value,version,created_at,updated_at) "
            "VALUES('shortcuts','{}',1,'2026-01-01','2026-01-01')"
        )
        assert not seed_demo(
            db, data_dir=tmp_path, demo_workspace=True, clock=FrozenClock(TODAY), zone=UTC
        )
        assert db.connection.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0


def test_demo_starts_empty_without_a_dataset(tmp_path: Path) -> None:
    """``demo_dataset=None``: the state delete all data leaves in the demo (ADR-138)."""
    app = create_app(demo_config(tmp_path / "jobpipeline-demo"), demo_dataset=None)
    with TestClient(app, base_url=BASE_URL, follow_redirects=False):
        assert count(app, "jobs") == 0


# --- dates ------------------------------------------------------------------------------------


def _all_dates(payload: dict[str, Any]) -> list[str]:
    return [
        value
        for table in payload["tables"].values()
        for row in table["rows"]
        for value in row
        if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}(T.*)?", value)
    ]


def test_demo_dates_shifted_to_today(tmp_path: Path) -> None:
    shipped = dataset()
    moved = json.loads(shifted_dataset(FrozenClock(TODAY), UTC))
    delta = TODAY.date() - ANCHOR
    for before, after in zip(_all_dates(shipped), _all_dates(moved), strict=True):
        if "T" in before:
            assert datetime.fromisoformat(after) - datetime.fromisoformat(before) == delta
            assert after[19:] == before[19:], "the stored form of the time is kept"
        else:
            assert date.fromisoformat(after) - date.fromisoformat(before) == delta
    # Month-only role dates and text are not dates to move.
    assert [r["start_date"] for r in rows(moved, "roles")] == [
        r["start_date"] for r in rows(shipped, "roles")
    ]

    with demo_app(tmp_path / "jobpipeline-demo") as (app, _):
        db: Database = app.state.workspace.db
        found = [r[0] for r in db.connection.execute("SELECT found_at FROM jobs")]
        # Recent but never in the future, whatever the time of day.
        assert (TODAY - timedelta(days=2)).isoformat() <= max(found) <= TODAY.isoformat()
        stamps = [r[0] for r in db.connection.execute("SELECT created_at FROM facts")]
        assert all(value <= TODAY.isoformat() for value in stamps)
        events = [r[0] for r in db.connection.execute("SELECT starts_at FROM events")]
        assert events and all(value[:4] == "2027" for value in events)
        snoozed = [
            r[0] for r in db.connection.execute("SELECT snooze_until FROM jobs WHERE snooze_until")
        ]
        assert snoozed and all(value > TODAY.date().isoformat() for value in snoozed)


def test_demo_dates_follow_the_app_time_zone(tmp_path: Path) -> None:
    """Today is the app's day: late on 10 March in UTC is already 11 March in UTC+10."""
    late = datetime(2027, 3, 10, 23, 30, tzinfo=UTC)
    east = timezone(timedelta(hours=10))
    moved = json.loads(shifted_dataset(FrozenClock(late), east))
    shipped = dataset()
    before = rows(shipped, "events")[0]["starts_at"]
    after = rows(moved, "events")[0]["starts_at"]
    delta = datetime.fromisoformat(after) - datetime.fromisoformat(before)
    assert delta == date(2027, 3, 11) - ANCHOR


def test_a_failed_seed_fails_start_up_and_a_retry_seeds(tmp_path: Path) -> None:
    """A broken dataset stops the demo from starting and leaves nothing behind but the empty
    database; the next start with a sound dataset seeds it (ADR-138 item 5)."""
    folder = tmp_path / "jobpipeline-demo"
    broken = tmp_path / "broken.json"
    broken.write_text('{"format": 1, "format": 1}', encoding="utf-8")  # a duplicate field
    with pytest.raises(PortabilityError), demo_app(folder, demo_dataset=broken):
        pass
    assert not [p for p in folder.iterdir() if p.name.startswith(STAGING_PREFIX)]
    with demo_app(folder) as (app, _):
        assert count(app, "jobs") == len(rows(dataset(), "jobs"))


def test_the_shipped_dataset_passes_the_importer_checks_before_its_dates_move(
    tmp_path: Path,
) -> None:
    doubled = tmp_path / "doubled.json"
    raw = DATASET.read_text(encoding="utf-8")
    doubled.write_text(raw.replace('{"format":1,', '{"format":1,"format":1,', 1))
    assert doubled.read_text(encoding="utf-8") != raw
    with pytest.raises(PortabilityError):
        shifted_dataset(FrozenClock(TODAY), UTC, doubled)


# --- the dataset itself -----------------------------------------------------------------------


def test_demo_dataset_is_fictional() -> None:
    payload = dataset()
    assert payload["format"] == 1
    raw = json.dumps(payload)
    hosts = EMAIL.findall(raw) + URL.findall(raw)
    assert hosts, "the persona has an address and a link"
    assert all(host == "example.test" or host.endswith(".example.test") for host in hosts), hosts
    details = json.loads(
        next(r["value"] for r in rows(payload, "settings") if r["key"] == "personal_details")
    )
    assert "Fictional" in details["name"]
    assert details["phone"] in (None, "") or details["phone"].startswith("+1 555")


def test_demo_dataset_covers_the_journey() -> None:
    """ADR-131 item 10: a persona, roles, facts, skills, about 15 jobs in every triage state,
    companies, applications in every phase, events and one practice CV template."""
    payload = dataset()
    jobs = rows(payload, "jobs")
    assert 13 <= len(jobs) <= 17
    assert {job["triage_state"] for job in jobs} == TRIAGE_STATES
    assert len({job["company_id"] for job in jobs}) >= 6
    assert all(len(job["snapshot_text"] or "") > 200 for job in jobs), "real job text"

    phases = {r["key"]: r["phase"] for r in rows(payload, "application_statuses")}
    applications = rows(payload, "applications")
    assert {phases[a["status_key"]] for a in applications} == {"preparing", "active", "closed"}
    assert len(rows(payload, "events")) >= 2
    assert len(rows(payload, "roles")) >= 2
    facts = rows(payload, "facts")
    assert sum(fact["status"] == "confirmed" for fact in facts) >= 8
    assert len(rows(payload, "skills")) >= 4
    assert len(rows(payload, "cv_templates")) == 1
    assert {r["key"] for r in rows(payload, "settings")} >= {"ai_provider", "personal_details"}
    assert payload["files"] == []


def test_demo_dataset_matches_the_newest_schema() -> None:
    """Rebuild it (``python -m tests.demo_builder``) after every new migration."""
    newest = max(int(path.name[:4]) for path in MIGRATIONS_DIR.glob("[0-9][0-9][0-9][0-9]_*.sql"))
    assert dataset()["schema_version"] == newest


def test_demo_dataset_is_what_the_builder_makes(tmp_path: Path) -> None:
    """The shipped file is the builder's output, so it is never edited by hand."""
    from tests.demo_builder import build_demo

    built = tmp_path / "workspace.json"
    build_demo(built)
    rebuilt = json.loads(built.read_bytes())
    shipped = dataset()
    assert {k: v for k, v in rebuilt.items() if k != "app_version"} == {
        k: v for k, v in shipped.items() if k != "app_version"
    }
