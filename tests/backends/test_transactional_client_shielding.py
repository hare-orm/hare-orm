import asyncio
from types import SimpleNamespace

import pytest

from hare.dialects.base.client.transaction_lifecycle import transaction_ending as client_module
from hare.dialects.base.client.transaction_lifecycle.transaction_ending import TransactionEnding
from tests.utils.waiting import WAIT_DEADLINE_SECONDS

pytestmark = pytest.mark.database_independent


@pytest.mark.asyncio
async def test_shielded_cancellation_does_not_hang_forever_on_a_never_finishing_operation(monkeypatch):
    """A shielded operation that never completes (dead server, no OS-level timeout) used to make
    _run_shielded_from_cancellation() wait forever after a cancellation - defeating an explicit
    shutdown/timeout the caller asked for. Bounded by SHIELDED_CANCELLATION_WAIT_TIMEOUT_SECONDS
    now - set to 0 here, so the bound is hit at once."""
    monkeypatch.setattr(client_module, "SHIELDED_CANCELLATION_WAIT_TIMEOUT_SECONDS", 0)

    landed = False

    def on_landed() -> None:
        nonlocal landed
        landed = True

    started = asyncio.Event()

    async def never_finishes() -> None:
        started.set()
        await asyncio.Event().wait()  # never set - hangs forever

    abandoned_tasks: list[asyncio.Task] = []

    async def run_it() -> None:
        await TransactionEnding.run_shielded_from_cancellation(
            never_finishes(), on_landed=on_landed, on_timeout=abandoned_tasks.append
        )

    task = asyncio.ensure_future(run_it())
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, WAIT_DEADLINE_SECONDS)

    assert landed is False
    # The abandoned operation keeps running by design - stop it so it doesn't outlive the test.
    for abandoned_task in abandoned_tasks:
        abandoned_task.cancel()


@pytest.mark.asyncio
async def test_shielded_cancellation_still_lands_when_the_operation_finishes_within_the_timeout(monkeypatch):
    """The timeout bound must not break the existing, intended behavior: an operation that IS
    still genuinely in flight (just briefly) when cancelled must still be allowed to finish and
    have on_landed() fire, not get abandoned just because a cancellation happened at all."""
    monkeypatch.setattr(client_module, "SHIELDED_CANCELLATION_WAIT_TIMEOUT_SECONDS", WAIT_DEADLINE_SECONDS)

    landed = False

    def on_landed() -> None:
        nonlocal landed
        landed = True

    started = asyncio.Event()
    released = asyncio.Event()

    async def finishes_once_released() -> None:
        started.set()
        await released.wait()

    async def run_it() -> None:
        await TransactionEnding.run_shielded_from_cancellation(finishes_once_released(), on_landed=on_landed)

    task = asyncio.ensure_future(run_it())
    await started.wait()
    task.cancel()
    released.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert landed is True


@pytest.mark.asyncio
async def test_shielded_cancellation_calls_on_timeout_when_the_bound_is_hit(monkeypatch):
    """on_timeout must fire exactly when the wait bound is actually exhausted with the task
    still running - the signal TransactionContextPooled.__aexit__ uses to avoid handing a
    possibly-still-in-use physical connection back to the pool for reuse."""
    monkeypatch.setattr(client_module, "SHIELDED_CANCELLATION_WAIT_TIMEOUT_SECONDS", 0)

    timed_out_task: asyncio.Task | None = None

    def on_timeout(abandoned_task: asyncio.Task) -> None:
        nonlocal timed_out_task
        timed_out_task = abandoned_task

    started = asyncio.Event()

    async def never_finishes() -> None:
        started.set()
        await asyncio.Event().wait()

    async def run_it() -> None:
        await TransactionEnding.run_shielded_from_cancellation(
            never_finishes(), on_landed=lambda: None, on_timeout=on_timeout
        )

    task = asyncio.ensure_future(run_it())
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert timed_out_task is not None
    assert not timed_out_task.done()
    timed_out_task.cancel()


