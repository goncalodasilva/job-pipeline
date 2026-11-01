"""Pure answer feedback (M8.2 part A; spec 3.1 items 5 and 6; ADR-124 items 7 and 8).

The model only writes words about the answer. Everything a person could be misled by is
decided in code: the speaking length, the consistency with the CV sent, which fact ids may be
pointed at and which claims are not in the fact bank.

Assumed interface (chosen here where the sources left it open; the implementer matches it).
``app/domain/answer_feedback.py`` (pure Python, no I/O):

- ``WORDS_PER_MINUTE == 130``; ``speaking_seconds(answer) -> int``: the whitespace-separated
  words at that pace, rounded to the nearest second (130 words are 60 seconds).
- ``length_verdict(seconds) -> str``: ``"too_short"`` under 30 seconds, ``"good"`` from 30 to 120
  seconds inclusive, ``"too_long"`` over 120.
- ``resolve_feedback(data, answer, sent_facts, *, cv_text, known_skills=()) -> Feedback``.
  ``data`` is the model's output as the schema describes it (``checklist``, ``suggestions``,
  ``suggested_wording``, ``unsupported_claims``); ``sent_facts`` are the ``AIFact`` of the
  request (``facts_for_ai``); ``cv_text`` is the CV text sent ("" when there is none).
- ``Feedback`` (frozen):

  - ``checklist``: ``Mapping[str, CheckItem]`` in the order ``answers``, ``structure``,
    ``evidence``, ``length``, ``consistent_with_cv_sent``; ``CheckItem(verdict, comment)``.
    The model's verdicts (``yes``, ``partly``, ``no``) are kept when valid and become
    ``unclear`` otherwise. ``length`` and ``consistent_with_cv_sent`` are code-owned: the
    model has no say in them, and anything it sends under those names is ignored.
    ``consistent_with_cv_sent`` is ``yes`` when ``new_claims(answer, [cv_text])`` is empty,
    ``no`` when it is not, and ``not_checked`` when no CV was sent.
  - ``seconds``: ``speaking_seconds(answer)``.
  - ``suggestions``: at most three ``Suggestion(text, fact_ids)``; only ids of facts that were
    sent (exactly as ``F<number>@<version>``) survive, each once.
  - ``fact_refs``: the surviving ids of all suggestions, each once, in order.
  - ``wording``: ``SuggestedWording(text, claims)``; ``claims`` are ``new_claims`` of the
    wording against the sent facts, plus the model's ``added_details``; ``flagged`` is true
    when there are any.
  - ``unsupported``: ``tuple[Claim, ...]``: ``new_claims(answer, the sent facts' text)``
    first, then the model's ``unsupported_claims`` that appear in the answer (compared without
    regard to case) as ``Claim("added_detail", text)``, each claim once.
  - ``not_in_cv``: ``new_claims(answer, [cv_text])`` (empty when no CV was sent).

- ``sentence_of(answer, claim) -> str``: the sentence of the answer that holds the claim's text
  (without regard to case), stripped.

Everything is fictional.
"""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.domain.fact_selection import facts_for_ai
from app.domain.facts import FactStatus, Privacy
from app.domain.honesty import Claim
from tests.tailoring_ai_inputs import SENT, record

AI_FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "ai" / "answer_feedback"
CV_TEXT = "Planned delivery routes for a harbor depot. Cut invoice processing time by 35%."
GROUNDED = "I planned 60 delivery routes a week with a team of 3."


def feedback_module() -> Any:
    import importlib

    return importlib.import_module("app.domain.answer_feedback")


