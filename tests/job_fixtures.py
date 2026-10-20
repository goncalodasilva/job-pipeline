"""Deterministic fictional inbox data, independent of AI and network providers."""

from datetime import UTC, datetime, timedelta
from typing import Any

from app.domain.job_normalization import company_key
from app.modules.storage import Database
from app.modules.storage.jobs_repo import insert_company, insert_job


def generate_jobs(count: int = 1000) -> tuple[dict[str, Any], ...]:
    start = datetime(2026, 10, 20, 9, tzinfo=UTC)
    return tuple(
        {
            "id": f"job-{i + 1:04d}",
            "title": f"Fictional Greenhouse Engineer fixture{i + 1:04d}",
            "snapshot_text": (
                f"Fictional greenhouse role fixture{i + 1:04d}. Maintain irrigation systems."
            ),
            "company_id": f"company-{i % 20 + 1:04d}",
            "company_name": f"Fictional Orchard {i % 20 + 1}",
            "industry": (
                "Fictional agriculture",
                "Fictional transport",
                "Fictional energy",
                "Fictional software",
                "Fictional education",
            )[i % 5],
            "work_mode": ("remote", "hybrid", "on_site")[i % 3],
            "seniority": ("senior", "mid", "junior", "lead")[i % 4],
            "fit_band": ("strong", "possible", "stretch", None)[i % 4],
            "deal_breaker": int(i % 7 == 0),
            "posted_date": (start.date() - timedelta(days=i % 90)).isoformat(),
            "found_at": (start + timedelta(seconds=i)).isoformat(),
        }
        for i in range(count)
    )


def seed_jobs(database: Database, rows: tuple[dict[str, Any], ...]) -> None:
    with database.transaction() as conn:
        companies = {r["company_id"]: r["company_name"] for r in rows}
        stamp = "2026-10-20T09:00:00+00:00"
        for id, name in companies.items():
            insert_company(conn, id=id, name=name, name_key=company_key(name), now=stamp)
        for r in rows:
            insert_job(
                conn,
                id=r["id"],
                title=r["title"],
                snapshot_text=r["snapshot_text"],
                company_id=r["company_id"],
                captured_at=r["found_at"],
                found_at=r["found_at"],
                now=r["found_at"],
            )
            conn.execute(
                "UPDATE jobs SET industry=?,work_mode=?,seniority=?,fit_band=?,"
                "deal_breaker=?,posted_date=? WHERE id=?",
                (
                    r["industry"],
                    r["work_mode"],
                    r["seniority"],
                    r["fit_band"],
                    r["deal_breaker"],
                    r["posted_date"],
                    r["id"],
                ),
            )
