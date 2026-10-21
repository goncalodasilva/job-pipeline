"""Application editor input; all text is data and dates are local wall values."""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime

EVENT_TYPES = {
    "interview": "Interview",
    "follow_up": "Follow-up",
    "deadline": "Deadline",
    "offer_deadline": "Offer deadline",
}
EVENT_FIELDS = frozenset(
    {
        "type",
        "date",
        "time",
        "duration_minutes",
        "time_zone",
        "all_day",
        "round",
        "format",
        "interviewers",
        "location",
        "details",
        "thank_you_reminder",
    }
)


class DetailInvalid(Exception):
    def __init__(self, errors: dict[str, str]) -> None:
        self.errors = errors
        super().__init__("Check the highlighted fields.")


def clean_text(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "").strip()


def checked_note(value: str) -> str:
    text = clean_text(value)
    if not text or len(text) > 10000:
        raise DetailInvalid({"text": "Write a note of up to 10,000 characters."})
    return text


@dataclass(frozen=True)
class EventInput:
    kind: str
    day: date
    wall: datetime | None
    minutes: int
    zone: str
    all_day: bool
    text: Mapping[str, str]
    thank_you: bool


def checked_event(values: Mapping[str, str]) -> EventInput:
    errors: dict[str, str] = {}
    kind = values.get("type", "")
    if kind not in EVENT_TYPES:
        errors["type"] = "Choose an event type."
    all_day = values.get("all_day", "") == "1"
    for key in ("all_day", "thank_you_reminder"):
        if values.get(key, "") not in ("", "1"):
            errors[key] = "Choose this option or leave it clear."
    if all_day and kind not in ("deadline", "offer_deadline"):
        errors["all_day"] = "Only deadlines can be all day."
    day = date.min
    try:
        raw = values.get("date", "")
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", raw):
            raise ValueError
        day = date.fromisoformat(raw)
    except ValueError:
        errors["date"] = "Choose a valid date."
    wall = None
    minutes = 60
    if not all_day:
        try:
            raw_time = values.get("time", "")
            if not re.fullmatch(r"[0-9]{2}:[0-9]{2}", raw_time):
                raise ValueError
            wall = datetime.fromisoformat(f"{day.isoformat()}T{raw_time}")
        except ValueError:
            errors["time"] = "Choose a valid time."
        raw_minutes = values.get("duration_minutes", "")
        if not re.fullmatch(r"[0-9]{1,4}", raw_minutes) or not 1 <= int(raw_minutes) <= 1440:
            errors["duration_minutes"] = "Use a length between 1 minute and 24 hours."
        else:
            minutes = int(raw_minutes)
    text = {
        key: clean_text(values.get(key, ""))
        for key in ("round", "format", "interviewers", "location", "details")
    }
    for key, value in text.items():
        if len(value) > 10000:
            errors[key] = "Use up to 10,000 characters."
    if errors:
        raise DetailInvalid(errors)
    return EventInput(
        kind,
        day,
        wall,
        minutes,
        values.get("time_zone", ""),
        all_day,
        text,
        values.get("thank_you_reminder") == "1" and kind == "interview",
    )
