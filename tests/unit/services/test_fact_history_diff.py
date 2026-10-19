"""The history's diff runs (M3.6; TD-023): ``fact_history.diff_runs`` turns ``word_diff`` into
the runs the page marks, and ``diff_parts`` a version into its parts and its note. Spacing
alone is never a marked change; a changed run right after another is flagged, so the page
keeps them apart. Pure functions; every wording is fictional."""

from app.domain.facts import FactContent
from app.services.fact_history import (
    NOTE_SAME,
    NOTE_SPACING_ONLY,
    DiffRun,
    diff_parts,
    diff_runs,
)

ROUTES = "Planned 60 delivery routes a week."
MORE_ROUTES = "Planned 80 delivery routes a week."


def marked(runs: tuple[DiffRun, ...], op: str) -> list[str]:
    return [run.text for run in runs if run.op == op]


def reads(runs: tuple[DiffRun, ...]) -> str:
    """The version as it reads: every run but the removed ones."""
    return "".join(run.text for run in runs if run.op != "removed")


def test_unchanged_text_is_one_reading_with_nothing_marked() -> None:
    runs, differs = diff_runs(ROUTES, ROUTES)
    assert differs is False
    assert {run.op for run in runs} == {"equal"} and reads(runs) == ROUTES
    assert not any(run.gap for run in runs)


def test_changed_words_are_marked_and_the_rest_reads_whole() -> None:
    runs, differs = diff_runs(ROUTES, MORE_ROUTES)
    assert differs is True
    assert marked(runs, "removed") == ["60"] and marked(runs, "added") == ["80"]
    assert reads(runs) == MORE_ROUTES


def test_a_changed_run_right_after_another_is_flagged_as_a_gap() -> None:
    """ "60" replaced by "80": the two runs touch, so the second one says so and the page
    writes a space between them. No other run is flagged."""
    runs, _ = diff_runs(ROUTES, MORE_ROUTES)
    changed = [run for run in runs if run.op != "equal"]
    assert [run.gap for run in changed] == [False, True]
    assert not any(run.gap for run in runs if run.op == "equal")
    second = runs.index(changed[1])
    assert runs[second - 1] is changed[0], "the flagged run follows the other one directly"


def test_changed_runs_with_words_between_them_have_no_gap() -> None:
    runs, _ = diff_runs("Planned 60 routes a week.", "Planned 80 routes every week.")
    assert marked(runs, "added") == ["80", "every"]
    flagged = [run.text for run in runs if run.gap]
    assert set(flagged) <= {"80", "60", "every", "a"}
    # Each pair is flagged once: the run that comes second.
    assert len(flagged) == 2


def test_spacing_only_change_marks_nothing_but_differs() -> None:
    spaced = "Planned 60  delivery routes\na week."
    runs, differs = diff_runs(ROUTES, spaced)
    assert differs is True, "the texts are not the same"
    assert {run.op for run in runs} == {"equal"}, "spacing is never an added or removed run"
    assert reads(runs) == spaced, "added spacing reads as plain text"


def test_removed_spacing_is_dropped() -> None:
    spaced = "Planned 60  delivery routes\na week."
    runs, differs = diff_runs(spaced, ROUTES)
    assert differs is True
    assert {run.op for run in runs} == {"equal"}
    assert reads(runs) == ROUTES, "what the old text had more of is left out, not shown"
    assert all(run.text.strip() or run.op == "equal" for run in runs)


def test_spacing_between_two_changed_words_does_not_hide_the_gap() -> None:
    """Removed spacing between a removed and an added run is dropped, which leaves the two
    touching: the second is flagged."""
    runs, _ = diff_runs("a  week", "weekly")
    changed = [run for run in runs if run.op != "equal"]
    assert len(changed) >= 2
    for index, run in enumerate(runs):
        follows_change = index > 0 and runs[index - 1].op != "equal"
        assert run.gap is (run.op != "equal" and follows_change)


def test_parts_note_says_only_spacing_changed() -> None:
    before = FactContent(text=ROUTES)
    parts, note = diff_parts(before, FactContent(text="Planned 60  delivery routes a week."))
    assert note == NOTE_SPACING_ONLY == "Only spacing changed."
    assert [part.label for part in parts] == [None]


def test_parts_note_says_the_wording_is_the_same() -> None:
    before = FactContent(text=ROUTES)
    parts, note = diff_parts(before, FactContent(text=ROUTES))
    assert note == NOTE_SAME != NOTE_SPACING_ONLY
    assert reads(parts[0].runs) == ROUTES


def test_parts_have_no_note_for_a_first_version_or_a_marked_change() -> None:
    first, note = diff_parts(None, FactContent(text=ROUTES))
    assert note is None and {run.op for run in first[0].runs} == {"equal"}
    _changed, note = diff_parts(FactContent(text=ROUTES), FactContent(text=MORE_ROUTES))
    assert note is None


def test_story_parts_are_diffed_one_by_one_under_their_labels() -> None:
    before = FactContent(situation="Invoices took nine days.", result="Six days.")
    after = FactContent(
        situation="Invoices took nine days.", action="Mapped it.", result="Four days."
    )
    parts, note = diff_parts(before, after)
    assert note is None
    assert [part.label for part in parts] == ["Situation", "Action", "Result"]
    by_label = {part.label: part.runs for part in parts}
    assert {run.op for run in by_label["Situation"]} == {"equal"}
    assert marked(by_label["Action"], "added") and not marked(by_label["Action"], "removed")
    assert marked(by_label["Result"], "added") == ["Four"]
    assert marked(by_label["Result"], "removed") == ["Six"]
