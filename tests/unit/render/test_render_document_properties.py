"""Property: any generated document renders to a Word file whose text is its plain text.

(M6.1; spec 4.1.) The strategy draws from every Unicode category on purpose, including control
characters, lone surrogates, private use, unassigned code points and U+FFFE/U+FFFF: the renderer
and ``plain_text()`` must both clean them, and rendering never raises for any text.
"""

from hypothesis import given, settings
from hypothesis import strategies as st

from tests.fakes.clock import FrozenClock
from tests.render_support import NOW, docx_core_properties, docx_text, rendering, word_renderer

# Every code point, surrogates included (hypothesis leaves Cs out unless asked).
CHARACTERS = st.characters(exclude_categories=())
# Characters XML cannot hold, one private-use character and a lone surrogate, drawn often.
AWKWARD = [chr(code) for code in (0xD800, 0xDFFF, 0xFFFE, 0xFFFF, 0xE000)]
TEXT = st.one_of(
    st.text(CHARACTERS, max_size=30),
    st.text(st.sampled_from(list("ab \t\né€‮\x00\x07\r") + AWKWARD), max_size=12),
)
LINES = st.lists(TEXT, max_size=3).map(tuple)


@st.composite
def documents(draw: st.DrawFn) -> object:
    module = rendering()
    entries = st.builds(module.RenderEntry, heading=TEXT, lines=LINES)
    sections = st.builds(
        module.RenderSection,
        heading=TEXT,
        lines=LINES,
        entries=st.lists(entries, max_size=3).map(tuple),
    )
    return draw(
        st.builds(
            module.RenderDocument,
            title=TEXT,
            author=TEXT,
            page_size=st.sampled_from(["A4", "Letter"]),
            header_lines=LINES,
            sections=st.lists(sections, max_size=3).map(tuple),
        )
    )


@settings(max_examples=60, deadline=None)
@given(document=documents())
def test_render_document_text_round_trips(document: object) -> None:
    clean = rendering().clean_render_text
    rendered = word_renderer(FrozenClock(NOW)).render(document)

    assert docx_text(rendered.content) == document.plain_text()  # type: ignore[attr-defined]
    properties = docx_core_properties(rendered.content)
    assert properties.title == clean(document.title)  # type: ignore[attr-defined]
    assert properties.author == clean(document.author)  # type: ignore[attr-defined]
