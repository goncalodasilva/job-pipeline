"""The fictional CV fixtures (M3.1; AC-M3.7 groundwork; ADR-089 item 2; golden rules 2 and 3).

These tests need no PDF or Word library. They check that the manifest and the folder agree,
that every marker really is in its file, that the committed files are what the committed
generator writes, and that no file names a person, a tool or a date.
"""

import getpass
import importlib.util
import io
import re
import zipfile
from pathlib import Path
from types import ModuleType

import pytest

from tests.cv_fixtures import (
    CV_DIR,
    GENERATOR,
    MANIFEST_NAME,
    OUTCOMES,
    fixture_path,
    load_manifest,
)

# file name -> (kind that sniffing must answer, outcome of reading it)
REQUIRED: dict[str, tuple[str | None, str]] = {
    "single_column.pdf": ("pdf", "text"),
    "two_column.pdf": ("pdf", "text"),
    "table.pdf": ("pdf", "text"),
    "with_tables.docx": ("docx", "text"),
    "cv.txt": ("text", "text"),
    "truncated.pdf": ("pdf", "unreadable"),
    "encrypted.pdf": ("pdf", "encrypted"),
    "image_only.pdf": ("pdf", "no_text"),
    "not_word.zip": (None, "unsupported"),
    "oversized_entry.docx": ("docx", "unreadable"),
}
ZIP_SUFFIXES = (".docx", ".zip")
FIXED_ZIP_DATE = (1980, 1, 1, 0, 0, 0)
OVERSIZED_AT_LEAST = 64 * 1024 * 1024
EMAIL_DOMAIN = re.compile(rb"[A-Za-z0-9._-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)")

# Places a PDF or Word file names its author, the tool that wrote it, or when.
METADATA_TOKENS = (
    b"/Info",
    b"/Author",
    b"/Creator",
    b"/Producer",
    b"/CreationDate",
    b"/ModDate",
    b"/Metadata",
    b"<dc:creator",
    b"lastModifiedBy",
    b"<Application>",
    b"xmp:CreatorTool",
)


def _generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location("make_cv_fixtures", GENERATOR)
    assert spec is not None and spec.loader is not None, f"missing generator: {GENERATOR}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _zip_members(data: bytes) -> list[tuple[str, int, int, tuple[int, ...]]]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return [(i.filename, i.file_size, i.CRC, i.date_time) for i in archive.infolist()]


def _readable_layers(name: str, data: bytes) -> list[bytes]:
    """The raw bytes and, for a zip, every member's uncompressed bytes."""
    layers = [data]
    if name.endswith(ZIP_SUFFIXES):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            layers.extend(archive.read(info) for info in archive.infolist())
    return layers


def _marker_is_in_file(name: str, marker: str) -> bool:
    data = fixture_path(name).read_bytes()
    if name.endswith(".pdf"):
        # The generator writes each marker as one uncompressed PDF string.
        escaped = marker.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        return b"(" + escaped.encode("ascii") + b")" in data
    if name.endswith(".docx"):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            document = archive.read("word/document.xml").decode("utf-8")
        return f">{marker}</w:t>" in document
    return marker in data.decode("utf-8")


def test_cv_fixture_manifest_lists_every_file_with_expected_markers() -> None:
    manifest = load_manifest()
    on_disk = {path.name for path in CV_DIR.iterdir() if path.name != MANIFEST_NAME}

    assert set(manifest) == on_disk, "the manifest and tests/fixtures/cv/ must list the same files"
    for name, (kind, outcome) in REQUIRED.items():
        assert name in manifest, f"required fixture missing: {name}"
        assert manifest[name]["kind"] == kind, name
        assert manifest[name]["outcome"] == outcome, name

    for name, entry in manifest.items():
        assert entry["outcome"] in OUTCOMES, name
        assert entry["kind"] in ("pdf", "docx", "text", None), name
        assert entry["about"].strip(), name
        groups = entry["marker_groups"]
        if entry["outcome"] != "text":
            assert groups == [], f"{name} gives no text, so it has no markers"
            continue
        markers = [marker for group in groups for marker in group]
        assert len(markers) >= 3, f"{name} needs at least three markers"
        for marker in markers:
            assert marker == marker.strip() and len(marker) >= 4, (name, marker)
            assert _marker_is_in_file(name, marker), f"{name}: marker not in the file: {marker!r}"


def test_cv_fixture_markers_cover_table_cells_and_both_columns() -> None:
    manifest = load_manifest()

    # One group per column, so reading order inside each column is checked.
    assert len(manifest["two_column.pdf"]["marker_groups"]) == 2
    # One group per table row, plus the text around the table.
    assert len(manifest["table.pdf"]["marker_groups"]) >= 4
    assert len(manifest["with_tables.docx"]["marker_groups"]) >= 3


def test_cv_fixtures_carry_no_author_or_tool_user_metadata() -> None:
    user = getpass.getuser()
    home = str(Path.home())

    for name in load_manifest():
        data = fixture_path(name).read_bytes()
        if name.endswith(ZIP_SUFFIXES):
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                assert archive.comment == b"", name
                for info in archive.infolist():
                    assert not info.filename.startswith("docProps/"), (name, info.filename)
                    assert info.date_time == FIXED_ZIP_DATE, (name, info.filename)
                    assert info.comment == b"" and info.extra == b"", (name, info.filename)
        for layer in _readable_layers(name, data):
            for token in METADATA_TOKENS:
                assert token not in layer, f"{name} carries metadata: {token!r}"
            assert home.encode() not in layer, f"{name} names a home folder"
            if len(user) >= 4:
                assert user.encode() not in layer, f"{name} names the user who made it"


def test_cv_fixtures_use_only_fictional_contact_details() -> None:
    for name in load_manifest():
        for layer in _readable_layers(name, fixture_path(name).read_bytes()):
            for domain in EMAIL_DOMAIN.findall(layer):
                assert domain == b"example.com", f"{name}: an address outside example.com"


def test_oversized_entry_fixture_is_small_on_disk_and_huge_inside() -> None:
    path = fixture_path("oversized_entry.docx")

    assert path.stat().st_size < 1024 * 1024
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        largest = max(info.file_size for info in archive.infolist())
    # Still a Word file by content, so only the entry size can be the reason to refuse it.
    assert {"[Content_Types].xml", "word/document.xml"} <= names
    assert largest >= OVERSIZED_AT_LEAST


def test_not_word_zip_fixture_is_a_real_zip_without_word_parts() -> None:
    with zipfile.ZipFile(fixture_path("not_word.zip")) as archive:
        assert archive.testzip() is None
        assert "word/document.xml" not in archive.namelist()


@pytest.mark.slow
def test_cv_fixtures_match_what_the_generator_writes() -> None:
    built: dict[str, bytes] = _generator().build_fixtures()

    assert set(built) == {path.name for path in CV_DIR.iterdir()}
    for name, data in built.items():
        committed = (CV_DIR / name).read_bytes()
        if name.endswith(ZIP_SUFFIXES):
            # DEFLATE bytes can differ between zlib builds; names, sizes, CRCs and dates can't.
            assert _zip_members(committed) == _zip_members(data), name
        else:
            assert committed == data, f"{name} differs from the generator's output"
