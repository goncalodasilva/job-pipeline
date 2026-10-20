"""Errors the user can see (spec 2.11).

``user_message`` is the plain, blame-free text shown in the interface. The optional
``detail`` is for logs only and never reaches ``user_message``. ``detail`` must not contain
secrets or personal data: log redaction (M0.10) is a backstop, not permission.
``AppError`` is a base class; raise one of its subclasses.
``kind`` is the stable name stored in the database (``tasks.error``) and mapped back to a
message. It defaults to the class name; errors that get stored set it explicitly so a class
rename can't orphan stored rows.
"""

from datetime import date, datetime
from typing import Any, ClassVar

# Fixed English month names, so the system locale never changes user-facing text (spec 3.10).
_MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def format_day(day: date) -> str:
    """Write a date the way the interface shows it, e.g. "1 November 2026"."""
    return f"{day.day} {_MONTHS[day.month - 1]} {day.year}"


def format_short_day(day: date) -> str:
    """Write a date briefly, e.g. "Mon 26 Oct" (fixed English names, spec 3.10)."""
    return f"{_WEEKDAYS[day.weekday()]} {day.day} {_MONTHS[day.month - 1][:3]}"


def format_day_time(moment: datetime) -> str:
    """Write a moment the way the interface shows it, e.g. "10 October 2026, 14:05".

    The moment is written as given: the caller converts it to the local zone first."""
    return f"{format_day(moment.date())}, {moment.hour:02d}:{moment.minute:02d}"


class AppError(Exception):
    message = "Something went wrong. Try again."
    kind: ClassVar[str] = "AppError"

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if "kind" not in cls.__dict__:
            cls.kind = cls.__name__

    def __init__(self, *, detail: str | None = None) -> None:
        super().__init__(detail if detail is not None else self.message)

    @property
    def user_message(self) -> str:
        return self.message


class ProviderNotInstalled(AppError):
    kind = "ProviderNotInstalled"
    message = "Claude Code wasn't found. Install it, or choose Claude via API key."


class CalendarSyncUnsupported(AppError):
    message = "Download the calendar file and open it in your calendar app."


class ProviderAuthError(AppError):
    kind = "ProviderAuthError"
    message = "The token or key was rejected. Create a new one and paste it again."


class UsageLimitReached(AppError):
    kind = "UsageLimitReached"
    message = "Your Claude plan's usage limit was reached. Try later or switch provider."


class ProviderRequestRejected(AppError):
    """The provider refused the request itself (unknown model, request too large)."""

    kind = "ProviderRequestRejected"
    message = "Claude refused the request. Check the model in Settings > Providers."


class BudgetBlocked(AppError):
    kind = "BudgetBlocked"

    def __init__(self, service: str, resets_on: date, *, detail: str | None = None) -> None:
        self.service = service
        self.resets_on = resets_on
        super().__init__(detail=detail)

    @property
    def user_message(self) -> str:
        return f"Monthly limit reached for {self.service}. Resets on {format_day(self.resets_on)}."


def tier_cap_message(service: str, tier: str, resets_on: date, *, offer_fast: bool) -> str:
    """The ``TierCapReached`` sentence (spec 2.11). ``offer_fast``: say that this can run once
    on Fast; a caller that has checked Fast passes what it found (M2.24)."""
    text = (
        f"The {tier.capitalize()} limit for {service} is reached. "
        f"It resets on {format_day(resets_on)}"
    )
    return text + ("; you can run this once on Fast." if offer_fast else ".")


class TierCapReached(BudgetBlocked):
    """Only a tier's cap blocks (spec 3.7). ``offer_fast`` (Thorough only) means the caller
    may offer to run once on Fast after checking Fast; it never switches by itself."""

    kind = "TierCapReached"

    def __init__(
        self, service: str, tier: str, resets_on: date, *, detail: str | None = None
    ) -> None:
        self.tier = tier
        self.offer_fast = tier == "thorough"
        super().__init__(service, resets_on, detail=detail)

    @property
    def user_message(self) -> str:
        return tier_cap_message(self.service, self.tier, self.resets_on, offer_fast=self.offer_fast)