@pytest.mark.asyncio
async def test_shielded_cancellation_does_not_call_on_timeout_when_it_lands_in_time(monkeypatch):
    monkeypatch.setattr(client_module, "SHIELDED_CANCELLATION_WAIT_TIMEOUT_SECONDS", WAIT_DEADLINE_SECONDS)

    timed_out = False

    def on_timeout() -> None:
        nonlocal timed_out
        timed_out = True

    started = asyncio.Event()
    released = asyncio.Event()

    async def finishes_once_released() -> None:
        started.set()
        await released.wait()

    async def run_it() -> None:
        await TransactionEnding.run_shielded_from_cancellation(
            finishes_once_released(), on_landed=lambda: None, on_timeout=on_timeout
        )

    task = asyncio.ensure_future(run_it())
    await started.wait()
    task.cancel()
    released.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert timed_out is False


@pytest.mark.requires("asyncpg")
@pytest.mark.asyncio
async def test_pooled_transaction_context_exit_terminates_an_abandoned_connection_before_release(monkeypatch):
    """TransactionContextPooled.__aexit__ used to unconditionally hand the physical connection
    back to the pool for reuse, even when the shielded commit()/rollback() gave up waiting on a
    still-running task - a completely unrelated caller could then acquire and use a connection
    the abandoned task might still be mid-COMMIT against. It must now terminate() the connection
    first whenever _shielded_operation_abandoned was set, so the pool discards it instead of
    reusing it."""
    from hare.dialects.postgresql.drivers.asyncpg.client import AsyncpgTransactionClient

    events: list[str] = []

    fake_connection = SimpleNamespace(terminate=lambda: events.append("terminate"))

    async def fake_release(connection: object) -> None:
        assert connection is fake_connection
        events.append("release")

    fake_pool = SimpleNamespace(release=fake_release)
    fake_client = SimpleNamespace(
        _shielded_operation_abandoned=True,
        _prepared_without_release=False,
        _connection=fake_connection,
        _held_pool=fake_pool,
        _parent=SimpleNamespace(_pool=fake_pool, _pool_release=lambda pool, connection: pool.release(connection)),
    )

    await AsyncpgTransactionClient._give_back_transaction_resources(fake_client)  # type: ignore[arg-type]

    assert events == ["terminate", "release"]


@pytest.mark.requires("asyncpg")
@pytest.mark.asyncio
async def test_pooled_transaction_context_exit_releases_normally_when_not_abandoned(monkeypatch):
    """The common case (no timeout ever hit) must be unaffected - no terminate() call, just the
    existing plain release()."""
    from hare.dialects.postgresql.drivers.asyncpg.client import AsyncpgTransactionClient

    events: list[str] = []

    fake_connection = SimpleNamespace(terminate=lambda: events.append("terminate"))

    async def fake_release(connection: object) -> None:
        assert connection is fake_connection
        events.append("release")

    fake_pool = SimpleNamespace(release=fake_release)
    fake_client = SimpleNamespace(
        _shielded_operation_abandoned=False,
        _prepared_without_release=False,
        _connection=fake_connection,
        _held_pool=fake_pool,
        _parent=SimpleNamespace(_pool=fake_pool, _pool_release=lambda pool, connection: pool.release(connection)),
    )

    await AsyncpgTransactionClient._give_back_transaction_resources(fake_client)  # type: ignore[arg-type]

    assert events == ["release"]


@pytest.mark.asyncio
async def test_an_error_after_the_cancellation_is_not_logged_as_one_of_a_shielded_future():
    """asyncio.shield() reported the error a COMMIT ended with after its task was cancelled - a
    COMMIT of an aborted transaction - to the loop's exception handler as "exception in shielded
    future"; the cancellation goes through and the error is read."""
    reported_contexts: list[dict] = []
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: reported_contexts.append(context))

    started = asyncio.Event()
    released = asyncio.Event()

    async def fails_later() -> None:
        started.set()
        await released.wait()
        raise RuntimeError("rolled back instead of committed")

    async def run_it() -> None:
        await TransactionEnding.run_shielded_from_cancellation(fails_later(), on_landed=lambda: None)

    try:
        task = asyncio.ensure_future(run_it())
        await started.wait()
        task.cancel()
        released.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        # The callbacks of the finished operation get every chance to report the error.
        for _ in range(10):
            await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous_handler)
    assert reported_contexts == []
