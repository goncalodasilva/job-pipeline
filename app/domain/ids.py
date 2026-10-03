"""Identifiers as an injected dependency (spec 2.4, 3.4). Only this module generates UUIDs."""

import uuid
from typing import Protocol


class IdFactory(Protocol):
    def new(self) -> str:
        """Return a new unique identifier."""
        ...


class UuidIdFactory:
    def new(self) -> str:
        return str(uuid.uuid4())
