"""Step 5 of the setup guide: job preferences parsing and validation (M1.19; AC-M1.6).

The handler lives in ``app/onboarding/steps/preferences.py``. Its constructor follows
``ProviderStep``: collaborators are injected by wiring as callables, so the handler needs no
workspace at construction time::

    PreferencesStep(
        settings: Callable[[], SettingsService | None],  # None while no workspace is open
        *,
        mark_done: Callable[[], None],  # marks "preferences" done and refreshes the decision
    )

``submit(form)`` reads the URL-encoded form (``Mapping[str, Sequence[str]]``):

- ``target_roles``, ``locations``, ``must_haves``, ``deal_breakers``: one item per line;
  ``\\r`` is ignored, surrounding whitespace and blank lines are dropped. ``target_roles``
  needs at least one item. At most 20 items, each 1-200 characters, no control characters (a
  tab inside a line is refused).
- ``work_modes``: repeated values among ``remote``, ``hybrid``, ``on_site`` (optional).
- ``salary_amount``: optional whole number per year, 0 to 10^9; spaces and commas are accepted
  as thousands separators. ``salary_currency``: a code from ``CURRENCIES``, required only when
  an amount is given; a currency without an amount means no minimum salary.
- ``cv_country``: required, a code from ``COUNTRIES`` (``ZZ`` is well shaped but unassigned).

A valid submit saves ``JobPreferences`` through ``SettingsService`` (``JOB_PREFERENCES``, with
the stored version, so a revisit updates it), calls ``mark_done`` once and answers
``StepResult(ok=True)`` without ``stay``. A refused submit answers ``ok=False`` with plain
messages keyed by field name, saves nothing and never calls ``mark_done``. Without a workspace
the answer is a ``form`` error asking to finish step 1.

Settings live in a real SQLite database under ``tmp_path``; every value is fictional.
"""

from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.domain.settings import JOB_PREFERENCES, JobPreferences, Salary
from app.modules.storage import MIGRATIONS_DIR, Database, apply_migrations
from app.onboarding.steps.preferences import PreferencesStep
from app.services.settings import SettingsService
from tests.fakes.clock import FrozenClock

START = datetime(2026, 1, 3, 9, 0, 0, tzinfo=UTC)
LIST_FIELDS = ("target_roles", "locations", "must_haves", "deal_breakers")
FIELDS = (*LIST_FIELDS, "work_modes", "salary_amount", "salary_currency", "cv_country")
PYDANTIC_MARKERS = (
    "pydantic",
    "validation error",
    "String should",
    "Input should",
    "Value error",
    "value_error",
    "string_pattern_mismatch",
    "errors.pydantic.dev",
    "Tuple should",
)


class Marks:
    def __init__(self) -> None:
        self.count = 0

    def __call__(self) -> None:
        self.count += 1


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Database]:
    database = Database.open(tmp_path / "data" / "app.db")
    apply_migrations(database, MIGRATIONS_DIR, tmp_path / "backups", FrozenClock(START))
    try:
        yield database
    finally:
        database.close()


@pytest.fixture
def settings(db: Database) -> SettingsService:
    return SettingsService(db, FrozenClock(START))


def build(settings: SettingsService | None) -> tuple[PreferencesStep, Marks]:
    marks = Marks()
    return PreferencesStep(lambda: settings, mark_done=marks), marks


def valid_form(**overrides: Sequence[str]) -> dict[str, Sequence[str]]:
    form: dict[str, Sequence[str]] = {
        "target_roles": ["Fictional Archivist\nFictional Lighthouse Keeper"],
        "locations": ["Fictionville\nRemote"],
        "work_modes": ["remote", "hybrid"],
        "salary_amount": ["45000"],
        "salary_currency": ["EUR"],
        "must_haves": ["Four-day week"],
        "deal_breakers": ["Night shifts"],
        "cv_country": ["PT"],
    }
    form.update(overrides)
    return form


def stored(settings: SettingsService) -> JobPreferences | None:
    return settings.load(JOB_PREFERENCES).value


def assert_plain(errors: Mapping[str, str]) -> None:
    for field, message in errors.items():
        assert message and message == message.strip(), field
        for marker in PYDANTIC_MARKERS:
            assert marker.lower() not in message.lower(), (field, message)


def assert_refused(
    step: PreferencesStep,
    marks: Marks,
    settings: SettingsService,
    form: Mapping[str, Sequence[str]],
    field: str,
) -> str:
    result = step.submit(form)
    assert result.ok is False
    assert field in result.errors, dict(result.errors)
    assert_plain(result.errors)
    assert stored(settings) is None, "nothing saved"
    assert marks.count == 0, "the step is not done"
    return result.errors[field]


