"""A fact's history as the page shows it (M3.6; AC-M3.3): its events and its versions, newest
first, each version after the first with the word-level diff against the one before.

Built here so templates hold no rules. A changed run of words is marked as added or removed;
a run that is only spacing never is (TD-023), and a version that changed nothing else says so
in words. Everything reads only. Wording is the user's own or AI-made text: it is handed on as
plain strings for the templates to escape, and nothing of it is logged.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, tzinfo
from typing import Final

from app.domain.diff import DiffOp, word_diff
from app.domain.errors import format_day_time
from app.domain.facts import FactContent
from app.services.fact_editor import PRIVACY_LABELS
from app.services.fact_views import PRACTICE_PROVIDER, FactBank, local_time, open_bank
from app.services.facts import EventView, VersionView

_SOURCES: Final[Mapping[str, str]] = {
    "manual": "Written by you",
    "cv_import": "From a CV import",
    "merge": "From a merge",
    "merge_undo": "Merge undone",
    "refine": "Refined with AI, accepted by you",
}
_SOURCE_OTHER: Final = "Saved"
_EVENTS: Final[Mapping[str, str]] = {
    "created": "Created",
    "confirmed": "Confirmed",
    "edited": "Wording changed",
    "details_changed": "Details changed",
    "archived": "Archived",
    "restored": "Restored",
    "discarded": "Discarded",
    "discard_undone": "Discard undone",
    "merged": "A draft was merged into this fact",
    "merged_into": "Merged into another fact",
    "merge_undone": "Merge undone",
    "refined": "Refined with AI",
}
_EVENT_OTHER: Final = "Changed"
_PRIVACY_CHANGED: Final = "privacy_changed"
# A plain wording has no label of its own; a story's parts each have one.
_PARTS: Final = (
    ("text", None),
    ("situation", "Situation"),
    ("action", "Action"),
    ("result", "Result"),
)
NOTE_SPACING_ONLY: Final = "Only spacing changed."
NOTE_SAME: Final = "Same wording as the version before."


@dataclass(frozen=True)
class DiffRun:
    """A run of words: ``op`` is "equal", "added" or "removed". ``gap`` marks a changed run
    that comes right after another one, with nothing between them: the page puts a space
    there, so "60" replaced by "80" never reads "6080"."""

    op: str
    text: str
    gap: bool = False


@dataclass(frozen=True)
class PartView:
    label: str | None
    runs: tuple[DiffRun, ...]


@dataclass(frozen=True)
class VersionEntry:
    number: int
    source: str
    practice: bool
    when_iso: str
    when_text: str
    parts: tuple[PartView, ...]
    note: str | None
    """What to say when nothing is marked in a version that has one before it."""


@dataclass(frozen=True)
class EventLine:
    label: str
    when_iso: str
    when_text: str


@dataclass(frozen=True)
class HistoryView:
    fact_id: str
    events: tuple[EventLine, ...]
    versions: tuple[VersionEntry, ...]


def diff_runs(old: str, new: str) -> tuple[tuple[DiffRun, ...], bool]:
    """The new text as runs, with what the old one had and lost, and whether anything at all
    differs. Spacing alone is never a marked change: added spacing reads as plain text and
    removed spacing is left out."""
    runs: list[DiffRun] = []
    differs = False
    for segment in word_diff(old, new):
        differs = differs or segment.op is not DiffOp.EQUAL
        if segment.text.strip():
            changed = segment.op is not DiffOp.EQUAL
            gap = changed and bool(runs) and runs[-1].op != DiffOp.EQUAL
            runs.append(DiffRun(segment.op.value, segment.text, gap))
        elif segment.op is not DiffOp.REMOVED:
            runs.append(DiffRun(DiffOp.EQUAL.value, segment.text))
    return tuple(runs), differs


def diff_parts(
    before: FactContent | None, content: FactContent
) -> tuple[tuple[PartView, ...], str | None]:
    """A version's parts, diffed against ``before`` when there is one, and its note."""
    parts = []
    differs = False
    for name, label in _PARTS:
        new = getattr(content, name) or ""
        old = new if before is None else (getattr(before, name) or "")
        if not (new or old):
            continue
        runs, changed = diff_runs(old, new)
        differs = differs or changed
        parts.append(PartView(label, runs))
    marked = any(run.op != DiffOp.EQUAL for part in parts for run in part.runs)
    if before is None or marked:
        return tuple(parts), None
    return tuple(parts), NOTE_SPACING_ONLY if differs else NOTE_SAME


def _event_label(event: EventView) -> str:
    if event.type == _PRIVACY_CHANGED:
        chosen = PRIVACY_LABELS.get(str(event.detail.get("to")), "")
        return f"Privacy changed to {chosen}" if chosen else "Privacy changed"
    return _EVENTS.get(event.type, _EVENT_OTHER)


class FactHistory:
    """``bank`` answers the open workspace's fact services (None while none is open)."""

    def __init__(self, *, bank: Callable[[], FactBank | None], tz: tzinfo) -> None:
        self._bank = bank
        self._tz = tz

    def of(self, fact_id: str) -> HistoryView:
        """The history of one fact, whatever its status. Raises ``FactNotFound``."""
        bank = open_bank(self._bank)
        versions = bank.facts.versions(fact_id)
        events = bank.facts.events(fact_id)
        entries = [
            self._entry(None if index == 0 else versions[index - 1], version)
            for index, version in enumerate(versions)
        ]
        lines = [EventLine(_event_label(event), *self._when(event.created_at)) for event in events]
        return HistoryView(fact_id, tuple(reversed(lines)), tuple(reversed(entries)))

    def _when(self, moment: datetime) -> tuple[str, str]:
        """The moment for a ``<time>``: with its offset, and as the page writes it."""
        return moment.isoformat(), format_day_time(local_time(moment, self._tz))

    def _entry(self, before: VersionView | None, version: VersionView) -> VersionEntry:
        parts, note = diff_parts(None if before is None else before.content, version.content)
        return VersionEntry(
            number=version.version,
            source=_SOURCES.get(version.source, _SOURCE_OTHER),
            practice=version.provider == PRACTICE_PROVIDER,
            when_iso=self._when(version.created_at)[0],
            when_text=self._when(version.created_at)[1],
            parts=parts,
            note=note,
        )
