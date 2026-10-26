"""M6.6 the optional CV photo: upload by content, limits, metadata removal, replace and remove,
private generated file names, orphans, the protected inline preview (AC-M6.6 groundwork;
ADR-106 item 4; spec 2.9 rules 5 and 13).

The routes, markup and messages are described in ``tests/details_pages.py``. In short:
``POST /settings/details/photo`` (multipart ``file`` and ``version``) validates the bytes by
content (``app/domain/images.py``: JPEG or PNG, at most 5 MB, 100 to 4000 px a side read from the
headers), strips the metadata, writes ``assets/photo-<generated id>.<jpg|png>`` with
``os.open(..., O_WRONLY | O_CREAT | O_EXCL, 0o600)`` BEFORE saving the reference in the
``personal_details`` setting (``SettingsService.save``), then deletes the old file; ``POST
/settings/details/photo/remove`` saves the setting without the photo, then deletes the file.
Photo refusals are 422s tied to the ``file`` control; their copy mentions the facts: the type error
names "JPEG" and "PNG", the size error "5 MB", the side error "100" and "4000".

``sweep_orphan_photos`` is NOT called by the tests: the workspace open does it (like the CV
imports), removing regular files directly inside ``assets/`` whose names have the generated
shape ``photo-<id>.jpg|png`` and that the setting does not name. A real migrated SQLite database
in a temporary folder, the fake provider, no network; every picture is generated.
"""

import os
import re
import sqlite3
import stat
import threading
from pathlib import Path
from typing import Any

import pytest

from app.domain.errors import VersionConflict
from app.services.settings import SettingsService
from tests.cv_import_harness import importing
from tests.details_pages import (
    DETAILS,
    GOOD,
    INVALID,
    PHOTO,
    PHOTO_REMOVE,
    Desk,
    announced,
    autofocus_in_section,
    details_app,
    done_text,
    element,
    errors_in,
    invalid_names,
    photo_name_in,
    preview_bytes,
)
from tests.image_support import (
    ICC_DATA,
    TRAILER,
    TRAILER_MARK,
    comment_segment,
    contains_secret,
    exif_gps_segment,
    icc_chunk,
    icc_segments,
    iptc_segment,
    jfif_segment,
    jpeg_frame_size,
    jpeg_icc,
    jpeg_image,
    mpf_segment,
    padded_jpeg,
    png_chunk,
    png_image,
    png_pixels,
    png_size,
    png_types,
    read_jpeg,
    text_chunks,
    without_trailer,
    xmp_segment,
)
from tests.leak_scan import RawLogRecords
from tests.upload_bodies import FieldPart, FilePart, multipart_body

pytestmark = pytest.mark.integration

MIB = 1024 * 1024
STORED_NAME = r"photo-[A-Za-z0-9_-]{1,64}\.(?:jpg|png)"
JPEG, PNG = jpeg_image(120, 150), png_image(100, 120)


@pytest.fixture
def desk(tmp_path):
    with details_app(tmp_path) as made:
        yield made


def refused_with(response: Any, *needles: str) -> str:
    """The photo error: a 422 tied to the ``file`` control whose text holds every needle."""
    assert response.status_code == 422, (response.status_code, response.text[:200])
    assert invalid_names(response.text) == ["file"], invalid_names(response.text)
    message = errors_in(response.text)["file"]
    for needle in needles:
        assert needle in message, (needle, message)
    assert announced(response).startswith("Photo not saved")
    focused = autofocus_in_section(response.text)
    assert len(focused) == 1 and focused[0].attrs.get("name") == "file"
    return message


def stored_bytes(desk: Desk) -> bytes:
    name = desk.photo_name()
    assert name is not None, "no photo is stored"
    return (desk.assets / name).read_bytes()


# --- by content only, and the limits ---------------------------------------------------------


