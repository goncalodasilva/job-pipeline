"""The shipped demo is an importer input, never an alternate write path (AC-M9.4)."""

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta

import pytest

from app.config import RuntimeConfig
from tests.fakes.clock import FrozenClock
from tests.integration.test_portability import fresh
from tests.running_app import PROJECT_ROOT, isolated_env

pytestmark = pytest.mark.integration


def test_ac_m9_4_demo_never_reads_or_writes_real_data_folder(tmp_path):
    env = isolated_env(
        tmp_path / "data", tmp_path / "config", allow_fakes=False, explicit_data_dir=False
    )
    # A malformed real bootstrap must never even be opened by the demo.
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.toml").write_text("malformed = [")
    result = subprocess.run(
        [sys.executable, "-m", "tests.demo_audit_child"],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=40,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"accesses": [], "jobs": 15}


def test_demo_uses_demo_keychain_service():
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


def config(folder, *, source="demo"):
    return RuntimeConfig(
        data_dir=folder,
        config_dir=folder.parent / "real-config",
        port=8000,
        log_level="INFO",
        demo=True,
        setup=False,
        no_browser=True,
        data_dir_source=source,
    )


def test_demo_seed_refuses_non_demo_workspace(tmp_path):
    from app.services.demo import seed_demo

    with fresh(tmp_path) as db, pytest.raises(ValueError, match="demo"):
        seed_demo(
            config(db.path.parent, source="cli"),
            db,
            FrozenClock(datetime(2026, 11, 7, tzinfo=UTC)),
        )


def test_demo_seed_only_once_on_empty_demo_folder(tmp_path):
    from app.services.demo import seed_demo

    with fresh(tmp_path) as db:
        clock = FrozenClock(datetime(2026, 11, 7, tzinfo=UTC))
        assert seed_demo(config(db.path.parent), db, clock)
        before = db.connection.execute("SELECT id, found_at FROM jobs ORDER BY id").fetchall()
        assert len(before) == 15
        clock.advance(timedelta(days=5))
        assert not seed_demo(config(db.path.parent), db, clock)
        assert (
            db.connection.execute("SELECT id, found_at FROM jobs ORDER BY id").fetchall() == before
        )


def test_demo_dates_shifted_to_today(tmp_path):
    from app.services.demo import seed_demo

    with fresh(tmp_path) as db:
        seed_demo(config(db.path.parent), db, FrozenClock(datetime(2027, 3, 10, tzinfo=UTC)))
        dates = [r[0] for r in db.connection.execute("SELECT starts_at FROM events")]
        assert any(d.startswith("2027-03-10") for d in dates)
        assert all("2026-" not in d for d in dates)


def test_demo_dataset_is_fictional():
    from app.services.demo import DATASET

    value = json.loads(DATASET.read_bytes())
    assert value["format"] == 1 and value["files"] == []
    raw = json.dumps(value)
    assert "example.test" in raw and "Fictional" in raw
    assert len(value["tables"]["jobs"]["rows"]) == 15
    assert value["tables"]["cv_templates"]["rows"]
    assert value["tables"]["facts"]["rows"]
    assert value["tables"]["roles"]["rows"]


def test_demo_seed_leaves_existing_content_alone(tmp_path):
    from app.services.demo import seed_demo

    with fresh(tmp_path) as db:
        db.connection.execute(
            "INSERT INTO settings(key,value,version,created_at,updated_at) "
            "VALUES('shortcuts','{}',1,'2026-01-01','2026-01-01')"
        )
        assert not seed_demo(
            config(db.path.parent), db, FrozenClock(datetime(2026, 11, 7, tzinfo=UTC))
        )
        assert db.connection.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
