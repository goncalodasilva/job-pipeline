"""M6.5 gap check resolution: ``app/domain/gap_check.py`` (AC-M6.8; spec 3.1 item 5; ADR-106
item 11). Pure; every value is fictional.

A verdict of met or partial counts only when it cites at least one fact and every cited id was
sent in that request (the fit rules of ``app/domain/fit.py``). Missing must-haves, the
"may not be worth applying for" sentence and the "This isn't in your fact bank." note are all
built by code.
"""

from typing import Any

import pytest

from app.domain.fact_selection import AIFact, facts_for_ai
from app.domain.facts import Privacy
from app.domain.gap_check import (
    NOT_IN_FACT_BANK,
    GapCheckResult,
    resolve_gap_check,
)
from tests.tailoring_ai_inputs import RECORDS, REQUIREMENTS, SENT, record


def output(requirements: list[dict[str, Any]], ideas: list[dict[str, Any]] | None = None) -> Any:
    return {"requirements": requirements, "framing_ideas": ideas or []}


def entry(requirement_id: str, verdict: str, *fact_ids: str) -> dict[str, Any]:
    return {"requirement_id": requirement_id, "verdict": verdict, "fact_ids": list(fact_ids)}


def resolve(data: Any, sent: tuple[AIFact, ...] = SENT) -> GapCheckResult:
    return resolve_gap_check(data, REQUIREMENTS, sent)


def verdicts(result: GapCheckResult) -> dict[str, str]:
    return {item.requirement_id: item.verdict for item in result.verdicts}


def test_verdicts_follow_the_requirements_in_order_with_their_text() -> None:
    result = resolve(
        output(
            [
                entry("req-3", "partial", "F3@1"),
                entry("req-1", "met", "F1@1", "F1@1"),
                entry("req-2", "missing"),
                entry("req-4", "missing"),
            ]
        )
    )

    assert [item.requirement_id for item in result.verdicts] == ["req-1", "req-2", "req-3", "req-4"]
    first = result.verdicts[0]
    assert (first.kind, first.text) == ("must_have", "Five years of route planning")
    assert (first.verdict, first.fact_ids, first.downgraded) == ("met", ("F1@1",), False)
    assert verdicts(result) == {
        "req-1": "met",
        "req-2": "missing",
        "req-3": "partial",
        "req-4": "missing",
    }
    assert result.verdicts[2].fact_ids == ("F3@1",)
    assert result.verdicts[1].fact_ids == ()


def test_gap_check_counts_unsent_citation_as_missing() -> None:
    result = resolve(
        output(
            [
                entry("req-1", "met", "F999@1"),
                entry("req-2", "partial", "F1@1", "F999@1"),
                entry("req-3", "met", "F3@2"),  # a version that was not sent
                entry("req-4", "met", "F4@1"),  # a never-use fact: never sent
            ]
        )
    )

    assert verdicts(result) == dict.fromkeys(("req-1", "req-2", "req-3", "req-4"), "missing")
    assert all(item.fact_ids == () and item.downgraded for item in result.verdicts)
    assert [item.requirement_id for item in result.missing_must_haves] == ["req-1", "req-2"]


@pytest.mark.parametrize("verdict", ["met", "partial"])
def test_gap_check_counts_an_uncited_verdict_as_missing(verdict: str) -> None:
    result = resolve(output([entry("req-1", verdict)]))

    assert result.verdicts[0].verdict == "missing"
    assert result.verdicts[0].downgraded


def test_missing_that_cites_facts_stays_missing_with_no_ids() -> None:
    result = resolve(output([entry("req-1", "missing", "F1@1")]))

    assert result.verdicts[0].verdict == "missing"
    assert result.verdicts[0].fact_ids == ()
    assert not result.verdicts[0].downgraded


def test_a_requirement_the_model_skipped_repeated_or_unknown_is_handled_by_code() -> None:
    result = resolve(
        output(
            [
                entry("req-1", "met", "F1@1"),
                entry("req-1", "met", "F1@1"),  # twice: ambiguous, so missing
                entry("req-unknown", "met", "F1@1"),  # not a requirement of this job
                entry("req-3", "met", "F3@1"),
            ]
        )
    )

    assert verdicts(result) == {
        "req-1": "missing",
        "req-2": "missing",
        "req-3": "met",
        "req-4": "missing",
    }
    assert "req-unknown" not in verdicts(result)


@pytest.mark.parametrize(
    "garbage",
    [
        {},
        {"requirements": "met"},
        {"requirements": [None, 3, "x"]},
        {"requirements": [{"requirement_id": "req-1", "verdict": "met", "fact_ids": "F1@1"}]},
        {"requirements": [{"requirement_id": "req-1", "verdict": "strong", "fact_ids": ["F1@1"]}]},
        {"requirements": [{"requirement_id": "req-1", "verdict": "met", "fact_ids": [1]}]},
        {"requirements": [], "framing_ideas": {"text": "x"}},
        {"requirements": [], "framing_ideas": [None, {"text": 4}, {"fact_ids": ["F1@1"]}]},
    ],
)
def test_malformed_shapes_degrade_to_missing_and_never_raise(garbage: Any) -> None:
    result = resolve(garbage)

    assert verdicts(result) == dict.fromkeys(("req-1", "req-2", "req-3", "req-4"), "missing")
    assert result.framing_ideas == ()


