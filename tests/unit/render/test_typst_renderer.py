"""The PDF renderer's use of typst (M6.2; AC-M6.5, AC-M6.6; ADR-107, ADR-108).

Tests here know the library is typst: the lazy ``_load_typst()`` loader, ``typst.Compiler(...)``
keyword arguments, ``sys.modules["typst"]``. Format-level behaviour through the public API is in
``test_pdf_renderer.py``; the file and network isolation is in ``test_typst_isolation.py``; the
helpers are in ``tests/typst_support.py``.

API assumed by these tests (the implementer follows it; change it only with the test author):

* ``app.render.providers.pdf`` imports no vendor library at import time. It has
  ``_load_typst() -> module`` (the lazy loader, called by ``render``; tests replace it) and
  ``PdfRenderer(clock, *, max_units=..., max_characters=..., max_photo_bytes=...,
  max_photo_side=..., max_photo_pixels=..., time_limit_seconds=...)``. Constructing the renderer
  never needs typst.
* typst is used as ``typst.Compiler(root=<fresh empty temp dir>, font_paths=[...],
  ignore_system_fonts=True, ...)`` with keyword arguments and ``compiler.compile(...)``, in this
  process, with ``timestamp`` taken from the injected clock.
* Units are the text pieces of the document: header lines, section headings and lines, entry
  headings and lines (counted before cleaning). Characters are the sum of their lengths plus the
  title and the author. Over a cap, or when the photo's header states a side over 4000 px or over
  16 megapixels, ``render`` raises ``app.domain.errors.RenderTooLarge`` before any typst call;
  a photo whose bytes are not its stated type raises ``RenderFailed``; over the time limit, or
  with ``MAX_IN_FLIGHT`` compiles already running (a timed-out compile counts until its thread
  ends), it raises ``RenderTimedOut``. No message contains document text.
* Missing typst: ``render`` raises ``app.render.RendererUnavailable`` whose message is exactly
  ``"PDF export needs typst. Word export still works; see the README."`` when ``_load_typst``
  raises ``ImportError`` or ``OSError``.
* ``FONT_SOURCE`` is ``"wheel"`` or ``"vendored"``. When ``"vendored"``: ``FONT_FILES`` (tuple of
  ``Path``, inside the repo) and ``FONT_SHA256`` (file name -> hex digest), with a licence file
  next to the fonts.
"""

import hashlib
import os
import re
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from app.domain.errors import AppError, RenderFailed, RenderTimedOut, RenderTooLarge
from app.render import RendererUnavailable
from tests.fakes.clock import FrozenClock
from tests.render_support import (
    NOW,
    PHOTOS,
    jpeg_header_only,
    pdf_module,
    pdf_renderer,
    png_header_only,
    sample_document,
    word_renderer,
)
from tests.typst_support import BlockedTypst, install_typst_spy

REPO_ROOT = Path(__file__).resolve().parents[3]
UNAVAILABLE = "PDF export needs typst. Word export still works; see the README."


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(NOW)


@pytest.fixture
def permits(monkeypatch: pytest.MonkeyPatch) -> threading.BoundedSemaphore:
    """A fresh in-flight limit for the test, so a compile left running by another test can't
    take a permit."""
    pdf = pdf_module()
    fresh = threading.BoundedSemaphore(pdf.MAX_IN_FLIGHT)
    monkeypatch.setattr(pdf, "_IN_FLIGHT", fresh)
    return fresh


def _pieces(document: Any) -> list[str]:
    found = list(document.header_lines)
    for section in document.sections:
        found.append(section.heading)
        found.extend(section.lines)
        for entry in section.entries:
            found.append(entry.heading)
            found.extend(entry.lines)
    return found


def _characters(document: Any) -> int:
    return (
        sum(len(piece) for piece in _pieces(document)) + len(document.title) + len(document.author)
    )


