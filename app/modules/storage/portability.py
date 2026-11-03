"""Typed, schema-checked SQLite snapshots (ADR-131). No SQL comes from an export."""

import base64
import binascii
import json
import math
import re
import sqlite3
from typing import Any

from app.modules.storage.database import Database
from app.modules.storage.runner import MIGRATIONS_DIR, load_migrations


class PortabilityError(ValueError):
    """The snapshot cannot be restored; never includes supplied content."""


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _catalog(conn: sqlite3.Connection) -> tuple[list[str], list[str]]:
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    fts = {r[1] for r in conn.execute("PRAGMA table_list") if r[2] in ("virtual", "shadow")}
    excluded = tables & (fts | {"schema_migrations", "sqlite_sequence"})
    return sorted(tables - excluded), sorted(excluded)


def _encode(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"base64": base64.b64encode(value).decode("ascii")}
    if isinstance(value, float) and not math.isfinite(value):
        raise PortabilityError("A database value cannot be exported.")
    return value


def _decode(value: Any) -> Any:
    if value is None or type(value) in (str, int):
        if type(value) is int and not -(2**63) <= value < 2**63:
            raise PortabilityError("Invalid database value.")
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if isinstance(value, dict) and set(value) == {"base64"}:
        try:
            return base64.b64decode(value["base64"], validate=True)
        except (ValueError, TypeError, binascii.Error):
            pass
    raise PortabilityError("Invalid database value.")


def export_tables(db: Database, *, include_usage_snapshots: bool = False) -> dict[str, Any]:
    """Read all persistent tables in one snapshot, explicitly listing omissions."""
    with db.read() as conn:
        names, excluded = _catalog(conn)
        if not include_usage_snapshots and "provider_usage_snapshots" in names:
            names.remove("provider_usage_snapshots")
            excluded.append("provider_usage_snapshots")
        tables = {}
        for name in names:
            cursor = conn.execute(f"SELECT * FROM {_quote(name)}")  # noqa: S608 - quoted catalog identifier
            tables[name] = {
                "columns": [c[0] for c in cursor.description],
                "rows": [[_encode(v) for v in row] for row in cursor],
            }
        return {
            "schema_version": conn.execute("SELECT max(version) FROM schema_migrations").fetchone()[
                0
            ],
            "tables": tables,
            "excluded": sorted(excluded),
        }


def _seed_rows(version: int) -> dict[str, list[tuple[Any, ...]]]:
    # Migration SQL alone defines defaults. Never guess that all rows in a seeded table
    # are disposable: a user may have renamed a status or changed a controlled value.
    conn = sqlite3.connect(":memory:")
    try:
        for migration in load_migrations(MIGRATIONS_DIR):
            if migration.version <= version:
                conn.executescript(migration.path.read_text(encoding="utf-8"))
        names, _ = _catalog(conn)
        return {n: conn.execute(f"SELECT * FROM {_quote(n)}").fetchall() for n in names}  # noqa: S608 - quoted catalog identifiers
    finally:
        conn.close()


def is_empty(db: Database) -> bool:
    """Only migration defaults and setup progress may exist before restore."""
    with db.read() as conn:
        return _is_empty(conn)


def _is_empty(conn: sqlite3.Connection) -> bool:
    version = conn.execute("SELECT max(version) FROM schema_migrations").fetchone()[0]
    seeds = _seed_rows(version)
    names, _ = _catalog(conn)
    return set(names) == set(seeds) and all(
        sorted(conn.execute(f"SELECT * FROM {_quote(n)}").fetchall(), key=repr)  # noqa: S608 - quoted catalog identifier
        == sorted(rows, key=repr)
        for n, rows in seeds.items()
        if n != "setup_state"
    )


