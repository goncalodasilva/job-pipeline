"""M7.4 open-question guard; all source wording is fictional."""

import pytest

from app.domain.questions import question_problems


@pytest.mark.parametrize(
    ("question", "problem"),
    [
        ("Did you plan routes?", "yes_no"),
        ("Were you planning routes?", "yes_no"),
        ("Was that your project?", "yes_no"),
        ("Have you planned routes?", "yes_no"),
        ("Has that worked?", "yes_no"),
        ("Is this your work?", "yes_no"),
        ("Are you planning routes?", "yes_no"),
        ("Do you plan routes?", "yes_no"),
        ("Does this work?", "yes_no"),
        ("Can you plan routes?", "yes_no"),
        ("Could you plan routes?", "yes_no"),
        ("Would you plan routes?", "yes_no"),
        ("You planned routes, right?", "tag"),
        ("You planned routes, correct?", "tag"),
        ("You planned routes, didn't you?", "tag"),
        ("That was your work, isn't it?", "tag"),
        ("How did you save 90%?", "new_claims"),
        ("What did you do at Globex Corporation?", "new_claims"),
        ("How did you use Terraform?", "new_claims"),
        ("Describe your routes.", "form"),
        ("", "form"),
        ("What did you do?", None),
        ("Tell me about a time you planned routes.", None),
        ("How did you plan 12 routes?", None),
    ],
)
def test_question_problems(question, problem):
    found = question_problems(question, ("Planned 12 routes.",), ("Terraform",))
    assert (problem in found) if problem else not found
