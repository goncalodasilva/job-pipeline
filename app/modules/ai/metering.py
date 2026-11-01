"""Counting one provider call on a ``UsageMeter`` (spec 3.7; M2.16).

The meter's methods block (database work), so each runs in a worker thread. A reservation or
completion that has started is never abandoned: if the asyncio task is cancelled meanwhile,
the work finishes first and the cancellation is raised afterwards, so no record is left
open by a cancel.
"""

import asyncio
import logging
from typing import Final

from app.modules.ai import (
    WEB_SEARCH_TOOL,
    ProviderUsageSnapshot,
    TokenUsage,
    UsageKind,
    UsageMeter,
    reserve_uninterrupted,
    settled,
)

__all__ = ["MAX_SEARCHES_PER_CALL", "MAX_SNAPSHOTS_PER_CALL", "CallMeter"]

_LOG: Final = logging.getLogger(__name__)
# However large the web-search budget is, one provider call gets no more searches than this.
MAX_SEARCHES_PER_CALL: Final = 20
# Reported usage queued during one call: a provider can't grow the queue without bound.
MAX_SNAPSHOTS_PER_CALL: Final = 50


class CallMeter:
    """The usage of one task run, one provider call at a time (``reserve``, then ``finish``):
    the call's request record, the web searches made inside it and the usage the provider
    reported while it ran. A retry reuses it, so both requests carry the same callbacks."""

    def __init__(
        self,
        meter: UsageMeter,
        *,
        tier: str,
        task_type: str,
        task_id: str | None,
        max_searches: int = MAX_SEARCHES_PER_CALL,
    ) -> None:
        if not 1 <= max_searches <= MAX_SEARCHES_PER_CALL:
            raise ValueError("Invalid search cap.")
        self._max_searches = max_searches
        self._search_lock = asyncio.Lock()
        self._meter = meter
        self._tier = tier
        self._task_type = task_type
        self._task_id = task_id
        self._reported_searches = 0
        self._request: str | None = None
        self._searches: list[str] = []
        self._snapshots: list[ProviderUsageSnapshot] = []

    async def reserve(self) -> None:
        """Count the call before it is made; raises what the meter raises (``BudgetBlocked``)."""
        self._request = await self._reserve("request")

    def on_search_usage(self, count: int) -> None:
        """Queue cumulative per-attempt reported search usage, including failed results."""
        if type(count) is int and count >= 0:
            self._reported_searches = max(self._reported_searches, count)

    def on_usage(self, snapshot: ProviderUsageSnapshot) -> None:
        """``AIRequest.on_usage``: runs on the event loop, so it only queues. Beyond
        ``MAX_SNAPSHOTS_PER_CALL`` the oldest one that a later report of the same service
        and window replaces is dropped, so the newest of each window is kept."""
        queue = self._snapshots
        queue.append(snapshot)
        if len(queue) > MAX_SNAPSHOTS_PER_CALL:
            newest = {(item.service, item.window): index for index, item in enumerate(queue)}
            replaced = (
                index
                for index, item in enumerate(queue)
                if newest[(item.service, item.window)] != index
            )
            del queue[next(replaced, 0)]  # every window different: the oldest goes

    async def on_tool_use(self, tool: str) -> bool:
        """``AIRequest.on_tool_use``: one search record per web search. Fails closed: any
        other tool, a used-up budget, any error while reserving or more than
        ``MAX_SEARCHES_PER_CALL`` searches in this call refuses the tool call (the last
        before anything is reserved)."""
        async with self._search_lock:
            if tool != WEB_SEARCH_TOOL or len(self._searches) >= self._max_searches:
                return False
            try:
                self._searches.append(await self._reserve("search"))
            except Exception as error:
                _LOG.info("A web search was refused (%s)", type(error).__name__)
                return False
            return True

    async def finish(
        self, *, success: bool, tokens: TokenUsage | None, reported_searches: int = 0
    ) -> None:
        """Complete the call's records and hand over the reported usage. Runs to its end
        even when the task is cancelled; a failing meter never changes the call's outcome."""
        await settled(self._finish(success, tokens, reported_searches))

    async def _reserve(self, kind: UsageKind) -> str:
        def reserve() -> str:
            return self._meter.reserve(
                tier=self._tier, task_type=self._task_type, task_id=self._task_id, kind=kind
            )

        return await reserve_uninterrupted(
            reserve, lambda record_id: self._complete(record_id, success=False, tokens=None)
        )

    async def _finish(
        self, success: bool, tokens: TokenUsage | None, reported_searches: int
    ) -> None:
        request, self._request = self._request, None
        if request is not None:
            await self._complete(request, success=success, tokens=tokens)
        reported = max(reported_searches, self._reported_searches)
        self._reported_searches = 0
        extra = max(0, reported - len(self._searches))
        if extra:
            try:
                await asyncio.to_thread(
                    self._meter.reported_searches,
                    extra,
                    tier=self._tier,
                    task_type=self._task_type,
                    task_id=self._task_id,
                    success=success,
                )
            except Exception as error:
                _LOG.warning("Reported searches could not be counted (%s)", type(error).__name__)
        searches, self._searches = self._searches, []
        for record_id in searches:
            await self._complete(record_id, success=success, tokens=None)
        snapshots, self._snapshots = self._snapshots, []
        for snapshot in snapshots:
            try:
                await asyncio.to_thread(self._meter.snapshot, snapshot)
            except Exception as error:
                _LOG.warning("Reported usage couldn't be kept (%s)", type(error).__name__)

    async def _complete(self, record_id: str, *, success: bool, tokens: TokenUsage | None) -> None:
        """An unfinished record still counts, so a failed completion is only logged."""
        try:
            await asyncio.to_thread(self._meter.complete, record_id, success=success, tokens=tokens)
        except Exception as error:
            _LOG.warning("A usage record couldn't be completed (%s)", type(error).__name__)
