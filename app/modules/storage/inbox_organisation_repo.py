"""Saved views and atomic controlled-value decisions. Callers own transactions."""

import json
import sqlite3
from typing import Any

from app.domain.errors import VersionConflict
from app.domain.ids import IdFactory
from app.domain.job_normalization import value_key

VALUE_FIELDS = ("industry", "work_mode", "employment_type", "seniority")
NAME_TAKEN = "A view already uses that name. Choose another."


def saved_views(conn: sqlite3.Connection) -> tuple[dict[str, Any], ...]:
    return tuple(
        {"id": id_, "name": name, "values": json.loads(raw), "version": version}
        for id_, name, raw, version in conn.execute(
            "SELECT id,name,filters_json,version FROM saved_views ORDER BY name COLLATE NOCASE,id"
        )
    )


def unique_name(conn: sqlite3.Connection, name: str, excluding: str = "") -> None:
    if any(
        row["name"].casefold() == name.casefold() and row["id"] != excluding
        for row in saved_views(conn)
    ):
        raise ValueError(NAME_TAKEN)


def create_view(
    conn: sqlite3.Connection, id_: str, name: str, values: dict[str, str], now: str
) -> None:
    unique_name(conn, name)
    try:
        conn.execute(
            "INSERT INTO saved_views (id,name,filters_json,created_at,updated_at) "
            "VALUES (?,?,?,?,?)",
            (id_, name, json.dumps(values), now, now),
        )
    except sqlite3.IntegrityError:
        raise ValueError(NAME_TAKEN) from None


def change_view(
    conn: sqlite3.Connection, id_: str, name: str | None, version: int, now: str
) -> None:
    row = conn.execute("SELECT version FROM saved_views WHERE id=?", (id_,)).fetchone()
    if row is None:
        raise LookupError
    if row[0] != version:
        raise VersionConflict()
    if name is None:
        conn.execute("DELETE FROM saved_views WHERE id=? AND version=?", (id_, version))
    else:
        unique_name(conn, name, id_)
        try:
            conn.execute(
                "UPDATE saved_views SET name=?,version=version+1,updated_at=? "
                "WHERE id=? AND version=?",
                (name, now, id_, version),
            )
        except sqlite3.IntegrityError:
            raise ValueError(NAME_TAKEN) from None


def controlled(conn: sqlite3.Connection) -> tuple[dict[str, Any], ...]:
    return tuple(
        {
            "id": id_,
            "field": field,
            "value": value,
            "aliases": json.loads(aliases),
            "version": version,
        }
        for id_, field, value, aliases, version in conn.execute(
            "SELECT id,field,value,aliases,version FROM controlled_values ORDER BY field,value"
        )
    )


def pending_count(conn: sqlite3.Connection) -> int:
    """How many unknown values wait, without counting the jobs that hold each one."""
    marks = ",".join("?" * len(VALUE_FIELDS))
    row = conn.execute(
        f"SELECT count(*) FROM field_value_queue WHERE status='pending' AND field IN ({marks})",  # noqa: S608 - placeholders only
        VALUE_FIELDS,
    ).fetchone()
    return int(row[0])


def pending(conn: sqlite3.Connection) -> tuple[dict[str, Any], ...]:
    found = []
    for id_, field, value, version in conn.execute(
        "SELECT id,field,value,version FROM field_value_queue WHERE status='pending' "
        "ORDER BY field,value,id"
    ):
        if field not in VALUE_FIELDS:
            continue
        count = conn.execute(f"SELECT count(*) FROM jobs WHERE {field}=?", (value,)).fetchone()[0]  # noqa: S608 - field whitelist above
        found.append(
            {"id": id_, "field": field, "value": value, "version": version, "count": count}
        )
    return tuple(found)


def decide(
    conn: sqlite3.Connection,
    id_: str,
    action: str,
    version: int,
    target: str,
    ids: IdFactory,
    now: str,
) -> None:
    row = conn.execute(
        "SELECT field,value,status,version FROM field_value_queue WHERE id=?", (id_,)
    ).fetchone()
    if row is None:
        raise LookupError
    field, value, status, stored_version = row
    if stored_version != version or status != "pending":
        raise VersionConflict()
    if field not in VALUE_FIELDS or action not in ("map", "new", "ignore"):
        raise ValueError("Choose a valid action for this field.")
    if action != "ignore":
        choices = [item for item in controlled(conn) if item["field"] == field]
        owners = [
            item
            for item in choices
            if value_key(value) in {value_key(v) for v in (item["value"], *item["aliases"])}
        ]
        if action == "new":
            if owners or not value_key(value):
                raise ValueError(
                    "This text already belongs to a value, or has no letters or numbers."
                )
            destination = value
            conn.execute(
                "INSERT INTO controlled_values (id,field,value,created_at,updated_at) "
                "VALUES (?,?,?,?,?)",
                (ids.new(), field, value, now, now),
            )
        else:
            target_id, _, raw_version = target.partition("@")
            picked = next((item for item in choices if item["id"] == target_id), None)
            if picked is None or not raw_version.isdecimal():
                raise ValueError("Choose an existing value for this field.")
            if str(picked["version"]) != raw_version:
                raise VersionConflict()
            if any(item["id"] != target_id for item in owners):
                raise ValueError("This text already belongs to another value.")
            destination = picked["value"]
            aliases = picked["aliases"]
            if value not in aliases and value != destination:
                aliases.append(value)
                conn.execute(
                    "UPDATE controlled_values SET aliases=?,updated_at=?,version=version+1 "
                    "WHERE id=? AND version=?",
                    (json.dumps(aliases), now, target_id, picked["version"]),
                )
        if destination != value:
            conn.execute(  # code-owned field; all user values are bound
                f"UPDATE jobs SET {field}=?,updated_at=?,version=version+1 WHERE {field}=? "  # noqa: S608
                "AND NOT EXISTS (SELECT 1 FROM json_each(jobs.user_corrected) WHERE value=?)",
                (destination, now, value, field),
            )
    conn.execute(
        "UPDATE field_value_queue SET status=?,updated_at=?,version=version+1 "
        "WHERE id=? AND version=?",
        ("ignored" if action == "ignore" else "mapped", now, id_, version),
    )


def filter_choices(conn: sqlite3.Connection) -> dict[str, tuple[dict[str, str], ...]]:
    result = {
        field: tuple(
            {"value": row[0], "label": row[0].replace("_", " ")}
            for row in conn.execute(
                f"SELECT DISTINCT {field} FROM jobs WHERE {field} IS NOT NULL ORDER BY {field}"  # noqa: S608 - constant fields below
            )
            if row[0]
        )
        for field in ("industry", "work_mode", "seniority")
    }
    result["company"] = tuple(
        {"value": id_, "label": name}
        for id_, name in conn.execute(
            "SELECT id,name FROM companies ORDER BY name COLLATE NOCASE,id"
        )
    )
    return result
