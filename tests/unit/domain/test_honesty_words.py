"""The honesty check reads quantities written as words, keeps names after a semicolon or a
colon, and can take sources whose names are withheld (M3.8 review fixes; spec 3.1 item 5;
ADR-089 item 5). Pure; every value is fictional."""

from decimal import Decimal

import pytest

from app.domain.honesty import Claim, extract_quantities, new_claims

FACT = "Cut invoice processing time by 35% across 4 warehouses."


def values(text: str) -> set[tuple[str, Decimal]]:
    return {(q.kind, q.value) for q in extract_quantities(text)}


# --- names after ; and : -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "suggested",
    [
        "Cut invoice processing time by 35%; Globex praised it.",
        "Cut invoice processing time by 35% for one client: Globex hired the team.",
        "Cut invoice processing time by 35%:Globex hired the team.",
    ],
)
def test_a_name_after_a_semicolon_or_colon_is_flagged(suggested: str) -> None:
    found = new_claims(suggested, [FACT])

    assert Claim("name", "Globex") in found, found


def test_a_known_word_after_a_colon_is_not_a_name() -> None:
    assert new_claims("Invoice processing time: Cut by 35% across 4 warehouses.", [FACT]) == ()


# --- quantities written as words ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "kind", "value"),
    [
        ("across fourteen warehouses", "number", Decimal(14)),
        ("across Fourteen warehouses", "number", Decimal(14)),
        ("a team of zero", "number", Decimal(0)),
        ("by eighty-five percent", "percent", Decimal(85)),
        ("by eighty five per cent", "percent", Decimal(85)),
        ("by twenty-one %", "percent", Decimal(21)),
        ("twelve hundred invoices", "number", Decimal(1200)),
        ("one hundred and five routes", "number", Decimal(105)),
        ("two thousand three hundred orders", "number", Decimal(2300)),
        ("a hundred orders", "number", Decimal(100)),
        ("three million customers", "number", Decimal(3_000_000)),
        ("one billion records", "number", Decimal(1_000_000_000)),
        ("five thousand euros", "money", Decimal(5000)),
        ("Doubled throughput", "number", Decimal(2)),
        ("tripled the output", "number", Decimal(3)),
        ("quadrupled sales", "number", Decimal(4)),
        ("halved the backlog", "number", Decimal("0.5")),
        ("cut the backlog by half", "number", Decimal("0.5")),
        ("twice as fast", "number", Decimal(2)),
    ],
)
def test_number_words_are_quantities(text: str, kind: str, value: Decimal) -> None:
    assert values(text) == {(kind, value)}


def test_number_words_keep_their_text_and_order() -> None:
    found = extract_quantities("Cut time by eighty-five percent across 4 sites, twice a year.")

    assert [(q.kind, q.text) for q in found] == [
        ("percent", "eighty-five percent"),
        ("number", "4"),
        ("number", "twice"),
    ]


def test_separate_number_words_are_separate_quantities() -> None:
    assert [q.value for q in extract_quantities("one two")] == [Decimal(1), Decimal(2)]
    assert [q.value for q in extract_quantities("five four hundred")] == [
        Decimal(5),
        Decimal(400),
    ]
    assert [q.value for q in extract_quantities("twenty thirty")] == [Decimal(20), Decimal(30)]
    assert [q.value for q in extract_quantities("a thousand thousand")] == [
        Decimal(1000),
        Decimal(1000),
    ]


def test_and_or_a_multiple_word_never_joins_two_numbers() -> None:
    assert [q.value for q in extract_quantities("one and two")] == [Decimal(1), Decimal(2)]
    assert [q.text for q in extract_quantities("five doubled, twice tripled")] == [
        "five",
        "doubled",
        "twice",
        "tripled",
    ]


def test_number_words_inside_other_words_are_not_quantities() -> None:
    for text in ("someone", "often", "tension", "network", "behalf", "twenty-somethings"):
        assert extract_quantities(text) == (), text


def test_a_digit_quantity_with_a_scale_word_is_read_once() -> None:
    assert [(q.kind, q.value) for q in extract_quantities("Served 2 million customers")] == [
        ("number", Decimal(2_000_000))
    ]


@pytest.mark.parametrize(
    ("suggested", "source"),
    [
        ("Cut invoice time across 14 warehouses.", "Cut invoice time across fourteen warehouses."),
        ("Cut invoice time across fourteen warehouses.", "Cut invoice time across 14 warehouses."),
        ("Cut invoice time by eighty-five percent.", "Cut invoice time by 85%."),
        ("Cut invoice time by 85%.", "Cut invoice time by eighty five per cent."),
        ("Doubled throughput.", "Raised throughput 2x."),
        ("Halved the backlog.", "Cut the backlog by half."),
    ],
)
def test_word_and_digit_forms_support_each_other(suggested: str, source: str) -> None:
    assert new_claims(suggested, [source]) == ()


@pytest.mark.parametrize(
    ("suggested", "claim"),
    [
        ("Cut invoice time by 35% across fourteen warehouses.", Claim("number", "fourteen")),
        (
            "Cut invoice time by eighty-five percent across 4 warehouses.",
            Claim("percent", "eighty-five percent"),
        ),
        ("Doubled invoice throughput by 35% across 4 warehouses.", Claim("number", "Doubled")),
        ("Cut invoice time by 35% across 4 warehouses, twice.", Claim("number", "twice")),
        ("Cut invoice time by thirty-five across 4 warehouses.", Claim("number", "thirty-five")),
        (
            "Cut invoice time by 35% across four hundred warehouses.",
            Claim("number", "four hundred"),
        ),
    ],
)
def test_invented_number_words_are_flagged(suggested: str, claim: Claim) -> None:
    found = new_claims(suggested, [FACT])

    assert claim in found, found


