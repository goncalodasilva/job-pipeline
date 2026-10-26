"""Settings > Your details: the contact details and the optional CV photo (M6.6; ADR-106 items 3
and 4; spec 2.9 rules 2, 3, 5 and 13; TD-280).

The details are the versioned setting ``personal_details`` (``SettingsService``); the photo is a
file ``assets/photo-<generated id>.<jpg|png>`` in the data folder that the setting names. Nothing
here is read by an AI task: personal details and the photo are never put in a request, a task row,
an event or a log. A refused value is reported by field name with fixed text; no entered value, no
file name and no validation-library text leaves this module.

Photo order (so a crash never loses the user's picture): validate and clean the bytes, write the
new file exclusively (mode 0600), flush it (and its folder) to disk, save the setting that names
it, and only then delete the old file; a failure at any step removes the new file and leaves
the setting as it was. A file the setting does not name, with a generated name, is an orphan;
``sweep_orphan_photos`` removes those when a workspace opens.
"""

import base64
import logging
import os
import re
import sqlite3
import stat
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from pydantic import ValidationError

from app.domain.clock import Clock
from app.domain.errors import DetailsNotSaved, PhotoNotSaved, VersionConflict
from app.domain.ids import IdFactory
from app.domain.images import (
    PHOTO_MAX_BYTES,
    PHOTO_MAX_PIXELS,
    PHOTO_MAX_SIDE,
    PHOTO_MIN_SIDE,
    InvalidImage,
    PhotoType,
    photo_dimensions,
    sniff_image,
    strip_metadata,
)
from app.domain.settings import PERSONAL_DETAILS, PersonalDetails, PhotoRef
from app.modules.storage import Database, get_setting
from app.services.settings import SettingsService

_log = logging.getLogger(__name__)

ASSETS_FOLDER: Final = "assets"
FIELDS: Final = ("name", "email", "phone", "location", "link_1", "link_2", "link_3")
LINK_FIELDS: Final = FIELDS[4:]
FILE_FIELD: Final = "file"
_ONE_LINE: Final = "Use one line of up to 100 characters, without special characters."
FIELD_MESSAGES: Final = {
    "name": _ONE_LINE,
    "email": "Write the email as name@example.com.",
    "phone": "Write 6 to 15 digits; spaces, + - ( ) and . are fine.",
    "location": _ONE_LINE,
    **dict.fromkeys(
        LINK_FIELDS, "Write the link as an http or https address of up to 300 characters."
    ),
}
NOT_A_PICTURE: Final = "Choose a JPEG or PNG picture."
TOO_BIG: Final = "The picture is too big. Choose one of 5 MB or less."
BAD_SIDES: Final = (
    f"The picture must be between {PHOTO_MIN_SIDE} and {PHOTO_MAX_SIDE} pixels on each side."
)
_GENERATED_PHOTO: Final = re.compile(r"photo-[A-Za-z0-9_-]{1,64}\.(?:jpg|png)")
_MEDIA_TYPE: Final = {"jpg": "image/jpeg", "png": "image/png"}
_EXTENSION: Final[Mapping[str, str]] = {"jpeg": "jpg", "png": "png"}


class DetailsInvalid(ValueError):
    """Some fields can't be saved. ``fields`` maps each field name to its fixed message."""

    def __init__(self, fields: Mapping[str, str]) -> None:
        super().__init__("invalid details")
        self.fields = dict(fields)