class UsageLimitsUnreadable(AppError):
    """The stored budgets can't be read, so no AI request runs until they are saved again."""

    kind = "UsageLimitsUnreadable"
    message = (
        "Your usage limits couldn't be read. Open Settings > Usage budgets and save them again."
    )


class BudgetNoticeRefused(AppError):
    """A budget warning was confirmed that isn't due in the current cycle."""

    kind = "BudgetNoticeRefused"
    message = "This warning can't be confirmed now. Reload the page to see your current usage."


class NetworkUnavailable(AppError):
    kind = "NetworkUnavailable"
    message = "No connection. Nothing was lost; try again."


class InvalidModelOutput(AppError):
    kind = "InvalidModelOutput"
    message = "The AI response didn't have the expected shape. Try again."


class VersionConflict(AppError):
    kind = "VersionConflict"
    message = "This item changed elsewhere. Reload to see the latest version."


class RefineSuggestionStale(VersionConflict):
    """Accept of a Refine with AI suggestion made for an earlier version of the fact (it was
    edited since). Reloading shows the same suggestion, so the words say what helps; the
    panel shows the same note (ADR-097)."""

    kind = "RefineSuggestionStale"
    message = (
        "Your fact changed after this suggestion. "
        "Discard it and ask again, or keep editing it yourself."
    )


class ConfigError(AppError):
    """A start-up setting is invalid. ``detail`` names the setting, never its value."""

    message = "A setting isn't valid. Check the command options, environment and settings file."


class DataFolderUnavailable(ConfigError):
    """The chosen data folder can't be created, opened or logged into (step 1; ADR-044).

    ``detail`` names the error type only, never the path.
    """

    message = "This folder can't be used. Choose another folder."


class DataFolderNotYours(ConfigError):
    """The chosen data folder belongs to another account (ADR-043)."""

    message = "This folder belongs to another user. Choose a folder you own."


class DataFolderInUse(ConfigError):
    """Another copy of the app holds the data folder's lock (ADR-072, ADR-076 item 9).

    ``detail`` never names the path.
    """

    kind = "DataFolderInUse"
    message = (
        "This folder is already open in another copy of the app. "
        "Close that copy first, or choose another folder."
    )

    def __init__(self, *, detail: str | None = "the data folder lock is held") -> None:
        super().__init__(detail=detail)


class FetchRefused(AppError):
    kind = "FetchRefused"
    message = "This address can't be fetched. Paste the job text instead."


class UnsupportedDocumentType(AppError):
    """The content is not a PDF, a Word file or plain text (decided by content, never by name)."""

    kind = "UnsupportedDocumentType"
    message = (
        "This file type isn't supported. Upload a PDF or Word file, or paste the text instead."
    )


class UnreadableDocument(AppError):
    """The document is damaged or can't be read. ``detail`` names an error type at most, never
    the reader's own error text, which can quote the document."""

    kind = "UnreadableDocument"
    message = (
        "This file couldn't be read. It may be damaged. Save it again, or paste the text instead."
    )


class EncryptedDocument(UnreadableDocument):
    kind = "EncryptedDocument"
    message = (
        "This PDF is protected by a password. Save a copy without the password, "
        "or paste the text instead."
    )


class NoTextFound(AppError):
    """The document has nothing to read, as in a scan (no OCR; ADR-089 item 11)."""

    kind = "NoTextFound"
    message = "No text was found in this file. If it's a scan, paste the text instead."


class ParseTimedOut(AppError):
    kind = "ParseTimedOut"
    message = "Reading this file took too long. Try again, or paste the text instead."


class UploadTooLarge(AppError):
    kind = "UploadTooLarge"
    message = "This file is larger than 10 MB. Upload a smaller file, or paste the text instead."


class UploadNotAccepted(AppError):
    """The request isn't one file or a pasted text (two files, both, neither, or malformed)."""

    kind = "UploadNotAccepted"
    message = "Upload one file (PDF, Word or plain text), or paste the text instead."


class CvTextTooLong(AppError):
    """The CV's text is longer than one import takes. It is refused whole, never cut."""

    kind = "CvTextTooLong"
    message = (
        "This CV has too much text to import in one go. Paste the part that matters most instead."
    )


class NothingExtracted(AppError):
    """The AI answered, but listed nothing from the CV."""

    kind = "NothingExtracted"
    message = "Nothing could be listed from this CV. Try again, or paste the text instead."