ACCEPTED = {
    "png_named_txt": (PNG, "notes.txt", "text/plain", "png", "image/png"),
    "png_declared_jpeg": (PNG, "me.jpg", "image/jpeg", "png", "image/png"),
    "jpeg_named_png": (JPEG, "me.png", "image/png", "jpg", "image/jpeg"),
    "jpeg_no_name_no_type": (JPEG, "", "application/octet-stream", "jpg", "image/jpeg"),
    "jpeg_odd_name": (JPEG, "a b/../c.PNG", "application/x-evil", "jpg", "image/jpeg"),
}
REFUSED = {
    "gif": b"GIF89a" + bytes(200),
    "webp": b"RIFF\x24\x00\x00\x00WEBPVP8 " + bytes(200),
    "bmp": b"BM" + bytes(200),
    "pdf": b"%PDF-1.4\n" + bytes(200),
    "svg": b'<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200"/>',
    "html": b"<!doctype html><script>alert(1)</script>",
    "zip": b"PK\x03\x04" + bytes(200),
    "text": b"image/png me.png",
    "jpeg_magic_in_the_middle": b"\x00" * 10 + JPEG,
    "gif_then_jpeg": b"GIF89a" + JPEG,
    "truncated_jpeg": JPEG[:60],
    "truncated_png": PNG[:70],
    "png_bad_crc": PNG[:45] + bytes([PNG[45] ^ 0xFF]) + PNG[46:],
    "jpeg_without_end": jpeg_image(120, 150, end=False),
    "jpeg_zero_side": jpeg_image(0, 150),
}


def test_photo_accepts_png_and_jpeg_by_content_only(desk: Desk) -> None:
    for label, (data, filename, declared, extension, media) in ACCEPTED.items():
        answer = desk.upload(data, filename=filename, content_type=declared)

        assert answer.status_code == 200, (label, answer.status_code, answer.text[:300])
        name = desk.photo_name()
        assert name is not None and name.endswith(f".{extension}"), label
        assert desk.names() == [name], f"{label}: one photo file, the previous one is replaced"
        assert preview_bytes(answer.text) == (media, stored_bytes(desk)), label
        assert done_text(answer.text) == "Photo saved." == announced(answer)
        assert len(autofocus_in_section(answer.text)) == 1

    assert desk.run.spy.requests == [], "a photo is never sent to the AI"


@pytest.mark.parametrize("label", sorted(REFUSED))
def test_photo_refuses_what_is_not_a_jpeg_or_png_by_content(desk: Desk, label: str) -> None:
    assert desk.upload(JPEG).status_code == 200  # a stored photo must survive a refusal
    before = desk.snapshot()

    # The declared type and the name say it is fine; the content decides.
    answer = desk.upload(REFUSED[label], filename="me.jpg", content_type="image/jpeg")

    refused_with(answer, "JPEG", "PNG") if label != "jpeg_zero_side" else refused_with(answer)
    assert desk.snapshot() == before and stored_bytes(desk) != REFUSED[label]
    assert desk.run.spy.requests == []


def test_photo_with_nothing_or_the_wrong_part_is_refused(desk: Desk) -> None:
    version = str(desk.version())
    bodies = {
        "no file": multipart_body(FieldPart(version, name="version")),
        "empty file": multipart_body(
            FieldPart(version, name="version"), FilePart("me.jpg", b"", content_type="image/jpeg")
        ),
        "a pasted text": multipart_body(
            FieldPart(version, name="version"), FieldPart("data:image/png;base64,AAAA")
        ),
        "file in another field": multipart_body(
            FieldPart(version, name="version"), FilePart("me.jpg", JPEG, name="avatar")
        ),
    }
    for label, body in bodies.items():
        answer = desk.upload_body(body)
        assert answer.status_code == 422, (label, answer.status_code)
        assert invalid_names(answer.text) == ["file"], label
    not_multipart = desk.post(PHOTO, {"version": version, "file": "x"})
    assert not_multipart.status_code in (400, 415, 422)
    assert desk.row() is None and desk.files() == []


