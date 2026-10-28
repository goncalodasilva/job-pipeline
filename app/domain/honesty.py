"""The honesty check (spec 3.1 items 3 and 5; ADR-089 item 5, ADR-092 item 4). Pure.

``extract_quantities`` finds every number, percentage, year and money amount written in a
text, with its value, so that "1,200" and "1200", "35%" and "35 percent", "€5k" and "€5,000"
are the same quantity and "3.5" and "35" never are. Numbers written as words count too:
"fourteen" is 14, "eighty-five percent" is 85%, "twelve hundred" is 1200, "two dozen" is
24, and "doubled", "doubling", "double", "twice", "tripled", "tripling", "triple",
"quadrupled", "quadrupling", "quadruple", "twofold" to "tenfold", "halved" and "half" are
the multiples they name. Plural scales ("hundreds", "dozens") and fractions ("a
third", "two thirds", "three quarters") are compared by their words, never by a value, so
"two thirds" never passes for 2. "one" used as a pronoun ("no one", "one of", "one another",
"the one", "one at a time") is no quantity.

``new_claims`` lists what a suggested wording says that none of its allowed sources (the
fact, its role, the user's own answers, the confirmed facts sent) does. Sources whose names
are withheld (``name_hidden_sources``: facts marked "generalize names") support quantities
and skills, never a capitalised name:

- quantities, compared by value: a number or a year by value, a percentage only with a
  percentage, a money amount only with a money amount;
- capitalised names (runs of capitalised words) not found in any source, whatever the case
  the source writes them in. The first word of a sentence is capitalised for grammar, so on
  its own it is not a name; "I" is never one;
- names of the controlled skills list (``known_skills``), as whole words without regard to
  case, that no source names;
- every non-blank detail the model itself says it added (``added_details``).

Known limit (ADR-089 item 5): lower-case, non-numeric embellishment ("saving the team a
week") is caught only by the model's own report, the prompt and the user reading the diff.
Number words are English only. A name can also escape the name check as a single capitalised
word at the start of a sentence, after a newline, or written in lower case.
The check never decides anything: it lists claims for the user to acknowledge or edit out.

``check_units`` applies this to the units of a generated document (spec 3.1 items 2-4 and 6;
ADR-106): each unit is compared only with the facts it cites, and a citation to a fact not
sent in that request, an uncited content unit or a structure unit that is not exactly a
template heading is ``unsupported``. A word that mixes Latin and other letters (a Cyrillic
look-alike inside "ten") is a ``needs_check`` of its own. ``approval_blockers`` re-reads the
citations against the facts as they stand at approval time.
"""

import re
import unicodedata
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final, Literal

from app.domain.fact_selection import AIFact, fact_text
from app.domain.facts import FactRef, FactStatus, Privacy

QuantityKind = Literal["number", "percent", "year", "money"]
ClaimKind = Literal["number", "percent", "year", "money", "name", "skill", "added_detail"]

_CURRENCY_SIGNS: Final = "€$£¥"
_CURRENCY_CODES: Final = "EUR|USD|GBP"
_CURRENCY_WORDS: Final = "euros?|dollars?|pounds?|yen"
_CURRENCIES: Final = {
    "€": "EUR",
    "$": "USD",
    "£": "GBP",
    "¥": "¥",
    "EURO": "EUR",
    "EUROS": "EUR",
    "DOLLAR": "USD",
    "DOLLARS": "USD",
    "POUND": "GBP",
    "POUNDS": "GBP",
    "YEN": "¥",
}
# A quantity: an optional currency before it, a run of digits with dots or commas between
# digits ("35", "1,200", "3.5"), an optional multiplier ("5k", "2 million"), then an optional
# percent sign or word, or a currency after it.
_QUANTITY: Final = re.compile(
    r"(?:(?<!\w)(?P<sign>[+\u2212-]))?"
    rf"(?P<pre>[{_CURRENCY_SIGNS}][ \t]?|(?i:{_CURRENCY_CODES})[ \t])?"
    r"(?(pre)(?P<inner_sign>[+\u2212-])?)"
    r"(?P<num>\d+(?:[.,]\d+)*)"
    r"(?P<mult>(?:k|K|m|M|bn)(?![^\W\d_])|[ \t](?i:thousand|million|billion)\b)?"
    r"(?P<pct>[ \t]?%|[ \t](?i:percent|per[ \t]cent)\b)?"
    rf"(?P<post>[ \t]?[{_CURRENCY_SIGNS}]|[ \t](?i:{_CURRENCY_CODES}|{_CURRENCY_WORDS})\b)?"
)
_THOUSANDS: Final = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?")
_PLAIN: Final = re.compile(r"\d+(?:\.\d+)?")
_DECIMAL_COMMA: Final = re.compile(r"\d+,\d+")
_YEAR: Final = re.compile(r"(?:19|20)[0-9]{2}")
_MULTIPLIERS: Final = {
    "k": Decimal(1_000),
    "thousand": Decimal(1_000),
    "m": Decimal(1_000_000),
    "million": Decimal(1_000_000),
    "bn": Decimal(1_000_000_000),
    "billion": Decimal(1_000_000_000),
}
# A word: letters first, then letters, digits, apostrophes, dots, ampersands or hyphens,
# ending on a letter or digit ("Ltd" in "Ltd.", "O'Neil", "B2B").
_WORD: Final = re.compile(r"[^\W\d_](?:[\w'.&-]*[^\W_])?")
# What ends a sentence (or a line) between two words: the next word starts a new one. A
# semicolon or a colon doesn't: a capitalised word after one is still a name.
_SENTENCE_BREAK: Final = re.compile(r"[.!?\n]")
_NOT_NAMES: Final = frozenset({"I"})

