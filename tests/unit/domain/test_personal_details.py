"""Personal details and the photo reference (M6.4; AC-M6.6 groundwork; ADR-106 items 3 and 4;
spec 2.9 rule 13): ``PersonalDetails`` and ``PhotoRef`` in ``app/domain/settings.py``. Pure
validation; every value is fictional.

Assumed interface (chosen here where the sources left it open; the implementer follows it):

- ``PhotoRef``: frozen pydantic model, refusing unknown fields, with ``stored_name: str`` only,
  the file name inside the data folder's ``assets/``: exactly ``photo-<id>.<jpg|png>`` where
  ``<id>`` has the shape of an ``IdFactory`` id (``is_safe_id``: 1 to 64 of A-Z, a-z, 0-9,
  ``_``, ``-``). Nothing that could name another path is accepted.
- ``PersonalDetails``: frozen pydantic model, refusing unknown fields (so a date of birth, a
  nationality or a marital status can't be stored: ADR-106 item 3), all optional so a
  half-filled form can be saved:
    * ``name``: str or None, at most 100 characters;
    * ``email``: str or None, at most 254 characters, shaped ``local@domain.tld`` with no
      whitespace, no control characters, none of ``<>,;"\\``, exactly one ``@`` and a dot
      inside the domain;
    * ``phone``: str or None, at most 30 characters, only digits, spaces, ``+``, ``-``, ``(``,
      ``)`` and ``.``, with 6 to 15 digits;
    * ``location``: str or None, at most 100 characters;
    * ``links``: tuple of at most 3 strings (a blank entry is refused), each at most 300
      characters, an ``http`` or ``https`` URL with a host, no user name or password, no whitespace;
    * ``photo``: ``PhotoRef`` or None.
  Texts are stripped; a blank one is None. ``name``, ``location`` and every text refuse control
  characters (a newline would break a header line). Markup and other text are data and stay as
  written. Defaults: None, None, None, None, ``()``, None.
- ``PersonalDetails.can_export`` (property): True only when ``name`` is set (the CV header and
  the letter sign-off need it).
- ``PERSONAL_DETAILS = SettingSpec("personal_details", PersonalDetails, PersonalDetails())``.
- Errors are ``pydantic.ValidationError`` whose ``loc[0]`` is the field, so the form can show
  each one beside its field.
"""

import importlib
from collections.abc import Callable
from types import ModuleType
from typing import Any

import pytest
from pydantic import ValidationError


def settings_module() -> ModuleType:
    return importlib.import_module("app.domain.settings")


def details(**fields: Any) -> Any:
    return settings_module().PersonalDetails(**fields)


def refused_fields(**fields: Any) -> set[str]:
    with pytest.raises(ValidationError) as caught:
        details(**fields)
    return {str(error["loc"][0]) for error in caught.value.errors()}


EMAIL_OK = ["ana.sample+jobs@example.org", "jão@exemplo.pt", "a@b.co"]
EMAIL_BAD = [
    "no-at-sign",
    "two@@example.org",
    "a@b@example.org",
    "@example.org",
    "ana@",
    "ana@example",
    "ana sample@example.org",
    "ana@example .org",
    "ana@example.org\nBcc: other@example.org",
    "ana@example.org\x00",
    "<script>@example.org",
]
PHONE_OK = ["+351 912 345 678", "(555) 010-0199", "555.010.0199", "  +44 20 7946 0000  "]
PHONE_BAD = [
    "call me",
    "12345",  # five digits
    "1234567890123456",  # sixteen digits
    "+1 555 010 0199 ext 5",
    "555-010-0199;rm -rf",
    "<b>5550100199</b>",
    "555\n0100199",
    "+++++-----",
]
LINKS_OK = [
    "https://example.com/in/ana-sample",
    "http://example.org",
    "HTTPS://Example.com/Portfolio?x=1#top",
    "  https://example.net/a-b  ",
]
LINKS_BAD = [
    "javascript:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "file:///etc/passwd",
    "ftp://example.com/cv.pdf",
    "mailto:ana@example.org",
    "example.com/portfolio",
    "//example.com/portfolio",
    "https://",
    "https:///no-host",
    "https://user:secret@example.com/",
    "https://example.com/has space",
    "https://example.com/\nsecond-line",
    "",
]


