"""Shared ``TextExtractor`` contract (M3.1; AC-M3.7; spec 2.4, 4.1; ADR-089 item 2).

``app.modules.extract.extractor_for(kind)`` gives the extractor for "pdf", "docx" or "text".
Whatever the format, an extractor behaves the same way: ``extract(path)`` returns the text of a
good file, raises ``UnreadableDocument`` for a damaged one and ``NoTextFound`` for one with
nothing to read, and never changes the file. This is the in-process parser that
``extract_text`` runs inside its child process. All files are fictional. The new names are
imported inside the tests.
"""

import importlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from tests.cv_fixtures import assert_markers_in_order, fixture_path

pytestmark = pytest.mark.contract


@dataclass(frozen=True)
class Samples:
    good: str  # fixture with markers in the manifest
    damaged: bytes
    no_text: bytes


def _samples(kind: str) -> Samples:
    if kind == "pdf":
        return Samples(
            good="single_column.pdf",
            damaged=fixture_path("truncated.pdf").read_bytes(),
            no_text=fixture_path("image_only.pdf").read_bytes(),
        )
    if kind == "docx":
        return Samples(
            good="with_tables.docx",
            damaged=fixture_path("not_word.zip").read_bytes(),
            no_text=fixture_path("empty_body.docx").read_bytes(),
        )
    return Samples(good="cv.txt", damaged=b"Alex Example \xe9\xe8 \xff\xfe", no_text=b" \n\t\r\n ")


def _module() -> Any:
    return importlib.import_module("app.modules.extract")


def _error(name: str) -> type[Exception]:
    errors = importlib.import_module("app.domain.errors")
    assert hasattr(errors, name), f"app.domain.errors.{name} doesn't exist yet"
    found: type[Exception] = getattr(errors, name)
    return found


@pytest.mark.parametrize("kind", ["pdf", "docx", "text"])
def test_text_extractor_contract(kind: str, tmp_path: Path) -> None:
    extractor = _module().extractor_for(kind)
    samples = _samples(kind)

    assert extractor.kind == kind

    # A good file: the text, as a str, with every marker; the file is left as it was.
    good = tmp_path / "upload-good"
    original = fixture_path(samples.good).read_bytes()
    good.write_bytes(original)
    text = extractor.extract(good)
    assert isinstance(text, str)
    assert_markers_in_order(text, samples.good)
    assert good.read_bytes() == original
    assert extractor.extract(good) == text, "reading the same file twice gives the same text"

    # A damaged file: one friendly error type, never the library's own exception.
    damaged = tmp_path / "upload-damaged"
    damaged.write_bytes(samples.damaged)
    with pytest.raises(_error("UnreadableDocument")):
        extractor.extract(damaged)

    # A file with nothing to read.
    no_text = tmp_path / "upload-no-text"
    no_text.write_bytes(samples.no_text)
    with pytest.raises(_error("NoTextFound")):
        extractor.extract(no_text)

    # Nothing was written next to the uploads.
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "upload-damaged",
        "upload-good",
        "upload-no-text",
    ]


def test_extractor_for_unknown_kind_is_refused() -> None:
    with pytest.raises((_error("UnsupportedDocumentType"), ValueError, KeyError)):
        _module().extractor_for("png")


def test_text_extractor_protocol_is_part_of_the_public_interface() -> None:
    module = _module()

    for name in ("TextExtractor", "extractor_for", "extract_text", "sniff_type"):
        assert hasattr(module, name), f"app.modules.extract.{name} is missing"
