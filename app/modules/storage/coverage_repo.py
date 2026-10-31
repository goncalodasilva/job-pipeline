"""Read-only batched coverage sources. Call inside Database.read() for one snapshot."""

import json
import sqlite3

from app.domain.coverage import (
    CoverageData,
    CoverageEvidence,
    CoverageLink,
    CoverageRequirement,
    CoverageSkill,
    coverage_data,
)

_CURRENT = """
WITH current_requirements AS (
    SELECT r.id, j.id AS job_id, r.kind, r.text, coalesce(st.phase, 'saved') AS job_state,
           s.id AS summary_id, s.summary, a.id AS application_id
    FROM jobs j
    LEFT JOIN applications a ON a.job_id = j.id
    LEFT JOIN application_statuses st ON st.key = a.status_key
    JOIN job_summaries s ON s.id = (
        SELECT id FROM job_summaries WHERE job_id = j.id
        ORDER BY created_at DESC, rowid DESC LIMIT 1
    )
    JOIN job_requirements r ON r.summary_id = s.id
    WHERE j.merged_into_job_id IS NULL AND j.triage_state <> 'removed'
      AND ((a.id IS NULL AND j.triage_state IN ('new', 'seen', 'snoozed', 'to_apply'))
           OR st.phase IN ('preparing', 'active'))
)
"""
_REQUIREMENTS = (
    _CURRENT  # noqa: S608 - concatenation of code-owned SQL constants only
    + "SELECT id,job_id,kind,text,job_state FROM current_requirements ORDER BY id"
)
_ASSESSMENTS = (
    _CURRENT  # noqa: S608 - concatenation of code-owned SQL constants only
    + """
SELECT DISTINCT r.id, f.id, f.type, f.status, f.privacy
FROM current_requirements r
JOIN job_assessments a ON a.id = (
    SELECT id FROM job_assessments WHERE job_id = r.job_id AND summary_id = r.summary_id
    ORDER BY created_at DESC, rowid DESC LIMIT 1
)
JOIN json_each(a.result_json, '$.requirements') m
  ON json_extract(m.value, '$.requirement_id') = r.id
JOIN json_each(m.value, '$.fact_ids') ref
JOIN facts f ON f.number = CAST(substr(ref.value, 2, instr(ref.value, '@') - 2) AS INTEGER)
JOIN fact_versions v ON v.id = f.current_version_id
WHERE json_extract(m.value, '$.met') = 1
  AND ref.value = 'F' || f.number || '@' || v.version
  AND f.status = 'confirmed' AND f.privacy <> 'never_use'
  AND NOT EXISTS (
      SELECT 1 FROM json_each(m.value, '$.fact_ids') cited
      WHERE NOT EXISTS (
          SELECT 1 FROM json_each(a.fact_refs_json) sent WHERE sent.value = cited.value
      )
  )
"""
)
_GAPS = (
    _CURRENT  # noqa: S608 - concatenation of code-owned SQL constants only
    + """
SELECT DISTINCT r.id, f.id, f.type, f.status, f.privacy
FROM current_requirements r
JOIN gap_checks g ON g.id = (
    SELECT id FROM gap_checks WHERE application_id = r.application_id
    ORDER BY created_at DESC, id DESC LIMIT 1
) AND g.summary = r.summary
JOIN json_each(g.result, '$.requirements') m
  ON json_extract(m.value, '$.requirement_id') = r.id
JOIN json_each(m.value, '$.fact_ids') ref
JOIN gap_check_facts sent ON sent.gap_check_id = g.id
JOIN facts f ON f.id = sent.fact_id AND f.current_version_id = sent.fact_version_id
JOIN fact_versions v ON v.id = sent.fact_version_id
WHERE json_extract(m.value, '$.verdict') IN ('met', 'partial')
  AND ref.value = 'F' || f.number || '@' || v.version
  AND f.status = 'confirmed' AND f.privacy <> 'never_use'
  AND NOT EXISTS (
      SELECT 1 FROM json_each(m.value, '$.fact_ids') cited
      WHERE NOT EXISTS (
          SELECT 1 FROM gap_check_facts gf JOIN facts sf ON sf.id = gf.fact_id
          JOIN fact_versions sv ON sv.id = gf.fact_version_id
          WHERE gf.gap_check_id = g.id AND cited.value = 'F' || sf.number || '@' || sv.version
      )
  )
"""
)
_SKILL_FACTS = """
SELECT s.normalized_name, f.id, f.type, f.status, f.privacy FROM fact_skills fs
JOIN skills s ON s.id = fs.skill_id JOIN facts f ON f.id = fs.fact_id
WHERE f.status = 'confirmed' AND f.privacy <> 'never_use'
ORDER BY s.normalized_name, f.id
"""
_SKILLS = "SELECT name,aliases FROM skills ORDER BY normalized_name"


_CLOSED_JOB = """
SELECT r.id, j.id, r.kind, r.text, 'closed'
FROM jobs j
JOIN applications a ON a.job_id = j.id
JOIN application_statuses st ON st.key = a.status_key AND st.phase = 'closed'
JOIN job_summaries s ON s.id = (
    SELECT id FROM job_summaries WHERE job_id = j.id ORDER BY created_at DESC, rowid DESC LIMIT 1
)
JOIN job_requirements r ON r.summary_id = s.id
WHERE j.id = ? AND j.merged_into_job_id IS NULL AND j.triage_state <> 'removed'
ORDER BY r.id
"""


def closed_job_requirements(
    conn: sqlite3.Connection, job_id: str
) -> tuple[CoverageRequirement, ...]:
    """The current requirements of a job whose application is closed (rejected, withdrawn): out
    of the coverage clusters, but still what a session about that role is about (M7.9)."""
    return tuple(CoverageRequirement(*row) for row in conn.execute(_CLOSED_JOB, (job_id,)))


def read(conn: sqlite3.Connection) -> CoverageData:
    """Exactly five reads, independent of job, requirement and fact counts. No fact text."""
    requirements = tuple(CoverageRequirement(*row) for row in conn.execute(_REQUIREMENTS))
    citations = [
        CoverageLink(row[0], CoverageEvidence(*row[1:]))
        for sql in (_ASSESSMENTS, _GAPS)
        for row in conn.execute(sql)
    ]
    skill_facts = tuple((row[0], CoverageEvidence(*row[1:])) for row in conn.execute(_SKILL_FACTS))
    skills = tuple(
        CoverageSkill(row[0], tuple(json.loads(row[1]))) for row in conn.execute(_SKILLS)
    )
    return coverage_data(requirements, skills, citations, skill_facts)