_UNITS: Final = {
    word: Decimal(value)
    for value, word in enumerate(
        [
            "zero",
            "one",
            "two",
            "three",
            "four",
            "five",
            "six",
            "seven",
            "eight",
            "nine",
            "ten",
            "eleven",
            "twelve",
            "thirteen",
            "fourteen",
            "fifteen",
            "sixteen",
            "seventeen",
            "eighteen",
            "nineteen",
        ]
    )
}
_TENS: Final = {
    word: Decimal(10 * value)
    for value, word in enumerate(
        ["twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"], start=2
    )
}
_HUNDRED: Final = "hundred"
_SCALES: Final = {
    "thousand": Decimal(1_000),
    "million": Decimal(1_000_000),
    "billion": Decimal(1_000_000_000),
}
# Words that state a multiple on their own.
_TIMES: Final = {
    "doubled": Decimal(2),
    "doubling": Decimal(2),
    "double": Decimal(2),
    "twice": Decimal(2),
    "tripled": Decimal(3),
    "tripling": Decimal(3),
    "triple": Decimal(3),
    "quadrupled": Decimal(4),
    "quadrupling": Decimal(4),
    "quadruple": Decimal(4),
    "halved": Decimal("0.5"),
    "half": Decimal("0.5"),
    **{f"{word}fold": value for word, value in _UNITS.items() if 2 <= value <= 10},
}
_DOZEN: Final = "dozen"
# Words whose value is vague ("hundreds") or easy to misread ("two thirds" is not 2): such a
# quantity is supported only by the same words in a source. A fraction may follow a unit.
_PLURAL_SCALES: Final = {
    "hundreds": Decimal(100),
    "thousands": Decimal(1_000),
    "millions": Decimal(1_000_000),
    "billions": Decimal(1_000_000_000),
    "dozens": Decimal(12),
}
_FRACTIONS: Final = {
    "third": Decimal(3),
    "thirds": Decimal(3),
    "quarter": Decimal(4),
    "quarters": Decimal(4),
}
_NUMBER_WORDS: Final = frozenset(
    {*_UNITS, *_TENS, _HUNDRED, *_SCALES, *_TIMES, _DOZEN, *_PLURAL_SCALES, *_FRACTIONS}
)
# "one" as a pronoun, not a count: "no one", "the one", "one of", "one another",
# "one at a time".
_PRONOUN_BEFORE: Final = re.compile(r"(?<![^\W\d_])(?:no|the)[ \t]+\Z", re.IGNORECASE)
_PRONOUN_AFTER: Final = re.compile(
    r"[ \t]+(?:of|another|at[ \t]+a[ \t]+time)(?![^\W\d_])", re.IGNORECASE
)
# A run of letters, maybe hyphenated ("eighty-five"); a digit or an underscore ends it.
_LETTER_RUN: Final = re.compile(r"[^\W\d_]+(?:-[^\W\d_]+)*")
# What may stand between two words of one number: spaces, maybe with "and".
_NUMBER_GAP: Final = re.compile(r"[ \t]+(?P<conj>and[ \t]+)?", re.IGNORECASE)
# What may follow a number written in words: a percent, or a currency after it.
_WORD_SUFFIX: Final = re.compile(
    r"(?P<pct>[ \t]?%|[ \t](?i:percent|per[ \t]cent)\b)"
    rf"|(?P<post>[ \t](?i:{_CURRENCY_CODES}|{_CURRENCY_WORDS})\b)"
)


