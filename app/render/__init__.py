"""Document rendering interface (spec 2.4; ADR-106).

Services hand a ``RenderDocument`` to a ``DocumentRenderer`` and get the bytes of a file back.
This package imports no vendor library: python-docx and any PDF library live only in
``app/render/providers``, and providers are chosen in ``app/wiring.py``.
"""

from dataclasses import dataclass
from typing import Protocol

from app.domain.rendering import RenderDocument


class RendererUnavailable(Exception):
    """A renderer cannot run here, for example because a system library is missing."""


@dataclass(frozen=True)
class RenderedFile:
    content: bytes
    page_count: int | None  # None where the format has no fixed pages, as for Word


class DocumentRenderer(Protocol):
    @property
    def format(self) -> str: ...

    @property
    def media_type(self) -> str: ...

    def render(self, document: RenderDocument) -> RenderedFile: ...


__all__ = ["DocumentRenderer", "RenderedFile", "RendererUnavailable"]