class CvImportNotFound(AppError):
    """The id names no uploaded CV: never stored, or removed (also while its task ran)."""

    kind = "CvImportNotFound"
    message = "This uploaded CV wasn't found. It may have been removed. Upload it again."


class CvImportBusy(AppError):
    """The upload can't be removed while its task is queued or running."""

    kind = "CvImportBusy"
    message = "This CV is being read. Cancel the task or wait for it to finish, then remove it."


class CvImportAlreadyDone(AppError):
    """The upload was already imported; its drafts are made once."""

    kind = "CvImportAlreadyDone"
    message = "This CV was already imported. Its drafts are in your fact bank."


class ProviderNotConfigured(AppError):
    """No usable AI provider is saved, so nothing that needs one can start."""

    kind = "ProviderNotConfigured"
    message = "No AI provider is connected. Connect one in Settings > Providers, then try again."


class SecretStoreUnavailable(AppError):
    """The system keychain can't be used. ``detail`` names the error type, never a value."""

    kind = "SecretStoreUnavailable"
    message = "Your system keychain isn't available. You can use an environment variable instead."


class TaskNotRetryable(AppError):
    """Retry was asked for a task that is still queued or running, or has succeeded."""

    kind = "TaskNotRetryable"
    message = (
        "This task can't be retried. Only failed, cancelled or interrupted tasks can run again."
    )


class TaskFailed(AppError):
    """A task handler raised something other than an ``AppError``. ``detail`` names the
    exception type only, never its text."""

    kind = "TaskFailed"
    message = "This task stopped because of an unexpected problem. Try again."


class PracticeNotOffered(AppError):
    """The practice task (M2.17) was started or claimed while it isn't offered: fakes aren't
    allowed, or the saved provider isn't the fake."""

    kind = "PracticeNotOffered"
    message = "Practice tasks are only available while the fake provider is active."


class UnknownTaskType(AppError):
    """A queued task has a type no handler is registered for (for example, a newer app
    version queued it)."""

    kind = "UnknownTaskType"
    message = "This task can't run in this version of the app."


class UnknownSetupStep(AppError):
    message = "This setup step doesn't exist."


class StepNotSkippable(AppError):
    message = "This step is needed, so it can't be skipped."


class FactNotFound(AppError):
    """The id names no fact, role or skill."""

    kind = "FactNotFound"
    message = "This item wasn't found. Reload the page and try again."


class FactTransitionRefused(AppError):
    """A status change the fact lifecycle doesn't allow (spec 3.2)."""

    kind = "FactTransitionRefused"
    message = (
        "This can't be done to this fact in its current state. Reload to see the latest version."
    )


class FactInputInvalid(AppError):
    """What the user typed can't be saved. ``field`` names the form field it belongs to and
    ``message`` says what to change; without one it reads as the class message, which is also
    what a stored kind maps back to."""

    kind = "FactInputInvalid"
    message = "This can't be saved as it is. Check what you typed and try again."

    def __init__(
        self, field: str = "", message: str | None = None, *, detail: str | None = None
    ) -> None:
        self.field = field
        self._message = message
        super().__init__(detail=detail)

    @property
    def user_message(self) -> str:
        return self._message if self._message is not None else self.message


class RefineNeverUse(AppError):
    """Refine with AI asked for a fact marked "never use": nothing is sent (ADR-089 item 6)."""

    kind = "RefineNeverUse"
    message = (
        'This fact is marked "Never use in applications", so it is never sent to the AI. '
        "To refine it, change its privacy setting first."
    )


class RefineNotAvailable(AppError):
    """Refine with AI asked for a discarded or archived fact."""

    kind = "RefineNotAvailable"
    message = "Only drafts and confirmed facts can be refined. Restore this fact first."


class RefineNotOpen(AppError):
    """No Refine with AI conversation is open for the fact."""

    kind = "RefineNotOpen"
    message = "There is no open Refine with AI conversation for this fact. Start one first."


class RefineBusy(AppError):
    """An answer when the AI has no question waiting for one (none was asked, or the last
    question is answered). Its subclasses are the cases where a turn is on its way."""

    kind = "RefineBusy"
    message = "The AI has no question waiting for an answer right now."


