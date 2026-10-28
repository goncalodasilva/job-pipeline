"""Approved document exports and the read-only Submit checklist (M6.10; ADR-106 item 10).

Rendering and file operations run off the event loop. A service lock serializes exports; the
short commit transaction checks the current versions again before installing any files/rows.
An exported version is reused, including the contact details embedded at its first export.
"""

import asyncio
import base64
import hashlib
import os
import sqlite3
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from urllib.parse import urlsplit

from app.domain.clock import Clock
from app.domain.errors import ApplicationNotFound, RenderFailed, VersionConflict
from app.domain.ids import IdFactory, is_safe_id
from app.domain.rendering import PageSize, PhotoType, RenderDocument, RenderEntry, RenderSection
from app.modules.storage import Database, applications_repo, documents_repo, exports_repo
from app.render import DocumentRenderer, RenderedFile, RendererUnavailable
from app.services.document_views import DocumentView
from app.services.documents import DocumentService
from app.services.export_files import application_folder, read, write
from app.services.letters import LetterService, LetterView
from app.services.personal_details import PersonalDetailsService

NO_NAME: Final = "Add your name in Settings > Your details before exporting."
NOT_APPROVED: Final = "Approve the CV in step 3 before exporting."
NOT_SAVED: Final = "The files couldn't be saved. Nothing was changed. Try again."
PARTIAL_EXPORT: Final = (
    "Existing files have been kept. To add a missing PDF or Word file, create and approve "
    "a new document version in step 3 or 4."
)
MEDIA: Final = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


class ExportRefused(ValueError):
    pass


@dataclass(frozen=True)
class ExportInput:
    kind: str
    version_id: str
    number: int
    document: RenderDocument


@dataclass(frozen=True)
class SubmitView:
    application_id: str
    version: int
    applied: bool
    details: bool
    cv: bool
    letter: bool
    no_letter: bool
    files_done: bool
    pages: int | None
    max_pages: int | None
    files: tuple[exports_repo.StoredExport, ...]
    posting: str | None
    reason: str


def approved(view: DocumentView | LetterView) -> bool:
    return bool(view.version and view.version.status in ("approved", "sent"))