@dataclass(frozen=True)
class Quantity:
    """One quantity as written in a text: its kind, its value and its text. ``form`` is set
    (the words, folded) for a quantity compared by its words rather than its value
    ("hundreds", "two thirds"); its ``value`` is then only indicative."""

    kind: QuantityKind
    value: Decimal
    text: str
    currency: str | None = None
    form: str | None = None


@dataclass(frozen=True)
class Claim:
    """Something a wording says that its sources don't: ``text`` as the wording writes it
    (for ``added_detail``, as the model reported it)."""

    kind: ClaimKind
    text: str


def _prepared(text: str) -> str:
    """``text`` as it is read: without invisible format characters (a zero-width space,
    a soft hyphen) and with compatibility forms spelled out (NFKC: full-width digits)."""
    visible = "".join(char for char in text if unicodedata.category(char) != "Cf")
    return unicodedata.normalize("NFKC", visible)


def _number_value(digits: str) -> Decimal:
    """The value of a run of digits with separators: "1,200" (thousands), "3.5", "3,5" (a
    decimal comma). Anything else ("1.2.3") reads as its digits, one whole number."""
    if _THOUSANDS.fullmatch(digits):
        return Decimal(digits.replace(",", ""))
    if _PLAIN.fullmatch(digits):
        return Decimal(digits)
    if _DECIMAL_COMMA.fullmatch(digits):
        return Decimal(digits.replace(",", "."))
    return Decimal(re.sub(r"[.,]", "", digits))


def _multiplier(mult: str | None) -> Decimal:
    if not mult:
        return Decimal(1)
    return _MULTIPLIERS[mult.strip().casefold()]


def _quantity(match: re.Match[str]) -> Quantity:
    num, mult = match["num"], match["mult"]
    value = _number_value(num) * _multiplier(mult)
    if (match["sign"] in ("-", "\u2212")) != (match["inner_sign"] in ("-", "\u2212")):
        value = -value
    kind: QuantityKind
    if match["pre"] or match["post"]:
        kind = "money"
    elif match["pct"]:
        kind = "percent"
    elif not mult and _YEAR.fullmatch(num):
        kind = "year"
    else:
        kind = "number"
    written_currency = (match["pre"] or match["post"] or "").strip().upper()
    currency = _CURRENCIES.get(written_currency, written_currency) or None
    return Quantity(kind, value, match[0].strip(), currency)


@dataclass(frozen=True)
class _Word:
    """One number word of a text: where it is, and the word folded."""

    start: int
    end: int
    word: str


def _number_words(text: str, taken: Sequence[tuple[int, int]]) -> Iterator[_Word]:
    """The number words of ``text`` outside the ``taken`` spans (the digit quantities), in
    order. A hyphenated run counts only when each part is a number word: "eighty-five" is
    read, "twenty-somethings" is not."""
    for run in _LETTER_RUN.finditer(text):
        if any(start < run.end() and run.start() < end for start, end in taken):
            continue
        parts = run[0].split("-")
        if all(part.casefold() in _NUMBER_WORDS for part in parts):
            position = run.start()
            for part in parts:
                yield _Word(position, position + len(part), part.casefold())
                position += len(part) + 1


