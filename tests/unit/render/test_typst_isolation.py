"""The PDF renderer cannot read files or reach the network through typst, and user text is never
typst code (M6.2; AC-M6.6; ADR-107 "static template", "empty temp root", "data only through
sys_inputs").

API assumed (see ``test_typst_renderer.py`` for the rest): the module constant
``app.render.providers.pdf.TEMPLATE_SOURCE`` is the whole typst source as a ``str``, the same
text for every document, passed as the compile input; the document's words reach typst only as
JSON in ``sys_inputs`` (string values). The only thing the compile may see in its root is the
in-memory photo, written as one regular file in the fresh root (or passed in
``sys_inputs``). Every test that uses typst goes through the spy in ``tests/typst_support.py``.
"""

import base64
import re
import select
import socket
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.fakes.clock import FrozenClock
from tests.render_support import (
    NOW,
    PHOTOS,
    pdf_module,
    pdf_renderer,
    pdf_squash,
    pdf_text,
    rendering,
    sample_document,
)
from tests.typst_support import install_typst_spy

REPO_ROOT = Path(__file__).resolve().parents[3]
PRIVATE_MARKER = "SENTINEL-4f2a9c-private"  # in a file only; it must never reach a PDF


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock(NOW)


@pytest.fixture
def secret_file(tmp_path: Path) -> Path:
    path = tmp_path / "private" / "notes.txt"
    path.parent.mkdir()
    path.write_text(f"{PRIVATE_MARKER}\n", encoding="utf-8")
    return path


@pytest.fixture
def listener() -> Iterator[socket.socket]:
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(5)
    server.setblocking(False)
    yield server
    server.close()


def _connections_waiting(server: socket.socket) -> bool:
    readable, _, _ = select.select([server], [], [], 0)
    return bool(readable)


# ---- No files, no network ---------------------------------------------------------------------