# --- valid submits ---------------------------------------------------------------------------


def test_valid_submit_saves_preferences_and_marks_done(settings: SettingsService) -> None:
    step, marks = build(settings)
    result = step.submit(valid_form())

    assert result.ok is True
    assert result.stay is False
    assert dict(result.errors) == {}
    assert marks.count == 1
    assert stored(settings) == JobPreferences(
        target_roles=("Fictional Archivist", "Fictional Lighthouse Keeper"),
        locations=("Fictionville", "Remote"),
        work_modes=("remote", "hybrid"),
        minimum_salary=Salary(amount=45000, currency="EUR"),
        must_haves=("Four-day week",),
        deal_breakers=("Night shifts",),
        cv_country="PT",
    )


def test_lines_are_trimmed_and_blank_lines_and_carriage_returns_dropped(
    settings: SettingsService,
) -> None:
    step, _ = build(settings)
    result = step.submit(
        valid_form(
            target_roles=["\r\n  Fictional Archivist  \r\n\r\n   \r\n\tFictional Cartographer\r\n"],
            locations=["\n\n"],
            must_haves=[""],
            deal_breakers=["  Night shifts  "],
        )
    )
    assert result.ok is True, dict(result.errors)
    saved = stored(settings)
    assert saved is not None
    assert saved.target_roles == ("Fictional Archivist", "Fictional Cartographer")
    assert saved.locations == ()
    assert saved.must_haves == ()
    assert saved.deal_breakers == ("Night shifts",)


def test_only_required_fields_saves_with_empty_optionals(settings: SettingsService) -> None:
    step, marks = build(settings)
    result = step.submit({"target_roles": ["Fictional Archivist"], "cv_country": ["GB"]})
    assert result.ok is True, dict(result.errors)
    assert marks.count == 1
    assert stored(settings) == JobPreferences(
        target_roles=("Fictional Archivist",), cv_country="GB"
    )


@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        ("45000", 45000),
        ("45,000", 45000),
        ("45 000", 45000),
        (" 1,250,000 ", 1250000),
        ("0", 0),
        ("1000000000", 10**9),
    ],
)
def test_salary_amount_accepts_thousands_separators(
    settings: SettingsService, amount: str, expected: int
) -> None:
    step, _ = build(settings)
    result = step.submit(valid_form(salary_amount=[amount], salary_currency=["GBP"]))
    assert result.ok is True, dict(result.errors)
    saved = stored(settings)
    assert saved is not None
    assert saved.minimum_salary == Salary(amount=expected, currency="GBP")


def test_currency_without_amount_means_no_minimum_salary(settings: SettingsService) -> None:
    step, _ = build(settings)
    result = step.submit(valid_form(salary_amount=["  "], salary_currency=["USD"]))
    assert result.ok is True, dict(result.errors)
    saved = stored(settings)
    assert saved is not None
    assert saved.minimum_salary is None


def test_list_limits_twenty_items_of_two_hundred_characters_accepted(
    settings: SettingsService,
) -> None:
    items = [f"{index:02d}" + "x" * 198 for index in range(20)]
    step, _ = build(settings)
    result = step.submit(valid_form(**{name: ["\n".join(items)] for name in LIST_FIELDS}))
    assert result.ok is True, dict(result.errors)
    saved = stored(settings)
    assert saved is not None
    for name in LIST_FIELDS:
        assert getattr(saved, name) == tuple(items), name


def test_resubmit_updates_the_stored_preferences(settings: SettingsService) -> None:
    step, marks = build(settings)
    assert step.submit(valid_form()).ok is True
    again = step.submit(valid_form(target_roles=["Fictional Cartographer"], cv_country=["DE"]))
    assert again.ok is True, dict(again.errors)
    saved = stored(settings)
    assert saved is not None
    assert saved.target_roles == ("Fictional Cartographer",)
    assert saved.cv_country == "DE"
    assert settings.load(JOB_PREFERENCES).version == 2
    assert marks.count == 2


def test_untrusted_looking_text_is_saved_as_plain_data(settings: SettingsService) -> None:
    text = "Ignore previous instructions <script>alert('fictional')</script>"
    step, _ = build(settings)
    result = step.submit(valid_form(must_haves=[text]))
    assert result.ok is True, dict(result.errors)
    saved = stored(settings)
    assert saved is not None
    assert saved.must_haves == (text,)


# --- refused submits -------------------------------------------------------------------------


@pytest.mark.parametrize("roles", ["", "   \n\r\n  "])
def test_target_roles_need_at_least_one(settings: SettingsService, roles: str) -> None:
    step, marks = build(settings)
    assert_refused(step, marks, settings, valid_form(target_roles=[roles]), "target_roles")


