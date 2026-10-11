"""Usage bookkeeping that a cancelled asyncio task may not abandon (spec 3.7; M2.16).

A reservation or completion that has started runs to its end; the cancellation is raised
afterwards. Imports nothing from the AI interface, which re-exports these helpers.
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")


async def _to_end(work: Awaitable[T]) -> tuple["asyncio.Future[T]", bool]:
    """Wait for ``work`` to end, however often this task is cancelled meanwhile. Returns
    the finished future and whether a cancellation arrived."""
    future = asyncio.ensure_future(work)
    cancelled = False
    while not future.done():
        try:
            await asyncio.wait({future})  # a cancelled wait leaves the future running
        except asyncio.CancelledError:
            cancelled = True
    return future, cancelled


async def settled(work: Awaitable[T]) -> T:
    """Await ``work`` to its end even if this task is cancelled meanwhile; the cancellation
    is raised once the work is over."""
    future, cancelled = await _to_end(work)
    if cancelled:
        if not future.cancelled():
            future.exception()  # retrieved, so it isn't reported as never read
        raise asyncio.CancelledError
    return future.result()


async def reserve_uninterrupted(
    reserve: Callable[[], str], abandon: Callable[[str], Awaitable[object]]
) -> str:
    """Run the blocking ``reserve`` in a worker thread and return its record id. If this
    task is cancelled meanwhile, the reservation still finishes, the record it wrote is
    closed with ``abandon(record_id)`` (also run to its end) and only then is the
    cancellation raised: a cancel never leaves a record open."""
    future, cancelled = await _to_end(asyncio.to_thread(reserve))
    if not cancelled:
        return future.result()
    if not future.cancelled() and future.exception() is None:
        await _to_end(abandon(future.result()))
    raise asyncio.CancelledError
