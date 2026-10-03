"""Task runner stub: an idle asyncio worker run by the lifespan (M0.13, spec 2.6)."""

import asyncio

import pytest

from app.tasks import TaskRunner


def _other_tasks() -> set[asyncio.Task[object]]:
    return {task for task in asyncio.all_tasks() if task is not asyncio.current_task()}


async def test_runner_stub_start_stop_idempotent() -> None:
    runner = TaskRunner()
    assert runner.status == "stopped"

    await runner.start()
    await runner.start()
    assert runner.status == "running"
    assert len(_other_tasks()) == 1, "starting twice must create exactly one worker"

    await runner.stop()
    await runner.stop()
    assert runner.status == "stopped"
    assert _other_tasks() == set(), "stop must signal and await the worker"


async def test_runner_stub_can_restart_after_stop() -> None:
    runner = TaskRunner()
    await runner.start()
    await runner.stop()

    await runner.start()
    assert runner.status == "running"
    assert len(_other_tasks()) == 1

    await runner.stop()
    assert runner.status == "stopped"
    assert _other_tasks() == set()


async def test_runner_stub_stop_before_start_is_fine() -> None:
    runner = TaskRunner()
    await runner.stop()
    assert runner.status == "stopped"
    assert _other_tasks() == set()


async def test_runner_stub_worker_stays_idle_until_stopped() -> None:
    runner = TaskRunner()
    await runner.start()
    for _ in range(5):
        await asyncio.sleep(0)  # yield to the worker; it must not finish on its own
    assert runner.status == "running"
    (worker,) = _other_tasks()
    assert not worker.done()
    await runner.stop()
    assert worker.done()


async def test_cancelling_stop_is_not_swallowed() -> None:
    runner = TaskRunner()
    await runner.start()
    stopper = asyncio.create_task(runner.stop())
    await asyncio.sleep(0)  # let stop() reach its await on the worker
    stopper.cancel()

    with pytest.raises(asyncio.CancelledError):
        await stopper