def test_personal_details_defaults_are_blank_and_not_exportable() -> None:
    blank = details()

    assert (blank.name, blank.email, blank.phone, blank.location, blank.photo) == (None,) * 5
    assert blank.links == ()
    assert blank.can_export is False
    assert settings_module().PERSONAL_DETAILS.key == "personal_details"
    assert settings_module().PERSONAL_DETAILS.model is settings_module().PersonalDetails
    assert settings_module().PERSONAL_DETAILS.default == blank


def test_personal_details_name_is_needed_to_export() -> None:
    assert details(name="Ana Sample").can_export is True
    assert details(name="   ").can_export is False
    assert details(email="ana.sample@example.org", phone="+351 912 345 678").can_export is False


def test_personal_details_texts_are_stripped_and_blank_means_none() -> None:
    filled = details(name="  Ana Sample ", location="  Porto, Portugal ", phone="", email=" ")

    assert (filled.name, filled.location, filled.phone, filled.email) == (
        "Ana Sample",
        "Porto, Portugal",
        None,
        None,
    )


def _check_email() -> None:
    for value in EMAIL_OK:
        assert details(email=value).email == value
    assert details(email="  ana.sample@example.org ").email == "ana.sample@example.org"
    for value in EMAIL_BAD:
        assert refused_fields(email=value) == {"email"}, f"{value!r} should be refused"


def _check_phone() -> None:
    for value in PHONE_OK:
        assert details(phone=value).phone == value.strip()
    for value in PHONE_BAD:
        assert refused_fields(phone=value) == {"phone"}, f"{value!r} should be refused"


def _check_links_http_only() -> None:
    for link in LINKS_OK:
        assert details(links=[link]).links == (link.strip(),)
    for link in LINKS_BAD:
        assert refused_fields(links=[link]) == {"links"}, f"{link!r} should be refused"
    three = LINKS_OK[:3]
    assert details(links=three).links == tuple(three)
    assert refused_fields(links=[*three, "https://example.org/fourth"]) == {"links"}
    assert refused_fields(links=[three[0], "javascript:alert(1)"]) == {"links"}
    assert details(links=[]).links == ()


def _check_lengths() -> None:
    for field, limit in {"name": 100, "location": 100}.items():
        assert getattr(details(**{field: "n" * limit}), field) == "n" * limit
        assert refused_fields(**{field: "n" * (limit + 1)}) == {field}

    long_email = "a" * 64 + "@" + ".".join(["b" * 61] * 3) + ".org"
    assert len(long_email) == 254
    assert details(email=long_email).email == long_email
    assert refused_fields(email="a" + long_email) == {"email"}

    assert details(phone="1" * 15).phone == "1" * 15
    assert refused_fields(phone="1" * 16) == {"phone"}
    assert refused_fields(phone="1 " * 20) == {"phone"}  # twenty digits and 40 characters
    assert refused_fields(phone="(" * 31) == {"phone"}

    prefix = "https://example.com/"
    exact = prefix + "a" * (300 - len(prefix))
    assert details(links=[exact]).links == (exact,)
    assert refused_fields(links=[exact + "a"]) == {"links"}


@pytest.mark.parametrize(
    "check",
    [
        pytest.param(_check_email, id="email"),
        pytest.param(_check_phone, id="phone"),
        pytest.param(_check_links_http_only, id="links_http_only"),
        pytest.param(_check_lengths, id="lengths"),
    ],
)
def test_personal_details_validation(check: Callable[[], None]) -> None:
    check()


def test_personal_details_errors_are_tied_to_their_fields() -> None:
    assert refused_fields(
        email="nope", phone="call me", links=["javascript:x"], name="n" * 101
    ) == {
        "email",
        "phone",
        "links",
        "name",
    }
    assert refused_fields(email="nope") == {"email"}


def test_personal_details_refuse_control_characters_and_keep_markup_as_data() -> None:
    for bad in ("Ana\nSample", "Ana\x00Sample", "Ana\tSample", "Ana\r\nSample"):
        assert refused_fields(name=bad) == {"name"}
        assert refused_fields(location=bad) == {"location"}

    hostile = "<script>alert(1)</script> & {{ 7 * 7 }} \U0001f600"
    assert details(name=hostile).name == hostile
    assert details(location="São Paulo — BR").location == "São Paulo — BR"


