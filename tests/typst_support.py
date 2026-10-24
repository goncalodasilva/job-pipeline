"""Test helpers that know the PDF library is typst (M6.2; ADR-108).

Everything here assumes ``app.render.providers.pdf`` builds ``typst.Compiler(root=..., ...)`` with
keyword arguments and calls ``compiler.compile(...)`` through the module its ``_load_typst()``
returns. Tests that are about the PDF format, not the library, use ``tests/render_support.py``
only, so a different PDF library would change this file and the ``*typst*`` tests and nothing else.
"""

import importlib
import os
import threading
from typing import Any

from tests.render_support import pdf_module


class SpyCompiler:
    """Wraps one real ``typst.Compiler``; records how it was built and used."""

    def __init__(self, spy: "TypstSpy", real: Any, root: str | None) -> None:
        self._spy = spy
        self._real = real
        self._root = root

    def compile(self, *args: Any, **kwargs: Any) -> Any:
        call = {
            "args": args,
            "kwargs": kwargs,
            "root_listing": root_listing(self._root),
            "thread": threading.get_ident(),
        }
        self._spy.compiles.append(call)
        if self._spy.before_compile is not None:
            self._spy.before_compile()
        return self._real.compile(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


def root_listing(root: str | None) -> list[tuple[str, bool]] | None:
    """Every path under ``root`` as (relative path, is_symlink); None when it doesn't exist."""
    if root is None or not os.path.isdir(root):
        return None
    found: list[tuple[str, bool]] = []
    for folder, names, files in os.walk(root):
        for name in (*names, *files):
            path = os.path.join(folder, name)
            found.append((os.path.relpath(path, root), os.path.islink(path)))
    return sorted(found)


class TypstSpy:
    """Stands in for the ``typst`` module: the real ``Compiler`` behind a recorder.

    The PDF renderer must build its compiler as ``typst.Compiler(root=..., ...)`` with keyword
    arguments, and call ``compiler.compile(...)``, through the module its ``_load_typst()``
    returns. ``before_compile`` runs just before the real compile (a test may block there).
    """

    def __init__(self, real: Any) -> None:
        self._real = real
        self.constructions: list[dict[str, Any]] = []
        self.compiles: list[dict[str, Any]] = []
        self.before_compile: Any = None

    def Compiler(self, *args: Any, **kwargs: Any) -> SpyCompiler:
        root = kwargs.get("root")
        self.constructions.append(
            {
                "args": args,
                "kwargs": kwargs,
                "root_listing": root_listing(None if root is None else str(root)),
                "root_is_symlink": root is not None and os.path.islink(str(root)),
                "root_exists": root is not None and os.path.isdir(str(root)),
            }
        )
        return SpyCompiler(
            self, self._real.Compiler(*args, **kwargs), None if root is None else str(root)
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    def effective(self, key: str) -> Any:
        """A setting given to the constructor or to ``compile`` (the call wins)."""
        value = None
        for event in (*self.constructions, *self.compiles):
            value = event["kwargs"].get(key, value)
        return value


def install_typst_spy(monkeypatch: Any) -> TypstSpy:
    """Route ``app.render.providers.pdf`` through a ``TypstSpy`` around the real typst."""
    real = importlib.import_module("typst")
    spy = TypstSpy(real)
    monkeypatch.setattr(pdf_module(), "_load_typst", lambda: spy)
    return spy


class BlockedTypst:
    """A typst whose compile never finishes until the test lets it. Counts the compiles started."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.started = threading.Event()
        self.compiles = 0

    def Compiler(self, *args: Any, **kwargs: Any) -> "BlockedTypst":
        return self

    def compile(self, *args: Any, **kwargs: Any) -> bytes:
        self.compiles += 1
        self.started.set()
        self.release.wait(timeout=30)
        return b"%PDF-1.7\n"