def test_pdf_importing_the_provider_does_not_import_typst() -> None:
    """The library is loaded on the first render, so the app starts without it."""
    code = (
        "import sys, app.render.providers.pdf\n"
        "assert 'typst' not in sys.modules, 'typst was imported at import time'\n"
    )
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _not_loaded() -> Any:
    raise AssertionError("typst was loaded for a document that is over a cap")


def test_pdf_too_many_units_raises_render_too_large(
    clock: FrozenClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    document = sample_document()
    units = len(_pieces(document))
    assert units == 12
    assert pdf_renderer(clock, max_units=units).render(document).page_count == 1, "at the cap"

    monkeypatch.setattr(pdf_module(), "_load_typst", _not_loaded)
    with pytest.raises(RenderTooLarge) as caught:
        pdf_renderer(clock, max_units=units - 1).render(document)

    assert isinstance(caught.value, AppError)
    assert caught.value.user_message
    assert "Example" not in caught.value.user_message, "the message holds no document text"


def test_pdf_too_many_characters_raises_render_too_large(
    clock: FrozenClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    document = sample_document()
    characters = _characters(document)  # pieces, title and author
    assert pdf_renderer(clock, max_characters=characters).render(document).page_count == 1

    monkeypatch.setattr(pdf_module(), "_load_typst", _not_loaded)
    with pytest.raises(RenderTooLarge) as caught:
        pdf_renderer(clock, max_characters=characters - 1).render(document)
    assert "Example" not in caught.value.user_message


def test_pdf_photo_over_the_cap_raises_render_too_large(
    clock: FrozenClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    photo = PHOTOS["png"]
    document = sample_document(photo=photo, photo_type="png")
    assert pdf_renderer(clock, max_photo_bytes=len(photo)).render(document).page_count == 1

    monkeypatch.setattr(pdf_module(), "_load_typst", _not_loaded)
    with pytest.raises(RenderTooLarge):
        pdf_renderer(clock, max_photo_bytes=len(photo) - 1).render(document)


def test_pdf_default_caps_reject_oversized_documents(
    clock: FrozenClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf = pdf_module()
    monkeypatch.setattr(pdf, "_load_typst", _not_loaded)
    renderer = pdf_renderer(clock)
    many_units = sample_document(header_lines=("x",) * (pdf.MAX_UNITS + 1))
    many_characters = sample_document(header_lines=("x" * (pdf.MAX_CHARACTERS + 1),))
    big_photo = sample_document(
        photo=b"\x89PNG\r\n\x1a\n" + b"\x00" * pdf.MAX_PHOTO_BYTES, photo_type="png"
    )

    for document in (many_units, many_characters, big_photo):
        with pytest.raises(RenderTooLarge):
            renderer.render(document)


def test_pdf_render_time_limit_raises_render_timed_out(
    clock: FrozenClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocked = BlockedTypst()
    monkeypatch.setattr(pdf_module(), "_load_typst", lambda: blocked)
    renderer = pdf_renderer(clock, time_limit_seconds=0.05)

    try:
        with pytest.raises(RenderTimedOut) as caught:
            renderer.render(sample_document())
        assert blocked.started.is_set(), "the render did start"
    finally:
        blocked.release.set()

    assert isinstance(caught.value, AppError) and caught.value.user_message
    assert "Example" not in caught.value.user_message


def test_vendored_font_matches_pinned_hash(
    clock: FrozenClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Works for both choices of ``FONT_SOURCE``: system fonts are never used, so the output does
    not depend on the machine; a vendored font is pinned by SHA-256 and ships with its licence."""
    pdf = pdf_module()
    spy = install_typst_spy(monkeypatch)

    pdf_renderer(clock).render(sample_document())

    assert pdf.FONT_SOURCE in ("wheel", "vendored")
    (construction,) = spy.constructions
    assert construction["kwargs"].get("ignore_system_fonts") is True
    font_paths = [Path(str(p)) for p in construction["kwargs"].get("font_paths") or []]
    if pdf.FONT_SOURCE == "wheel":
        assert font_paths == [], "fonts come from the typst wheel only"
        assert not getattr(pdf, "FONT_FILES", ())
        return

    files = tuple(Path(p) for p in pdf.FONT_FILES)
    assert files, "a vendored font needs at least one file"
    assert set(pdf.FONT_SHA256) == {f.name for f in files}
    for font in files:
        assert font.is_file() and font.resolve().is_relative_to(REPO_ROOT / "app")
        assert re.fullmatch(r"[0-9a-f]{64}", pdf.FONT_SHA256[font.name])
        assert hashlib.sha256(font.read_bytes()).hexdigest() == pdf.FONT_SHA256[font.name]
        licences = [
            sibling
            for sibling in font.parent.iterdir()
            if re.match(r"(?i)(ofl|licen[cs]e|copying)", sibling.name)
        ]
        assert licences, f"no licence file next to {font.name}"
    assert {p.resolve() for p in font_paths} == {f.parent.resolve() for f in files}


@pytest.mark.parametrize("failure", [ImportError("no typst here"), OSError("cannot load library")])
def test_missing_system_library_raises_renderer_unavailable(
    failure: Exception, clock: FrozenClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken() -> Any:
        raise failure

    monkeypatch.setattr(pdf_module(), "_load_typst", broken)
    renderer = pdf_renderer(clock)  # building the renderer needs nothing

    for _ in range(2):  # the failure is not cached as something else
        with pytest.raises(RendererUnavailable) as caught:
            renderer.render(sample_document())
        assert str(caught.value) == UNAVAILABLE

    # Word is unaffected.
    assert word_renderer(clock).render(sample_document()).content


def test_missing_typst_package_app_starts_and_word_works(tmp_path: Path) -> None:
    """The real import path with ``import typst`` failing: the provider imports, the app builds
    and serves, the PDF renderer is unavailable and the Word renderer works."""
    code = """
import sys
sys.modules["typst"] = None  # makes "import typst" raise ImportError
from datetime import UTC, datetime
from fastapi.testclient import TestClient
from app.config import RuntimeConfig
from app.main import create_app
from app.render import RendererUnavailable
from app.render.providers.pdf import PdfRenderer
from tests.fakes.clock import FrozenClock
from tests.render_support import NOW, sample_document, word_renderer
from tests.setup_harness import complete_setup

from pathlib import Path
root = Path(sys.argv[1])
clock = FrozenClock(NOW)
complete_setup(root / "data", root / "cfg", clock=clock)
config = RuntimeConfig(data_dir=root / "data", config_dir=root / "cfg", port=8000,
    log_level="INFO", demo=False, setup=False, no_browser=False)
with TestClient(create_app(config, clock=clock), base_url="http://127.0.0.1:8000") as client:
    assert client.get("/").status_code < 500
try:
    PdfRenderer(clock).render(sample_document())
except RendererUnavailable as error:
    assert str(error) == sys.argv[2], str(error)
else:
    raise SystemExit("PDF rendered without typst")
assert word_renderer(clock).render(sample_document()).content
"""
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code, str(tmp_path), UNAVAILABLE],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        cwd=REPO_ROOT,
        env=env,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_pdf_title_and_author_count_toward_the_character_cap(
    clock: FrozenClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf = pdf_module()
    monkeypatch.setattr(pdf, "_load_typst", _not_loaded)
    renderer = pdf_renderer(clock)

    for changes in (
        {"title": "T" * (pdf.MAX_CHARACTERS + 1)},
        {"author": "A" * pdf.MAX_CHARACTERS},
    ):
        with pytest.raises(RenderTooLarge) as caught:
            renderer.render(sample_document(**changes))
        assert "TTTT" not in caught.value.user_message


@pytest.mark.parametrize(
    "photo",
    [
        png_header_only(4001, 10),
        png_header_only(10, 4001),
        png_header_only(4000, 4000 + 1),
        jpeg_header_only(4001, 10),
        jpeg_header_only(10, 4001),
    ],
)
def test_pdf_photo_stating_a_huge_size_is_refused_before_typst_loads(
    photo: bytes, clock: FrozenClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pdf_module(), "_load_typst", _not_loaded)
    photo_type = "png" if photo.startswith(b"\x89PNG") else "jpeg"

    assert len(photo) < 1_000, "a small file that only claims to be big"
    with pytest.raises(RenderTooLarge):
        pdf_renderer(clock).render(sample_document(photo=photo, photo_type=photo_type))


def test_pdf_photo_over_the_pixel_cap_is_refused_before_typst_loads(
    clock: FrozenClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pdf_module(), "_load_typst", _not_loaded)
    document = sample_document(photo=PHOTOS["png"], photo_type="png")

    with pytest.raises(RenderTooLarge):
        pdf_renderer(clock, max_photo_pixels=3).render(document)  # the picture is 2 x 2
    with pytest.raises(RenderTooLarge):
        pdf_renderer(clock, max_photo_side=1).render(document)


@pytest.mark.parametrize(
    ("photo", "photo_type"),
    [
        (PHOTOS["jpeg"], "png"),
        (PHOTOS["png"], "jpeg"),
        (b"GIF89a" + b"\x00" * 40, "png"),
        (b"\x89PNG\r\n\x1a\n", "png"),
        (b"\xff\xd8\xff", "jpeg"),
    ],
)
def test_pdf_photo_not_matching_its_type_fails_before_typst_loads(
    photo: bytes, photo_type: str, clock: FrozenClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pdf_module(), "_load_typst", _not_loaded)

    with pytest.raises(RenderFailed) as caught:
        pdf_renderer(clock).render(sample_document(photo=photo, photo_type=photo_type))

    assert isinstance(caught.value, AppError) and caught.value.user_message


# ---- Compiles in flight -----------------------------------------------------------------------


def test_pdf_at_most_max_in_flight_compiles_run_at_once_and_timed_out_ones_still_count(
    clock: FrozenClock, monkeypatch: pytest.MonkeyPatch, permits: threading.BoundedSemaphore
) -> None:
    blocked = BlockedTypst()
    monkeypatch.setattr(pdf_module(), "_load_typst", lambda: blocked)
    renderer = pdf_renderer(clock, time_limit_seconds=0.05)

    try:
        for _ in range(pdf_module().MAX_IN_FLIGHT):
            with pytest.raises(RenderTimedOut):
                renderer.render(sample_document())
        assert blocked.started.is_set()
        started = blocked.compiles

        with pytest.raises(RenderTimedOut) as caught:
            renderer.render(sample_document())

        assert blocked.compiles == started, "no third compile was started"
        assert "Example" not in caught.value.user_message
    finally:
        blocked.release.set()

    # The worker threads, not the callers, give the permits back once the compiles end.
    taken = [permits.acquire(timeout=10) for _ in range(pdf_module().MAX_IN_FLIGHT)]
    assert all(taken), "the finished compiles released their permits"
    for _ in taken:
        permits.release()


def test_pdf_permit_is_released_when_the_compile_fails(
    clock: FrozenClock, monkeypatch: pytest.MonkeyPatch, permits: threading.BoundedSemaphore
) -> None:
    spy = install_typst_spy(monkeypatch)

    def fail() -> None:
        raise RuntimeError("compile failed with Alex Example's text")

    spy.before_compile = fail
    for _ in range(pdf_module().MAX_IN_FLIGHT + 1):
        with pytest.raises(RenderFailed) as caught:
            pdf_renderer(clock).render(sample_document())
        assert "Alex" not in str(caught.value) and caught.value.__cause__ is None
        assert caught.value.__suppress_context__, "the library error is not chained"

    for _ in range(pdf_module().MAX_IN_FLIGHT):
        assert permits.acquire(blocking=False)


def test_pdf_permit_is_released_after_a_normal_render(
    clock: FrozenClock, permits: threading.BoundedSemaphore
) -> None:
    renderer = pdf_renderer(clock)

    for _ in range(pdf_module().MAX_IN_FLIGHT + 2):
        assert renderer.render(sample_document()).page_count == 1
