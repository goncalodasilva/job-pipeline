"""The mechanical checks of a CV import against the CV's text (M3.4; AC-M3.1; ADR-089 item 5):
``app/domain/cv_checks.py``. Pure; every value is fictional."""

import pytest

from app.domain.cv_checks import (
    CvText,
    has_duplicates,
    normalise,
    one_space,
    real_partial_date,
    says_no_more_than,
    unescape_lt,
    word_tokens,
)

CV = (
    "Example Widgets Ltd - Operations Analyst (2021 - 2024)\n"
    "- Cut invoice processing time by 35%\n   across 4 warehouses.\n"
    "- Handled 1,200 orders and a 3.5 day backlog.\n"
)


# The cases of ``number_tokens`` (the M3.4 number helper) moved to
# ``tests/unit/domain/test_honesty.py::test_extract_quantities_kinds_and_values`` when
# ``app/domain/honesty.py`` replaced it (M3.8; ADR-089 item 5, ADR-092 item 4).


def test_one_space_folds_whitespace_runs() -> None:
    assert one_space("  a \n\t b  ") == "a b"


def test_quote_found_whatever_the_whitespace() -> None:
    cv = CvText(CV)

    assert cv.states("Cut invoice processing time by 35% across 4 warehouses.")
    assert cv.states("by 35%\nacross   4")
    assert not cv.states("Cut invoice processing time by 35% across 5 warehouses.")
    assert not cv.states("cut invoice processing time"), "compared as written"
    assert not cv.states("   "), "a blank quote is not in the CV"


def test_number_not_in_cv_is_noticed_and_a_digit_is_not_a_number() -> None:
    cv = CvText(CV)

    assert cv.has_numbers_of(["Cut time by 35%.", None, "4 warehouses", ""])
    assert cv.has_numbers_of(["Handled 1,200 orders."])
    assert not cv.has_numbers_of(["Cut time by 3%."]), "3 is not 35 or 3.5"
    assert not cv.has_numbers_of(["Handled 1200 orders."]), "formats are compared in M3.8"
    assert not cv.has_numbers_of(["fine", "Saved 9 hours."])
    assert cv.has_numbers_of([None, None])


def test_number_words_are_left_to_the_word_check() -> None:
    """The refine check reads numbers written in words (M3.8 review); an import still
    compares only numbers written in digits, and words through ``has_words_of``."""
    cv = CvText("Handled twenty one orders, doubled throughput and cut costs by half.")

    assert cv.has_numbers_of(["Handled twenty-one orders."])
    assert cv.has_numbers_of(["Cut costs by fifty percent."])
    assert cv.has_numbers_of(["Tripled throughput."])
    assert not cv.has_numbers_of(["Handled 21 orders."])


def test_names_must_be_in_the_cv_and_blank_ones_are_skipped() -> None:
    cv = CvText(CV)

    assert cv.names("Example Widgets Ltd", "Operations Analyst")
    assert cv.names("Example Widgets Ltd", "")
    assert cv.names()
    assert not cv.names("Imaginary Aerospace Corp", "Operations Analyst")
    assert not cv.names("Example Widgets Ltd", "Operations Lead")


def test_has_duplicates() -> None:
    assert has_duplicates(["r1", "r2", "r1"])
    assert not has_duplicates(["r1", "r2"])
    assert not has_duplicates([])


@pytest.mark.parametrize(
    ("given", "kept"),
    [
        ("2021", "2021"),
        ("2021-02", "2021-02"),
        ("2024-02-29", "2024-02-29"),
        ("2021-02-31", None),
        ("2021-13", None),
        ("", None),
        (None, None),
    ],
)
def test_real_partial_date_drops_what_the_calendar_lacks(
    given: str | None, kept: str | None
) -> None:
    assert real_partial_date(given) == kept


# --- review fixes: one normaliser, the unit's own quote, skills and years ----------------------

SOFT_HYPHEN, ZERO_WIDTH, LIGATURE_FI = chr(0x00AD), chr(0x200B), chr(0xFB01)
EN_DASH, EM_DASH, CURLY_APOSTROPHE = chr(0x2013), chr(0x2014), chr(0x2019)
LEFT_QUOTE, RIGHT_QUOTE = chr(0x201C), chr(0x201D)


@pytest.mark.parametrize(
    ("given", "normal"),
    [
        (f"ware{SOFT_HYPHEN}houses", "warehouses"),
        (f"ware{ZERO_WIDTH}houses", "warehouses"),
        (f"{LIGATURE_FI}nance", "finance"),
        (f"the team{CURLY_APOSTROPHE}s {LEFT_QUOTE}plan{RIGHT_QUOTE}", 'the team\'s "plan"'),
        (f"2021 {EN_DASH} 2024 {EM_DASH} now", "2021 - 2024 - now"),
        ("stock &lt; 5 days", "stock < 5 days"),
        ("  a \n\t b  ", "a b"),
        ("Keeps Its Case", "Keeps Its Case"),
        ("", ""),
    ],
)
def test_normalise_gives_the_same_text_for_both_sides(given: str, normal: str) -> None:
    assert normalise(given) == normal