class PhotoInvalid(ValueError):
    """The picture is refused; ``message`` says why in fixed words."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class PhotoPreview:
    media_type: str
    data_base64: str


@dataclass(frozen=True)
class DetailsView:
    details: PersonalDetails
    version: int  # 0: nothing stored yet
    photo: PhotoPreview | None

    def values(self) -> dict[str, str]:
        """What the form shows in its inputs: the saved text, links shifted up."""
        saved = self.details
        shown = {
            "name": saved.name or "",
            "email": saved.email or "",
            "phone": saved.phone or "",
            "location": saved.location or "",
        }
        for index, field in enumerate(LINK_FIELDS):
            shown[field] = saved.links[index] if index < len(saved.links) else ""
        return shown


class PersonalDetailsService:
    def __init__(
        self,
        db: Callable[[], Database | None],
        clock: Clock,
        ids: IdFactory,
        data_dir: Callable[[], Path | None],
    ) -> None:
        self._db, self._clock, self._ids, self._data_dir = db, clock, ids, data_dir
        self._lock = threading.Lock()  # one write at a time, so file and setting stay in step

    def _settings(self) -> SettingsService:
        db = self._db()
        if db is None:
            raise DetailsNotSaved(detail="no workspace")
        return SettingsService(db, self._clock)

    def _assets(self) -> Path:
        folder = self._data_dir()
        if folder is None:
            raise PhotoNotSaved(detail="no workspace")
        return folder / ASSETS_FOLDER

    # --- reading ----------------------------------------------------------------------------

    def view(self) -> DetailsView:
        """The saved details, their version and the photo as an inline preview. Writes nothing."""
        loaded = self._settings().load(PERSONAL_DETAILS)
        return DetailsView(loaded.value, loaded.version, self._preview(loaded.value.photo))

    def _preview(self, ref: PhotoRef | None) -> PhotoPreview | None:
        if ref is None:
            return None
        try:
            assets = self._assets()
            if not stat.S_ISDIR(assets.lstat().st_mode):  # a link to a folder is not the folder
                return None
            data = self._read_photo(assets / ref.stored_name)
        except (OSError, PhotoNotSaved) as error:
            _log.warning("Couldn't read the stored photo (%s)", type(error).__name__)
            return None
        kind = None if data is None else sniff_image(data)
        extension = ref.stored_name.rsplit(".", 1)[-1]
        if data is None or kind is None or _EXTENSION[kind] != extension:
            return None
        return PhotoPreview(_MEDIA_TYPE[extension], base64.b64encode(data).decode("ascii"))

    @staticmethod
    def _read_photo(path: Path) -> bytes | None:
        """A regular file's bytes (never through a link), at most the photo cap. The file that
        was opened must be the one that was checked, so a swap in between reads nothing."""
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode):
            return None
        with open(path, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
                return None
            data = stream.read(PHOTO_MAX_BYTES + 1)
        return data if len(data) <= PHOTO_MAX_BYTES else None

    # --- details ----------------------------------------------------------------------------

    def save_details(self, values: Mapping[str, str], *, version: int) -> None:
        """Save the form's text fields (missing ones are blank), keeping the stored photo.
        ``DetailsInvalid`` (nothing stored), ``VersionConflict`` for a stale ``version``."""
        errors = _field_errors(values)
        if errors:
            raise DetailsInvalid(errors)
        links = tuple(
            link for field in LINK_FIELDS if (link := values.get(field, "").strip()) != ""
        )
        with self._lock:
            settings = self._settings()
            current = settings.load(PERSONAL_DETAILS)
            details = PersonalDetails.model_validate(
                {
                    **{field: values.get(field, "") for field in FIELDS[:4]},
                    "links": links,
                    "photo": current.value.photo,
                }
            )
            try:
                settings.save(PERSONAL_DETAILS, details, expected_version=version)
            except sqlite3.Error as error:
                raise DetailsNotSaved(detail=type(error).__name__) from None

    # --- photo ------------------------------------------------------------------------------

    def save_photo(self, data: bytes, *, version: int) -> None:
        """Check, clean and store ``data`` as the photo, replacing the one stored.
        ``PhotoInvalid`` (nothing stored), ``VersionConflict``, ``PhotoNotSaved``."""
        kind, cleaned = _clean_photo(data)
        with self._lock:
            settings = self._settings()
            current = settings.load(PERSONAL_DETAILS)
            if current.version != version:
                raise VersionConflict()
            name = f"photo-{self._ids.new()}.{_EXTENSION[kind]}"
            try:  # the reference is checked before any file is written
                ref = PhotoRef.model_validate({"stored_name": name})
            except ValidationError:
                raise PhotoNotSaved(detail="ValidationError") from None
            assets = self._assets()
            path = assets / name
            try:
                _prepare_folder(assets)
                _write_private(path, cleaned)
                _sync_folder(assets)
            except OSError as error:
                _remove_file(path)
                raise PhotoNotSaved(detail=type(error).__name__) from None
            changed = current.value.model_copy(update={"photo": ref})
            try:
                settings.save(PERSONAL_DETAILS, changed, expected_version=version)
            except sqlite3.Error as error:
                _remove_file(path)
                raise PhotoNotSaved(detail=type(error).__name__) from None
            except BaseException:  # a stale version, or anything else: no stray file
                _remove_file(path)
                raise
            if current.value.photo is not None:
                _remove_file(assets / current.value.photo.stored_name)

    def remove_photo(self, *, version: int) -> None:
        """Forget the photo, then delete its file. Nothing to remove is not an error."""
        with self._lock:
            settings = self._settings()
            current = settings.load(PERSONAL_DETAILS)
            if current.version != version:
                raise VersionConflict()
            photo = current.value.photo
            if photo is None:
                return
            try:
                settings.save(
                    PERSONAL_DETAILS,
                    current.value.model_copy(update={"photo": None}),
                    expected_version=version,
                )
            except sqlite3.Error as error:
                raise PhotoNotSaved(detail=type(error).__name__) from None
            _remove_file(self._assets() / photo.stored_name)


def _field_errors(values: Mapping[str, str]) -> dict[str, str]:
    """Each refused field with its fixed message. Every field is tried alone so a message is
    tied to the field it came from; the validation library's text is never used."""
    errors: dict[str, str] = {}
    for field in FIELDS:
        value = values.get(field, "")
        candidate = {"links": (value.strip(),)} if field in LINK_FIELDS else {field: value}
        if field in LINK_FIELDS and not value.strip():
            continue  # a blank link is dropped
        try:
            PersonalDetails.model_validate(candidate)
        except ValidationError:
            errors[field] = FIELD_MESSAGES[field]
    return errors