def fixture(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((AI_FIXTURES / f"{name}.json").read_text())["data"]
    return loaded


def resolve(
    data: dict[str, Any],
    answer: str = GROUNDED,
    *,
    cv_text: str = CV_TEXT,
    known_skills: tuple[str, ...] = (),
    sent: Any = SENT,
) -> Any:
    return feedback_module().resolve_feedback(
        data, answer, sent, cv_text=cv_text, known_skills=known_skills
    )


def words(count: int) -> str:
    return " ".join(["word"] * count)


# --- speaking time and the length verdict -----------------------------------------------------


def test_speaking_time_is_130_words_a_minute():
    module = feedback_module()
    assert module.WORDS_PER_MINUTE == 130
    assert module.speaking_seconds("") == 0
    assert module.speaking_seconds("   \n ") == 0
    assert module.speaking_seconds(words(130)) == 60
    assert module.speaking_seconds(words(65)) == 30
    assert module.speaking_seconds(words(260)) == 120


@pytest.mark.parametrize(
    ("seconds", "verdict"),
    [
        (0, "too_short"),
        (29, "too_short"),
        (30, "good"),
        (75, "good"),
        (120, "good"),
        (121, "too_long"),
        (600, "too_long"),
    ],
)
def test_length_verdict_boundaries(seconds, verdict):
    assert feedback_module().length_verdict(seconds) == verdict


@given(st.integers(0, 3000), st.integers(0, 3000))
def test_speaking_time_is_monotonic_in_word_count(first, second):
    module = feedback_module()
    low, high = sorted((first, second))
    assert module.speaking_seconds(words(low)) <= module.speaking_seconds(words(high))
    # And the verdict order follows the time: never shorter text with a "longer" verdict.
    order = ["too_short", "good", "too_long"]
    assert order.index(module.length_verdict(module.speaking_seconds(words(low)))) <= order.index(
        module.length_verdict(module.speaking_seconds(words(high)))
    )


@given(st.lists(st.text(alphabet="abcdef-'", min_size=1, max_size=8), max_size=60))
def test_speaking_time_ignores_spacing(tokens):
    module = feedback_module()
    plain = " ".join(tokens)
    spaced = "  \n".join(tokens)
    assert module.speaking_seconds(plain) == module.speaking_seconds(f"  {spaced}\n ")


# --- the checklist is resolved in code --------------------------------------------------------


def _model_verdict(key: str) -> None:
    for given_verdict, kept in (("yes", "yes"), ("partly", "partly"), ("no", "no")):
        data = fixture("ok")
        data["checklist"][key]["verdict"] = given_verdict
        result = resolve(data)
        assert result.checklist[key].verdict == kept
        assert result.checklist[key].comment == data["checklist"][key]["comment"]
    # Output that got past the schema anyway is not trusted: an unknown verdict is "unclear".
    for bad in ("perfect", "YES", "", None, 5, ["yes"]):
        data = fixture("ok")
        data["checklist"][key]["verdict"] = bad
        assert resolve(data).checklist[key].verdict == "unclear", bad


def _length_from_word_count() -> None:
    module = feedback_module()
    for count, expected in ((5, "too_short"), (130, "good"), (260, "good"), (400, "too_long")):
        answer = words(count)
        data = fixture("ok")
        # A forged length from the model changes nothing.
        data["checklist"]["length"] = {"verdict": "too_long" if count < 100 else "too_short"}
        data["length"] = {"verdict": "good"}
        result = resolve(data, answer)
        assert result.seconds == module.speaking_seconds(answer)
        assert result.checklist["length"].verdict == expected, count
        assert result.checklist["length"].comment, "the comment is written by code"


def _consistent_with_cv_sent() -> None:
    clean = resolve(fixture("ok"), "I planned delivery routes for a harbor depot.")
    assert clean.checklist["consistent_with_cv_sent"].verdict == "yes"
    assert clean.not_in_cv == ()
    # 60 routes are in the fact bank but not in the CV that was sent: two different lists.
    data = fixture("ok")
    data["consistent_with_cv_sent"] = {"verdict": "yes", "comment": "forged"}
    data["checklist"]["consistent_with_cv_sent"] = {"verdict": "yes", "comment": "forged"}
    differs = resolve(data, GROUNDED)
    assert differs.checklist["consistent_with_cv_sent"].verdict == "no"
    assert {c.text for c in differs.not_in_cv} >= {"60"}
    assert differs.unsupported == (), "the fact bank states 60 routes and a team of 3"
    assert differs.checklist["consistent_with_cv_sent"].comment != "forged"
    # No CV sent: nothing can be compared, and nothing is claimed.
    nothing = resolve(fixture("ok"), GROUNDED, cv_text="")
    assert nothing.checklist["consistent_with_cv_sent"].verdict == "not_checked"
    assert nothing.not_in_cv == ()


CHECKLIST_CASES: dict[str, Callable[[], None]] = {
    "answers": lambda: _model_verdict("answers"),
    "structure": lambda: _model_verdict("structure"),
    "evidence": lambda: _model_verdict("evidence"),
    "length_from_word_count": _length_from_word_count,
    "consistent_with_cv_sent": _consistent_with_cv_sent,
}


@pytest.mark.parametrize("case", list(CHECKLIST_CASES))
def test_feedback_checklist_resolved_in_code(case):
    CHECKLIST_CASES[case]()


def test_checklist_lists_the_five_checks_in_order():
    result = resolve(fixture("ok"))
    assert list(result.checklist) == [
        "answers",
        "structure",
        "evidence",
        "length",
        "consistent_with_cv_sent",
    ]


# --- fact pointers ----------------------------------------------------------------------------


def test_feedback_fact_pointers_only_sent_ids():
    # F1@1 was sent; F4@1 (never-use, not sent) and F99@1 (invented) were not.
    result = resolve(fixture("adversarial_claims"))
    assert [s.fact_ids for s in result.suggestions] == [("F1@1",)]
    assert result.fact_refs == ("F1@1",)

    data = fixture("ok")
    data["suggestions"] = [
        {"text": "One.", "fact_ids": ["F1@2", "F1", "f1@1", "F1@1", "F1@1", "F2@1"]},
        {"text": "Two.", "fact_ids": ["F2@1", "F3@1"]},
        {"text": "Three.", "fact_ids": []},
        {"text": "Four.", "fact_ids": ["F1@1"]},
    ]
    result = resolve(data)
    assert len(result.suggestions) == 3, "at most three suggestions are kept"
    assert result.suggestions[0].fact_ids == ("F1@1", "F2@1"), "wrong version, bad shape, repeats"
    assert result.suggestions[1].fact_ids == ("F2@1", "F3@1")
    assert result.suggestions[2].fact_ids == ()
    assert result.fact_refs == ("F1@1", "F2@1", "F3@1")


def test_feedback_pointers_never_name_facts_that_were_not_sent():
    # Nothing was sent at all (no confirmed facts): nothing can be pointed at.
    result = resolve(fixture("ok"), sent=())
    assert result.fact_refs == ()
    assert all(s.fact_ids == () for s in result.suggestions)


# --- claims that are not in the fact bank ----------------------------------------------------


def test_clean_answer_has_no_unsupported_claims():
    result = resolve(fixture("ok"), GROUNDED)
    assert result.unsupported == ()
    assert result.wording.claims == ()
    assert result.wording.flagged is False


@pytest.mark.parametrize(
    ("answer", "skills", "kind", "text"),
    [
        ("I planned 75 delivery routes a week with a team of 3.", (), "number", "75"),
        (
            "I planned delivery routes for Globex Corporation, with a team of 3.",
            (),
            "name",
            "Globex Corporation",
        ),
        (
            "I planned the routes using Terraform with a team of 3.",
            ("Terraform",),
            "skill",
            "Terraform",
        ),
    ],
    ids=["number", "employer", "skill"],
)
def test_code_flags_what_the_sent_facts_do_not_state(answer, skills, kind, text):
    result = resolve(fixture("ok"), answer, known_skills=skills)
    assert Claim(kind, text) in result.unsupported
    # Without the controlled skill list, a lower-case skill word is not a claim.
    if kind == "skill":
        assert Claim(kind, text) not in resolve(fixture("ok"), answer).unsupported


def test_the_employer_of_a_sent_facts_role_is_not_a_new_claim():
    answer = "I planned routes at Example Widgets Ltd with a team of 3."
    assert resolve(fixture("ok"), answer).unsupported == ()


def test_a_generalized_fact_never_supports_its_names():
    sent = facts_for_ai(
        [record(8, "Ran the Fernhill Bank payments migration.", privacy=Privacy.GENERALIZE)]
    )
    answer = "I ran the Fernhill Bank payments migration."
    data = fixture("ok")
    data["suggestions"] = []
    result = resolve(data, answer, sent=sent)
    assert Claim("name", "Fernhill Bank") in result.unsupported


def test_a_draft_or_never_use_fact_does_not_support_a_claim():
    # Only what was sent counts, and facts_for_ai sent neither of these.
    sent = facts_for_ai(
        [
            record(
                5,
                "Managed 17 stores.",
                status=FactStatus.DRAFT,
            ),
            record(4, "Managed 18 stores.", privacy=Privacy.NEVER_USE),
        ]
    )
    result = resolve(fixture("ok"), "I managed 17 stores and 18 shops.", sent=sent)
    assert {c.text for c in result.unsupported} >= {"17", "18"}


def test_model_reported_claims_are_kept_only_when_the_answer_says_them():
    answer = "I planned delivery routes and saved the team a week every month."
    result = resolve(fixture("adversarial_claims"), answer)
    # "Fernhill Bank" is not in the answer, so it is not a claim about the answer.
    assert [c.text for c in result.unsupported] == ["saved the team a week every month"]
    assert result.unsupported[0].kind == "added_detail"
    # Compared without regard to case.
    data = fixture("ok")
    data["unsupported_claims"] = ["SAVED THE TEAM a week"]
    assert [c.text for c in resolve(data, answer).unsupported] == ["SAVED THE TEAM a week"]


def test_a_claim_found_by_code_and_reported_by_the_model_is_listed_once():
    data = fixture("ok")
    data["unsupported_claims"] = ["75", "75 delivery routes"]
    answer = "I planned 75 delivery routes a week with a team of 3."
    texts = [c.text for c in resolve(data, answer).unsupported]
    assert texts.count("75") == 1
    assert texts[0] == "75", "code-found claims come first"


def test_feedback_suggested_wording_with_new_claims_flagged():
    result = resolve(fixture("adversarial_claims"))
    assert result.wording.flagged is True
    kinds = {(c.kind, c.text) for c in result.wording.claims}
    assert ("number", "12") in kinds
    assert ("name", "Fernhill Bank") in kinds
    assert ("added_detail", "trained 12 new colleagues") in kinds
    # The wording is kept as written so that it can be shown with its flags, never rewritten.
    assert result.wording.text.startswith("I planned 60 delivery routes a week")
    # The obeyed-injection output is flagged the same way.
    obeyed = resolve(fixture("injection_obeyed"), "I planned delivery routes for a depot.")
    assert obeyed.wording.flagged is True
    assert ("name", "Globex Corporation") in {(c.kind, c.text) for c in obeyed.wording.claims}


def test_wording_restating_the_facts_is_not_flagged():
    result = resolve(fixture("ok"))
    assert result.wording.text.startswith("I planned 60 delivery routes")
    assert result.wording.flagged is False


def test_obeyed_injection_changes_no_code_owned_part():
    answer = (AI_FIXTURES / "injection_answer.txt").read_text()
    result = resolve(fixture("injection_obeyed"), answer)
    # Every "yes" the model gave for the checks it owns is kept as words, the rest is code's.
    assert result.checklist["consistent_with_cv_sent"].verdict == "no"
    assert result.checklist["length"].verdict == "too_short"
    assert "Globex Corporation" in {c.text for c in result.unsupported}
    assert result.fact_refs == ()


# --- the sentence behind a claim --------------------------------------------------------------


def test_sentence_of_returns_the_sentence_holding_the_claim():
    module = feedback_module()
    answer = "I planned routes. Later I managed 17 stores across the region. It went well!"
    assert module.sentence_of(answer, Claim("number", "17")) == (
        "Later I managed 17 stores across the region."
    )
    assert module.sentence_of(answer, Claim("name", "IT WENT WELL")) == "It went well!"
    assert module.sentence_of(answer, Claim("added_detail", "I planned routes")) == (
        "I planned routes."
    )


def test_sentence_of_keeps_decimals_and_newlines_inside_their_sentences():
    module = feedback_module()
    answer = "Our score rose to 3.5 points\nafter the change. We were pleased."
    assert module.sentence_of(answer, Claim("number", "3.5")) == (
        "Our score rose to 3.5 points\nafter the change."
    )


# --- review fixes: suggestions, reported claims, caps, wording of times --------------------------


def _obeyed_suggestion_data(fact_ids: list[str]) -> dict[str, Any]:
    data = fixture("injection_obeyed")
    data["suggestions"][0]["fact_ids"] = fact_ids
    return data


def test_a_suggestion_that_says_things_the_facts_do_not_is_flagged():
    data = _obeyed_suggestion_data([])
    result = resolve(data, "I planned delivery routes for a depot.")
    (suggestion,) = result.suggestions
    assert suggestion.flagged is True
    assert Claim("name", "Globex Corporation") in suggestion.claims
    # The answer's own words are not support: saying it there does not make it stated by a fact.
    said = resolve(data, "I was Chief Executive at Globex Corporation.")
    assert Claim("name", "Globex Corporation") in said.suggestions[0].claims


def test_a_suggestion_that_restates_the_facts_is_not_flagged_and_keeps_its_flags_when_stored():
    clean = resolve(fixture("ok"))
    assert [s.flagged for s in clean.suggestions] == [False, False]
    module = feedback_module()
    flagged = resolve(_obeyed_suggestion_data([]), "I planned delivery routes for a depot.")
    again = module.feedback_from_data(flagged.to_data())
    assert again.suggestions[0].claims == flagged.suggestions[0].claims
    assert again == flagged


def test_a_suggestion_citing_only_facts_that_were_not_sent_is_dropped():
    data = fixture("ok")
    data["suggestions"] = [
        {"text": "Invented.", "fact_ids": ["F99@1"]},
        {"text": "Mixed.", "fact_ids": ["F99@1", "F1@1"]},
        {"text": "Uncited.", "fact_ids": []},
    ]
    result = resolve(data)
    assert [(s.text, s.fact_ids) for s in result.suggestions] == [
        ("Mixed.", ("F1@1",)),
        ("Uncited.", ()),
    ]
    assert resolve(fixture("injection_obeyed")).suggestions == ()


def test_model_reported_claims_must_be_whole_words_of_the_answer_and_not_stated_by_a_fact():
    answer = "I planned 60 delivery routes together, saved a week every month."
    data = fixture("ok")
    data["unsupported_claims"] = [
        "I",  # too short to mean anything
        "gether",  # inside a word, not a whole word
        "the",  # inside "together"
        "delivery routes",  # a sent fact states it
        "saved a week every month",  # a real addition
    ]
    result = resolve(data, answer)
    assert [c.text for c in result.unsupported] == ["saved a week every month"]
    # A short but whole word of two characters is still allowed to be a claim.
    data["unsupported_claims"] = ["ex"]
    assert [
        c.text for c in resolve(data, "I was an ex lead of 60 delivery routes.").unsupported
    ] == ["ex"]


def test_flagged_claims_are_capped_with_a_count_of_the_rest_and_the_stored_form_stays_small():
    module = feedback_module()
    answer = " ".join(str(1000 + i) for i in range(700))
    result = resolve(fixture("ok"), answer)
    assert len(result.unsupported) == module.MAX_FLAGGED == 20
    assert len(result.not_in_cv) == 20
    assert result.unsupported_more > 600 and result.not_in_cv_more > 600
    # The consistency check is decided on everything found, not on what is shown.
    assert result.checklist["consistent_with_cv_sent"].verdict == "no"
    stored = json.dumps(result.to_data(), ensure_ascii=False)
    assert len(stored) < module.MAX_FEEDBACK_CHARS < 50_000
    assert module.feedback_from_data(json.loads(stored)) == result


def test_every_list_in_the_stored_feedback_is_bounded_together_under_the_database_limit():
    module = feedback_module()
    figures = " ".join(str(1000 + i) for i in range(700))
    data = fixture("ok")
    data["suggestions"] = [{"text": f"Say {figures[:380]}", "fact_ids": []}] * 3
    data["suggested_wording"]["text"] = figures[:3900]
    data["unsupported_claims"] = [f"{n:03d} {'x' * 280}" for n in range(20)]
    answer = figures + " " + " ".join(data["unsupported_claims"])
    result = resolve(data, answer[:4000])
    assert len(json.dumps(result.to_data(), ensure_ascii=False)) < module.MAX_FEEDBACK_CHARS


@pytest.mark.parametrize(
    ("words_count", "spoken"),
    [
        (130, "1 minute"),
        (325, "2 minutes 30 seconds"),
        (390, "3 minutes"),
        (195, "1 minute 30 seconds"),
        (65, "30 seconds"),
        (1, "0 seconds"),
    ],
)
def test_speaking_time_over_a_minute_is_written_in_words(words_count, spoken):
    result = resolve(fixture("ok"), words(words_count))
    assert result.spoken == spoken
    assert spoken in result.checklist["length"].comment
    assert " min " not in result.checklist["length"].comment


def test_cv_snapshot_lines_the_current_cv_no_longer_holds_are_dropped():
    module = feedback_module()
    snapshot = "Kept line one.\nA line since marked never use.\n\nKept line two."
    current = "Kept line two.\nKept line one.\nA line added later."

    assert module.current_cv_lines(snapshot, current) == "Kept line one.\n\nKept line two."
    assert module.current_cv_lines(snapshot, snapshot) == snapshot
    assert module.current_cv_lines(snapshot, "") == "", "nothing of it is left to send"
    assert module.current_cv_lines("", current) == ""
    assert module.current_cv_lines("Kept line one.  \r\nGone line.", "  Kept line one.") == (
        "Kept line one.  "
    ), "only whole lines count, and their edges are not compared"
