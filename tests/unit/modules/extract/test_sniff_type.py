"""``sniff_type``: the document type is decided by content, never by name (M3.1; AC-M3.7;
spec 2.9 rule 5; ADR-089 item 2).

``sniff_type(data: bytes)`` answers "pdf", "docx" or "text", or raises
``UnsupportedDocumentType``. It takes the bytes and nothing else, so a file name or a browser's
content type can't influence it. The new names are imported inside the tests.
"""

import importlib
import inspect
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tests.cv_fixtures import fixture_path, load_manifest

# The eight-byte PNG signature and the start of an IHDR chunk.
PNG_BYTES = bytes.fromhex("89504e470d0a1a0a0000000d49484452") + bytes(32)


def _extract() -> Any:
    return importlib.import_module("app.modules.extract")


def _unsupported() -> type[Exception]:
    errors = importlib.import_module("app.domain.errors")
    assert hasattr(errors, "UnsupportedDocumentType"), "UnsupportedDocumentType doesn't exist yet"
    refused: type[Exception] = errors.UnsupportedDocumentType
    return refused


def _fixture(name: str) -> Callable[[Path], bytes]:
    return lambda tmp_path: fixture_path(name).read_bytes()


def _pdf_saved_as_txt(tmp_path: Path) -> bytes:
    renamed = tmp_path / "cv.txt"
    renamed.write_bytes(fixture_path("single_column.pdf").read_bytes())
    return renamed.read_bytes()


# case id -> (how to get the bytes, expected kind or None when the type is refused)
CASES: dict[str, tuple[Callable[[Path], bytes], str | None]] = {
    "pdf": (_fixture("single_column.pdf"), "pdf"),
    "docx": (_fixture("with_tables.docx"), "docx"),
    "text": (_fixture("cv.txt"), "text"),
    "zip_not_docx": (_fixture("not_word.zip"), None),
    "png": (lambda tmp_path: PNG_BYTES, None),
    "empty": (lambda tmp_path: b"", None),
    "pdf_named_txt": (_pdf_saved_as_txt, "pdf"),
}


@pytest.mark.parametrize("case", list(CASES))
def test_sniff_type(case: str, tmp_path: Path) -> None:
    load, expected = CASES[case]
    data = load(tmp_path)
    sniff_type = _extract().sniff_type

    if expected is None:
        with pytest.raises(_unsupported()):
            sniff_type(data)
    else:
        assert sniff_type(data) == expected


def test_sniff_type_takes_the_content_and_nothing_else() -> None:
    # No file name, extension or declared content type can be passed in at all.
    parameters = inspect.signature(_extract().sniff_type).parameters

    assert list(parameters) == ["data"]


@pytest.mark.parametrize("name", sorted(load_manifest()))
def test_sniff_type_agrees_with_the_fixture_manifest(name: str) -> None:
    expected = load_manifest()[name]["kind"]
    data = fixture_path(name).read_bytes()
    sniff_type = _extract().sniff_type

    if expected is None:
        with pytest.raises(_unsupported()):
            sniff_type(data)
    else:
        # Damaged, encrypted and oversized files still have a type; reading them fails later.
        assert sniff_type(data) == expected


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(b"\x00\x01\x02\x03 not text at all \xff\xfe\x00", id="binary"),
        pytest.param(b"Alex Example\x00Operations Analyst", id="text_with_nul"),
        pytest.param(b"Alex Example \xe9\xe8 \xff", id="not_utf8"),
        pytest.param(fixture_path("not_word.zip").read_bytes()[:60], id="cut_off_zip"),
        pytest.param(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + bytes(64), id="old_word_format"),
    ],
)
def test_sniff_type_refuses_other_content(data: bytes) -> None:
    with pytest.raises(_unsupported()):
        _extract().sniff_type(data)


def test_sniff_type_accepts_utf8_text_with_a_byte_order_mark() -> None:
    data = b"\xef\xbb\xbf" + "Alex Example\nOperations Analyst, Évora\n".encode()

    assert _extract().sniff_type(data) == "text"