def _clean_photo(data: bytes) -> tuple[PhotoType, bytes]:
    """The picture's type and its bytes without metadata, or ``PhotoInvalid``."""
    if len(data) > PHOTO_MAX_BYTES:
        raise PhotoInvalid(TOO_BIG)
    kind = sniff_image(data)
    if kind is None:
        raise PhotoInvalid(NOT_A_PICTURE)
    try:
        cleaned = strip_metadata(data, kind)
    except InvalidImage:
        raise PhotoInvalid(NOT_A_PICTURE) from None
    size = photo_dimensions(cleaned, kind)
    if (
        size is None
        or not min(size) >= PHOTO_MIN_SIDE
        or max(size) > PHOTO_MAX_SIDE
        or size[0] * size[1] > PHOTO_MAX_PIXELS
    ):
        raise PhotoInvalid(BAD_SIDES)
    return kind, cleaned


def _prepare_folder(folder: Path) -> None:
    """Create ``assets/`` (0700) if missing and check it is a real folder, not a link."""
    folder.mkdir(mode=0o700, exist_ok=True)
    if not stat.S_ISDIR(folder.lstat().st_mode):
        raise NotADirectoryError("the assets folder is a link or not a folder")
    if os.name == "posix":
        folder.chmod(0o700)


def _write_private(path: Path, data: bytes) -> None:
    """Create ``path`` (never an existing file or a link) readable only by its owner; a failed
    write leaves no file."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())  # on disk before the setting names it
    except BaseException:
        _remove_file(path)
        raise


def _sync_folder(folder: Path) -> None:
    """Flush the folder's entry for the new file where the platform allows it (best effort)."""
    if os.name != "posix":
        return
    descriptor = os.open(folder, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _remove_file(path: Path) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError as error:
        _log.warning("Couldn't delete a stored photo file (%s)", type(error).__name__)


def sweep_orphan_photos(data_dir: Path, db: Database) -> int:
    """Remove the photo files the setting does not name (left by a kill between the file write
    and the setting save, or by a failed delete) and return how many went. Only regular files
    directly inside ``<data>/assets`` with a generated photo name are removed: never a link, a
    folder or another name, and nothing when the folder is missing or is itself a link, or when
    the stored setting can't be read (it might name a photo). Call it while the workspace
    opens. Blocking. Only the count is logged."""
    folder = data_dir / ASSETS_FOLDER
    try:
        if not stat.S_ISDIR(folder.lstat().st_mode):
            return 0
        with os.scandir(folder) as entries:
            found = {
                entry.name
                for entry in entries
                if _GENERATED_PHOTO.fullmatch(entry.name) and entry.is_file(follow_symlinks=False)
            }
    except OSError:  # no folder yet, or one that can't be read: nothing to sweep
        return 0
    if not found:
        return 0
    with db.read() as conn:
        stored = get_setting(conn, PERSONAL_DETAILS.key)
    keep: str | None = None
    if stored is not None:
        try:
            ref = PersonalDetails.model_validate_json(stored.value).photo
        except ValidationError:
            return 0
        keep = None if ref is None else ref.stored_name
    removed = 0
    for name in sorted(found - {keep}):
        try:
            os.unlink(folder / name)  # never follows a link
        except OSError:
            continue
        removed += 1
    if removed:
        _log.info("Removed %d stored photo file(s) no setting names", removed)
    return removed


__all__ = [
    "ASSETS_FOLDER",
    "FIELDS",
    "FIELD_MESSAGES",
    "FILE_FIELD",
    "LINK_FIELDS",
    "DetailsInvalid",
    "DetailsView",
    "PersonalDetailsService",
    "PhotoInvalid",
    "PhotoPreview",
    "sweep_orphan_photos",
]
