"""``check_units(name_sources=...)`` (M6.9; AC-M6.1 for letters; spec 3.1 item 3). A cover letter
passes the job's title and company as extra texts that may support a capitalised name, and
nothing else: no quantity, no skill, and a CV passes none. Pure; every value is fictional."""

from tests.unit.domain.test_honesty_units import OPS_FACT, check, fact, unit

COMPANY = "Harbor Kite Fictional"
TITLE = "Route Planner"
TEXT = "Cut invoice processing time by 35% across 4 warehouses for Harbor Kite Fictional."


def test_a_name_source_supports_the_name_it_contains() -> None:
    units = [unit("u1", TEXT, ("F1@1",))]

    assert check(units, [fact(1, OPS_FACT)], name_sources=[COMPANY, TITLE]) == ()


def test_without_name_sources_the_same_name_needs_a_check() -> None:
    found = check([unit("u1", TEXT, ("F1@1",))], [fact(1, OPS_FACT)])

    assert [(flag.kind, flag.claim) for flag in found] == [("needs_check", COMPANY)]


def test_a_name_source_never_supports_a_quantity_or_another_name() -> None:
    text = "Cut invoice processing time by 90% for Harbor Kite Fictional and Globex Corporation."

    found = check([unit("u1", text, ("F1@1",))], [fact(1, OPS_FACT)], name_sources=[COMPANY])

    assert {flag.claim for flag in found} == {"90%", "Globex Corporation"}


def test_a_name_source_never_supports_a_skill() -> None:
    text = "Cut invoice processing time with Python."

    found = check(
        [unit("u1", text, ("F1@1",))],
        [fact(1, OPS_FACT)],
        known_skills=["Python"],
        name_sources=["Python Planner Ltd"],
    )

    assert [(flag.kind, flag.claim) for flag in found] == [("needs_check", "Python")]


def test_a_name_source_supports_only_a_name_that_is_all_of_it() -> None:
    # A single word of the company, or of a longer title, is not the name of anything the
    # source states; only the whole string is.
    text = "Cut invoice processing time by 35% across 4 warehouses for Kite."

    found = check([unit("u1", text, ("F1@1",))], [fact(1, OPS_FACT)], name_sources=[COMPANY])
    assert [(flag.kind, flag.claim) for flag in found] == [("needs_check", "Kite")]

    longer = "Senior Route Planner Globex Account"
    text = "Cut invoice processing time by 35% across 4 warehouses as a Globex Account lead."
    found = check([unit("u1", text, ("F1@1",))], [fact(1, OPS_FACT)], name_sources=[longer])
    assert {flag.claim for flag in found} == {"Globex Account"}
    whole = (
        "Cut invoice processing time by 35% across 4 warehouses "
        "as Senior Route Planner Globex Account."
    )
    assert check([unit("u1", whole, ("F1@1",))], [fact(1, OPS_FACT)], name_sources=[longer]) == ()
