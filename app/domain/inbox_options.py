"""Validated inbox URLs and saved view payloads; no I/O."""

import unicodedata
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from urllib.parse import urlencode

from app.domain.inbox import InboxFilters

GROUPS = ("company", "industry", "band", "work_mode", "seniority")
FILTER_FIELDS = tuple(InboxFilters.__dataclass_fields__)
OPTION_FIELDS = (*FILTER_FIELDS, "group_by")


def checked_name(value: str) -> str:
    value = value.strip()
    if not value or len(value) > 80 or any(unicodedata.category(c).startswith("C") for c in value):
        raise ValueError("Enter a name using 1 to 80 visible characters.")
    return value


@dataclass(frozen=True)
class InboxSelection:
    filters: InboxFilters
    group_by: str = ""

    def values(self) -> dict[str, str]:
        raw = asdict(self.filters)
        defaults = asdict(InboxFilters())
        values = {
            k: str(int(v)) if isinstance(v, bool) else str(v)
            for k, v in raw.items()
            if v is not None and v != defaults[k]
        }
        if self.group_by:
            values["group_by"] = self.group_by
        return values

    def query(self, *, include_view: bool = False) -> str:
        values = self.values()
        if include_view:
            values["view"] = self.filters.view
        else:
            values.pop("view", None)
        return urlencode(values)


def selection(values: Mapping[str, str], *, strict: bool = False) -> InboxSelection:
    if strict and (
        set(values) - set(OPTION_FIELDS) or any(not isinstance(v, str) for v in values.values())
    ):
        raise ValueError("Choose valid inbox filters.")
    clean = {k: v for k, v in values.items() if k in OPTION_FIELDS and isinstance(v, str)}
    for field in OPTION_FIELDS:
        text = clean.get(field, "")
        if len(text) > 500 or any(unicodedata.category(c).startswith("C") for c in text):
            if strict:
                raise ValueError("Use up to 500 visible characters per filter.")
            clean.pop(field, None)
    for field, choices, default in (
        ("view", ("queue", "snoozed", "removed", "to_apply"), "queue"),
        ("sort", ("newest", "posting_age", "band"), "newest"),
        ("band", ("", "strong", "possible", "stretch"), ""),
        ("flagged", ("", "0", "1"), ""),
        ("group_by", ("", *GROUPS), ""),
    ):
        if clean.get(field, default) not in choices:
            if strict:
                raise ValueError("Choose valid inbox filters.")
            clean[field] = default
    return InboxSelection(
        InboxFilters(
            band=clean.get("band") or None,
            work_mode=clean.get("work_mode") or None,
            industry=clean.get("industry") or None,
            seniority=clean.get("seniority") or None,
            company=clean.get("company") or None,
            view=clean.get("view", "queue"),
            sort=clean.get("sort", "newest"),
            search=clean.get("search", ""),
            flagged={"1": True, "0": False}.get(clean.get("flagged", "")),
        ),
        clean.get("group_by", ""),
    )
