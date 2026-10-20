"""Hold a real summary provider call for a process-kill test; fictional input only."""

import sys
import threading
from pathlib import Path

from tests.cv_import_harness import importing


def main(folder: Path) -> None:
    with importing(folder, seed=False, hold_timeout=None) as run:
        job = run.app.state.job_intake.add_text("Fictional gardener role")
        run.hold.hold = True
        ref = run.app.state.job_summary.start(job.source_job_id)
        run.wait_entered(1)
        (folder / "SUMMARY_READY").write_text(ref.id)
        threading.Event().wait()


if __name__ == "__main__":
    main(Path(sys.argv[1]))
