"""Mechanical keys for controlled values and company names (M4.4)."""

import re
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Final

_SUFFIXES: Final = frozenset(
    {
        "ltd",
        "limited",
        "llc",
        "inc",
        "incorporated",
        "corp",
        "corporation",
        "sa",
        "lda",
        "gmbh",
        "plc",
    }
)


def value_key(value: str) -> str:
    folded = unicodedata.normalize("NFKD", value.casefold())
    return "".join(c for c in folded if c.isalnum())


def normalize_value(value: str, controlled: Mapping[str, Sequence[str]]) -> str:
    key = value_key(value)
    for canonical, aliases in controlled.items():
        if any(value_key(candidate) == key for candidate in (canonical, *aliases)):
            return canonical
    return value


def company_key(name: str) -> str:
    parts = re.findall(r"[^\W_]+", unicodedata.normalize("NFKD", name.casefold().replace(".", "")))
    while len(parts) > 1 and parts[-1] in _SUFFIXES:
        parts.pop()
    return "".join(parts)
