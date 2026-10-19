"""Hold a real refine task's provider call for the process-kill test. Fictional data only."""

import sys
import threading
from pathlib import Path

from tests.refine_harness import ACCEPTED, refining


def main(folder: Path) -> None:
    with refining(folder, seed=False, hold_timeout=None) as r:
        fact = r.target()
        r.run.hold.hold = True
        assert r.start(fact.id).status_code in ACCEPTED
        r.run.wait_entered(1)
        task = r.tasks()[0]
        (folder / "REFINE_READY").write_text(task.id, encoding="utf-8")
        threading.Event().wait()


if __name__ == "__main__":
    main(Path(sys.argv[1]))