def test_photo_size_and_dimension_limits(desk: Desk) -> None:
    # Five megabytes exactly is fine; one byte more is refused with the size in words.
    exact = desk.upload(padded_jpeg(5 * MIB))
    assert exact.status_code == 200, exact.text[:300]
    assert len(stored_bytes(desk)) <= 5 * MIB
    before = desk.snapshot()
    refused_with(desk.upload(padded_jpeg(5 * MIB + 1)), "5 MB")
    assert desk.snapshot() == before

    # Far over the upload cap: still a refusal that stores nothing.
    huge = desk.upload(padded_jpeg(11 * MIB))
    assert huge.status_code in (413, 422)
    if huge.status_code == 422:
        refused_with(huge, "5 MB")
    assert desk.snapshot() == before

    inside = {
        "jpeg 100x100": jpeg_image(100, 100),
        "png 100x100": png_image(100, 100),
        "jpeg 4000x4000": jpeg_image(4000, 4000),
        "png 4000x100": png_image(4000, 100),
        "jpeg 100x4000": jpeg_image(100, 4000),
    }
    for label, data in inside.items():
        answer = desk.upload(data)
        assert answer.status_code == 200, (label, answer.status_code, answer.text[:200])

    outside = {
        "jpeg 99x100": jpeg_image(99, 100),
        "jpeg 100x99": jpeg_image(100, 99),
        "png 99x99": png_image(99, 99),
        "png 99x4000": png_image(99, 4000),
        "jpeg 4001x100": jpeg_image(4001, 100),
        "png 100x4001": png_image(100, 4001),
        "jpeg 4001x4001": jpeg_image(4001, 4001),
        "jpeg 65535x65535": jpeg_image(65535, 65535),
        "png 2^31-1 square": png_image(2**31 - 1, 2**31 - 1),
        "png 2^32-1 x 100": png_image(2**32 - 1, 100),
    }
    kept = desk.snapshot()
    for label, data in outside.items():
        answer = desk.upload(data)
        message = refused_with(answer)
        assert "100" in message and "4000" in message, f"{label}: {message}"
        assert desk.snapshot() == kept, label


# --- metadata ----------------------------------------------------------------------------------

METADATA = {
    "jpeg_exif_gps": (
        jpeg_image(
            120,
            150,
            before=(jfif_segment(), exif_gps_segment(), iptc_segment(), comment_segment()),
            after_frame=(comment_segment(),),
            trailer=TRAILER,
        ),
        "jpg",
    ),
    "jpeg_xmp": (
        jpeg_image(
            120,
            150,
            before=(xmp_segment(), mpf_segment(), *icc_segments(2), exif_gps_segment()),
            trailer=TRAILER,
        ),
        "jpg",
    ),
    "png_text": (
        png_image(
            100,
            120,
            before_idat=(*text_chunks()[:3], icc_chunk()),
            after_idat=(*text_chunks()[3:], png_chunk(b"prVt", b"FICTIONAL-PRIVATE-CHUNK-payload")),
            trailer=TRAILER,
        ),
        "png",
    ),
}


@pytest.mark.parametrize("case", sorted(METADATA))
def test_photo_metadata_stripped(desk: Desk, case: str) -> None:
    original, extension = METADATA[case]
    assert contains_secret(original), "the fixture does hold private metadata"
    assert TRAILER_MARK in original, "the fixture has junk after its end marker"
    parsable = without_trailer(original)  # the strict readers parse up to the end marker

    answer = desk.upload(original, filename="holiday-in-secret-place.jpg")

    assert answer.status_code == 200, answer.text[:300]
    stored = stored_bytes(desk)
    assert desk.photo_name().endswith(f".{extension}")  # type: ignore[union-attr]
    assert contains_secret(stored) == [], contains_secret(stored)
    assert TRAILER_MARK not in stored, "trailing data is dropped"
    if extension == "jpg":
        assert read_jpeg(stored).scan == read_jpeg(parsable).scan, "the pixels are untouched"
        assert jpeg_frame_size(stored) == jpeg_frame_size(parsable)
        assert jpeg_icc(stored) == jpeg_icc(parsable)
        assert 0xE1 not in read_jpeg(stored).markers and 0xFE not in read_jpeg(stored).markers
        assert 0xED not in read_jpeg(stored).markers
        if jpeg_icc(parsable):
            assert all(ICC_DATA in profile for profile in jpeg_icc(stored))
    else:
        assert png_pixels(stored) == png_pixels(parsable), "the pixels are untouched"
        assert png_size(stored) == png_size(parsable)
        types = png_types(stored)
        assert b"iCCP" in types, "the colour profile stays"
        for gone in (b"tEXt", b"zTXt", b"iTXt", b"tIME", b"eXIf", b"prVt"):
            assert gone not in types, gone
    # What the page shows back is the stripped file, and the original is stored nowhere.
    assert preview_bytes(answer.text) == (
        "image/jpeg" if extension == "jpg" else "image/png",
        stored,
    )
    for path in desk.data_dir.rglob("*"):
        if path.is_file():
            assert contains_secret(path.read_bytes()) == [], f"{path.name} holds metadata"