class _Spelled:
    """A number being read from its words, left to right."""

    def __init__(self, first: _Word) -> None:
        self.start = first.start
        self.end = first.end
        self.total = Decimal(0)  # what the last scale word ("thousand") closed
        self.current = Decimal(0)  # what came after it
        # "unit", "tens", "hundred", "scale", or one that ends the number: "times",
        # "dozen" or "form" (a plural scale or a fraction)
        self.last = ""
        self.scale: Decimal | None = None
        self.words: list[str] = []
        self._add(first.word)

    @property
    def form(self) -> str | None:
        """The words, when the number is compared by them rather than by its value."""
        return " ".join(self.words) if self.last == "form" else None

    @property
    def pronoun(self) -> bool:
        """Whether this is the single word "one" used as a pronoun ("no one", "one of")."""
        return self.words == ["one"]

    @property
    def value(self) -> Decimal:
        return self.total + self.current

    def takes(self, gap: str, word: str) -> bool:
        """Whether ``word``, after ``gap``, continues this number ("twenty" "one", "one
        hundred and five"), and if so reads it."""
        joined = _NUMBER_GAP.fullmatch(gap)
        if gap != "-" and joined is None:
            return False
        if joined is not None and joined["conj"] and self.last not in ("hundred", "scale"):
            return False
        if not self._fits(word):
            return False
        self._add(word)
        return True

    def _fits(self, word: str) -> bool:
        if self.last in ("times", "dozen", "form") or word in _TIMES or word in _PLURAL_SCALES:
            return False  # "doubled", "a dozen", "two thirds" and "hundreds" end a number
        if word == _DOZEN or word in _FRACTIONS:  # "two dozen", "two thirds"
            return self.last in ("unit", "tens")
        if word in _UNITS:
            # "twenty one", "hundred five", "thousand two"; never "five four".
            return (self.last == "tens" and _UNITS[word] < 10) or self.last in (
                "hundred",
                "scale",
            )
        if word in _TENS:
            return self.last in ("hundred", "scale")
        if word == _HUNDRED:
            return self.last == "unit"
        return self.scale is None or _SCALES[word] < self.scale

    def _add(self, word: str) -> None:
        self.words.append(word)
        if word in _TIMES:
            self.total = _TIMES[word]
            self.last = "times"
        elif word == _DOZEN:
            self.current = (self.current or Decimal(1)) * 12
            self.last = "dozen"
        elif word in _PLURAL_SCALES:
            self.total = _PLURAL_SCALES[word]
            self.last = "form"
        elif word in _FRACTIONS:
            self.current = (self.current or Decimal(1)) / _FRACTIONS[word]
            self.last = "form"
        elif word in _UNITS:
            self.current += _UNITS[word]
            self.last = "unit"
        elif word in _TENS:
            self.current += _TENS[word]
            self.last = "tens"
        elif word == _HUNDRED:
            self.current = (self.current or Decimal(1)) * 100
            self.last = "hundred"
        else:
            self.scale = _SCALES[word]
            self.total += (self.current or Decimal(1)) * self.scale
            self.current = Decimal(0)
            self.last = "scale"


def _spelled_numbers(text: str, taken: Sequence[tuple[int, int]]) -> Iterator[_Spelled]:
    """The numbers the words of ``text`` spell, each from its first to its last word."""
    number: _Spelled | None = None
    for item in _number_words(text, taken):
        if number is not None and number.takes(text[number.end : item.start], item.word):
            number.end = item.end
            continue
        if number is not None:
            yield number
        number = _Spelled(item)
    if number is not None:
        yield number


def _word_quantities(text: str, taken: Sequence[tuple[int, int]]) -> Iterator[tuple[int, Quantity]]:
    """The quantities written in words, each with where it starts; a percent or a currency
    may follow the words."""
    for number in _spelled_numbers(text, taken):
        if number.pronoun and (
            _PRONOUN_BEFORE.search(text, 0, number.start) or _PRONOUN_AFTER.match(text, number.end)
        ):
            continue
        end = number.end
        kind: QuantityKind = "number"
        currency: str | None = None
        suffix = _WORD_SUFFIX.match(text, end)
        if suffix is not None:
            end = suffix.end()
            if suffix["pct"]:
                kind = "percent"
            else:
                kind, written = "money", suffix["post"].strip().upper()
                currency = _CURRENCIES.get(written, written)
        written = text[number.start : end]
        yield number.start, Quantity(kind, number.value, written, currency, number.form)


def extract_quantities(text: str) -> tuple[Quantity, ...]:
    """Every number, percentage, year and money amount written in ``text``, in digits or in
    words, in order. A four-digit whole number from 1900 to 2099 written without separators
    is a year ("1,999" is a number); words are never a year."""
    prepared = _prepared(text)
    digits = list(_QUANTITY.finditer(prepared))
    found = [(match.start(), _quantity(match)) for match in digits]
    found += _word_quantities(prepared, [match.span() for match in digits])
    return tuple(quantity for _start, quantity in sorted(found, key=lambda item: item[0]))


_Key = tuple[Decimal, str | None, str | None]