class RefineTurnRunning(RefineBusy):
    """A turn of the open conversation is still queued or running."""

    kind = "RefineTurnRunning"
    message = "The AI hasn't answered yet. Wait for its answer, then try again."


class RefineStopping(RefineBusy):
    """A conversation that was cancelled or discarded still has its turn stopping, so a new
    one can't open yet (ADR-097 item 6)."""

    kind = "RefineStopping"
    message = "The previous conversation is still stopping. Try again in a moment."


class RefineContextChanged(AppError):
    """A fact sent earlier in the Refine with AI conversation (its wording, or quoted in the
    AI's questions or suggestion) is now marked "never use", or (for Accept) "generalize
    names" when it was sent without that mark: going on would use or send it in a way the
    user no longer allows (ADR-089 item 6)."""

    kind = "RefineContextChanged"
    message = (
        "The privacy setting of a fact used earlier in this conversation has changed. "
        "Discard this conversation and start again."
    )


class RefineAnswersUsed(AppError):
    """A conversation takes at most three answers."""

    kind = "RefineAnswersUsed"
    message = (
        "This conversation already has three answers. Accept the suggestion, edit the fact "
        "yourself or discard it."
    )


class RefineNoSuggestion(AppError):
    """Accept asked for a Refine with AI conversation whose AI hasn't suggested a wording
    (yet), so there is nothing the posted wording could have come from."""

    kind = "RefineNoSuggestion"
    message = "There is no suggested wording to accept yet."


class RefineClaimsUnacknowledged(AppError):
    """Accept refused: the wording says something that isn't in the fact bank, and the user
    neither confirmed it nor edited it out (spec 3.1 item 5; AC-M3.5). ``claims`` are the
    texts still to confirm, for the page to list; never logged."""

    kind = "RefineClaimsUnacknowledged"
    message = (
        "This isn't in your fact bank. Confirm each item below is true, or edit it out, "
        "before accepting."
    )

    def __init__(self, claims: tuple[str, ...] = (), *, detail: str | None = None) -> None:
        self.claims = claims
        super().__init__(detail=detail)


class TriageNotAllowed(AppError):
    """A triage move the job's current state doesn't allow (M4.2), for example removing a
    job that is already removed, or a stale page acting on a job that moved since."""

    kind = "TriageNotAllowed"
    message = "This job can't be changed that way now. Reload to see its current state."


class JobTriageInputInvalid(TriageNotAllowed):
    """A triage form value needing a field-specific correction."""

    def __init__(self, field: str = "", message: str | None = None) -> None:
        self.field = field
        self._message = message
        super().__init__()

    @property
    def user_message(self) -> str:
        return self._message or self.message


class SkillAliasTaken(AppError):
    kind = "SkillAliasTaken"
    message = "Another skill already uses this name. Choose a different name."


BUDGET_MESSAGE = (
    "A monthly usage limit was reached. Open Settings > Usage budgets to see when it resets."
)
"""What a stored budget kind reads as: the service, tier and reset date aren't stored."""


def _own_subclasses(cls: type[AppError]) -> list[type[AppError]]:
    found: list[type[AppError]] = []
    for sub in cls.__subclasses__():
        if sub.__module__ == __name__:  # never a class defined elsewhere
            found.append(sub)
        found.extend(_own_subclasses(sub))
    return found


def message_for_kind(kind: str | None) -> str:
    """The message for an error kind stored in ``tasks.error``. An exact match on a kind of
    this module's classes only: anything else (None, other text, another letter case) reads
    as the generic message, and what was stored is never part of the answer."""
    if not isinstance(kind, str):
        return AppError.message
    for cls in _own_subclasses(AppError):
        if cls.kind == kind:
            return BUDGET_MESSAGE if issubclass(cls, BudgetBlocked) else cls.message
    return AppError.message


class JobTextNotFound(AppError):
    message = "No job text was found. Paste the job text instead."


class JobTextTooLong(AppError):
    message = "This job has more than 50,000 characters. Paste a shorter posting instead."


class JobLinkInvalid(AppError):
    message = "Use an http or https link to the original posting."


class JobNotFound(AppError):
    message = "This job wasn't found. It may have been removed."


class FitNotAvailable(AppError):
    message = "Summarize the job and add confirmed facts to see how you match."