class ExportService:
    def __init__(
        self,
        database: Callable[[], Database | None],
        data_dir: Callable[[], Path | None],
        clock: Clock,
        ids: IdFactory,
        documents: DocumentService,
        letters: LetterService,
        details: PersonalDetailsService,
        renderers: Sequence[DocumentRenderer],
    ) -> None:
        self._database, self._data_dir = database, data_dir
        self._clock, self._ids = clock, ids
        self._documents, self._letters, self._details = documents, letters, details
        self._renderers = tuple(renderers)
        self._lock = threading.Lock()

    def _db(self) -> Database:
        db = self._database()
        if db is None:
            raise ApplicationNotFound()
        return db

    def view(self, aid: str) -> SubmitView:
        cv, letter = self._documents.view(aid), self._letters.view(aid)
        with self._db().read() as conn:
            application = applications_repo.get_application(conn, aid)
            rows = exports_repo.listing(conn, aid)
            posting = exports_repo.posting(conn, aid)
        if application is None:
            raise ApplicationNotFound()
        # The link is data, with an http(s) scheme only; Jinja escapes the attribute.
        if posting and (
            urlsplit(posting).scheme not in ("http", "https") or not urlsplit(posting).hostname
        ):
            posting = None
        name = bool(cv.contact.name)
        current = {v.version.id for v in (cv, letter) if approved(v) and v.version}
        files = tuple(row for row in rows if row.document_version_id in current)
        cv_pdf = next((row for row in files if row.kind == "cv" and row.format == "pdf"), None)
        all_files = approved(cv) and all(
            {r.format for r in files if r.document_version_id == vid} == {"pdf", "docx"}
            for vid in current
        )
        return SubmitView(
            aid,
            application.version,
            bool(application.applied_at),
            name,
            approved(cv),
            approved(letter),
            letter.version is None,
            all_files,
            cv_pdf.pages if cv_pdf else None,
            cv.template.max_pages if cv.template else None,
            files,
            posting,
            NO_NAME if not name else NOT_APPROVED if not approved(cv) else "",
        )

    async def export(self, aid: str) -> str:
        return await asyncio.to_thread(self._export, aid)

    def _inputs(self, aid: str) -> tuple[ExportInput, ...]:
        cv, letter = self._documents.view(aid), self._letters.view(aid)
        if not cv.contact.name:
            raise ExportRefused(NO_NAME)
        if not approved(cv) or cv.version is None or cv.template is None:
            raise ExportRefused(NOT_APPROVED)
        sections = []
        for section in cv.sections:
            entries = []
            for block in section.blocks:
                h = block.header
                heading = (
                    " · ".join(
                        str(s)
                        for s in (
                            h.title,
                            h.employer,
                            h.start_text,
                            h.end_text or ("present" if h.current else None),
                        )
                        if s
                    )
                    if h
                    else ""
                )
                entries.append(RenderEntry(heading, tuple(u.text for u in block.units)))
            sections.append(RenderSection(section.heading, entries=tuple(entries)))
        photo = None
        photo_type: PhotoType | None = None
        if cv.template.photo_slot:
            preview = self._details.view().photo
            if preview:
                photo = base64.b64decode(preview.data_base64)
                photo_type = "png" if preview.media_type == "image/png" else "jpeg"
        page: PageSize = "A4" if cv.template.page_size == "A4" else "Letter"
        rendered_cv = RenderDocument(
            "CV",
            cv.contact.name,
            page,
            (cv.contact.name, *cv.contact.lines),
            tuple(sections),
            photo,
            photo_type,
        )
        inputs = [ExportInput("cv", cv.version.id, cv.version.number, rendered_cv)]
        if approved(letter) and letter.version:
            f = letter.frame
            document = RenderDocument(
                "Cover letter",
                f.name,
                page,
                sections=(
                    RenderSection(
                        "",
                        (f.greeting, f.opening, *(u.text for u in letter.units), f.closing, f.name),
                    ),
                ),
            )
            inputs.append(
                ExportInput("cover_letter", letter.version.id, letter.version.number, document)
            )
        return tuple(inputs)

    def _export(self, aid: str) -> str:
        with self._lock:
            inputs = self._inputs(aid)
            db, root = self._db(), self._data_dir()
            if root is None:
                raise ApplicationNotFound()
            with db.read() as conn:
                existing = {
                    (r.document_version_id, r.format) for r in exports_repo.listing(conn, aid)
                }
            pending: list[tuple[ExportInput, str, RenderedFile]] = []
            unavailable: list[str] = []
            for item in inputs:
                formats = {fmt for vid, fmt in existing if vid == item.version_id}
                if formats:
                    # Live details and role headers may have changed since the first export.
                    # Never add a different rendition to that immutable file set.
                    if formats != {"pdf", "docx"}:
                        unavailable.append(PARTIAL_EXPORT)
                    continue
                for renderer in self._renderers:
                    if renderer.format not in MEDIA:
                        raise RenderFailed()
                    try:
                        pending.append((item, renderer.format, renderer.render(item.document)))
                    except RendererUnavailable as error:
                        unavailable.append(str(error))
            if not pending:
                return " ".join(dict.fromkeys(unavailable))
            created: list[str] = []
            try:
                with application_folder(root, aid, create=True) as folder:
                    try:
                        with db.transaction() as conn:
                            # An edit during rendering must not publish files for a stale version.
                            for item in inputs:
                                doc = documents_repo.document_for_application(conn, aid, item.kind)
                                if doc is None or doc.current_version_id != item.version_id:
                                    raise VersionConflict()
                            now = self._clock.now().isoformat()
                            for item, fmt, result in pending:
                                prefix = "cv" if item.kind == "cv" else "cover-letter"
                                suffix = hashlib.sha256(item.version_id.encode()).hexdigest()[:24]
                                name = f"{prefix}-v{item.number}-{suffix}.{fmt}"
                                write(folder, name, result.content, created)
                                row = exports_repo.StoredExport(
                                    self._ids.new(),
                                    item.version_id,
                                    fmt,
                                    name,
                                    hashlib.sha256(result.content).hexdigest(),
                                    len(result.content),
                                    result.page_count,
                                    item.kind,
                                    item.number,
                                )
                                exports_repo.insert(conn, row, now)
                            os.fsync(folder)
                    except BaseException:
                        for name in created:
                            os.unlink(name, dir_fd=folder)
                        raise
            except (OSError, sqlite3.Error):
                raise ExportRefused(NOT_SAVED) from None
            return " ".join(dict.fromkeys(unavailable))

    def download(self, aid: str, export_id: str) -> tuple[exports_repo.StoredExport, bytes] | None:
        if not is_safe_id(aid) or not is_safe_id(export_id):
            return None
        with self._db().read() as conn:
            row = next((r for r in exports_repo.listing(conn, aid) if r.id == export_id), None)
        root = self._data_dir()
        if row is None or root is None or row.format not in MEDIA:
            return None
        try:
            with application_folder(root, aid) as folder:
                content = read(folder, row.stored_name, row.size_bytes)
            if hashlib.sha256(content).hexdigest() != row.sha256:
                return None
            return row, content
        except OSError:
            return None
