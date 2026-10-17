"""The capture inbox (M3.7; detailed design "Fact bank", Capture inbox; ADR-090 item 10).

A note is a quick line the user jots down to turn into a fact later. It is added, listed while
open, deleted softly (``deleted_at``) with an undo, or turned into a fact through the editor,
whose save records the fact on the note (``FactService.create`` with ``note_id``) so it leaves
the inbox. Note text is the user's own: it is handed on as a plain string for the templates to
escape and never logged.
"""

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from app.domain.clock import Clock
from app.domain.errors import FactInputInvalid, FactNotFound
from app.domain.ids import IdFactory, is_safe_id
from app.modules.storage import Database, capture_repo

TEXT_FIELD: Final = "text"
MAX_NOTE_CHARS: Final = 5000
MSG_NOTE_ADDED: Final = "Note added."
MSG_NOTE_DELETED: Final = "Note deleted."
MSG_NOTE_RESTORED: Final = "Note restored."
# A delete of a note that is already gone from the inbox (deleted or turned into a fact).
MSG_NOT_IN_INBOX: Final = "That note is no longer in the inbox."
# An undo of a note that wasn't deleted.
MSG_ALREADY_IN_INBOX: Final = "That note is already in the inbox."
_NOTE_NEEDED: Final = "Write something before adding the note."
_NOTE_TOO_LONG: Final = f"Keep a note to {MAX_NOTE_CHARS} characters or fewer."


@dataclass(frozen=True)
class NoteView:
    id: str
    text: str


def _not_found() -> FactNotFound:
    return FactNotFound(detail="no such note")


class CaptureService:
    """``db`` answers the open workspace's database (None while none is open)."""

    def __init__(self, db: Callable[[], Database | None], clock: Clock, ids: IdFactory) -> None:
        self._db = db
        self._clock = clock
        self._ids = ids

    def _open(self) -> Database:
        db = self._db()
        if db is None:  # setup sends every request to the guide until one is open
            raise RuntimeError("no workspace is open")
        return db

    def notes(self) -> tuple[NoteView, ...]:
        """The open notes, oldest first. Reads only."""
        with self._open().read() as conn:
            return tuple(NoteView(note.id, note.text) for note in capture_repo.list_open(conn))

    def open_note(self, note_id: str) -> NoteView | None:
        """The note, while it is open (not deleted, not turned into a fact). Reads only."""
        if not is_safe_id(note_id):
            return None
        with self._open().read() as conn:
            note = capture_repo.get_note(conn, note_id)
        return NoteView(note.id, note.text) if note is not None and note.open else None

    def add(self, text: str) -> NoteView:
        """Store a note. Raises ``FactInputInvalid`` (field ``text``) for a blank or too long
        one; nothing is stored then."""
        cleaned = text.replace("\x00", "").strip()
        if not cleaned:
            raise FactInputInvalid(TEXT_FIELD, _NOTE_NEEDED)
        if len(cleaned) > MAX_NOTE_CHARS:
            raise FactInputInvalid(TEXT_FIELD, _NOTE_TOO_LONG)
        note_id = self._ids.new()
        with self._open().transaction() as conn:
            capture_repo.insert_note(conn, note_id=note_id, text=cleaned, now=self._now())
        return NoteView(note_id, cleaned)

    def delete(self, note_id: str) -> bool:
        """Delete an open note softly, so it can be brought back; whether it was open (a
        note deleted or turned into a fact already is left as it is). Raises
        ``FactNotFound`` for an id that names no note."""
        with self._open().transaction() as conn:
            self._get(conn, note_id)
            return capture_repo.delete_note(conn, note_id, now=self._now())

    def undo_delete(self, note_id: str) -> bool:
        """Bring a deleted note back; whether it was deleted (any other note is left as it
        is). Raises ``FactNotFound`` for an id that names no note."""
        with self._open().transaction() as conn:
            self._get(conn, note_id)
            return capture_repo.undo_delete(conn, note_id, now=self._now())

    def _get(self, conn: sqlite3.Connection, note_id: str) -> capture_repo.StoredNote:
        if not is_safe_id(note_id):
            raise _not_found()
        note = capture_repo.get_note(conn, note_id)
        if note is None:
            raise _not_found()
        return note

    def _now(self) -> str:
        return self._clock.now().isoformat()
