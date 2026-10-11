"""The usage service as the executor's ``UsageMeter`` (spec 3.7; M2.16).

One meter per provider service: a request is counted on that service, a web search on the
web-search service (ADR-076 item 4). Blocking, like the usage service: the executor calls it
in worker threads.
"""

from app.domain.usage import WEB_SEARCH_SERVICE, ServiceId
from app.modules.ai import ProviderUsageSnapshot, TokenUsage, UsageKind
from app.services.usage import UsageService
from app.services.usage_values import check_service


class ServiceUsageMeter:
    def __init__(self, usage: UsageService, service: str) -> None:
        """Raises ``ValueError`` for a service the budgets don't know."""
        self._usage = usage
        self._service: ServiceId = check_service(service)

    def reserve(self, *, tier: str, task_type: str, task_id: str | None, kind: UsageKind) -> str:
        if kind == "request":
            service = self._service
        elif kind == "search":
            service = WEB_SEARCH_SERVICE
        else:
            raise ValueError("unknown usage kind")
        return self._usage.reserve(service, tier=tier, task_id=task_id, task_type=task_type)

    def complete(self, record_id: str, *, success: bool, tokens: TokenUsage | None) -> None:
        self._usage.complete(record_id, success=success, tokens=tokens)

    def snapshot(self, snapshot: ProviderUsageSnapshot) -> None:
        self._usage.save_snapshot(snapshot)