def _key(quantity: Quantity) -> _Key:
    """What a quantity is compared by: its value and currency, or its words (``form``)."""
    if quantity.form is not None:
        return Decimal(0), quantity.currency, quantity.form
    return quantity.value, quantity.currency, None


def _allowed_values(sources: Iterable[str]) -> dict[str, set[_Key]]:
    """The values the sources give, pooled the way they are compared: numbers and years
    together, percentages apart, money apart."""
    pools: dict[str, set[_Key]] = {"number": set(), "percent": set(), "money": set()}
    for source in sources:
        for quantity in extract_quantities(source):
            pools[_pool(quantity.kind)].add(_key(quantity))
    return pools


def _pool(kind: QuantityKind) -> str:
    return "number" if kind == "year" else kind


def _new_quantities(suggested: str, sources: Sequence[str]) -> Iterator[Claim]:
    allowed = _allowed_values(sources)
    for quantity in extract_quantities(suggested):
        if _key(quantity) not in allowed[_pool(quantity.kind)]:
            yield Claim(quantity.kind, quantity.text)


def _words(text: str) -> list[re.Match[str]]:
    return list(_WORD.finditer(text))


def _folded_words(text: str) -> tuple[str, ...]:
    return tuple(match[0].casefold() for match in _words(_prepared(text)))


def _contains(words: Sequence[str], wanted: Sequence[str]) -> bool:
    """Whether ``wanted`` is a run of consecutive words of ``words``."""
    size = len(wanted)
    return any(tuple(words[i : i + size]) == tuple(wanted) for i in range(len(words) - size + 1))


def _name_runs(text: str) -> Iterator[tuple[list[str], bool]]:
    """Runs of capitalised words separated only by spaces, each with whether its first
    word starts a sentence."""
    run: list[str] = []
    starts_sentence = False
    previous_end: int | None = None
    for match in _words(text):
        word = match[0]
        gap = text[previous_end : match.start()] if previous_end is not None else ""
        at_start = previous_end is None or bool(_SENTENCE_BREAK.search(gap))
        capitalised = word[0].isupper() and word not in _NOT_NAMES
        if run and (not capitalised or at_start or gap.strip()):
            yield run, starts_sentence
            run = []
        if capitalised:
            if not run:
                starts_sentence = at_start
            run.append(word)
        previous_end = match.end()
    if run:
        yield run, starts_sentence


def _new_names(
    suggested: str,
    sources: Sequence[str],
    skills: frozenset[str],
    whole_names: Sequence[str] = (),
) -> Iterator[Claim]:
    """The capitalised names the sources don't support. A name may be any run of words inside a
    source; ``whole_names`` (a job's title or company) support only a run that is all of one."""
    source_words = [_folded_words(source) for source in sources]
    whole_words = [_folded_words(name) for name in whole_names]

    def known(words: Sequence[str]) -> bool:
        folded = [word.casefold() for word in words]
        return (
            " ".join(folded) in skills
            or any(_contains(s, folded) for s in source_words)
            or tuple(folded) in whole_words
        )

    # Currency codes belong to their amount, rather than to capitalised names.
    without_codes = re.sub(rf"\b(?:{_CURRENCY_CODES})\b", " ", _prepared(suggested))
    for run, starts_sentence in _name_runs(without_codes):
        if known(run):
            continue
        rest = run[1:] if starts_sentence else run
        # A capitalised first word of a sentence alone is grammar, not a name.
        if rest and not known(rest):
            yield Claim("name", " ".join(run))


def _mentions(text: str, name: str) -> bool:
    """Whether ``name`` is in ``text`` as whole words, without regard to case ("R" is not in
    "report"). Both are already prepared and folded."""
    start = text.find(name)
    while start >= 0:
        end = start + len(name)
        cut_before = start > 0 and text[start - 1].isalnum() and text[start].isalnum()
        cut_after = end < len(text) and text[end].isalnum() and text[end - 1].isalnum()
        if not (cut_before or cut_after):
            return True
        start = text.find(name, start + 1)
    return False


def _folded(text: str) -> str:
    return " ".join(_prepared(text).casefold().split())