def test_unescape_lt_only_touches_the_escaped_sign() -> None:
    assert unescape_lt("stock &lt; 5 days &amp; more") == "stock < 5 days &amp; more"
    assert unescape_lt("nothing to do") == "nothing to do"


@pytest.mark.parametrize(
    "cv_line",
    [
        f"Cut invoice processing time by 35% across 4 ware{SOFT_HYPHEN}houses.",
        f"Cut invoice processing time by 35% across 4 ware{ZERO_WIDTH}houses.",
    ],
)
def test_quote_found_in_a_cv_with_invisible_characters(cv_line: str) -> None:
    cv = CvText(f"Experience\n- {cv_line}\n")

    assert cv.states("Cut invoice processing time by 35% across 4 warehouses.")


def test_quote_found_across_ligatures_dashes_curly_quotes_and_the_escaped_sign() -> None:
    cv = CvText(
        f"- Ran the {LIGATURE_FI}nance desk{CURLY_APOSTROPHE}s close (2021 {EN_DASH} 2024)\n"
        "- Kept stock < 5 days\n"
    )

    assert cv.states("Ran the finance desk's close (2021 - 2024)")
    assert cv.states("Kept stock &lt; 5 days"), "the CV is shown to the model with &lt;"
    assert cv.states("Kept stock < 5 days")
    assert not cv.states("ran the finance desk's close"), "still compared as written"


def test_word_tokens_are_casefolded_runs_of_letters_and_digits() -> None:
    assert word_tokens("Managed 5k people, Évora's team-lead") == {
        "managed",
        "5k",
        "people",
        "évora",
        "s",
        "team",
        "lead",
    }
    assert word_tokens(f"{LIGATURE_FI}nance &lt; 5") == {"finance", "5"}
    assert word_tokens("") == set()


QUOTE = "Cut invoice processing time by 35% across 4 warehouses."


@pytest.mark.parametrize(
    "parts",
    [
        [QUOTE, None, None, None],
        ["cut invoice processing time", None, None, None],
        [None, None, "Cut invoice processing time", "by 35% across 4 warehouses."],
        [f"Cut invoice processing time by 35% across 4 ware{SOFT_HYPHEN}houses.", None],
    ],
)
def test_wording_taken_from_its_own_quote_is_faithful(parts: list[str | None]) -> None:
    assert says_no_more_than(QUOTE, parts)


@pytest.mark.parametrize(
    ("quote", "parts", "why"),
    [
        # Numbers of another bullet: the CV states 60 and 3, this quote doesn't.
        (QUOTE, ["Cut invoice processing time by 60% across 3 warehouses."], "moved numbers"),
        (QUOTE, [QUOTE.replace(".", " with SAP.")], "an added tool"),
        (QUOTE, [QUOTE.replace(".", " at Imaginary Aerospace Corp.")], "an added employer"),
        (QUOTE, [None, None, "Cut invoice processing time", "Saved the quarter."], "a result"),
        ("Managed 5 people", ["Managed 5k people"], "5k is not 5"),
        ("Managed 5 people", ["Managed 5 million people"], "an added million"),
        ("Handled 1,200 orders.", ["Handled 1200 orders."], "formats are compared in M3.8"),
        ("Handled a 3.5 day backlog.", ["Handled a 3 day backlog."], "3 is not 3.5"),
        ("English", ["Fluent English, used daily to lead a team of 50 engineers."], "one word"),
    ],
)
def test_wording_that_says_more_than_its_quote_is_noticed(
    quote: str, parts: list[str | None], why: str
) -> None:
    assert not says_no_more_than(quote, parts), why


def test_skill_must_be_named_in_the_cv_whatever_its_case() -> None:
    cv = CvText("Skills: SQL, Excel, C++, Google Sheets, .NET and stock forecasting\n")

    for named in ("SQL", "sql", "Excel", "C++", "Google Sheets", ".NET", "Stock  Forecasting"):
        assert cv.mentions(named), named
    for invented in ("Kubernetes", "Go", "Sheet", "R", "SQL Server", " ", ""):
        assert not cv.mentions(invented), invented


def test_year_must_be_written_in_the_cv() -> None:
    cv = CvText("Example Widgets Ltd (03.2021 - 2024)\nHandled 1,999 orders.\n")

    assert cv.has_year("2021") and cv.has_year("2021-03") and cv.has_year("2024-02-29")
    assert not cv.has_year("1999"), "1,999 orders is not the year 1999"
    assert not cv.has_year("2022")
