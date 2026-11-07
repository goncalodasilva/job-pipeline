"""Restore the bundled fictional export once, only in the designated demo workspace."""

import json
import re
import tempfile
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Final

from app.config import RuntimeConfig, is_demo_workspace
from app.domain.clock import Clock
from app.modules.storage import Database
from app.services.data_portability import DataPortabilityService

DATASET: Final = Path(__file__).resolve().parents[1] / "demo_data" / "workspace.json"
ANCHOR: Final = date(2026, 11, 7)
_DATE: Final = re.compile(r"\d{4}-\d{2}-\d{2}(?:T.*)?")


def shifted_dataset(clock: Clock) -> bytes:
    payload: dict[str, Any] = json.loads(DATASET.read_bytes())
    delta = clock.now().astimezone(UTC).date() - ANCHOR
    for table in payload["tables"].values():
        for row in table["rows"]:
            for index, value in enumerate(row):
                if isinstance(value, str) and _DATE.fullmatch(value):
                    if "T" in value:
                        row[index] = (datetime.fromisoformat(value) + delta).isoformat()
                    else:
                        row[index] = (date.fromisoformat(value) + delta).isoformat()
    return json.dumps(payload, ensure_ascii=True).encode("utf-8")


def seed_demo(config: RuntimeConfig, db: Database, clock: Clock) -> bool:
    """Called under the startup workspace lock, before recovery or setup writes.

    The importer rechecks emptiness within its transaction. An existing demo is retained,
    including an empty-looking demo whose user has already saved settings or setup progress.
    """
    if not is_demo_workspace(config) or db.path.parent != config.data_dir:
        raise ValueError("Seeding requires the designated demo workspace")
    service = DataPortabilityService(lambda: db, lambda: config.data_dir, clock)
    if not service.can_restore():
        return False
    with tempfile.TemporaryDirectory(prefix="jobpipeline-demo-") as temporary:
        archive = Path(temporary) / "workspace.json"
        archive.write_bytes(shifted_dataset(clock))
        service.restore(archive)
    return True
