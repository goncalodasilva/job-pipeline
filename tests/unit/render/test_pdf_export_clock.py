"""The normal application clock includes fractions of a second; PDF export must accept it."""

from datetime import UTC, datetime

from app.domain.rendering import RenderDocument
from app.render.providers.pdf import PdfRenderer
from tests.fakes.clock import FrozenClock


def test_pdf_accepts_clock_with_microseconds():
    clock = FrozenClock(datetime(2026, 10, 28, 12, 0, 0, 123456, tzinfo=UTC))
    result = PdfRenderer(clock).render(
        RenderDocument("CV", "Fictional Person", header_lines=("Fictional Person",))
    )
    assert result.content.startswith(b"%PDF") and result.page_count == 1
