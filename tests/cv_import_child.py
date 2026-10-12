"""A process for the CV import kill test (M3.4; tests/integration/failures): the real app on
folders the test seeded, with one import whose provider call never returns.

Run as ``python -m tests.cv_import_child <folder>`` with the isolated environment of
``tests/running_app.py``. It pastes the fictional CV through ``POST /facts/import``, waits
until the task's provider call is in flight, writes the task's id to ``<folder>/READY`` and
then waits to be killed. If the import can't be started it writes the reason to
``<folder>/FAILED`` and exits with 1. Nothing here leaves the process: no network, no port.
"""

import sys
import threading
from pathlib import Path

from tests.cv_fixtures import fixture_path
from tests.cv_import_harness import ACCEPTED, importing

READY = "READY"
FAILED = "FAILED"


def main(folder: Path) -> int:
    cv_text = fixture_path("cv.txt").read_text(encoding="utf-8")
    with importing(folder, seed=False, hold_timeout=None) as run:
        run.hold.hold = True
        response = run.paste(cv_text)
        if response.status_code not in ACCEPTED:
            (folder / FAILED).write_text(
                f"POST /facts/import answered {response.status_code}", encoding="utf-8"
            )
            return 1
        task_id = run.only_task().id
        run.wait_entered(1)
        (folder / READY).write_text(task_id, encoding="utf-8")
        threading.Event().wait()  # until the test kills this process
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