def test_pdf_never_fetches_files_or_network(
    clock: FrozenClock,
    secret_file: Path,
    listener: socket.socket,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = rendering()
    port = listener.getsockname()[1]
    path = secret_file.as_posix()
    url = f"http://127.0.0.1:{port}/probe.png"
    hostile = (
        f"file://{path}",
        f'#read("{path}")',
        f'#image("{path}")',
        f'#image("file://{path}")',
        f'#image("{url}")',
        f'#include "{path}"',
        f'#import "{path}": *',
        f'#json("{path}")',
        f'#eval(read("{path}"))',
        f"{url}",
        f"<img src='{url}'>",
    )
    document = sample_document(
        title=hostile[0],
        author=hostile[1],
        header_lines=hostile[:2],
        sections=(
            module.RenderSection(
                heading=hostile[2],
                lines=hostile[3:7],
                entries=(module.RenderEntry(heading=hostile[7], lines=hostile[8:]),),
            ),
        ),
        photo=PHOTOS["png"],
        photo_type="png",
    )
    spy = install_typst_spy(monkeypatch)
    attempts: list[Any] = []
    real_connect = socket.socket.connect
    monkeypatch.setattr(
        socket.socket,
        "connect",
        lambda self, address: attempts.append(address) or real_connect(self, address),
    )

    rendered = pdf_renderer(clock).render(document)

    text = pdf_text(rendered.content)
    for line in hostile:
        assert pdf_squash(line) in pdf_squash(text), f"{line[:30]!r} should appear as plain text"
    assert PRIVATE_MARKER not in text
    assert PRIVATE_MARKER.encode() not in rendered.content
    assert not _connections_waiting(listener), "the renderer connected to the local listener"
    assert attempts == [], "no socket connection was made from Python either"

    # The spy: the only thing the compile could see besides the template is the in-memory photo.
    assert spy.compiles, "the renderer must call Compiler.compile (the spy's only hook)"
    for construction in spy.constructions:
        root = Path(str(construction["kwargs"]["root"]))
        assert not secret_file.resolve().is_relative_to(root.resolve())
        assert not root.resolve().is_relative_to(secret_file.parent.resolve())
    for call in spy.compiles:
        listing = call["root_listing"]
        assert listing is not None
        assert [name for name, link in listing if link] == [], "no symlink in the root"
        assert len(listing) <= 1, f"only the photo may be in the root, found {listing}"


def test_pdf_with_no_photo_gives_typst_an_empty_root(
    clock: FrozenClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = install_typst_spy(monkeypatch)

    pdf_renderer(clock).render(sample_document())

    assert [call["root_listing"] for call in spy.compiles] == [[]]


def _photo_in(blob: str, photo: bytes) -> bool:
    forms = (photo.hex(), base64.b64encode(photo).decode(), ",".join(str(b) for b in photo))
    squashed = blob.replace(" ", "").replace("\n", "")
    return any(form in squashed for form in forms)


def test_pdf_photo_is_the_only_resource_and_comes_from_memory(
    clock: FrozenClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The photo reaches typst either as the one regular file in the fresh root (byte for byte)
    or inside ``sys_inputs`` (hex, base64 or a list of byte values). Nothing else is there."""
    photo = PHOTOS["png"]
    spy = install_typst_spy(monkeypatch)
    files: list[bytes] = []

    def read_root() -> None:
        root = Path(str(spy.constructions[-1]["kwargs"]["root"]))
        files.extend(f.read_bytes() for f in root.rglob("*") if f.is_file())

    spy.before_compile = read_root
    pdf_renderer(clock).render(sample_document(photo=photo, photo_type="png"))

    listing = spy.compiles[0]["root_listing"]
    assert len(listing) <= 1 and not [name for name, link in listing if link]
    inputs = "".join((spy.effective("sys_inputs") or {}).values())
    assert files == [photo] or _photo_in(inputs, photo), "the photo bytes reach typst from memory"
    assert len(files) == len(listing)


# ---- The root ---------------------------------------------------------------------------------


def test_pdf_temp_root_is_fresh_empty_and_removed(
    clock: FrozenClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = install_typst_spy(monkeypatch)
    renderer = pdf_renderer(clock)

    renderer.render(sample_document())
    renderer.render(sample_document())

    roots = [Path(str(c["kwargs"]["root"])) for c in spy.constructions]
    assert len(roots) == 2 and roots[0] != roots[1], "a fresh root for every render"
    for root, construction in zip(roots, spy.constructions, strict=True):
        assert root.is_absolute()
        assert construction["root_exists"] and not construction["root_is_symlink"]
        assert construction["root_listing"] == [], "empty when the compiler is built"
        assert not root.exists(), "removed after the render"
        assert not root.resolve().is_relative_to(REPO_ROOT)
        assert not root.resolve().is_relative_to(Path.cwd().resolve())


def test_pdf_temp_root_is_removed_when_the_compile_fails(
    clock: FrozenClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = install_typst_spy(monkeypatch)

    def fail() -> None:
        raise RuntimeError("compile failed")

    spy.before_compile = fail
    with pytest.raises(Exception):  # noqa: B017 - the type of a compile failure is free
        pdf_renderer(clock).render(sample_document())

    root = Path(str(spy.constructions[0]["kwargs"]["root"]))
    assert not root.exists()


def test_pdf_root_through_a_symlinked_temp_folder_cannot_leak(
    clock: FrozenClock,
    tmp_path: Path,
    secret_file: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even when the temp folder is a symlink to a folder holding private files, the root is a
    new directory of its own, empty, and nothing from the real folder is rendered."""
    link = tmp_path / "odd-tmp"
    link.symlink_to(secret_file.parent, target_is_directory=True)
    monkeypatch.setattr(tempfile, "tempdir", str(link))
    spy = install_typst_spy(monkeypatch)
    document = sample_document(
        header_lines=("notes.txt", '#read("notes.txt")', '#image("../private/notes.txt")')
    )

    rendered = pdf_renderer(clock).render(document)

    (construction,) = spy.constructions
    root = Path(str(construction["kwargs"]["root"]))
    assert not construction["root_is_symlink"]
    assert construction["root_listing"] == []
    assert spy.compiles[0]["root_listing"] == []
    assert not root.exists()
    assert PRIVATE_MARKER not in pdf_text(rendered.content)
    assert [p.name for p in secret_file.parent.iterdir()] == ["notes.txt"], "nothing was added"


# ---- The template and the data ----------------------------------------------------------------

FORBIDDEN_IN_TEMPLATE = (
    r"#?\bimport\b",
    r"#?\binclude\b",
    r"#?\bread\s*\(",
    r"\beval\b",
    r"\bplugin\s*\(",
    r"\b(?:csv|xml|yaml|toml|cbor)\s*\(",
    r"\bbibliography\s*\(",
    r"\bimage\s*\(\s*\"",
    r"\bjson\s*\(\s*\"",
    r"\bfont\s*:\s*\"[^\"]*[/\\]",
    r"://",
    r"@preview",
)


def _without_comments(source: str) -> str:
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    return re.sub(r"(?m)^\s*//.*$|[ \t]//[^\"\n]*$", "", source)


def test_pdf_template_source_has_no_imports_reads_or_paths() -> None:
    source = pdf_module().TEMPLATE_SOURCE

    assert isinstance(source, str) and len(source) > 100
    code = _without_comments(source)
    for pattern in FORBIDDEN_IN_TEMPLATE:
        assert not re.search(pattern, code), f"the template matches {pattern!r}"
    for pattern in (r"://", r"@preview"):
        assert not re.search(pattern, source), f"the template mentions {pattern!r}"
    # Images, if any, are decoded from bytes that come in as data, never opened by name.
    for call in re.finditer(r"\bimage\s*\(\s*([^,)\s]+)", code):
        assert not call.group(1).startswith(('"', "'")), call.group(0)


def test_pdf_user_text_reaches_typst_only_as_data(
    clock: FrozenClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = rendering()
    marker = "Mark7731"
    document = sample_document(
        title=f"Title {marker}",
        author=f"Author {marker}",
        header_lines=(f"Header {marker}",),
        sections=(
            module.RenderSection(
                heading=f"Section {marker}",
                lines=(f"Line {marker}",),
                entries=(module.RenderEntry(heading=f"Entry {marker}", lines=(f"Role {marker}",)),),
            ),
        ),
    )
    spy = install_typst_spy(monkeypatch)

    pdf_renderer(clock).render(document)

    source = spy.effective("input")
    assert source is not None, "the template is the compile input"
    source_text = source.decode("utf-8") if isinstance(source, bytes) else str(source)
    assert source_text == pdf_module().TEMPLATE_SOURCE, "the same source for every document"
    assert marker not in source_text
    inputs = spy.effective("sys_inputs")
    assert isinstance(inputs, dict) and inputs
    assert all(isinstance(k, str) and isinstance(v, str) for k, v in inputs.items())
    assert marker in "".join(inputs.values())
    timestamp = spy.effective("timestamp")
    assert timestamp == NOW or timestamp == int(NOW.timestamp()), "the date is the clock's"


def test_pdf_template_source_is_identical_for_different_documents(
    clock: FrozenClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = install_typst_spy(monkeypatch)
    renderer = pdf_renderer(clock)

    renderer.render(sample_document())
    renderer.render(sample_document(header_lines=("Someone Else",), page_size="Letter"))

    assert len(spy.compiles) == 2
    sources = {
        call["kwargs"].get("input") or construction["kwargs"].get("input")
        for call, construction in zip(spy.compiles, spy.constructions, strict=True)
    }
    assert None not in sources, "the template is the compile input"
    assert len(sources) == 1


TYPST_SYNTAX = (
    "#set page(width: 1pt)",
    '#import "@preview/cetz:0.1.0": canvas',
    "= Not a heading",
    "- not a list",
    "+ not numbered",
    "$ x^2 + y $",
    "`raw` ```block```",
    "*bold* _emphasis_",
    "@label <label>",
    "// not a comment",
    "/* not a block comment */",
    "back\\slash \\#escape \\u{41}",
    "#{ 1 + 1 } #(2 * 3) #[content]",
    "]] ) } ({ [[ ] \\",
    "#let x = 1",
    "/ Term: description",
)


def test_pdf_text_that_looks_like_typst_is_rendered_literally(clock: FrozenClock) -> None:
    module = rendering()
    document = sample_document(
        header_lines=TYPST_SYNTAX[:4],
        sections=(
            module.RenderSection(
                heading=TYPST_SYNTAX[4],
                lines=TYPST_SYNTAX[5:10],
                entries=(module.RenderEntry(heading=TYPST_SYNTAX[10], lines=TYPST_SYNTAX[11:]),),
            ),
        ),
    )

    rendered = pdf_renderer(clock).render(document)

    text = pdf_text(rendered.content)
    assert pdf_squash(text) == pdf_squash(document.plain_text())
    assert rendered.page_count == 1, "#set page(...) did not change the page"
