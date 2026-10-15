"""Identifiers as an injected dependency (spec 2.4, 3.4). Only this module generates UUIDs."""

import re
import uuid
from typing import Final, Protocol, TypeGuard

_SAFE_ID: Final = re.compile(r"[A-Za-z0-9_-]{1,64}")


def is_safe_id(value: object) -> TypeGuard[str]:
    """Whether ``value`` has the shape every ``IdFactory`` makes: 1 to 64 of A-Z, a-z, 0-9,
    ``_`` and ``-``. Only such an id is looked up, put in a URL or used as an element id;
    anything else names nothing. (Tasks have ``is_task_id``, the same shape.)"""
    return isinstance(value, str) and _SAFE_ID.fullmatch(value) is not None


class IdFactory(Protocol):
    def new(self) -> str:
        """Return a new unique identifier."""
        ...


class UuidIdFactory:
    def new(self) -> str:
        return str(uuid.uuid4())