@pytest.mark.parametrize(
    "field", ["date_of_birth", "nationality", "marital_status", "gender", "age"]
)
def test_personal_details_do_not_store_sensitive_attributes(field: str) -> None:
    assert refused_fields(**{field: "anything"}) == {field}


def test_personal_details_are_immutable_and_round_trip_through_json() -> None:
    module = settings_module()
    original = details(
        name="Ana Sample",
        email="ana.sample@example.org",
        phone="+351 912 345 678",
        location="Porto",
        links=["https://example.com/in/ana-sample"],
        photo={"stored_name": "photo-id-0001.jpg"},
    )

    with pytest.raises(ValidationError):
        original.name = "Someone Else"
    dumped = original.model_dump(mode="json")
    assert dumped["links"] == ["https://example.com/in/ana-sample"]
    assert dumped["photo"] == {"stored_name": "photo-id-0001.jpg"}
    assert module.PersonalDetails.model_validate(dumped) == original
    assert module.PersonalDetails.model_validate_json(original.model_dump_json()) == original


@pytest.mark.parametrize(
    "stored_name",
    ["photo-id-0001.jpg", "photo-id-0001.png", "photo-" + "A" * 64 + ".png", "photo-a_b-C9.jpg"],
)
def test_photo_ref_accepts_generated_asset_names(stored_name: str) -> None:
    module = settings_module()

    assert module.PhotoRef(stored_name=stored_name).stored_name == stored_name
    assert details(photo={"stored_name": stored_name}).photo == module.PhotoRef(
        stored_name=stored_name
    )


@pytest.mark.parametrize(
    "stored_name",
    [
        "",
        "photo-.jpg",
        "photo-id-0001.gif",
        "photo-id-0001.jpeg",
        "photo-id-0001.JPG",
        "photo-id-0001",
        "photo-id-0001.jpg.exe",
        "photo-" + "A" * 65 + ".png",
        "photo-id 0001.jpg",
        "photo-../etc.jpg",
        "../photo-id-0001.jpg",
        "assets/photo-id-0001.jpg",
        "/etc/passwd",
        "photo-id-0001.jpg\n",
        "avatar-id-0001.jpg",
        ".photo-id-0001.jpg",
    ],
)
def test_photo_ref_refuses_anything_but_a_generated_asset_name(stored_name: str) -> None:
    module = settings_module()

    with pytest.raises(ValidationError):
        module.PhotoRef(stored_name=stored_name)
    assert refused_fields(photo={"stored_name": stored_name}) == {"photo"}


def test_photo_ref_is_immutable_and_refuses_extra_fields() -> None:
    module = settings_module()
    ref = module.PhotoRef(stored_name="photo-id-0001.jpg")

    with pytest.raises(ValidationError):
        ref.stored_name = "photo-id-0002.jpg"
    with pytest.raises(ValidationError):
        module.PhotoRef(stored_name="photo-id-0001.jpg", path="/Users/someone/me.jpg")


BIDI_CHARACTERS = [
    "‪",
    "‫",
    "‬",
    "‭",
    "‮",
    "⁦",
    "⁧",
    "⁨",
    "⁩",
    "‎",
    "‏",
    "؜",
]


@pytest.mark.parametrize("mark", BIDI_CHARACTERS, ids=lambda mark: f"U+{ord(mark):04X}")
def test_personal_details_refuse_bidi_override_isolate_and_mark_characters(mark: str) -> None:
    assert refused_fields(name=f"Ana{mark}Sample") == {"name"}
    assert refused_fields(location=f"Lisbon{mark}") == {"location"}
    assert refused_fields(email=f"ana{mark}@example.org") == {"email"}
    assert refused_fields(email=f"ana@exam{mark}ple.org") == {"email"}
    assert refused_fields(phone=f"+351 912 345 678{mark}") == {"phone"}
    assert refused_fields(links=(f"https://example.org/{mark}x",)) == {"links"}
    assert refused_fields(links=(f"https://exam{mark}ple.org",)) == {"links"}


def test_personal_details_allow_a_zero_width_space() -> None:
    assert details(name="Ana​Sample").name == "Ana​Sample"
