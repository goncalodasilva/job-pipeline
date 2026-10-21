"""Contacts and interactions (M5.6): the writes behind the company page and the drawer.

A contact belongs to one company; an application can link only its own company's contacts, and
contacts are never deleted in M5 (only unlinked). Contact and interaction text is untrusted data
and is never logged here. Reads of these live in the company and application-detail services.
"""

import sqlite3
from collections.abc import Callable, Mapping
from datetime import UTC

from app.domain.clock import Clock
from app.domain.contacts import (
    NOT_THIS_COMPANY,
    ContactInvalid,
    checked_contact,
    checked_interaction,
    interaction_instant,
)
from app.domain.errors import ApplicationNotFound, VersionConflict
from app.domain.ids import IdFactory, is_safe_id
from app.modules.storage import Database, applications_repo, companies_repo, contacts_repo
from app.modules.storage import application_detail_repo as detail_repo

NO_COMPANY = "This job has no company yet, so it can't have contacts."


class ContactsService:
    def __init__(
        self, database: Callable[[], Database | None], clock: Clock, ids: IdFactory
    ) -> None:
        self._database, self._clock, self._ids = database, clock, ids

    def _now(self) -> str:
        return self._clock.now().astimezone(UTC).isoformat()

    def _company_db(self, company_id: str) -> Database:
        db = self._database()
        if db is None or not is_safe_id(company_id):
            raise LookupError
        return db

    def _application_db(self, application_id: str) -> Database:
        db = self._database()
        if db is None or not is_safe_id(application_id):
            raise ApplicationNotFound()
        return db

    # --- the company page ---------------------------------------------------------------

    def company_exists(self, company_id: str) -> bool:
        try:
            db = self._company_db(company_id)
        except LookupError:
            return False
        with db.read() as conn:
            return companies_repo.get(conn, company_id) is not None

    def contact_exists(self, company_id: str, contact_id: str) -> bool:
        """Whether the company has this contact (an unsafe or other company's id is False)."""
        if not is_safe_id(contact_id):
            return False
        try:
            db = self._company_db(company_id)
        except LookupError:
            return False
        with db.read() as conn:
            found = contacts_repo.get(conn, contact_id)
            return found is not None and found["company_id"] == company_id

    def add(self, company_id: str, values: Mapping[str, str]) -> str:
        """Add a contact to the company; returns its id. ``LookupError`` for an unknown company,
        ``ContactInvalid`` for fields to correct (nothing is saved)."""
        db = self._company_db(company_id)
        checked = checked_contact(values)
        with db.transaction() as conn:
            if companies_repo.get(conn, company_id) is None:
                raise LookupError
            contact_id = self._ids.new()
            contacts_repo.insert(
                conn, id=contact_id, company_id=company_id, values=checked, now=self._now()
            )
        return contact_id

    def edit(
        self, company_id: str, contact_id: str, values: Mapping[str, str], *, expected_version: int
    ) -> None:
        """Save the six fields. ``LookupError`` when the company has no such contact,
        ``VersionConflict`` for a stale version, ``ContactInvalid`` for a field to correct."""
        if not is_safe_id(contact_id):
            raise LookupError
        with self._company_db(company_id).transaction() as conn:
            current = contacts_repo.get(conn, contact_id)
            if current is None or current["company_id"] != company_id:
                raise LookupError
            if current["version"] != expected_version:
                raise VersionConflict()
            checked = checked_contact(values)
            if all(checked[field] == current[field] for field in checked):
                return
            contacts_repo.update(
                conn,
                contact_id,
                values=checked,
                expected_version=expected_version,
                now=self._now(),
            )

    # --- the application drawer ---------------------------------------------------------

    def link(self, application_id: str, contact_id: str) -> None:
        """Link one of the application's company's contacts (linking twice changes nothing).
        ``ContactInvalid`` on ``contact_id`` for anything else."""
        with self._application_db(application_id).transaction() as conn:
            company_id = self._company_of(conn, application_id)
            if not self._is_company_contact(conn, company_id, contact_id):
                raise ContactInvalid({"contact_id": NOT_THIS_COMPANY})
            now = self._now()
            if contacts_repo.link(conn, application_id, contact_id, now):
                applications_repo.touch_application(conn, application_id, now)

    def add_and_link(self, application_id: str, values: Mapping[str, str]) -> None:
        """Add a contact to the application's company and link it, in one step."""
        db = self._application_db(application_id)
        with db.transaction() as conn:
            company_id = self._company_of(conn, application_id)
            checked = checked_contact(values)
            if company_id is None:
                raise ContactInvalid({"name": NO_COMPANY})
            now = self._now()
            contact_id = self._ids.new()
            contacts_repo.insert(
                conn, id=contact_id, company_id=company_id, values=checked, now=now
            )
            contacts_repo.link(conn, application_id, contact_id, now)
            applications_repo.touch_application(conn, application_id, now)

    def unlink(self, application_id: str, contact_id: str) -> bool:
        """Remove the link only. False for an id that can't name a contact (nothing is done)."""
        with self._application_db(application_id).transaction() as conn:
            self._company_of(conn, application_id)
            if not is_safe_id(contact_id):
                return False
            contacts_repo.unlink(conn, application_id, contact_id)
            applications_repo.touch_application(conn, application_id, self._now())
        return True

    def log_interaction(self, application_id: str, values: Mapping[str, str]) -> None:
        """Log an interaction on the application, optionally with one of the company's
        contacts. ``ContactInvalid`` with an error per field to correct."""
        with self._application_db(application_id).transaction() as conn:
            company_id = self._company_of(conn, application_id)
            errors: dict[str, str] = {}
            checked = None
            try:
                checked = checked_interaction(values)
            except ContactInvalid as error:
                errors.update(error.errors)
            contact_id = values.get("contact_id", "")
            if contact_id and not self._is_company_contact(conn, company_id, contact_id):
                errors["contact_id"] = NOT_THIS_COMPANY
            if errors or checked is None:
                raise ContactInvalid(errors)
            now = self._now()
            contacts_repo.insert_interaction(
                conn,
                id=self._ids.new(),
                contact_id=contact_id or None,
                application_id=application_id,
                happened_at=interaction_instant(checked.day).isoformat(),
                channel=checked.channel,
                note=checked.note,
                now=now,
            )
            applications_repo.touch_application(conn, application_id, now)

    @staticmethod
    def _company_of(conn: sqlite3.Connection, application_id: str) -> str | None:
        found = detail_repo.header(conn, application_id)
        if found is None:
            raise ApplicationNotFound()
        company_id = found["company_id"]
        return company_id if isinstance(company_id, str) else None

    @staticmethod
    def _is_company_contact(
        conn: sqlite3.Connection, company_id: str | None, contact_id: str
    ) -> bool:
        if company_id is None or not is_safe_id(contact_id):
            return False
        found = contacts_repo.get(conn, contact_id)
        return found is not None and found["company_id"] == company_id