def test_ac_m6_8_missing_must_haves_says_may_not_be_worth_applying() -> None:
    result = resolve(
        output(
            [
                entry("req-1", "missing"),
                entry("req-2", "missing"),
                entry("req-3", "met", "F3@1"),
                entry("req-4", "missing"),
            ]
        )
    )

    assert [item.requirement_id for item in result.missing_must_haves] == ["req-1", "req-2"]
    assert result.message == (
        "Your confirmed facts don't show 2 of this role's must-haves: "
        "Five years of route planning; Experience with Terraform. "
        "It may not be worth applying for."
    )


def test_message_counts_a_downgraded_must_have_and_ignores_key_and_nice_gaps() -> None:
    result = resolve(
        output(
            [
                entry("req-1", "met", "F1@1"),
                entry("req-2", "met", "F999@1"),
                entry("req-3", "missing"),
                entry("req-4", "missing"),
            ]
        )
    )

    assert result.message == (
        "Your confirmed facts don't show 1 of this role's must-haves: "
        "Experience with Terraform. It may not be worth applying for."
    )


def test_no_message_when_no_must_have_is_missing_and_partial_is_not_missing() -> None:
    result = resolve(
        output(
            [
                entry("req-1", "partial", "F1@1"),
                entry("req-2", "met", "F2@1"),
                entry("req-3", "missing"),
            ]
        )
    )

    assert result.missing_must_haves == ()
    assert result.message is None


def test_message_titles_are_one_line_and_short() -> None:
    from app.domain.fit import Requirement

    long_text = "Experience\nwith   " + "very " * 80 + "long requirements"
    requirements = (Requirement("r", "must_have", long_text),)

    result = resolve_gap_check(output([entry("r", "missing")]), requirements, SENT)

    assert result.message is not None
    assert "\n" not in result.message
    assert "  " not in result.message
    assert result.message.startswith(
        "Your confirmed facts don't show 1 of this role's must-haves: "
    )
    assert len(result.message) < 300
    assert result.message.endswith("It may not be worth applying for.")


def test_gap_check_framing_claim_flagged() -> None:
    ideas = [
        {"text": "Planned 60 delivery routes a week with a team of 3.", "fact_ids": ["F1@1"]},
        {
            "text": "Cut invoice processing time by 85% across 14 warehouses at Globex Co.",
            "fact_ids": ["F2@1"],
        },
    ]

    result = resolve(output([], ideas))

    faithful, invented = result.framing_ideas
    assert faithful.flags == ()
    assert faithful.note is None
    assert invented.note == NOT_IN_FACT_BANK == "This isn't in your fact bank."
    claims = invented.claims
    assert "Globex Co" in claims or "Globex Co." in claims
    assert any("85" in claim for claim in claims)
    assert any("14" in claim for claim in claims)
    assert invented.fact_ids == ("F2@1",)


def test_framing_idea_citing_an_unsent_fact_or_nothing_is_flagged() -> None:
    ideas = [
        {"text": "Worked on routing.", "fact_ids": ["F999@1"]},
        {"text": "Worked on routing.", "fact_ids": ["F4@1"]},  # never-use: not sent
        {"text": "Worked on routing.", "fact_ids": []},
    ]

    result = resolve(output([], ideas))

    assert [idea.note for idea in result.framing_ideas] == [NOT_IN_FACT_BANK] * 3
    assert result.framing_ideas[0].claims == ("F999@1",)
    assert result.framing_ideas[2].claims == ("Worked on routing.",)


def test_framing_claim_supported_only_by_a_generalized_fact_may_not_name_it() -> None:
    records = (
        record(
            1, "Planned routes for Contoso Freight with a team of 3.", privacy=Privacy.GENERALIZE
        ),
    )
    sent = facts_for_ai(records)
    ideas = [{"text": "Planned routes for Contoso Freight with a team of 3.", "fact_ids": ["F1@1"]}]

    result = resolve(output([], ideas), sent)

    assert result.framing_ideas[0].claims == ("Contoso Freight",)


def test_framing_skills_come_from_the_controlled_list_when_given() -> None:
    ideas = [{"text": "Used Kubernetes to plan routes.", "fact_ids": ["F1@1"]}]

    plain = resolve_gap_check(output([], ideas), REQUIREMENTS, SENT)
    listed = resolve_gap_check(output([], ideas), REQUIREMENTS, SENT, known_skills=["Kubernetes"])

    assert "Kubernetes" not in plain.framing_ideas[0].claims
    assert "Kubernetes" in listed.framing_ideas[0].claims


def test_result_is_plain_data_for_storage() -> None:
    result = resolve(
        output(
            [entry("req-1", "met", "F1@1"), entry("req-2", "missing")],
            [{"text": "Planned 60 delivery routes a week.", "fact_ids": ["F1@1"]}],
        )
    )

    data = result.to_data()

    assert data["requirements"][0] == {
        "requirement_id": "req-1",
        "kind": "must_have",
        "text": "Five years of route planning",
        "verdict": "met",
        "fact_ids": ["F1@1"],
        "downgraded": False,
    }
    assert data["missing_must_haves"] == ["req-2"]
    assert data["message"] == result.message
    assert data["framing_ideas"] == [
        {"text": "Planned 60 delivery routes a week.", "fact_ids": ["F1@1"], "claims": []}
    ]
    assert RECORDS  # the module-level fixtures are shared with the task tests
