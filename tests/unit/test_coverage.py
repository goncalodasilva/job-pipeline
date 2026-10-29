"""M7.1 clusters and strength, using fictional requirements and evidence only."""

import hashlib
import re
from statistics import median
from time import perf_counter

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.domain.coverage import (
    CoverageEvidence,
    CoverageRequirement,
    CoverageSkill,
    cluster_requirements,
    strength,
)


def req(text, id="r1", job="j1", kind="key"):
    return CoverageRequirement(id, job, kind, text, "saved")


@pytest.mark.parametrize("name", ["Python", "\uff30\uff39\uff34\uff28\uff2f\uff2e", "py"])
def test_clusters_group_requirements_naming_the_same_controlled_skill(name):
    skills = [CoverageSkill("Python", ("py",))]
    groups = cluster_requirements(
        [req(f"Use {name} daily"), req("Python experience", "r2")], skills
    )
    assert len(groups) == 1
    assert groups[0].key == "skill:python"
    assert {r.id for r in groups[0].requirements} == {"r1", "r2"}


@pytest.mark.parametrize("text", ["CPython internals", "Pythonic design", "C++ development"])
def test_skill_match_is_a_whole_phrase_and_preserves_punctuation(text):
    groups = cluster_requirements([req(text)], [CoverageSkill("Python"), CoverageSkill("C")])
    assert groups[0].key.startswith("text:")


def test_longest_skill_phrase_wins_deterministically():
    skills = [CoverageSkill("SQL"), CoverageSkill("SQL Server"), CoverageSkill("Python")]
    rows = [req("Build with SQL Server and Python")]
    assert cluster_requirements(rows, skills) == cluster_requirements(rows, list(reversed(skills)))
    assert cluster_requirements(rows, skills)[0].key == "skill:sql server"


def test_clusters_group_near_identical_wording():
    rows = [req("Maintain reliable irrigation systems."), req("Maintain irrigation systems", "r2")]
    assert len(cluster_requirements(rows, [])) == 1


@pytest.mark.parametrize(
    "first,second",
    [
        ("Manage regional delivery budgets", "Maintain regional irrigation systems"),
        ("Lead harvest planning", "Lead payroll audits"),
        ("Design database indexes", "Design greenhouse layouts"),
        ("and the", "or a"),
    ],
)
def test_clusters_keep_distinct_requirements_apart(first, second):
    assert len(cluster_requirements([req(first), req(second, "r2")], [])) == 2


@given(
    st.permutations(
        [
            req("Maintain reliable irrigation systems", "r1"),
            req("Maintain irrigation systems", "r2"),
            req("Python and SQL", "r3"),
            req("Python experience", "r4"),
            req("Audit regional payroll", "r5"),
        ]
    )
)
def test_clusters_independent_of_input_order(rows):
    skills = [CoverageSkill("Python"), CoverageSkill("SQL")]
    assert cluster_requirements(rows, skills) == cluster_requirements(
        sorted(rows, key=lambda r: r.id), skills
    )


def test_cluster_id_stable_and_safe_shape():
    first = cluster_requirements([req("Irrigation systems")], [])[0]
    again = cluster_requirements([req("Systems, irrigation!", "r2", "j2")], [])[0]
    assert first.key == again.key
    assert first.id == hashlib.sha256(first.key.encode()).hexdigest()[:12] == again.id
    assert re.fullmatch("[0-9a-f]{12}", first.id)


def test_cluster_label_most_frequent_wording():
    rows = [req("Python experience", "r1"), req("Use Python", "r2"), req("Python experience", "r3")]
    assert cluster_requirements(rows, [CoverageSkill("Python")])[0].label == "Python experience"
    # Shortest, then alphabetical; input order cannot choose the label.
    assert cluster_requirements(rows[:2], [CoverageSkill("Python")])[0].label == "Use Python"
    tied = [req("Use Python", "r1"), req("Add Python", "r2")]
    assert cluster_requirements(tied, [CoverageSkill("Python")])[0].label == "Add Python"


@pytest.mark.parametrize(
    "case,rows,expected",
    [
        ("none", [], "none"),
        ("thin_facts_only", [("f1", "fact", "confirmed", "normal")], "thin"),
        ("thin_one_story", [("f1", "story", "confirmed", "generalize")], "thin"),
        (
            "strong_two_stories",
            [("f1", "story", "confirmed", "normal"), ("f2", "story", "confirmed", "normal")],
            "strong",
        ),
        ("draft_ignored", [("f1", "story", "draft", "normal")], "none"),
        ("never_use_ignored", [("f1", "story", "confirmed", "never_use")], "none"),
        ("archived_ignored", [("f1", "story", "archived", "normal")], "none"),
        ("discarded_ignored", [("f1", "story", "discarded", "normal")], "none"),
        ("duplicate_story", [("f1", "story", "confirmed", "normal")] * 2, "thin"),
    ],
)
def test_ac_m7_5_strength_definitions(case, rows, expected):
    assert strength([CoverageEvidence(*row) for row in rows]) == expected


def fictional_requirements():
    wordings = (
        "Maintain reliable irrigation systems",
        "Maintain irrigation systems",
        "Audit regional payroll",
        "Prepare seasonal harvest forecasts",
        "Manage warehouse inventory",
        "Plan delivery routes",
        "Python data pipelines",
        "SQL reporting queries",
        "Coach greenhouse technicians",
        "Inspect crop quality",
    )
    return [req(wordings[n], f"r{job}-{n}", f"j{job}") for job in range(1000) for n in range(10)]


@pytest.mark.slow
def test_clustering_1000_jobs_under_budget():
    rows = fictional_requirements()
    skills = [CoverageSkill("Python"), CoverageSkill("SQL")]
    elapsed = []
    for _ in range(5):
        start = perf_counter()
        result = cluster_requirements(rows, skills)
        elapsed.append((perf_counter() - start) * 1000)
    assert len(result) == 9
    assert sum(len(group.requirements) for group in result) == 10_000
    print(f"M7.1 clustering milliseconds: {elapsed}; median={median(elapsed):.3f}")
    assert median(elapsed) < 100