# --- replace, remove and failure ---------------------------------------------------------------


class Order:
    """What the app did and when: each save of the personal_details setting (with the asset files
    that existed at that moment) and each deletion of a photo file (with the photo the stored
    setting named at that moment, read through another connection)."""

    def __init__(self, desk: Desk, monkeypatch: pytest.MonkeyPatch) -> None:
        self.events: list[tuple[Any, ...]] = []
        self.desk = desk
        self._lock = threading.Lock()
        original_save = SettingsService.save
        order = self

        def save(service: Any, spec: Any, value: Any, *, expected_version: int) -> Any:
            if spec.key == "personal_details":
                photo = None if value.photo is None else value.photo.stored_name
                order.note(("save", photo, order.names()))
            return original_save(service, spec, value, expected_version=expected_version)

        monkeypatch.setattr(SettingsService, "save", save)
        for attribute in ("unlink", "remove"):
            monkeypatch.setattr(os, attribute, self._watching(getattr(os, attribute)))

    def names(self) -> list[str]:
        return self.desk.names()

    def note(self, event: tuple[Any, ...]) -> None:
        with self._lock:
            self.events.append(event)

    def stored_photo(self) -> str | None:
        connection = sqlite3.connect(self.desk.data_dir / "app.db", timeout=5)
        try:
            row = connection.execute(
                "SELECT json_extract(value, '$.photo.stored_name') FROM settings "
                "WHERE key = 'personal_details'"
            ).fetchone()
        finally:
            connection.close()
        return None if row is None else row[0]

    def _watching(self, real: Any) -> Any:
        def watch(path: Any, *args: Any, **kwargs: Any) -> Any:
            try:
                target = Path(os.fsdecode(path))
            except TypeError:
                return real(path, *args, **kwargs)
            if target.name.startswith("photo-") and target.parent.name == "assets":
                self.note(("delete", target.name, self.stored_photo(), self.names()))
            return real(path, *args, **kwargs)

        return watch

    def of(self, kind: str) -> list[tuple[Any, ...]]:
        return [event for event in self.events if event[0] == kind]


def test_photo_replace_remove_and_failure_leave_no_stray_file(
    desk: Desk, monkeypatch: pytest.MonkeyPatch
) -> None:
    order = Order(desk, monkeypatch)

    # The first photo: the file is written before the setting that names it.
    assert desk.upload(JPEG).status_code == 200
    first = desk.photo_name()
    assert first is not None and desk.names() == [first]
    ((_, saved_photo, files_then),) = order.of("save")
    assert saved_photo == first and first in files_then, "file first, then the setting"
    assert order.of("delete") == []

    # Replace: the new file exists before the setting is saved; the old one goes only after.
    order.events.clear()
    assert desk.upload(PNG).status_code == 200
    second = desk.photo_name()
    assert second is not None and second != first and second.endswith(".png")
    assert desk.names() == [second], "the old file is deleted"
    ((_, saved_photo, files_then),) = order.of("save")
    assert saved_photo == second and sorted(files_then) == sorted([first, second])
    ((_, deleted, named_then, files_when_deleted),) = order.of("delete")
    assert deleted == first and named_then == second, "deleted after the setting was saved"
    assert second in files_when_deleted
    assert order.events.index(order.of("save")[0]) < order.events.index(order.of("delete")[0])

    # Remove: the setting no longer names it, then the file is deleted.
    order.events.clear()
    removed = desk.remove()
    assert removed.status_code == 200 and done_text(removed.text) == "Photo removed."
    assert announced(removed) == "Photo removed."
    assert desk.photo_name() is None and desk.files() == []
    assert element(removed.text, "photo-preview") is None
    assert element(removed.text, "photo-empty") is not None
    assert element(removed.text, "photo-remove-form") is None, "nothing to remove any more"
    ((_, saved_photo, _),) = order.of("save")
    ((_, deleted, named_then, _),) = order.of("delete")
    assert (saved_photo, deleted, named_then) == (None, second, None)
    assert desk.stored()["photo"] is None  # type: ignore[index]
    monkeypatch.undo()

    # A failed setting write leaves no new file, and the old photo and setting as they were.
    assert desk.upload(JPEG).status_code == 200
    kept_name, kept_bytes = desk.photo_name(), stored_bytes(desk)
    row_before = desk.row()
    desk.run.execute(
        "CREATE TRIGGER fictional_block_update BEFORE UPDATE ON settings "
        "WHEN NEW.key = 'personal_details' BEGIN SELECT RAISE(ABORT, 'fictional failure'); END"
    )
    failed = desk.upload(PNG)
    assert failed.status_code >= 400 and failed.status_code != 409
    assert "fictional failure" not in failed.text
    assert desk.row() == row_before and desk.names() == [kept_name]
    assert stored_bytes(desk) == kept_bytes
    failed_remove = desk.remove()
    assert failed_remove.status_code >= 400
    assert desk.row() == row_before and desk.names() == [kept_name], "a failed remove keeps it"
    desk.run.execute("DROP TRIGGER fictional_block_update")
    assert desk.upload(PNG).status_code == 200, "the app still works afterwards"
    assert len(desk.files()) == 1


