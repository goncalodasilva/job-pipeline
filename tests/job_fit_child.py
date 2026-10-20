"""Hold a fit call for the real process-kill probe; temporary fictional data only."""

import sys
import threading
from pathlib import Path

from app.services.facts import FactInput
from tests.cv_import_harness import importing
from tests.integration.test_job_fit import prepare
from tests.refine_harness import Refining


def main(folder: Path) -> None:
    with importing(folder, seed=False, hold_timeout=None) as run:
        job_id = prepare(Refining(run))
        run.app.state.facts.create(FactInput(text="Grew fictional flowers."))
        run.hold.hold = True
        ref = run.app.state.job_fit.start(job_id)
        run.wait_entered(2)
        (folder / "FIT_READY").write_text(ref.id)
        threading.Event().wait()


if __name__ == "__main__":
    main(Path(sys.argv[1]))
