"""Question-bank reuse, ranking, duplicate scope and generated-source checks."""

from dataclasses import replace

from hypothesis import given
from hypothesis import strategies as st

from app.domain.coverage import CoverageSkill
from app.domain.fit import Requirement


def api():
    from app.domain import question_bank

    return question_bank


def test_bank_tags_match_coverage_clusters_on_read():
    bank = api()
    q = bank.BankQuestion(
        "q1", "How would you test Python services?", "technical", ("Python development",)
    )
    result = bank.rank_questions(
        [q], [Requirement("r1", "key", "Build Python services")], skills=[CoverageSkill("Python")]
    )
    assert result.ranked[0].requirement_ids == ("r1",)
    assert result.uncovered == ()


def test_ac_m8_1_candidates_key_requirement_clusters_plus_general_behavioral():
    bank = api()
    questions = [
        bank.BankQuestion("q1", "Describe teamwork.", "behavioral"),
        bank.BankQuestion("q2", "SQL?", "technical", ("SQL",)),
        bank.BankQuestion("q3", "Python?", "technical", ("Python",)),
    ]
    result = bank.rank_questions(
        questions, [Requirement("r1", "key", "Python"), Requirement("r2", "nice", "SQL")]
    )
    assert {q.question.id for q in result.ranked} == {"q1", "q3"}


def test_ac_m8_1_company_specific_only_for_same_company():
    bank = api()
    q = bank.BankQuestion("q1", "Why this company?", "behavioral", company_id="c1")
    assert not bank.rank_questions([q], [], company_id="c2").ranked
    assert bank.rank_questions([q], [], company_id="c1").ranked


def test_ac_m8_1_ranking_weighted_overlap_asked_count_role_family_and_ready():
    bank = api()
    base = bank.BankQuestion("q1", "Tell me about a project.", "behavioral", ("Python",))
    questions = [
        base,
        replace(base, id="q2", asked_count=3),
        replace(base, id="q3", requirements=("Python", "SQL")),
        replace(base, id="q4", role_family="software engineer"),
        replace(base, id="q5", asked_count=10),
    ]
    result = bank.rank_questions(
        questions,
        [Requirement("r1", "must_have", "Python"), Requirement("r2", "key", "SQL")],
        title="Senior Software Engineer",
        ready_ids={"q5"},
    )
    assert [q.question.id for q in result.ranked] == ["q2", "q3", "q4", "q1"]
    assert [q.score for q in result.ranked] == [6, 5, 4, 3]
    assert result.already_prepared[0].question.id == "q5"


def test_ac_m8_1_uncovered_key_requirements_only():
    bank = api()
    q = bank.BankQuestion("q1", "Python?", "technical", ("Python",))
    result = bank.rank_questions(
        [q],
        [
            Requirement("r1", "key", "Python"),
            Requirement("r2", "must_have", "SQL"),
            Requirement("r3", "nice", "Go"),
        ],
    )
    assert [r.id for r in result.uncovered] == ["r2"]


def test_near_identical_questions_merge_only_within_type_and_company_scope():
    bank = api()
    q = bank.BankQuestion("q1", "Tell me about a difficult project.", "behavioral")
    twin = replace(q, id="q2", text="Tell me about a difficult project!")
    assert bank.duplicate_question(twin, [q]).id == "q1"
    assert bank.duplicate_question(replace(twin, type="technical"), [q]) is None
    assert bank.duplicate_question(replace(twin, company_id="c1"), [q]) is None


def test_role_family_ignores_seniority_words():
    assert api().role_family("Senior Staff Software Engineer II") == "software engineer"


@given(st.lists(st.integers(min_value=0, max_value=30), max_size=20))
def test_bank_ranking_total_and_deterministic(counts):
    bank = api()
    questions = [
        bank.BankQuestion(f"q{i:03}", "Describe teamwork.", "behavioral", asked_count=n)
        for i, n in enumerate(counts)
    ]
    first = bank.rank_questions(questions, [])
    assert first == bank.rank_questions(reversed(questions), [])
    assert len(first.ranked) == len(questions)
    assert [r.score for r in first.ranked] == sorted(counts, reverse=True)


def test_generated_question_with_claims_not_in_job_or_cv_dropped():
    bank = api()
    data = {
        "questions": [
            {
                "text": "How did you save 95 percent at FictionalCorp?",
                "type": "technical",
                "requirement_ids": ["r1"],
                "company_specific": False,
            },
            {
                "text": "How would you use Python?",
                "type": "technical",
                "requirement_ids": ["r1"],
                "company_specific": False,
            },
            {
                "text": "How would you use Python?",
                "type": "technical",
                "requirement_ids": ["unknown"],
                "company_specific": False,
            },
            {
                "text": "Tell me about your CV.",
                "type": "cv",
                "requirement_ids": ["r1"],
                "company_specific": False,
            },
        ],
        "ask_them": [],
    }
    result = bank.resolve_interview_questions(
        data, [Requirement("r1", "key", "Python")], job_text="Python development", cv_text=""
    )
    assert [q.text for q in result] == ["How would you use Python?"]


def test_ac_m8_3_web_items_need_a_searched_source():
    bank = api()
    data = {
        "questions": [
            {
                "text": "Describe teamwork.",
                "type": "behavioral",
                "source_url": "https://example.test/interviews",
                "source_date": "2025",
            },
            {
                "text": "Another question.",
                "type": "behavioral",
                "source_url": "https://other.test/unknown",
                "source_date": None,
            },
            {
                "text": "Unsafe question.",
                "type": "behavioral",
                "source_url": "javascript:alert(1)",
                "source_date": None,
            },
        ]
    }
    result = bank.resolve_web_items(
        data, ("https://example.test/interviews", "javascript:alert(1)")
    )
    assert len(result) == 1 and result[0].source_date == "2025"
    assert result[0].source_url == "https://example.test/interviews"
