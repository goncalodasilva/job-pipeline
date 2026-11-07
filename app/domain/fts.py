"""User text as a full-text query (AC-M3.6; spec 3.3; migration 0003's ``facts_fts``). Pure."""

import unicodedata

# Control characters (NUL above all) can't sit inside an FTS5 string, and a lone surrogate
# can't be sent to SQLite at all.
_LEFT_OUT = frozenset({"Cc", "Cs"})


def _piece(text: str) -> str:
    return "".join(char for char in text if unicodedata.category(char) not in _LEFT_OUT)


def fts_query(text: str) -> str | None:
    """``text`` as FTS5 quoted tokens joined by spaces, so every piece must be found and
    operators (AND, OR, NOT, NEAR, ``*``, ``^``, ``column:``, brackets) are plain text.
    ``None`` for blank text: no text filter."""
    pieces = [piece for piece in map(_piece, text.split()) if piece]
    if not pieces:
        return None
    return " ".join('"' + piece.replace('"', '""') + '"' for piece in pieces)


def fts_prefix_query(text: str) -> str | None:
    """``fts_query`` where each piece also finds the words it starts (``"piece"*``): search as
    you type, for the command palette (M9.5; ADR-138). Operators stay plain text."""
    pieces = [piece for piece in map(_piece, text.split()) if piece]
    if not pieces:
        return None
    return " ".join('"' + piece.replace('"', '""') + '"*' for piece in pieces)