def _new_skills(
    suggested: str, sources: Sequence[str], known_skills: Iterable[str]
) -> Iterator[Claim]:
    wording = _folded(suggested)
    folded_sources = [_folded(source) for source in sources]
    for skill in known_skills:
        name = _folded(skill)
        if not name or not _mentions(wording, name):
            continue
        if not any(_mentions(source, name) for source in folded_sources):
            yield Claim("skill", _as_written(suggested, name, skill))


def _as_written(suggested: str, name: str, skill: str) -> str:
    """The skill as the wording writes it ("power bi"), when it can be cut out of the
    wording as it stands; else the skill's own name."""
    prepared = " ".join(_prepared(suggested).split())
    start = prepared.casefold().find(name)
    if start >= 0 and len(prepared.casefold()) == len(prepared):
        return prepared[start : start + len(name)]
    return skill


def new_claims(
    suggested: str,
    allowed_sources: Iterable[str | None],
    *,
    known_skills: Iterable[str] = (),
    added_details: Iterable[str] = (),
    name_hidden_sources: Iterable[str | None] = (),
    name_only_sources: Iterable[str | None] = (),
) -> tuple[Claim, ...]:
    """What ``suggested`` says that no allowed source does, plus every detail the model
    reported adding, each claim once, in this order: quantities, names, skills, added
    details. ``name_hidden_sources`` (facts whose names the user withholds) support
    quantities and skills, but no capitalised name. ``name_only_sources`` (a cover letter's job
    title and company) support a capitalised name only when it is all of one source, and
    nothing else: no quantity, no skill."""
    sources = [source for source in allowed_sources if source]
    whole_names = [source for source in name_only_sources if source]
    with_hidden = [*sources, *(source for source in name_hidden_sources if source)]
    skills = tuple(known_skills)
    folded_skills = frozenset(_folded(skill) for skill in skills)
    found = [
        *_new_quantities(suggested, with_hidden),
        *_new_names(suggested, sources, folded_skills, whole_names),
        *_new_skills(suggested, with_hidden, skills),
        *(Claim("added_detail", detail.strip()) for detail in added_details if detail.strip()),
    ]
    return tuple(dict.fromkeys(found))


FlagKind = Literal["unsupported", "needs_check"]
UnitKind = Literal["structure", "content"]


@dataclass(frozen=True)
class GeneratedUnit:
    """One line or paragraph of a generated document. ``fact_ids`` are the ids as the model
    wrote them; ``role`` is the role label of the section the unit sits under (the same
    string as ``AIFact.role``), None outside a role section."""

    unit_id: str
    kind: UnitKind
    section: str
    role: str | None
    text: str
    fact_ids: tuple[str, ...] = ()
    added_details: tuple[str, ...] = ()


@dataclass(frozen=True)
class HonestyFlag:
    """One thing for the user to see before approving: ``unsupported`` blocks approval,
    ``needs_check`` is to be acknowledged or edited out. ``claim`` is the text, the cited id
    or the claim the flag is about."""

    kind: FlagKind
    claim: str
    unit_id: str


@dataclass(frozen=True)
class CurrentFact:
    """The current state of one fact: its number, its current version and how it stands."""

    number: int
    version: int
    status: FactStatus
    privacy: Privacy = Privacy.NORMAL


def check_units(
    units: Iterable[GeneratedUnit],
    sent: Iterable[AIFact],
    *,
    template_headings: Iterable[str] = (),
    known_skills: Iterable[str] = (),
    name_sources: Iterable[str] = (),
) -> tuple[HonestyFlag, ...]:
    """What the units say that the facts sent in that request don't (spec 3.1 item 3).
    ``name_sources`` are extra texts that may support a capitalised name and nothing else (a
    cover letter passes the job's title and company; a CV passes none).

    A structure unit must be exactly a template heading; any other text counts as uncited
    content. A content unit needs ids, every id must be among the facts sent, and its
    wording is compared only with the facts it cites. Flags come in unit order; within a
    unit the citation flags first, then the claims in ``new_claims`` order, each once."""
    by_ref = {str(fact.ref): fact for fact in sent}
    headings = frozenset(template_headings)
    skills = tuple(known_skills)
    names = tuple(name_sources)
    flags: list[HonestyFlag] = []
    for unit in units:
        flags.extend(_unit_flags(unit, by_ref, headings, skills, names))
    return tuple(flags)


