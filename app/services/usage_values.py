"""Value checks and conversions for the usage service (spec 3.7; ADR-078). No database."""

import logging
from datetime import UTC, datetime

from app.domain.budgets import BudgetTier
from app.domain.usage import ServiceId, is_service, is_stored_count
from app.modules.ai import ProviderUsageSnapshot, TokenUsage
from app.modules.storage import usage_repo

_log = logging.getLogger("app.services.usage")


def utc_text(instant: datetime) -> str:
    return instant.astimezone(UTC).isoformat()


def check_service(service: str) -> ServiceId:
    if is_service(service):
        return service
    raise ValueError("unknown usage service")


def check_tier(tier: str) -> BudgetTier:
    if tier == "fast":
        return "fast"
    if tier == "thorough":
        return "thorough"
    raise ValueError("unknown tier")


def token_counts(tokens: TokenUsage | None) -> tuple[int | None, int | None]:
    """Both counts, or neither when either can't be stored (never clamped)."""
    if tokens is None:
        return None, None
    if is_stored_count(tokens.input_tokens) and is_stored_count(tokens.output_tokens):
        return tokens.input_tokens, tokens.output_tokens
    return None, None


def snapshot_from_row(row: usage_repo.StoredSnapshot) -> ProviderUsageSnapshot | None:
    try:
        return ProviderUsageSnapshot(
            service=row.service,
            window=row.window,
            unit="requests" if row.unit == "requests" else "fraction",  # schema CHECKs the set
            used=row.used,
            limit=row.limit_count,
            utilization=row.utilization,
            resets_at=None if row.resets_at is None else datetime.fromisoformat(row.resets_at),
            observed_at=datetime.fromisoformat(row.observed_at),
        )
    except ValueError:
        _log.warning("a stored usage snapshot is not valid; ignoring it")
        return None
