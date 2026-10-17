"""Step 4: import a CV (M3.7; AC-M3.8; ADR-052, ADR-089 items 3 and 4).

Optional and recommended. The step offers the Fact bank's CV import: one file or a pasted
text goes through the same ``CvImportService`` (stored first, then started through the budget
gate), and the import runs in the background, making drafts only.

- ``upload(data)``: a refused upload returns its message on the file field, and nothing is
  stored. Once the upload is stored the step is done and the guide moves on, saying whether
  the import started; when the budget gate or a missing provider holds it back, the upload is
  kept and can be started from the Fact bank.
- ``submit(form)``: "Skip for now" marks the step skipped; nothing else is sent to it.
- The step is also marked done by wiring when the workspace opens with a fact or an upload
  in it (``fact_bank_started``), and when the Fact bank keeps an upload or adds a fact
  (ADR-052). GET never writes.
- Once the CV is stored nothing stops the guide: a start the budget gate holds back, a
  missing provider or any other failure keeps the upload for the Fact bank (``MSG_KEPT``).
  When only marking the step failed, sending the same CV again reuses the kept upload.

``mark_done`` and ``skip`` are injected by wiring; both store the status and refresh the
setup decision. Without a workspace the step asks the user to finish step 1 first.
"""

import hashlib
import logging
import threading
from collections.abc import Callable, Mapping
from typing import Final

from app.domain.errors import AppError, CvImportAlreadyDone, CvImportNotFound, ProviderNotConfigured
from app.onboarding.forms import first_value
from app.onboarding.registry import Form, StepResult
from app.services.budget_gate import BudgetGate, Start
from app.services.cv_import import STATE_UPLOADED, CvImportService

_log = logging.getLogger(__name__)

SKIP: Final = "skip"
FILE_FIELD: Final = "file"
TEXT_FIELD: Final = "text"
MSG_STEP_1_FIRST: Final = "Finish step 1 first: choose where the app keeps your data."
MSG_STARTED: Final = "CV import started."
MSG_KEPT: Final = (
    "Your CV was saved, but its import didn't start. Once setup is finished, start it from "
    "the Fact bank, under Drafts to review."
)
MSG_SKIPPED: Final = "CV import skipped."
MSG_CHOOSE: Final = "Choose a file or paste your CV's text, or choose Skip for now."
MSG_NOT_MOVED_ON: Final = "The app couldn't move on. Try again."
MSG_SAVED_NOT_MOVED_ON: Final = (
    "Your CV was saved, but the app couldn't move on. Try again, or choose Skip for now; the "
    "upload is kept in the Fact bank."
)
_NOT_STARTED: Final = (CvImportNotFound, CvImportAlreadyDone, ProviderNotConfigured)


class CvImportStep:
    # Only a pasted text is ever shown back, after a refusal; a file never is.
    echo_fields: tuple[str, ...] = (TEXT_FIELD,)

    def __init__(
        self,
        imports: CvImportService,
        gate: BudgetGate,
        *,
        workspace_open: Callable[[], bool],
        mark_done: Callable[[], None],
        skip: Callable[[], None],
    ) -> None:
        self._imports = imports
        self._gate = gate
        self._workspace_open = workspace_open
        self._mark_done = mark_done  # blocking: marks the step done, refreshes the decision
        self._skip = skip  # blocking: marks the step skipped, refreshes the decision
        # The upload stored by an attempt that couldn't move on: (digest, import id). The
        # same CV sent again reuses it instead of storing a second copy. A digest only.
        self._kept: tuple[str, str] | None = None
        self._lock = threading.Lock()  # submits run in worker threads

    def context(self) -> Mapping[str, object]:
        """No I/O beyond asking whether a workspace is open: writes nothing."""
        return {"workspace_open": self._workspace_open()}

    def submit(self, form: Form) -> StepResult:
        if not self._workspace_open():
            return StepResult(ok=False, errors={"form": MSG_STEP_1_FIRST})
        if first_value(form, "action") != SKIP:
            return StepResult(ok=False, errors={FILE_FIELD: MSG_CHOOSE})
        try:
            self._skip()
        except Exception as error:  # sqlite messages can hold paths: log the type only
            _log.warning("CV import step couldn't be skipped (%s).", type(error).__name__)
            return StepResult(ok=False, errors={"form": MSG_NOT_MOVED_ON})
        return StepResult(ok=True, message=MSG_SKIPPED)

    def upload(self, data: bytes, *, pasted: bool = False) -> StepResult:
        """Store the upload (``pasted``: it came as text, so a refusal belongs to the text
        field), mark the step done and start the import. Blocking (a parse in a child
        process, a file, the database)."""
        if not self._workspace_open():
            return StepResult(ok=False, errors={"form": MSG_STEP_1_FIRST})
        digest = hashlib.sha256(data).hexdigest()
        import_id = self._stored_before(digest)
        if import_id is None:
            try:
                import_id = self._imports.store(data)
            except AppError as error:  # the document errors: nothing was stored
                field = TEXT_FIELD if pasted else FILE_FIELD
                return StepResult(ok=False, errors={field: error.user_message})
        try:
            self._mark_done()
        except Exception as error:  # sqlite messages can hold paths: log the type only
            _log.warning("CV import step couldn't be marked done (%s).", type(error).__name__)
            with self._lock:
                self._kept = (digest, import_id)
            return StepResult(ok=False, errors={"form": MSG_SAVED_NOT_MOVED_ON})
        with self._lock:
            self._kept = None
        return StepResult(ok=True, message=self._start(import_id))

    def _stored_before(self, digest: str) -> str | None:
        """The import an earlier attempt stored for the same CV and couldn't move on from,
        while it is still kept and not imported."""
        with self._lock:
            kept = self._kept
        if kept is None or kept[0] != digest:
            return None
        waiting = {item.id for item in self._imports.imports() if item.state == STATE_UPLOADED}
        return kept[1] if kept[1] in waiting else None

    def _start(self, import_id: str) -> str:
        """Start the stored import when the budget gate lets it; what to say either way. A
        warning to confirm isn't asked here, and nothing that goes wrong once the CV is
        stored stops the guide: the upload waits in the Fact bank instead."""
        try:
            plan = self._imports.plan_start(import_id)
            if not isinstance(self._gate.check(plan.service, tier=plan.tier), Start):
                return MSG_KEPT
            self._imports.start(import_id)
        except _NOT_STARTED:
            return MSG_KEPT
        except Exception as error:  # messages can hold paths: log the type only
            _log.warning("CV import couldn't be started (%s).", type(error).__name__)
            return MSG_KEPT
        return MSG_STARTED
