"""Restore the bundled fictional export once, only in the designated demo workspace (M9.5 part C;
AC-M9.4; ADR-049; ADR-131 item 10; ADR-138 items 5 and 6).

The dataset is an ordinary M9.1 export file, so the demo is seeded through the importer
(``DataPortabilityService.restore``) and never through a second write path; the shipped file
passes the importer's own checks before its dates move. Its dates move so that the day it was
built (``ANCHOR``) becomes today in the app's time zone, from the injected clock.
"""

import json
import re
import tempfile
from datetime import date, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any, Final

from app.domain.clock import Clock
from app.modules.storage import Database
from app.services.data_portability import DataPortabilityService, parse_export

DATASET: Final = Path(__file__).resolve().parents[1] / "demo_data" / "workspace.json"
# The day the shipped dataset calls today (``tests/demo_builder.py`` builds it then).
ANCHOR: Final = date(2026, 11, 7)
# Inside the data folder, so a start-up after a kill removes it (``sweep_restore_staging``).
STAGING_PREFIX: Final = ".restore-demo-"
_DATE: Final = re.compile(r"\d{4}-\d{2}-\d{2}(?:T.*)?")


def _shift(value: str, delta: timedelta) -> str:
    if "T" in value:
        return (datetime.fromisoformat(value) + delta).isoformat()
    return (date.fromisoformat(value) + delta).isoformat()


def shifted_dataset(clock: Clock, zone: tzinfo, dataset: Path = DATASET) -> bytes:
    """The dataset with every stored date and time moved by whole days, so ``ANCHOR`` becomes the
    clock's date in ``zone``. Only whole column values that are ISO dates or times move.
    ``PortabilityError`` when the file is not a valid export."""
    payload: dict[str, Any] = parse_export(dataset.read_bytes())
    delta = clock.now().astimezone(zone).date() - ANCHOR
    for table in payload["tables"].values():
        for row in table["rows"]:
            for index, value in enumerate(row):
                if isinstance(value, str) and _DATE.fullmatch(value):
                    row[index] = _shift(value, delta)
    return json.dumps(payload, ensure_ascii=True).encode("utf-8")


def seed_demo(
    db: Database,
    *,
    data_dir: Path,
    demo_workspace: bool,
    clock: Clock,
    zone: tzinfo,
    dataset: Path = DATASET,
) -> bool:
    """Seed the demo; ``True`` when it was seeded, ``False`` when the workspace had content.

    ``demo_workspace`` is ``is_demo_workspace(config)``: ``--demo`` on the folder the app chose
    for it. Anything else (``--data-dir``, ``JOBPIPELINE_DATA_DIR``) is the user's folder and is
    refused with ``ValueError`` (ADR-049). Called under the start-up workspace lock, before
    recovery or setup writes. The importer checks emptiness again inside its transaction, so an
    existing demo is kept, including one whose user has only saved settings or setup progress.
    """
    if not demo_workspace or db.path.parent != data_dir:
        raise ValueError("Seeding requires the designated demo workspace")
    service = DataPortabilityService(lambda: db, lambda: data_dir, clock)
    if not service.can_restore():
        return False
    with tempfile.TemporaryDirectory(prefix=STAGING_PREFIX, dir=data_dir) as staging:
        archive = Path(staging) / "workspace.json"
        archive.write_bytes(shifted_dataset(clock, zone, dataset))
        service.restore(archive)
    return True