def _ordered(conn: sqlite3.Connection, names: list[str]) -> list[str]:
    # Current-version pointers deliberately form deferred cycles. Insert owners first;
    # version insert triggers can then build the FTS entry from the owner's pointer.
    dependencies = {}
    for name in names:
        foreign = conn.execute(f"PRAGMA foreign_key_list({_quote(name)})").fetchall()
        pointers = {r[0] for r in foreign if r[3] == "current_version_id"}
        dependencies[name] = {r[2] for r in foreign if r[0] not in pointers and r[2] != name} & set(
            names
        )
    result: list[str] = []
    while dependencies:
        ready = sorted(n for n, deps in dependencies.items() if not deps)
        if not ready:
            raise PortabilityError("Unsupported table dependency cycle.")
        for n in ready:
            result.append(n)
            del dependencies[n]
        for deps in dependencies.values():
            deps.difference_update(ready)
    return result


def import_tables(db: Database, snapshot: dict[str, Any]) -> None:
    """Replace only migration defaults; guards and rows roll back together on failure."""
    try:
        with db.transaction() as conn:
            if not _is_empty(conn):
                raise PortabilityError("Restore needs a workspace with no content.")
            version = conn.execute("SELECT max(version) FROM schema_migrations").fetchone()[0]
            if (
                type(snapshot.get("schema_version")) is not int
                or snapshot["schema_version"] != version
            ):
                raise PortabilityError("The export schema does not match this database.")
            names, excluded = _catalog(conn)
            tables = snapshot["tables"]
            omitted = snapshot["excluded"]
            if not isinstance(tables, dict) or not isinstance(omitted, list):
                raise PortabilityError("Invalid table manifest.")
            if set(omitted) not in (set(excluded), set(excluded) | {"provider_usage_snapshots"}):
                raise PortabilityError("Invalid table exclusions.")
            expected = set(names) - set(omitted)
            if set(tables) != expected or len(omitted) != len(set(omitted)):
                raise PortabilityError("Invalid table manifest.")
            prepared: dict[str, list[list[Any]]] = {}
            for name in names:
                if name not in tables:
                    prepared[name] = []
                    continue
                table = tables[name]
                columns = [r[1] for r in conn.execute(f"PRAGMA table_info({_quote(name)})")]
                if (
                    set(table) != {"columns", "rows"}
                    or table["columns"] != columns
                    or not isinstance(table["rows"], list)
                ):
                    raise PortabilityError("Invalid table columns.")
                rows = []
                for row in table["rows"]:
                    if not isinstance(row, list) or len(row) != len(columns):
                        raise PortabilityError("Invalid table row.")
                    values = [_decode(v) for v in row]
                    if name == "settings" and values[columns.index("key")] == "ai_provider":
                        index = columns.index("value")
                        setting = json.loads(values[index])
                        if not isinstance(setting, dict):
                            raise PortabilityError("Invalid provider settings.")
                        setting.pop("sign_in_saved_for", None)
                        setting["saved_sign_ins"] = []
                        values[index] = json.dumps(setting, separators=(",", ":"))
                    rows.append(values)
                prepared[name] = rows
            order = _ordered(conn, names)
            guards = [
                (r[0], r[1])
                for r in conn.execute(
                    "SELECT name, sql FROM sqlite_master WHERE type='trigger' ORDER BY name"
                )
                if re.search(r"\bRAISE\s*\(", r[1], re.I)
            ]
            for name, _ in guards:
                conn.execute(f"DROP TRIGGER {_quote(name)}")
            conn.execute("PRAGMA defer_foreign_keys = ON")
            for name in reversed(order):
                conn.execute(f"DELETE FROM {_quote(name)}")  # noqa: S608 - quoted catalog identifier
            for name in order:
                columns = [r[1] for r in conn.execute(f"PRAGMA table_info({_quote(name)})")]
                marks = ",".join("?" for _ in columns)
                quoted = ",".join(_quote(c) for c in columns)
                query = f"INSERT INTO {_quote(name)} ({quoted}) VALUES ({marks})"  # noqa: S608 - quoted catalog identifiers; bound values
                conn.executemany(query, prepared[name])
            for _, sql in guards:
                conn.execute(sql)
            if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise PortabilityError("The export contains broken references.")
    except PortabilityError:
        raise
    except (KeyError, TypeError, ValueError, sqlite3.Error, OverflowError) as error:
        raise PortabilityError("The export contains invalid database content.") from error