def test_thirty_five_percent_in_words_is_the_facts_number() -> None:
    assert (
        new_claims("Cut invoice time by thirty-five percent across four warehouses.", [FACT]) == ()
    )


# --- sources whose names are withheld ----------------------------------------------------------

GENERALIZED = "Ran the Fernhill Bank payments migration for 1200 accounts in SQL."


def test_names_of_a_name_hidden_source_are_flagged() -> None:
    found = new_claims(f"{FACT[:-1]} for Fernhill Bank.", [FACT], name_hidden_sources=[GENERALIZED])

    assert found == (Claim("name", "Fernhill Bank"),)


def test_quantities_and_skills_of_a_name_hidden_source_are_allowed() -> None:
    suggested = f"{FACT[:-1]}, migrating 1,200 accounts in SQL for a bank."

    found = new_claims(
        suggested, [FACT], known_skills=["SQL"], name_hidden_sources=[GENERALIZED, None, ""]
    )

    assert found == ()
    without = new_claims(suggested, [FACT], known_skills=["SQL"])
    assert set(without) == {Claim("number", "1,200"), Claim("skill", "SQL")}


def test_a_name_also_given_by_an_ordinary_source_is_allowed() -> None:
    sources = [FACT, "It was for Fernhill Bank."]

    assert (
        new_claims(f"{FACT[:-1]} for Fernhill Bank.", sources, name_hidden_sources=[GENERALIZED])
        == ()
    )


# --- second review round: more multiples, dozens, plural scales, fractions, "one" -------------


@pytest.mark.parametrize(
    ("word", "value"),
    [
        ("twofold", 2),
        ("threefold", 3),
        ("fourfold", 4),
        ("fivefold", 5),
        ("sixfold", 6),
        ("sevenfold", 7),
        ("eightfold", 8),
        ("ninefold", 9),
        ("tenfold", 10),
    ],
)
def test_fold_words_are_multiples(word: str, value: int) -> None:
    assert values(f"a {word} rise in throughput") == {("number", Decimal(value))}


def test_a_fold_word_and_its_value_support_each_other() -> None:
    assert new_claims("Grew throughput threefold.", ["Tripled throughput."]) == ()
    assert new_claims("Grew throughput 3x.", ["Grew throughput threefold."]) == ()
    found = new_claims("Grew throughput tenfold.", ["Tripled throughput."])
    assert found == (Claim("number", "tenfold"),)


@pytest.mark.parametrize(
    ("text", "value"),
    [("a dozen sites", 12), ("Dozen sites", 12), ("two dozen sites", 24), ("twenty dozen", 240)],
)
def test_dozen_is_twelve(text: str, value: int) -> None:
    assert values(text) == {("number", Decimal(value))}


def test_a_dozen_and_twelve_support_each_other() -> None:
    assert new_claims("Opened a dozen sites.", ["Opened 12 sites."]) == ()
    assert new_claims("Opened 24 sites.", ["Opened two dozen sites."]) == ()
    assert new_claims("Opened 13 sites.", ["Opened a dozen sites."]) == (Claim("number", "13"),)


@pytest.mark.parametrize(
    ("form", "text"),
    [
        ("hundreds", "Handled hundreds of invoices."),
        ("thousands", "Handled thousands of invoices."),
        ("millions", "Handled millions of invoices."),
        ("billions", "Handled billions of records."),
        ("dozens", "Opened dozens of sites."),
        ("third", "Cleared a third of the backlog."),
        ("two thirds", "Cleared two thirds of the backlog."),
        ("two thirds", "Cleared two-thirds of the backlog."),
        ("quarter", "Cut costs by a quarter."),
        ("three quarters", "Cut costs by three quarters."),
        ("one third", "Cut costs by one third."),
    ],
)
def test_plural_scales_and_fractions_are_one_quantity_supported_only_by_their_form(
    form: str, text: str
) -> None:
    (quantity,) = extract_quantities(text)
    assert quantity.text.replace("-", " ").casefold() == form

    # Never a plain value: "two thirds" is not 2, "hundreds" is not 100.
    plain = "Handled 2 of 100 invoices, 1000 of 12, 1,000,000 or 1,000,000,000 records, 3 or 4."
    assert Claim("number", quantity.text) in new_claims(text, [plain])
    assert new_claims("Handled 2 invoices.", [text]) == (Claim("number", "2"),)
    # The same form supports it, however it is spaced or hyphenated.
    assert new_claims(text, [text.replace("-", " ")]) == ()


def test_two_fraction_forms_do_not_support_each_other() -> None:
    found = new_claims("Cleared two thirds of the backlog.", ["Cleared a third of the backlog."])

    assert found == (Claim("number", "two thirds"),)


@pytest.mark.parametrize(
    "suggested",
    [
        "No one missed a deadline.",
        "Became one of the leads.",
        "The analysts trained one another.",
        "Picked the one that worked.",
        "Cleared the invoices one at a time.",
        "Someone and anyone could read it.",
    ],
)
def test_one_as_a_pronoun_is_no_quantity(suggested: str) -> None:
    assert new_claims(suggested, ["Cleared the invoices."]) == ()


@pytest.mark.parametrize(
    "suggested", ["Opened one warehouse.", "Within one year, cleared the invoices."]
)
def test_one_as_a_number_is_still_a_quantity(suggested: str) -> None:
    assert new_claims(suggested, ["Cleared the invoices."]) == (Claim("number", "one"),)