def _unit_flags(
    unit: GeneratedUnit,
    sent: Mapping[str, AIFact],
    headings: frozenset[str],
    skills: Sequence[str],
    names: Sequence[str],
) -> tuple[HonestyFlag, ...]:
    if unit.kind == "structure":
        return (
            () if unit.text in headings else (HonestyFlag("unsupported", unit.text, unit.unit_id),)
        )
    if not unit.fact_ids:
        return (HonestyFlag("unsupported", unit.text, unit.unit_id),)
    ids = tuple(dict.fromkeys(unit.fact_ids))
    cited = [sent[ref] for ref in ids if ref in sent]
    flags = [
        *(HonestyFlag("unsupported", ref, unit.unit_id) for ref in ids if ref not in sent),
        *(HonestyFlag("needs_check", ref, unit.unit_id) for ref in _other_role(unit, ids, sent)),
        *(
            HonestyFlag("needs_check", claim.text, unit.unit_id)
            for claim in _claims(unit, cited, skills, names)
        ),
        *(
            HonestyFlag("needs_check", word, unit.unit_id)
            for word in _mixed_script_words(unit.text)
        ),
    ]
    return tuple(dict.fromkeys(flags))


def _mixed_script_words(text: str) -> list[str]:
    """The words that mix Latin and other letters, which is how a look-alike letter hides a
    number word or a name from the comparison (a Cyrillic look-alike inside "ten"). Each is
    a claim."""
    found = []
    for match in _words(_prepared(text)):
        letters = [char for char in match[0] if char.isalpha()]
        latin = [char for char in letters if unicodedata.name(char, "").startswith("LATIN")]
        if latin and len(latin) < len(letters):
            found.append(match[0])
    return found


def _other_role(unit: GeneratedUnit, ids: Sequence[str], sent: Mapping[str, AIFact]) -> list[str]:
    """The cited ids of sent facts that do not belong to the role of the unit: a fact of a
    different role, and a fact with no role at all (education, skills), because a role header
    over a line that cites only role-less facts would put a real employer over words no
    work of that role supports."""
    if unit.role is None:
        return []
    return [ref for ref in ids if ref in sent and (sent[ref].role or None) != unit.role]


def _claims(
    unit: GeneratedUnit, cited: Sequence[AIFact], skills: Sequence[str], names: Sequence[str]
) -> tuple[Claim, ...]:
    """The claims of the unit's wording against its own cited facts; the text of a
    "generalize names" fact supports quantities and skills, never a name."""
    return new_claims(
        unit.text,
        [fact_text(fact) for fact in cited if not fact.generalize],
        known_skills=skills,
        added_details=unit.added_details,
        name_hidden_sources=[fact_text(fact) for fact in cited if fact.generalize],
        name_only_sources=names,
    )


def approval_blockers(
    units: Iterable[GeneratedUnit], facts_now: Iterable[CurrentFact]
) -> tuple[HonestyFlag, ...]:
    """What now stands in the way of approving the units (spec 3.1 item 4): a cited fact
    that is now never-use, not confirmed (archived, discarded, draft), missing, not a fact
    reference or of a version that doesn't exist is ``unsupported``; a cited version older
    than the current one is ``needs_check``. Each
    cited id is looked at once per unit, in citation order; the wording is not checked
    again and structure units are ignored."""
    current = {fact.number: fact for fact in facts_now}
    flags: list[HonestyFlag] = []
    for unit in units:
        if unit.kind == "structure":
            continue
        for cited in dict.fromkeys(unit.fact_ids):
            kind = _citation_problem(cited, current)
            if kind is not None:
                flags.append(HonestyFlag(kind, cited, unit.unit_id))
    return tuple(flags)


def _citation_problem(cited: str, current: Mapping[int, CurrentFact]) -> FlagKind | None:
    try:
        ref = FactRef.parse(cited)
    except ValueError:
        return "unsupported"
    fact = current.get(ref.number)
    if fact is None or fact.privacy == Privacy.NEVER_USE:
        return "unsupported"
    # Fail closed: only a confirmed fact, at a version that exists, can be cited.
    if fact.status != FactStatus.CONFIRMED or ref.version > fact.version:
        return "unsupported"
    return "needs_check" if ref.version < fact.version else None


__all__ = [
    "Claim",
    "ClaimKind",
    "CurrentFact",
    "FlagKind",
    "GeneratedUnit",
    "HonestyFlag",
    "Quantity",
    "QuantityKind",
    "UnitKind",
    "approval_blockers",
    "check_units",
    "extract_quantities",
    "new_claims",
]