def test_photo_first_upload_failures_leave_no_file_and_no_setting(desk: Desk) -> None:
    desk.run.execute(
        "CREATE TRIGGER fictional_block_insert BEFORE INSERT ON settings "
        "WHEN NEW.key = 'personal_details' BEGIN SELECT RAISE(ABORT, 'fictional failure'); END"
    )

    failed = desk.upload(JPEG)

    assert failed.status_code >= 400
    assert desk.row() is None and desk.files() == []
    desk.run.execute("DROP TRIGGER fictional_block_insert")

    # The file cannot be written: nothing is saved either.
    if desk.assets.exists():
        for path in desk.assets.iterdir():
            path.unlink()
        desk.assets.rmdir()
    desk.assets.write_bytes(b"not a folder")
    blocked = desk.upload(JPEG)
    assert blocked.status_code >= 400 and desk.row() is None
    assert desk.assets.read_bytes() == b"not a folder"
    desk.assets.unlink()
    assert desk.upload(JPEG).status_code == 200, "recovers once the folder is usable"


def test_photo_a_failed_delete_of_the_old_file_does_not_fail_the_replace(
    desk: Desk, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert desk.upload(JPEG).status_code == 200
    old = desk.photo_name()
    real_unlink, real_remove = os.unlink, os.remove

    def refusing(real: Any) -> Any:
        def call(path: Any, *args: Any, **kwargs: Any) -> Any:
            if str(os.fsdecode(path)).endswith(str(old)):
                raise PermissionError("fictional: cannot delete")
            return real(path, *args, **kwargs)

        return call

    monkeypatch.setattr(os, "unlink", refusing(real_unlink))
    monkeypatch.setattr(os, "remove", refusing(real_remove))

    answer = desk.upload(PNG)
    monkeypatch.undo()

    assert answer.status_code == 200, "the new photo is saved even if the old file stays"
    new = desk.photo_name()
    assert new is not None and new != old and new in desk.names() and old in desk.names()


def test_photo_upload_and_remove_are_versioned(desk: Desk) -> None:
    assert desk.save(0).status_code == 200 and desk.version() == 1

    stale = desk.upload(JPEG, version=0)
    assert stale.status_code == 409 and VersionConflict.message in stale.text
    assert "Reload" in stale.text
    assert desk.files() == [] and desk.version() == 1, "a stale upload leaves no file"
    assert desk.upload(JPEG, version=1).status_code == 200 and desk.version() == 2

    before = desk.snapshot()
    stale_remove = desk.remove(1)
    assert stale_remove.status_code == 409 and VersionConflict.message in stale_remove.text
    assert desk.snapshot() == before

    missing = desk.upload_body(multipart_body(FilePart("me.jpg", JPEG)))
    assert missing.status_code == 400 and missing.text == INVALID
    odd = desk.upload(JPEG, version="abc")
    assert odd.status_code == 400 and odd.text == INVALID
    assert desk.post(PHOTO_REMOVE, {}).status_code == 400
    assert desk.post(PHOTO_REMOVE, {"version": "2", "name": "x"}).status_code == 400
    assert desk.snapshot() == before

    # Saving the details later keeps the photo.
    photo = desk.photo_name()
    saved = desk.save(2, name="Zara Quillfeather-Marker")
    assert saved.status_code == 200 and photo is not None
    assert desk.photo_name() == photo and desk.names() == [photo]
    assert preview_bytes(saved.text) is not None


def test_first_photo_without_any_saved_details_creates_the_setting(desk: Desk) -> None:
    assert desk.row() is None

    answer = desk.upload(JPEG, version=0)

    assert answer.status_code == 200
    stored = desk.stored()
    assert stored is not None and stored["name"] is None and stored["links"] == []
    assert desk.version() == 1


# --- names, permissions, the client's file name ------------------------------------------------


def test_photo_files_0600_generated_names_no_client_name(
    desk: Desk, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = RawLogRecords()
    raw.install(monkeypatch)
    client_name = "Ana-Sample-passport-CLIENTNAME-9d2f.jpg"
    opened: list[tuple[str, int, int]] = []
    real_open = os.open

    def spy(path: Any, flags: int, mode: int = 0o777, **kwargs: Any) -> int:
        if "assets" in str(path):
            opened.append((str(path), flags, mode))
        return real_open(path, flags, mode, **kwargs)

    monkeypatch.setattr(os, "open", spy)
    previous = os.umask(0)  # permissions must not depend on the process's umask
    try:
        first = desk.upload(JPEG, filename=client_name)
        second = desk.upload(PNG, filename=client_name)
    finally:
        os.umask(previous)
        monkeypatch.undo()

    assert first.status_code == 200 and second.status_code == 200
    name = desk.photo_name()
    assert name is not None and desk.names() == [name]
    assert re.fullmatch(STORED_NAME, name), name
    assert photo_name_in(second) == [] or set(photo_name_in(second)) == {name}

    # Created exclusively and privately, in a folder only the owner can open.
    created = [call for call in opened if call[0].endswith(".jpg") or call[0].endswith(".png")]
    assert len(created) == 2, f"each photo is created with os.open: {opened}"
    for path, flags, mode in created:
        assert flags & os.O_CREAT and flags & os.O_EXCL, path
        assert mode == 0o600, path
    assert stat.S_IMODE((desk.assets / name).stat().st_mode) == 0o600
    assert stat.S_IMODE(desk.assets.stat().st_mode) == 0o700
    assert not (desk.assets / name).is_symlink()

    # The client's file name is never used, stored, shown or logged.
    needle = "CLIENTNAME"
    assert needle not in name and needle not in first.text and needle not in second.text
    assert needle not in str(desk.stored()) and needle not in " ".join(
        text for text in desk.tables_text().values()
    )
    assert not [p for p in desk.data_dir.rglob("*") if needle in p.name]
    assert not [where for where, entry in raw.entries if needle in entry or "passport" in entry]


# --- orphans -----------------------------------------------------------------------------------


def test_orphan_assets_removed_at_open_others_untouched(tmp_path: Path) -> None:
    outside = tmp_path / "outside-the-data-folder.png"
    outside.write_bytes(b"outside file that a link points to")
    with details_app(tmp_path) as desk:
        assert desk.upload(JPEG).status_code == 200
        kept_name = desk.photo_name()
        kept_bytes = stored_bytes(desk)
    assets = tmp_path / "data" / "assets"
    orphans = {
        "photo-orphan-0001.jpg": b"left by a crash",
        "photo-orphan-0002.png": b"left by a crash too",
        "photo-A_b-9.jpg": b"mixed characters are generated shapes",
        "photo-" + "a" * 64 + ".png": b"the longest generated id",
    }
    others = {
        "notes.txt": b"the user's own file",
        "avatar.jpg": b"not a generated name",
        "Photo-ABC.jpg": b"upper case P",
        "photo-orphan-0003.JPG": b"upper case extension",
        "photo-orphan-0004.gif": b"another extension",
        "photo-orphan-0005.jpg.bak": b"a backup",
        "photo-.jpg": b"no id",
        "photo-a b.jpg": b"a space can't be generated",
        "photo-" + "a" * 65 + ".jpg": b"an id too long to be generated",
        ".photo-orphan-0006.jpg": b"hidden",
    }
    for name, data in {**orphans, **others}.items():
        (assets / name).write_bytes(data)
    (assets / "photo-folder-0007.jpg").mkdir()
    (assets / "photo-folder-0007.jpg" / "inside.txt").write_bytes(b"inside a folder")
    (assets / "nested").mkdir()
    (assets / "nested" / "photo-orphan-0008.jpg").write_bytes(b"only direct children")
    (assets / "photo-link-0009.png").symlink_to(outside)
    before = {p.name: p for p in assets.iterdir()}
    assert len(before) >= len(orphans) + len(others) + 4

    with details_app(tmp_path) as reopened:  # opening the workspace sweeps
        left = sorted(p.name for p in assets.iterdir())
        assert not set(orphans) & set(left), f"orphans left: {set(orphans) & set(left)}"
        for name, data in others.items():
            assert (assets / name).read_bytes() == data, f"{name} was touched"
        assert (assets / "photo-folder-0007.jpg" / "inside.txt").read_bytes() == b"inside a folder"
        assert (assets / "nested" / "photo-orphan-0008.jpg").read_bytes() == b"only direct children"
        assert (assets / "photo-link-0009.png").is_symlink(), "a link is never followed or removed"
        assert outside.read_bytes() == b"outside file that a link points to"
        assert (assets / str(kept_name)).read_bytes() == kept_bytes, "the named photo stays"
        assert reopened.photo_name() == kept_name
        shown = reopened.show().text
        assert preview_bytes(shown) == ("image/jpeg", kept_bytes)
        assert reopened.run.spy.requests == []


def test_orphan_sweep_with_no_assets_folder_or_no_setting(tmp_path: Path) -> None:
    with importing(tmp_path) as first:
        assert first.data_dir == tmp_path / "data"
    assets = tmp_path / "data" / "assets"
    if assets.exists():
        assert list(assets.iterdir()) == []
    (assets).mkdir(exist_ok=True)
    (assets / "photo-orphan-0001.jpg").write_bytes(b"left by a crash")

    with importing(tmp_path, seed=False):
        assert not (assets / "photo-orphan-0001.jpg").exists(), "no setting names any photo"


# --- the protected inline preview --------------------------------------------------------------


def test_photo_preview_is_inline_protected_and_reads_only_the_stored_name(
    desk: Desk, tmp_path: Path
) -> None:
    assert desk.upload(JPEG).status_code == 200
    stored = stored_bytes(desk)
    token = desk.run.run.token

    protected = desk.client.get(DETAILS)
    assert protected.status_code == 403 and "data:image" not in protected.text
    shown = desk.show()
    assert preview_bytes(shown.text) == ("image/jpeg", stored)
    assert shown.headers["cache-control"] == "no-store"
    assert shown.headers["x-content-type-options"] == "nosniff"
    assert token not in shown.text
    assert 'src="/' not in shown.text.split("photo-preview")[1].split(">")[0], "no image URL"

    # Another stored file, named in the query string, changes nothing: only the setting decides.
    (desk.assets / "photo-other-0001.png").write_bytes(PNG)
    queried = desk.client.get(
        DETAILS + "?name=photo-other-0001.png&file=../app.db&photo=photo-other-0001.png",
        headers=desk.run.run.htmx(),
    )
    assert preview_bytes(queried.text) == ("image/jpeg", stored)

    # A setting tampered with outside the app never makes the app read another file.
    outside = tmp_path / "outside-secret.png"
    outside.write_bytes(png_image(100, 100) + b"OUTSIDE-FILE-CONTENT")
    (desk.assets / "photo-link-0002.png").symlink_to(outside)
    for tampered in (
        "../app.db",
        "../../outside-secret.png",
        "/etc/passwd",
        "photo-link-0002.png/../../app.db",
        "photo-missing-0003.png",  # a generated shape, but no such file
        "photo-link-0002.png",  # a link to a file outside the folder
    ):
        desk.run.execute(
            "UPDATE settings SET value = json_set(value, '$.photo', json_object('stored_name', ?)) "
            "WHERE key = 'personal_details'",
            (tampered,),
        )
        answer = desk.show()
        assert answer.status_code == 200, tampered
        assert preview_bytes(answer.text) is None, tampered
        assert b"OUTSIDE-FILE-CONTENT" not in answer.content, tampered
        assert b"SQLite format" not in answer.content and "root:" not in answer.text, tampered
        assert element(answer.text, "photo-empty") is not None, tampered


def test_photo_errors_never_echo_the_file_name(desk: Desk, monkeypatch: pytest.MonkeyPatch) -> None:
    raw = RawLogRecords()
    raw.install(monkeypatch)
    hostile = '<script>alert(1)</script>-FILENAME-MARKER".png'

    refused = desk.upload(b"GIF89a" + bytes(100), filename=hostile, content_type="image/png")
    too_big = desk.upload(padded_jpeg(5 * MIB + 1), filename=hostile)
    small = desk.upload(jpeg_image(50, 50), filename=hostile)

    for answer in (refused, too_big, small):
        assert answer.status_code == 422
        assert "FILENAME-MARKER" not in answer.text and "alert(1)" not in answer.text
        assert "FILENAME-MARKER" not in str(dict(answer.headers))
        assert invalid_names(answer.text) == ["file"]
    assert not [where for where, entry in raw.entries if "FILENAME-MARKER" in entry]
    assert desk.row() is None and desk.files() == []
    assert GOOD["name"] not in refused.text, "no contact detail is shown either"


def test_photo_route_reads_the_upload_with_the_photo_cap(
    desk: Desk, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 5 MB cap applies while the body streams, not only after it was read."""
    from app.api import settings_details

    seen: list[int] = []
    real = settings_details.read_upload

    async def spy(request: Any, **kwargs: Any) -> Any:
        seen.append(kwargs.get("max_bytes", -1))
        return await real(request, **kwargs)

    monkeypatch.setattr(settings_details, "read_upload", spy)

    assert desk.upload(JPEG).status_code == 200
    refused_with(desk.upload(padded_jpeg(6 * MIB)), "5 MB")
    assert seen == [5 * MIB, 5 * MIB]


def test_photo_preview_never_reads_through_an_assets_folder_that_is_a_link(
    desk: Desk, tmp_path: Path
) -> None:
    assert desk.upload(JPEG).status_code == 200
    name = desk.photo_name()
    assert name is not None and preview_bytes(desk.show().text) is not None
    elsewhere = tmp_path / "elsewhere"
    desk.assets.rename(elsewhere)
    desk.assets.symlink_to(elsewhere, target_is_directory=True)

    answer = desk.show()

    assert answer.status_code == 200
    assert preview_bytes(answer.text) is None and element(answer.text, "photo-empty") is not None


def test_photo_is_flushed_to_disk_before_the_setting_names_it(
    desk: Desk, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    real_fsync, real_save = os.fsync, SettingsService.save

    def fsync(descriptor: int) -> None:
        events.append("fsync")
        real_fsync(descriptor)

    def save(self: Any, *args: Any, **kwargs: Any) -> Any:
        events.append("save")
        return real_save(self, *args, **kwargs)

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(SettingsService, "save", save)

    assert desk.upload(JPEG).status_code == 200

    assert "fsync" in events and "save" in events
    assert events.index("fsync") < events.index("save")


def test_photo_with_a_generated_name_that_is_not_a_valid_reference_stores_nothing(
    desk: Desk,
) -> None:
    class BadIds:
        def new(self) -> str:
            return "not valid!"  # a legal file name, but not a stored photo reference

    service = desk.run.app.state.personal_details
    service._ids = BadIds()
    before = desk.snapshot()

    answer = desk.upload(JPEG)

    assert answer.status_code == 422 and "could" in answer.text.lower()
    assert desk.snapshot() == before, "no file and no setting"
