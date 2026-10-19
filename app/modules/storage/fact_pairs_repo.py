"""Rows of the ``fact_pair_dismissals`` table (migration 0003): pairs of facts the user said
are not duplicates, until they take it back. The two ids are stored in sorted order (the table
checks it). Callers own the transaction."""

import sqlite3

# Literal SQL only (no string building).
_INSERT = (
    "INSERT OR IGNORE INTO fact_pair_dismissals (fact_a_id, fact_b_id, created_at) VALUES (?, ?, ?)"
)
_SELECT_ALL = "SELECT fact_a_id, fact_b_id FROM fact_pair_dismissals"
_DELETE = "DELETE FROM fact_pair_dismissals WHERE fact_a_id = ? AND fact_b_id = ?"


def sorted_pair(first: str, second: str) -> tuple[str, str]:
    a, b = sorted((first, second))
    return a, b


def dismiss(conn: sqlite3.Connection, first: str, second: str, *, now: str) -> None:
    """Remember a pair; a pair already remembered is left as it is."""
    a, b = sorted_pair(first, second)
    conn.execute(_INSERT, (a, b, now))


def undismiss(conn: sqlite3.Connection, first: str, second: str) -> bool:
    """Forget a pair (the user's Undo); False when it wasn't remembered."""
    return conn.execute(_DELETE, sorted_pair(first, second)).rowcount == 1


def dismissed(conn: sqlite3.Connection) -> frozenset[tuple[str, str]]:
    """Every remembered pair, ids in sorted order."""
    return frozenset((str(row[0]), str(row[1])) for row in conn.execute(_SELECT_ALL).fetchall())