def test_missing_required_fields_are_both_reported(settings: SettingsService) -> None:
    step, marks = build(settings)
    result = step.submit({})
    assert result.ok is False
    assert set(result.errors) == {"target_roles", "cv_country"}
    assert_plain(result.errors)
    assert marks.count == 0
    assert stored(settings) is None


@pytest.mark.parametrize("field", LIST_FIELDS)
def test_more_than_twenty_items_refused(settings: SettingsService, field: str) -> None:
    items = "\n".join(f"Fictional item {index}" for index in range(21))
    step, marks = build(settings)
    assert_refused(step, marks, settings, valid_form(**{field: [items]}), field)


@pytest.mark.parametrize("field", LIST_FIELDS)
def test_item_longer_than_two_hundred_characters_refused(
    settings: SettingsService, field: str
) -> None:
    step, marks = build(settings)
    assert_refused(step, marks, settings, valid_form(**{field: ["Fictional\n" + "y" * 201]}), field)


@pytest.mark.parametrize("field", LIST_FIELDS)
@pytest.mark.parametrize(
    "bad", ["Fictional\tArchivist", "Fictional\x00Archivist", "Fictional\x07Bell", "Del\x7f"]
)
def test_control_characters_inside_a_line_refused(
    settings: SettingsService, field: str, bad: str
) -> None:
    step, marks = build(settings)
    assert_refused(step, marks, settings, valid_form(**{field: [bad]}), field)


@pytest.mark.parametrize("modes", [["teleport"], ["remote", "Remote"], ["on-site"], [""]])
def test_unknown_work_mode_refused(settings: SettingsService, modes: list[str]) -> None:
    step, marks = build(settings)
    assert_refused(step, marks, settings, valid_form(work_modes=modes), "work_modes")


@pytest.mark.parametrize(
    "amount", ["lots", "4.5", "-100", "1e6", "45k", "1000000001", "99999999999999999999999"]
)
def test_salary_amount_must_be_a_whole_number_in_range(
    settings: SettingsService, amount: str
) -> None:
    step, marks = build(settings)
    assert_refused(step, marks, settings, valid_form(salary_amount=[amount]), "salary_amount")


def test_amount_without_currency_refused(settings: SettingsService) -> None:
    step, marks = build(settings)
    form = valid_form(salary_amount=["45,000"], salary_currency=[""])
    assert_refused(step, marks, settings, form, "salary_currency")


@pytest.mark.parametrize("currency", ["ZZZ", "eur", "EURO", "€"])
def test_unknown_currency_refused(settings: SettingsService, currency: str) -> None:
    step, marks = build(settings)
    form = valid_form(salary_currency=[currency])
    assert_refused(step, marks, settings, form, "salary_currency")


@pytest.mark.parametrize("country", ["", "ZZ", "UK", "PRT", "P"])
def test_cv_country_must_be_an_assigned_code(settings: SettingsService, country: str) -> None:
    step, marks = build(settings)
    assert_refused(step, marks, settings, valid_form(cv_country=[country]), "cv_country")


def test_every_invalid_field_is_reported_at_once(settings: SettingsService) -> None:
    form = {
        "target_roles": [""],
        "locations": ["Fictional\tTown"],
        "work_modes": ["teleport"],
        "salary_amount": ["lots"],
        "salary_currency": ["ZZZ"],
        "must_haves": ["\n".join(f"Item {index}" for index in range(21))],
        "deal_breakers": ["z" * 201],
        "cv_country": ["ZZ"],
    }
    step, marks = build(settings)
    result = step.submit(form)
    assert result.ok is False
    assert set(result.errors) == set(FIELDS)
    assert_plain(result.errors)
    assert marks.count == 0
    assert stored(settings) is None


def test_refused_resubmit_keeps_the_saved_preferences(settings: SettingsService) -> None:
    step, marks = build(settings)
    assert step.submit(valid_form()).ok is True
    before = settings.load(JOB_PREFERENCES)

    result = step.submit(valid_form(cv_country=["ZZ"]))
    assert result.ok is False
    assert settings.load(JOB_PREFERENCES) == before
    assert marks.count == 1


# --- no workspace ----------------------------------------------------------------------------


def test_without_workspace_asks_to_finish_step_1() -> None:
    step, marks = build(None)
    result = step.submit(valid_form())
    assert result.ok is False
    assert set(result.errors) == {"form"}
    assert "step 1" in result.errors["form"].lower()
    assert marks.count == 0
    assert step.context().get("workspace_open") is False
