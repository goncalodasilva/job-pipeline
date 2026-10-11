"""The app's shutdown signal that ends event streams (M2.1; ADR-076 item 1).

``ShutdownSignal`` (``app/api/events.py``) is created in ``create_app``, before any event loop
exists, set by the CLI from uvicorn's signal handler (the main thread, between two bytecodes of
whatever the loop was running) and awaited by every open event stream on the server's loop. So
it must work without a loop at creation, wake waiters from any thread or signal handler, and
stay usable when a waiter was cancelled. Every wait here has a deadline; nothing sleeps.
"""

import asyncio
import signal
import threading

from app.api.events import ShutdownSignal

DEADLINE = 2.0
LATENCY = 1.0  # a wake-up that needs a timer to fire is too slow; a real one takes milliseconds


def test_shutdown_signal_starts_unset_and_needs_no_event_loop() -> None:
    shutdown = ShutdownSignal()  # as create_app does, with no loop running

    assert shutdown.is_set() is False


def test_shutdown_signal_set_is_idempotent() -> None:
    shutdown = ShutdownSignal()
    shutdown.set()
    shutdown.set()

    assert shutdown.is_set() is True


async def test_shutdown_signal_set_wakes_every_waiter_on_the_loop() -> None:
    shutdown = ShutdownSignal()
    waiters = [asyncio.create_task(shutdown.wait()) for _ in range(3)]
    await asyncio.sleep(0)  # let them start waiting
    assert not any(waiter.done() for waiter in waiters)

    shutdown.set()

    await asyncio.wait_for(asyncio.gather(*waiters), DEADLINE)
    assert shutdown.is_set()


async def test_shutdown_signal_wait_returns_at_once_when_already_set() -> None:
    shutdown = ShutdownSignal()
    shutdown.set()

    await asyncio.wait_for(shutdown.wait(), DEADLINE)


async def test_shutdown_signal_set_is_thread_safe() -> None:
    """``set`` from another thread wakes a loop that is idle in ``select`` at once. The helper
    thread starts only after the loop has run the waiter (its ``ready`` callback is queued
    behind it), so the loop is about to sleep with nothing else to wake it."""
    shutdown = ShutdownSignal()
    loop = asyncio.get_running_loop()
    ready = threading.Event()

    def set_from_another_thread() -> None:
        ready.wait(DEADLINE)
        shutdown.set()

    helper = threading.Thread(target=set_from_another_thread, name="fictional-signal-thread")
    waiter = asyncio.ensure_future(shutdown.wait())
    loop.call_soon(ready.set)
    helper.start()
    started = loop.time()

    await asyncio.wait_for(waiter, LATENCY)

    assert loop.time() - started < LATENCY / 2, "set() must wake the loop, not wait for a timer"
    helper.join(DEADLINE)
    assert not helper.is_alive()


async def test_shutdown_signal_set_in_a_real_signal_handler_wakes_an_idle_loop() -> None:
    """uvicorn's handler is a Python signal handler in the main thread, which is the loop's
    thread: the signal interrupts the loop's ``select``, the handler calls ``set``, and the
    interrupted ``select`` is restarted, so ``set`` must wake the loop itself. The signal is
    sent to the main thread by a helper that starts once the loop is about to idle."""
    shutdown = ShutdownSignal()
    loop = asyncio.get_running_loop()
    ready = threading.Event()
    main_thread = threading.get_ident()

    def send_signal() -> None:
        ready.wait(DEADLINE)
        signal.pthread_kill(main_thread, signal.SIGUSR1)

    helper = threading.Thread(target=send_signal, name="fictional-signal-sender")
    previous = signal.signal(signal.SIGUSR1, lambda _signum, _frame: shutdown.set())
    try:
        waiter = asyncio.ensure_future(shutdown.wait())
        loop.call_soon(ready.set)
        helper.start()
        started = loop.time()

        await asyncio.wait_for(waiter, LATENCY)

        assert loop.time() - started < LATENCY / 2, "set() must wake the loop, not wait for a timer"
    finally:
        signal.signal(signal.SIGUSR1, previous)
        helper.join(DEADLINE)
    assert shutdown.is_set()


async def test_shutdown_signal_set_ignores_a_cancelled_waiter() -> None:
    """A stream whose client left is cancelled while it waits; a later ``set`` must not fail
    on it, and later waiters still return."""
    shutdown = ShutdownSignal()
    gone = asyncio.create_task(shutdown.wait())
    await asyncio.sleep(0)
    gone.cancel()
    await asyncio.wait({gone})
    assert gone.cancelled()

    shutdown.set()

    await asyncio.wait_for(shutdown.wait(), DEADLINE)


async def test_shutdown_signal_works_on_a_second_event_loop() -> None:
    """Created without a loop and used by two different loops one after the other (the app
    object outlives a TestClient portal loop): nothing is bound to the first loop."""
    shutdown = ShutdownSignal()

    async def first_loop() -> None:
        try:
            await asyncio.wait_for(shutdown.wait(), 0.05)
        except TimeoutError:
            return
        raise AssertionError("an unset signal must not return from wait()")

    await asyncio.to_thread(asyncio.run, first_loop())  # a loop of its own, in a worker thread
    waiter = asyncio.create_task(shutdown.wait())  # and now this test's loop
    await asyncio.sleep(0)
    await asyncio.to_thread(shutdown.set)
    await asyncio.wait_for(waiter, DEADLINE)

    assert shutdown.is_set()


class _SetsOnRegister(dict[asyncio.Future[None], asyncio.AbstractEventLoop]):
    """The waiter table, but a stop request lands just before a waiter is entered in it."""

    def __init__(self, shutdown: ShutdownSignal) -> None:
        super().__init__()
        self.shutdown = shutdown

    def __setitem__(self, future: asyncio.Future[None], loop: asyncio.AbstractEventLoop) -> None:
        self.shutdown.set()  # nothing is registered yet, so there is no one to wake
        super().__setitem__(future, loop)


async def test_shutdown_signal_set_between_the_first_check_and_registering_still_returns() -> None:
    """``wait`` checks the flag, then registers, then checks again. A ``set`` in that gap finds
    no waiter to wake, so without the second check a stream would wait for ever."""
    shutdown = ShutdownSignal()
    shutdown._waiters = _SetsOnRegister(shutdown)

    await asyncio.wait_for(shutdown.wait(), DEADLINE)

    assert shutdown.is_set()


async def test_shutdown_signal_wake_up_of_a_cancelled_waiter_causes_no_loop_error() -> None:
    """``set`` queues a wake-up on the waiter's loop; if the waiter is cancelled before the loop
    runs it, the wake-up must find a finished future and leave it alone (setting a result on it
    would raise ``InvalidStateError`` inside the loop, which only the loop's handler would see)."""
    shutdown = ShutdownSignal()
    loop = asyncio.get_running_loop()
    errors: list[dict[str, object]] = []
    loop.set_exception_handler(lambda _loop, context: errors.append(context))
    try:
        waiter = asyncio.create_task(shutdown.wait())
        await asyncio.sleep(0)  # let it register and wait
        shutdown.set()  # the wake-up is queued on the loop
        waiter.cancel()  # and the waiter is gone before the loop runs it
        await asyncio.wait({waiter})
        assert waiter.cancelled()

        await asyncio.sleep(0)  # the queued wake-up runs in these turns
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(None)

    assert errors == []
