"""Fit is computed from requirement matches citing only the facts actually sent (M4.5)."""

from collections.abc import Collection, Mapping, Sequence
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any

from app.domain.cv_checks import normalise
from app.domain.job_normalization import currency_key
from app.domain.settings import JobPreferences, Salary


@dataclass(frozen=True)
class Requirement:
    id: str
    kind: str
    text: str


@dataclass(frozen=True)
class Match:
    requirement_id: str
    kind: str
    met: bool
    fact_ids: tuple[str, ...]


@dataclass(frozen=True)
class FitEvaluation:
    band: str
    matches: tuple[Match, ...]

    def result(self) -> dict[str, Any]:
        return {"requirements": [asdict(match) for match in self.matches]}


def evaluate_matches(
    requirements: Sequence[Requirement],
    reported: Sequence[Mapping[str, Any]],
    sent: Collection[str],
) -> FitEvaluation:
    matches = []
    for requirement in requirements:
        candidates = [item for item in reported if item.get("requirement_id") == requirement.id]
        item = candidates[0] if len(candidates) == 1 else {}
        refs = item.get("fact_ids", ())
        valid = (
            item.get("met") is True
            and isinstance(refs, list | tuple)
            and bool(refs)
            and all(isinstance(ref, str) and ref in sent for ref in refs)
        )
        matches.append(
            Match(
                requirement.id, requirement.kind, valid, tuple(dict.fromkeys(refs)) if valid else ()
            )
        )
    mandatory = [match for match in matches if match.kind == "must_have"]
    keys = [match for match in matches if match.kind == "key"]
    count = sum(match.met for match in keys)
    all_must = all(match.met for match in mandatory)
    if all_must and count * 4 >= len(keys) * 3:
        band = "strong"
    elif not all_must or count * 2 < len(keys):
        band = "stretch"
    else:
        band = "possible"
    return FitEvaluation(band, tuple(matches))


def salary_below_minimum(
    low: int | float | None,
    high: int | float | None,
    currency: str | None,
    period: str | None,
    minimum: Salary | None,
) -> bool:
    amount = high if high is not None else low
    return bool(
        minimum is not None
        and amount is not None
        and currency_key(currency) == currency_key(minimum.currency)
        and period is not None
        and period.casefold() in {"year", "yearly", "annual", "annually", "annum"}
        and Decimal(str(amount)) < minimum.amount
    )


def checked_conflicts(
    reported: Sequence[Mapping[str, Any]], preferences: JobPreferences | None, posting: str
) -> list[dict[str, Any]]:
    if preferences is None:
        return []
    lists = {"must_have": preferences.must_haves, "deal_breaker": preferences.deal_breakers}
    checked = {}
    for item in reported:
        kind, line, quote = item["kind"], item["line"], item["quote"]
        quote = normalise(quote) if isinstance(quote, str) else ""
        lines = lists.get(kind, ())
        if (
            isinstance(line, int)
            and not isinstance(line, bool)
            and 1 <= line <= len(lines)
            and bool(quote)
            and quote.casefold() in normalise(posting).casefold()
        ):
            checked[(kind, line)] = {
                "kind": kind,
                "line": line,
                "quote": quote,
                "preference": lines[line - 1],
            }
    return list(checked.values())
